"""Two-tool policy over a startup catalogue and a delivered work-order observation."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from knowledge.agent_facts import evidence_digest

SYMBOLS = ("B", "I")
FEATURES = ("bias", "initial_mean_degree", "initial_load", "writable_share",
            "unusable_link_share", "degraded_link_share", "relative_capacity_loss",
            "new_request_share")
SCHEMA = "b03.foundation-selector.v1"


def fraction(value, total):
    return value / total if total else 0.0


def rows(table):
    for identifier, mask, *overrides in table["rows"]:
        values = iter(overrides)
        yield dict(zip(table["columns"], [identifier, *(
            next(values) if mask & (1 << j) else default
            for j, default in enumerate(table["defaults"])
        )], strict=True))


class FoundationFeatures:
    def __init__(self, catalog):
        self.digest = evidence_digest(catalog)
        self.capacities = {k: v["effectiveParams"]["bandwidthBps"] for k, v in catalog["links"].items()}
        links, nodes = len(self.capacities), len(catalog["nodes"])
        self.density = fraction(2 * links, 2 * links + nodes)
        demand = sum(v["requirement"]["bandwidthBps"] for v in catalog["initialServices"].values())
        self.load = fraction(demand, demand + sum(self.capacities.values()))

    def encode(self, observation):
        reference = observation["networkReference"]
        if reference["catalogDigest"] != self.digest:
            raise ValueError("Model observation and startup catalogue refer to different editions")
        changes = reference["linksBelowStartupCapacity"]
        lost = []
        for name, item in changes.items():
            base = self.capacities[name]
            if base != item["startupBps"] or not 0 <= item["observedBps"] < base:
                raise ValueError("Invalid delivered capacity change")
            lost.append(1 - item["observedBps"] / base)
        links = list(rows(observation["links"]))
        services = list(rows(observation["services"]))
        active = [v for v in services if v["active"]]
        reported = len([s for s in reference["serviceIds"] if s is not None])
        return [1.0, self.density, self.load, fraction(sum(bool(s["writable"]) for s in active),len(active)),
                fraction(sum(not l["usable"] for l in links),len(links)), fraction(len(lost),len(links)),
                fraction(sum(lost),len(lost)), fraction(len(reference["reportedOutsideInitialDefinitions"]),reported)]


def initial_parameters():
    return {"schema": SCHEMA, "features": list(FEATURES), "symbols": list(SYMBOLS),
            "version": 0, "weights": [0.0] * len(FEATURES)}


def read_parameters(directory):
    parameters = json.loads((Path(directory) / "selector.json").read_bytes())
    if (parameters.get("schema") != SCHEMA or parameters.get("features") != list(FEATURES)
            or parameters.get("symbols") != list(SYMBOLS)
            or type(parameters.get("version")) is not int or parameters["version"] < 0
            or not isinstance(parameters.get("weights"), list) or len(parameters["weights"]) != len(FEATURES)
            or not all(type(x) in (int, float) and math.isfinite(x) for x in parameters["weights"])):
        raise ValueError("Incompatible selector checkpoint")
    return parameters


def write_parameters(directory, parameters):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "selector.json"
    with destination.open("x", encoding="utf-8") as out:
        json.dump(parameters, out, indent=2, allow_nan=False)
        out.write("\n")


def probabilities(weights, features, legal):
    if not legal or not set(legal) <= set(SYMBOLS):
        raise ValueError("Unexpected legal tool set")
    if len(legal) == 1:
        return {legal[0]: 1.0}
    z = math.fsum(w*x for w,x in zip(weights, features, strict=True))
    # Log-sum-exp for a binary policy; avoids overflow in both tails.
    denom = math.exp(-abs(z))
    p = (1 / (1 + denom)) if z >= 0 else (denom / (1 + denom))
    return {"B": p, "I": (denom / (1 + denom)) if z >= 0 else (1 / (1 + denom))}


class FoundationActor:
    symbols = list(SYMBOLS)

    def __init__(self, checkpoint, config):
        self.parameters = read_parameters(checkpoint)
        self.sample = config["mode"] == "online-ppo"
        self.seed = int(config.get("options", {}).get("drawSeed", 0))
        self.draws = 0
        self.facts = None

    @property
    def version(self):
        return self.parameters["version"]

    @property
    def identity(self):
        return {"version": self.version, "parameterSha256": evidence_digest(self.parameters)}

    def set_network_foundation(self, catalog):
        self.facts = FoundationFeatures(catalog)

    def choose(self, observation):
        if self.facts is None:
            raise ValueError("The foundation selector requires --foundation")
        x = self.facts.encode(observation)
        legal = observation["legalActions"]
        prob = probabilities(self.parameters["weights"], x, legal)
        detail = {"parameterVersion": self.version, "parameterSha256": evidence_digest(self.parameters),
                  "foundationFeatures": x, "legalProbabilities": prob}
        if self.sample:
            bits = int.from_bytes(hashlib.sha256(f"{self.seed}/{self.draws}".encode()).digest()[:8], "big") >> 11
            draw = bits / (1 << 53)
            chosen = ("B" if draw < prob["B"] else "I") if len(legal) == 2 else legal[0]
            detail.update(sampleId=f"{self.seed}/{self.draws}", samplingUniform=draw, policyMode="sample")
            self.draws += 1
            behavior = prob
        else:
            chosen = max(legal, key=lambda symbol: (prob[symbol], symbol == "I"))
            behavior = {s: float(s == chosen) for s in legal}
        return chosen, {**detail, "behaviorProbabilities": behavior, "behaviorProbability": behavior[chosen],
                        "oldLogProbability": math.log(behavior[chosen])}

    def save_checkpoint(self, directory):
        write_parameters(directory, self.parameters)
        (Path(directory) / "sampler.json").write_text(json.dumps({"seed": self.seed, "draws": self.draws}))

    def restore_checkpoint(self, directory):
        self.parameters = read_parameters(directory)
        cursor = Path(directory) / "sampler.json"
        if cursor.is_file():
            saved = json.loads(cursor.read_bytes())
            self.seed, self.draws = int(saved["seed"]), int(saved["draws"])


def build_model(*, checkpoint, tokenizer, device, config):
    if checkpoint is None:
        raise ValueError("Provide the selector checkpoint directory with --checkpoint")
    return FoundationActor(checkpoint, deepcopy(config))
