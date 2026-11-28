"""验证可选诊断知识、开局底图交接及独立回放核验的合成工单测试。"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in (".", "tests", "src", "src/packet", "simulator/resnet_py/src")]
from test_blackbox_rule_knowledge import replay, scenario, synthetic_state
from component_config import load_components
from frozen_policy import DecisionPolicy, FrozenActor, canonical, model_instructions
from knowledge.agent_facts import AgentNetworkFacts, verify_agent_facts
from knowledge.work_order import diagnosis_ids, diagnosis_query
from packet_roles import _PacketAgents, digest
from tech_knowledge import query


def foundation(state):
    lid = sorted(state["links"])[0]
    row = state["links"][lid]
    return {"schema": "b03.network-foundation.v1", "nodes": {"aster": {"nodeType": "relay"}},
            "links": {lid: {"effectiveParams": {"bandwidthBps": row["capacity_bps"] + 10}}},
            "initialServices": {}, "channelModel": {"model": "scenario-link-parameters"},
            "resources": {"queue_packets": 32, "queue_bytes": 65536, "control_headroom_fraction": 0}}


class DiagnosticKnowledge(unittest.TestCase):
    def test_opted_in_adapter_receives_detached_start_and_delivered_delta(self):
        from standalone_selector import Selector
        state = synthetic_state()
        catalog = foundation(state)
        lid = next(iter(catalog["links"]))
        observed = state["links"][lid]["capacity_bps"]
        seen = []

        def load_catalog(actor, payload):
            seen.append(deepcopy(payload))
            payload["nodes"].clear()

        with patch.object(Selector, "set_network_foundation", load_catalog, create=True):
            base = dict(method="model", profile="collab-opt-in.json", state=state, network_foundation=catalog)
            disabled = scenario(**base, model_options={"acceptKnowledge": False})
            self.assertFalse(seen)
            self.assertNotIn("networkReference", disabled[2]["tool_choice"]["observation"])
            enabled = scenario(**base, model_options={"acceptNetworkFoundation": True, "acceptKnowledge": False})
            with self.assertRaisesRegex(ValueError, "requires a startup network foundation"):
                scenario(**dict(base, network_foundation=None), model_options={"acceptNetworkFoundation": True})
        self.assertEqual(seen, [catalog])
        _, cond, plan, om, evidence, _ = enabled
        observation = plan["tool_choice"]["observation"]
        self.assertNotIn("knowledgeContext", observation)
        delta = om["networkBaseline"]
        self.assertEqual(delta["linksBelowStartupCapacity"], {lid: {"startupBps": observed + 10, "observedBps": observed}})
        self.assertEqual(delta["reportedOutsideInitialDefinitions"], ["ribbon"])
        reference = observation["networkReference"]
        self.assertEqual(reference, dict(delta, linkIds=sorted(state["links"]), nodeIds=["aster", "birch", "cedar"],
                                         serviceIds=["ribbon"]))
        self.assertEqual(plan["rc_network_baseline_digest"], digest(delta))
        events = [evidence, om, plan]
        args = (cond["rcPolicy"], cond["components"]["model"])
        self.assertEqual(verify_agent_facts(catalog, events, *args)["verifiedOrders"], 1)
        for rows in (events + [om], [om, plan], [dict(evidence, snapshot_id="wrong"), om, plan]):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                verify_agent_facts(catalog, rows, *args)
        with self.assertRaises(ValueError):
            verify_agent_facts(None, events, *args)
        with self.assertRaises(ValueError):
            verify_agent_facts(catalog, events, args[0], dict(args[1], options={}))
        forged = deepcopy(events)
        forged[1]["services"].clear()
        forged[2]["tool_choice"]["observation"]["networkReference"]["serviceIds"] = []
        with self.assertRaisesRegex(ValueError, "omits a service"):
            verify_agent_facts(catalog, forged, *args)
        forged = deepcopy(events)
        forged[1]["services"]["unreported"] = {}
        forged[2]["tool_choice"]["observation"]["networkReference"]["serviceIds"].append(None)
        with self.assertRaisesRegex(ValueError, "roster"):
            verify_agent_facts(catalog, forged, *args, cond["authority"])
        reference["serviceIds"] = []
        with self.assertRaisesRegex(ValueError, "Model network reference"):
            verify_agent_facts(catalog, events, *args)
        state["links"][lid] = {"usable": False}
        self.assertEqual(AgentNetworkFacts(catalog).compare_reports(state)["linksBelowStartupCapacity"], {})
        with self.assertRaises(ValueError):
            AgentNetworkFacts(dict(catalog, events=[{"time": 5}]))
        with self.assertRaises(ValueError):
            AgentNetworkFacts(dict(catalog, initialServices={"secret": {"startAtSec": 8}}))

    def test_fixed_rule_receives_original_input_and_om_grant(self):
        import packet_roles
        original = packet_roles.solve_action
        bound_inputs = []

        def capture(action, bound, context):
            bound_inputs.append(canonical([action, bound, context]).encode())
            return original(action, bound, context)

        with patch.object(packet_roles, "solve_action", capture):
            base = scenario(method="I", profile="components.json")
            enabled = scenario(method="I", profile="knowledge-om.json", network_foundation=foundation(synthetic_state()))
        self.assertEqual(len(bound_inputs), 2)
        self.assertEqual(bound_inputs[0], bound_inputs[1])
        self.assertEqual(base[3]["authority"], enabled[3]["authority"])
        self.assertEqual(enabled[2]["rc_knowledge_input_ids"], ["K046"])
        self.assertEqual(set(enabled[2]["knowledge_contexts"]), {"om"})
        replay(*enabled)

    def test_native_frozen_tokenizer_receives_the_original_prompt_bytes(self):
        spec = json.loads((ROOT / "config/model.json").read_bytes())
        # T141 在该合成已送达状态下的分词边界黄金提示词参照。
        expected = {18: (2114, "8bb69fdff2c0dee71eb11c6134c1a7ca3d87ccf920493a385f28fbb82d260367"),
                    372: (3559, "abd2af9d70f420dd55eed802d1d604611f29dc8f326e20b114a5da60115092ef")}
        captured = []

        class CapturedPrompt(Exception):
            pass

        class CaptureTokenizer:
            def apply_chat_template(self, messages, **kwargs):
                raw = canonical(messages).encode()
                captured.append((len(raw), sha256(raw).hexdigest()))
                raise CapturedPrompt

        actor = object.__new__(FrozenActor)
        actor.symbols, actor.version = spec["symbols"], spec["version"]
        actor.identity = {"kind": "frozen-model", "version": actor.version, "selection": "greedy_legal_token"}
        actor.instructions, actor.torch, actor.tokenizer = model_instructions(spec), None, CaptureTokenizer()
        with patch("frozen_policy.FrozenActor", return_value=actor):
            for profile, network in (("components.json", None), ("components.json", foundation(synthetic_state())),
                                     ("knowledge-om.json", foundation(synthetic_state()))):
                with self.subTest(profile=profile), self.assertRaises(CapturedPrompt):
                    scenario(method="model", profile=profile, checkpoint="captured-at-tokenizer", network_foundation=network)
        self.assertEqual(captured, [expected[actor.version]] * 3)

    def test_independent_chinese_knowledge_option_reaches_custom_model(self):
        from standalone_selector import Selector
        received = []

        def choose(actor, observation):
            received.append(deepcopy(observation))
            ids = [row["id"] for row in observation.get("knowledgeContext", [])]
            return ("B" if "K046" in ids else "A"), {"policyVersion": actor.version}

        with tempfile.TemporaryDirectory() as tmp, patch.object(Selector, "choose", choose):
            profile = Path(tmp) / "selector.json"
            profile.write_text(json.dumps({"extends": str(ROOT / "config/knowledge-om.json"), "model": {
                "id": "synthetic.selector", "entrypoint": "standalone_selector:build_model",
                "symbols": ["A", "B"]}}), encoding="utf-8")
            for enabled, action in ((False, "A"), (True, "B")):
                result = scenario(method="model", profile=profile, model_options={"acceptKnowledge": enabled})
                self.assertEqual(result[2]["tool_choice"]["selected"], action)
                self.assertNotIn("networkReference", received[-1])
                self.assertEqual("knowledgeContext" in received[-1], enabled)
                self.assertEqual("model" in result[2]["knowledge_contexts"], enabled)
                replay(*result)

    def test_multiple_symptoms_and_service_hits_return_each_id_only_once(self):
        binding = {"from": "diagnosis", "topics": {"demand_stale": ["K047", "K048"],
                                                   "path_observation_incomplete": ["K048", "CASE001"]}}
        diagnosis = {"services": {sid: {"status": "demand_stale", "potential_action_status": "path_observation_incomplete",
                                         "evidence": [{"reason": "demand_stale"}]} for sid in ("z", "x")}}
        question = diagnosis_query(binding, diagnosis, {})
        self.assertEqual(question["matches"], {"demand_stale": ["x", "z"], "path_observation_incomplete": ["x", "z"]})
        self.assertEqual(diagnosis_ids(binding, question), ["K047", "K048", "CASE001"])

    def test_rc_rejects_a_coherently_changed_om_delta_or_knowledge(self):
        host = _PacketAgents.handle
        catalog = foundation(synthetic_state())
        for corruption in ("capacity", "advice"):
            def alter(agents, request):
                request = deepcopy(request)
                if request["kind"] == "control_request":
                    payload = request["payload"]
                    if corruption == "capacity":
                        payload["diagnosis"]["networkBaseline"]["linksBelowStartupCapacity"] = {}
                    else:
                        payload["diagnosis"]["knowledgeContext"] = []
                        payload["om_advisory"]["output"] = []
                return host(agents, request)

            with self.subTest(corruption=corruption), patch.object(_PacketAgents, "handle", alter), self.assertRaises(ValueError):
                scenario(method="I", profile="knowledge-om.json", network_foundation=catalog)

    def test_om_queries_distinct_delivered_conditions_and_hands_them_to_rc(self):
        state = synthetic_state()
        for sid in ("violet", "fern"):
            row = deepcopy(state["services"]["ribbon"])
            row["service"]["service_id"] = sid
            state["services"][sid] = row
        state["services"]["ribbon"]["fresh"] = False
        base = scenario(method="I", ports=[], state=state, profile="knowledge-om.json")
        result = scenario(method="I", state=state, profile="knowledge-om.json")
        _, _, plan, diagnosis, _, captured = result
        self.assertEqual(set(plan["knowledge_contexts"]), {"om"})
        receipt = plan["knowledge_contexts"]["om"]
        self.assertEqual(receipt["schema"], "b03.knowledge.context.v3")
        self.assertEqual(receipt["query"], {"from":"diagnosis", "matches":{
            "demand_stale":["ribbon"], "observed_pending_admission":["fern","violet"]}})
        self.assertCountEqual([row["id"] for row in receipt["output"]], ["K046","K047"])
        self.assertEqual(diagnosis["knowledgeContext"], receipt["output"])
        self.assertEqual(captured["diagnosis"]["knowledgeContext"], receipt["output"])
        self.assertEqual(diagnosis["authority"], base[3]["authority"])
        for key in ("decisions", "authorized", "install_ids", "release_ids"):
            self.assertEqual(plan[key], base[2][key])
        self.assertNotIn("knowledge_contexts", base[2])
        replay(*result)

    def test_reason_and_cleanup_matches_require_specific_evidence(self):
        from knowledge_ports import diagnosis_query
        c = load_components(ROOT/"config/knowledge-om.json")
        binding = c.knowledge["bindings"]["om"]
        row = {"status":"configuration_refresh_required", "potential_action_status":"observed_physical_failure",
               "evidence":[{"link_id":"mint","reason":"confirmed_load_exceeds_observed_capacity"}]}
        diagnosis = {"services":{"ribbon":row}}
        state = {"controller_deferred_writes":["fern","willow"], "controller_pending_cleanups":["fern"]}
        matches = diagnosis_query(binding, diagnosis, state)["matches"]
        self.assertEqual(matches, {"confirmed_load_exceeds_observed_capacity":["ribbon"],
                                   "retaining_until_cleanup_readback":["fern"]})
        row.update(potential_action_status="path_observation_incomplete", evidence=[])
        self.assertEqual(diagnosis_query(binding, diagnosis, {})["matches"],
                         {"path_observation_incomplete":["ribbon"]})

    def test_replay_detects_coordinated_changes_to_om_knowledge(self):
        for tamper in ("missing_output", "wrong_service", "wrong_status"):
            result = scenario(method="I", profile="knowledge-om.json")
            components, _, plan, diagnosis, _, _ = result
            receipt = plan["knowledge_contexts"]["om"]
            if tamper == "missing_output":
                receipt["output"] = []
                diagnosis["knowledgeContext"] = []
            elif tamper == "wrong_service":
                receipt["query"]["matches"]["observed_pending_admission"] = ["invented"]
            else:
                row = diagnosis["services"]["ribbon"]
                row.update(status="demand_stale", potential_action_status="demand_stale")
                receipt["query"]["matches"] = {"demand_stale":["ribbon"]}
                receipt["output"] = query(components, identifier="K047")
                diagnosis["knowledgeContext"] = deepcopy(receipt["output"])
            with self.subTest(tamper=tamper), self.assertRaises(ValueError):
                replay(*result)

    def test_opted_in_rule_uses_om_advice_for_two_feasible_paths(self):
        for first, second, path in (("EX.PATH.HOPS","EX.PATH.DELAY",["rose"]),
                                    ("EX.PATH.DELAY","EX.PATH.HOPS",["mint","plum"])):
            bindings = {"om":{"from":"diagnosis","topics":{"observed_pending_admission":[first,second]}},
                        "rules":{"from":"om"}, "model":{"from":"om"}}
            with self.subTest(first=first):
                result = scenario(bindings=bindings)
                _, _, plan, diagnosis, _, _ = result
                contexts = plan["knowledge_contexts"]
                self.assertEqual(set(contexts), {"om","rules"})
                self.assertEqual([row["id"] for row in diagnosis["knowledgeContext"]], [first,second])
                self.assertEqual(contexts["om"]["output"], contexts["rules"]["output"])
                request = plan["solver_audit"]["rulePatch"]["input"]
                self.assertEqual(request["knowledgeContext"], contexts["om"]["output"])
                self.assertEqual(plan["decisions"]["ribbon"]["link_path"], path)
                replay(*result)

    def test_source_stop_replay_uses_the_producers_authority(self):
        from rule_replay import replay_diagnosis
        _, conditions, plan, _, _, _ = scenario(method="I", profile="knowledge-om.json")
        state = synthetic_state()
        choice = plan["decisions"]["ribbon"]
        state["confirmed"]["ribbon"] = {"epoch": 1, "decision": choice, "confirmed_at_ns": 20,
                                        "configuration_evidence": {"verified": False}}
        state["retained_wire_bps"] = {lid: choice["wire_bps"] for lid in choice["link_path"]}
        port = state["links"][choice["link_path"][-1]]
        live = choice["node_path"][-2]
        port.update(usable=False, reason="endpoint_offline", reports={
            live: {"status": "fresh", "node_online": True, "admin_up": True,
                   "sampled_at_ns": 40, "endpoint_online": False}})
        old = replay_diagnosis(conditions, state)[4]
        self.assertNotIn("source_stop", old)
        self.assertEqual(old["refresh_ids"], ["ribbon"])
        current = dict(conditions, implementationSha256={"src/packet/destination_guard.py": "registered"})
        new = replay_diagnosis(current, state)[4]
        self.assertEqual(new["source_stop"]["service_id"], "ribbon")
        self.assertEqual(new["refresh_ids"], [])
