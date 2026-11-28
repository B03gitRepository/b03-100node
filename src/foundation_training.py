"""Optimize a foundation-aware selector from sealed, on-policy development episodes."""
from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from episode_contract import TRAIN_SCRIPTS, _verified, trajectory
from foundation_policy import (FEATURES, FoundationFeatures, initial_parameters, probabilities,
                               read_parameters, write_parameters)
from knowledge.agent_facts import evidence_digest
from scene import PROFILE


def paired_conditions(conditions):
    """Retain physical, software, policy and authority identities across independent draws."""
    normalized = deepcopy(conditions)
    # The verifier authenticates each complete manifest before this comparison.
    # Only the two sampler seeds and the three digests derived from them may differ.
    components = normalized["components"]
    components["model"]["options"].pop("drawSeed", None)
    components.pop("manifestSha256")
    normalized["rcPolicy"].pop("workOrderComponentsSha256")
    proof = normalized["provenance"]
    proof["activeComponents"]["model"]["options"].pop("drawSeed", None)
    proof.pop("fingerprint")
    return evidence_digest(normalized)


def load_rollout(path, parameters):
    run = Path(path)
    report = _verified(run)
    cond = json.loads((run / "CONDITIONS.json").read_bytes())
    if cond.get("sceneOrigin", "official") != "official" or cond["script"] not in TRAIN_SCRIPTS[PROFILE["nodes"]]:
        raise ValueError("Training requires an authorized development script for this network scale")
    policy = cond["rcPolicy"]
    if (policy.get("adapterId") != "b03.foundation-selector" or policy.get("mode") != "online-ppo"
            or policy.get("parameterSha256") != evidence_digest(parameters)):
        raise ValueError("Training observations must come from the selected on-policy checkpoint")
    catalog = json.loads((run / "network-foundation.json").read_bytes())
    if (cond["authority"]["discovery"] != "all_delivered_registered"
            and set(cond["authority"]["allowedServiceIds"]) - catalog["initialServices"].keys()):
        raise ValueError("Future registrations must enter training work orders through delivered reports")
    facts = FoundationFeatures(catalog)
    def read_lines(name):
        with (run/name).open(encoding="utf-8") as rows:
            yield from (json.loads(line) for line in rows)
    choices = [row["tool_choice"] for row in read_lines("b03.jsonl") if row["event"] == "b03_rc_plan"
               and (row.get("tool_choice") or {}).get("modelCalled")]
    intervals = list(read_lines("business-intervals.jsonl"))
    result = trajectory(intervals, choices, report["business"]["U"])
    if len(choices) != report["actualModelCalls"]:
        raise ValueError("Sample count differs from verified model calls")
    calls = {c["sampleId"]: c for c in choices}
    if len(calls) != len(choices):
        raise ValueError("Duplicate sample in a rollout")
    for step in result["steps"]:
        c = calls[step["sampleId"]]
        obs = step["observation"]
        x = facts.encode(obs)
        expected = probabilities(parameters["weights"], x, obs["legalActions"])
        if (c["parameterVersion"] != parameters["version"] or c["parameterSha256"] != evidence_digest(parameters)
                or x != c["foundationFeatures"] or c["behaviorProbabilities"] != expected
                or c["behaviorProbability"] != expected[c["selected"]]):
            raise ValueError("Features, sampled action probability or checkpoint differ on replay")
        step["features"] = x
    # A counterfactual baseline at the same time comes from a different rollout of this case.
    # Store integral U(t), never W. Paired seeds must use the same starting conditions.
    ends, prefix, counts = [], [0], []
    for row in intervals:
        ends.append(row["end_ns"])
        counts.append(len(row["unmet"]))
        prefix.append(prefix[-1] + row["U_service_ns"])
    key = paired_conditions(cond)
    return dict(result=result, key=key, ends=ends, prefix=prefix, counts=counts, run=run,
                digest=report["verification"]["archiveIndexSha256"], seed=(cond["components"]["model"]
                .get("options",{}).get("drawSeed",0)))


def remaining(episode, stamp):
    result = episode["result"]
    i = bisect_right(episode["ends"], stamp)
    t = episode["ends"][i-1] if i else result["startNs"]
    spent = episode["prefix"][i]
    if i < len(episode["counts"]):
        spent += (stamp - t) * episode["counts"][i]
    return result["unmetServiceNs"] - spent


def training_examples(rollouts):
    groups = defaultdict(list)
    for episode in rollouts:
        groups[episode["key"]].append(episode)
    if len({ep["digest"] for ep in rollouts}) != len(rollouts):
        raise ValueError("A sealed rollout was supplied more than once")
    examples = []
    for episodes in groups.values():
        if len(episodes) < 2 or len({e["seed"] for e in episodes}) != len(episodes):
            raise ValueError("Each case requires independently seeded samples at identical settings")
        for episode in episodes:
            others = [ep for ep in episodes if ep is not episode]
            for s in episode["result"]["steps"]:
                t = s["observation"]["timeNs"]
                reference = math.fsum(remaining(ep,t) for ep in others)/len(others)
                advantage_s = (reference - remaining(episode,t)) / 1e9
                examples.append((s["features"],s["action"],s["behaviorProbability"],
                                 s["observation"]["legalActions"],advantage_s))
    return examples


def update(parameters, examples, *, epochs=4, learning_rate=0.16, clip=0.15):
    if not examples:
        raise ValueError("No verified in-window decisions for this update")
    weights = parameters["weights"][:]
    rms = math.sqrt(math.fsum(row[4]**2 for row in examples)/len(examples))
    if rms == 0:
        raise ValueError("These rollouts contain no measurable unweighted return contrast")
    history = []
    for epoch in range(epochs):
        grad = [0.0] * len(FEATURES)
        surrogate = 0.0
        for x, action, old, legal, raw_advantage in examples:
            p = probabilities(weights,x,legal)
            ratio = p[action] / old
            advantage = raw_advantage / rms
            surrogate += min(ratio*advantage, min(1+clip,max(1-clip,ratio))*advantage)
            if len(legal)<2 or (advantage>0 and ratio>1+clip) or (advantage<0 and ratio<1-clip):
                continue
            factor = advantage * ratio * (int(action=="B") - p["B"])
            for i,value in enumerate(x):
                grad[i] += factor * value / len(examples)
        weights = [w + learning_rate*g for w,g in zip(weights,grad,strict=True)]
        history.append({"epoch":epoch+1,"meanClippedObjective":surrogate/len(examples),
                        "gradientNorm":math.sqrt(math.fsum(v*v for v in grad))})
        if epoch == 0:
            history[-1]["gradientByFeature"] = dict(zip(FEATURES, grad, strict=True))
    if weights == parameters["weights"]:
        raise ValueError("Policy parameters did not change")
    return {**parameters,"version":parameters["version"]+1,"weights":weights}, history


def feature_evidence(before, after, examples, gradients):
    """Describe when each feature could influence the observed two-tool update."""
    evidence = {}
    for index, name in enumerate(FEATURES):
        seen = {symbol: 0 for symbol in before["symbols"]}
        informative = dict(seen)
        for x, action, _old, legal, advantage in examples:
            if len(legal) == 2 and x[index] != 0:
                seen[action] += 1
                if advantage != 0:
                    informative[action] += 1
        evidence[name] = {"presentBothLegalByAction": seen,
                          "nonzeroAdvantageByAction": informative,
                          "firstEpochNetGradient": gradients[name],
                          "weightBefore": before["weights"][index],
                          "weightAfter": after["weights"][index]}
    return evidence


def matched_observations(episodes):
    """Count different sampled actions at an identical observed state and paired conditions."""
    states = defaultdict(lambda: defaultdict(set))
    for episode in episodes:
        for step in episode["result"]["steps"]:
            obs = step["observation"]
            key = (episode["key"], evidence_digest(obs))
            states[key][step["action"]].add(remaining(episode, obs["timeNs"]))
    diverse = [returns for returns in states.values() if len(returns) > 1]
    separated = 0
    for returns in diverse:
        ranges = sorted((min(values), max(values)) for values in returns.values())
        if all(left[1] < right[0] for left, right in zip(ranges, ranges[1:])):
            separated += 1
    return {"statesWithBothActions": len(diverse),
            "statesWithDifferentRemainingU": sum(len(set.union(*rows.values())) > 1 for rows in diverse),
            "statesWithNonoverlappingActionRanges": separated}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("init");a.add_argument("--output",required=True,type=Path)
    a=sub.add_parser("update");a.add_argument("--checkpoint",required=True,type=Path)
    a.add_argument("--output",required=True,type=Path);a.add_argument("--report",required=True,type=Path)
    a.add_argument("runs",nargs="+",type=Path)
    args=p.parse_args()
    if args.command=="init":
        write_parameters(args.output,initial_parameters())
        return
    if args.output.exists() or args.report.exists():
        raise ValueError("Select new output paths for the checkpoint and update receipt")
    before=read_parameters(args.checkpoint)
    episodes=[load_rollout(r,before) for r in args.runs]
    examples=training_examples(episodes)
    after,history=update(before,examples)
    write_parameters(args.output,after)
    report={"schema":"b03.foundation-policy-update.v1","objective":"unweighted_remaining_service_seconds",
            "scale":PROFILE["nodes"],"episodes":[{"archiveSha256":ep["digest"],"seed":ep["seed"],
            "scoredDecisions":len(ep["result"]["steps"]),"U":ep["result"]["unmetServiceNs"]/1e9}
            for ep in episodes],"samples":len(examples),"epochs":history,"oldSha256":evidence_digest(before),
            "newSha256":evidence_digest(after),"parameterVersion":after["version"]}
    report["featureEvidence"] = feature_evidence(before, after, examples, history[0]["gradientByFeature"])
    report["matchedObservations"] = matched_observations(episodes)
    args.report.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
