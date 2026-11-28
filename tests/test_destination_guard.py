"""验证目的节点断联时，停源授权精确限定于仍在向失联终点发包的源端代际。"""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/packet"))
from destination_guard import choose_source_stop


def probe(stamp, peer_online, *, up=True):
    return {"sampled_at_ns": stamp, "node_online": True, "endpoint_online": peer_online,
            "admin_up": up, "status": "fresh"}


def episode():
    state = {"services": {}, "confirmed": {}, "links": {
        "last": {"src_node_id": "edge", "dst_node_id": "dest",
                 "reports": {"edge": probe(100, False), "dest": None}},
        "other": {"src_node_id": "backup", "dst_node_id": "dest",
                  "reports": {"backup": None, "dest": None}}}}
    for sid, rate in (("small", 3000), ("large", 5000)):
        state["services"][sid] = {"node_id": "source", "observed_at_ns": 96, "fresh": True,
                                  "registered": True, "active": True, "revision": 3,
                                  "service": {"src_node_id": "source", "dst_node_id": "dest"}}
        state["confirmed"][sid] = {"epoch": 7, "decision": {"node_path": ["source", "edge", "dest"],
                                                            "wire_bps": rate, "demand_revision": 3}}
    return state


class DestinationStopTests(unittest.TestCase):
    def test_largest_reachable_source_first_and_respects_authority(self):
        state=episode()
        stop=choose_source_stop(state, {"small","large"}, [])
        self.assertEqual((stop["service_id"],stop["epoch"],stop["link_id"]),("large",7,"last"))
        self.assertEqual(choose_source_stop(state, {"small"}, [])["service_id"], "small")
        self.assertIsNone(choose_source_stop(state, {"large"}, ["large"]))

    def test_newer_or_simultaneous_liveness_overrides_outage(self):
        for reporter, up in (("backup",True),("dest",False)):
            with self.subTest(reporter=reporter):
                state=episode()
                state["links"]["other"]["reports"][reporter]=probe(100,up)
                self.assertIsNone(choose_source_stop(state, {"small","large"}, []))

    def test_admin_down_stale_or_changed_demand_cannot_authorize_stop(self):
        for mut in ("admin", "stale_port", "revision", "stale_source", "active"):
            with self.subTest(mut=mut):
                state=episode()
                if mut=="admin": state["links"]["last"]["reports"]["edge"]["admin_up"]=False
                elif mut=="stale_port": state["links"]["last"]["reports"]["edge"]["status"]="stale"
                elif mut=="revision": state["services"]["large"]["revision"]+=1
                elif mut=="stale_source": state["services"]["large"]["fresh"]=False
                else: state["services"]["large"]["active"]=False
                self.assertIsNone(choose_source_stop(state, {"large"}, []))


    def test_stop_hold_and_recover_with_independent_packet_work(self):
        from dataclasses import replace
        from copy import deepcopy
        from unittest.mock import patch

        root = Path(__file__).resolve().parents[1]
        sys.path[:0] = [str(root / name) for name in ("tests", "src", "src/packet", "src/rules", "src/rules/vendor/src", "simulator/resnet_py/src")]
        from component_config import load_components
        from destination_guard import verify_source_stops
        from frozen_policy import DecisionPolicy
        from packet_device import install
        from packet_roles import PacketRoles
        from lifecycle_evidence import check_lifecycle
        from queue_evidence import verify_queue_evidence
        from scene import DEPENDENCY
        from resnet.contracts.scenario import EventBatch
        from resnet.simulation.simulation import Simulation
        from test_packet_lifecycle import contention_scenario

        base = contention_scenario()
        proto = base.services[0]
        services = tuple(replace(proto, service_id=sid, src_node_id="x", dst_node_id=dst, required_bps=rate)
                         for sid, dst, rate in (("large", "z", 700_000), ("small", "z", 400_000),
                                               ("healthy", "y", 300_000), ("later", "y", 200_000)))
        ids = [s.service_id for s in services]
        def traffic(active):
            return {"kind": "traffic_set", "active_service_ids": active}
        def power(kind):
            return {"kind": "official_event", "payload": {"eventType": kind, "attrs": {"nodeId": "z"}}}
        def batch(sec, sequence, ops, active):
            return EventBatch(sec * 1_000_000, sequence, tuple(ops), None, tuple(active))
        batches = (batch(0, 0, [traffic(ids[:3])], ids[:3]), batch(8, 1, [power("node_offline")], ids[:3]),
                   batch(12, 2, [traffic(ids)], ids), batch(20, 3, [power("node_online")], ids))
        scenario = replace(base, scenario_id="source-stop-test", duration_s=32, services=services, batches=batches)
        # 该控制节拍正好落在邻居恢复探针到达与设备自身首条回复之间。
        controls = {"controller_id": "x", "qos_profile": "official", "probe_interval_ns": 500_000_000,
                    "service_report_interval_ns": 700_000_000, "observation_ttl_ns": 2_000_000_000,
                    "tick_ns": 100_010_472, "old_rule_retention_ns": 300_000_000, "command_validity_ns": 500_000_000,
                    "ack_wait_ns": 200_000_000, "max_retries": 1, "query_interval_ns": 300_000_000,
                    "transaction_timeout_ns": 5_000_000_000}
        cfg = {"mode": "control", "end_s": 30, "drain_s": 2, "control": controls}
        probes, demands, actions = {}, {}, []
        probe_rows, deliveries = [], []

        class Traced(Simulation):
            def log(self, name, row):
                if name == "controller" and row["event"] == "observation":
                    probe_rows.append(row)
                if name == "packets-control" and row["event"] == "delivered" and row["message"]["kind"] == "probe":
                    deliveries.append(row)
                if name == "controller" and row.get("result", {}).get("accepted"):
                    if row["event"] == "observation":
                        p = row["probe"]
                        probes[p["link"]["link_id"], p["node_id"], p["seq"]] = row
                    elif row["event"] == "service_observation":
                        m = row["message"]
                        demands[m["service"]["service_id"], m["seq"]] = row
                super().log(name, row)
        sim = Traced(scenario, cfg)
        chooser = DecisionPolicy("I", components=load_components().activate("I"))
        conditions = {"simulation": {"control": sim.control.options},
                      "authority": {"allowedServiceIds": ids, "protectedServiceIds": []},
                      "deviceRecovery": {"contractVersion": "b03.deferred-cleanup.v2", "deferUnreachableCleanup": True},
                      "provenance": {"schema": "b03.provenance.v2"}}
        try:
            with PacketRoles(ids, actions.append, chooser=chooser) as roles:
                loop = install(sim, roles, actions.append)
                self.assertTrue(loop.defer_unreachable_cleanup)
                sim.advance(7_000_000_000)
                before = {sid: val["epoch"] for sid, val in loop.confirmed.items()}
                budgets = loop.budgets()
                sim.advance(19_000_000_000)
                during = {sid: val["epoch"] for sid, val in loop.confirmed.items()}
                held = loop.status()["deferred_cleanups"]
                rates = {sid: src.rate_bps for sid, src in sim.sources.items()}
                reserved_during = loop.budgets()
                sim.advance(29_000_000_000)
                after = {sid: val["epoch"] for sid, val in loop.confirmed.items()}
                plans = [r for r in actions if r["event"] == "b03_rc_plan"]
                stops = [r for r in plans if r.get("source_stop")]
                parks = [r for r in actions if r["event"] == "release_parked"]
                wakes = [r for r in actions if r["event"] == "release_recovery_probe"]
                released = [r for r in actions if r["event"] == "service_released"]
                check = verify_source_stops(plans, actions, probes, demands, conditions)
                lifecycle = check_lifecycle(plans, actions, demands, conditions, probes)
                conditions["simulation"]["queue_packets"] = sim.config["queue_packets"]
                conditions["releaseManifestSha256"] = DEPENDENCY["runtimeSha256"]
                queue_verified = verify_queue_evidence(conditions, {"control": sim.control.summary()}, probe_rows, actions,
                                                     {"xy": ("x", "y"), "yz": ("y", "z")}, deliveries)
        finally:
            sim.close()
        self.assertEqual(before, {"large": 1, "small": 1, "healthy": 1})
        self.assertEqual([r["source_stop"]["service_id"] for r in stops], ["large", "small"])
        self.assertEqual(check, 2)
        self.assertTrue(all(r["real_solver_calls"] == 0 and not r["tool_choice"]["modelCalled"] for r in stops))
        self.assertEqual({r["service_id"] for r in held}, {"large", "small"})
        self.assertTrue(all(r["source_stop_confirmed"] and r["offline_probe"] for r in parks))
        self.assertEqual(rates, {"large": 0, "small": 0, "healthy": 300_000, "later": 200_000})
        self.assertEqual(during["healthy"], before["healthy"])
        self.assertEqual(reserved_during["yz"], budgets["yz"])
        self.assertGreater(reserved_during["xy"], budgets["xy"])
        self.assertTrue(any(r["reporter_node_id"] != r["node_id"] for r in wakes))
        self.assertTrue(any(r["reporter_node_id"] == r["node_id"] for r in wakes))
        self.assertEqual({r["service_id"] for r in released}, {"large", "small"})
        self.assertTrue(all(set(r["query_ids"]) == {"x", "y", "z"} and r["epoch"] == 1 for r in released))
        self.assertEqual(after, {"large": 2, "small": 2, "healthy": 1, "later": 1})
        self.assertEqual(lifecycle["pendingReassessments"], 0)
        self.assertTrue(queue_verified)

        first = stops[0]
        dispatch = next(r for r in actions if r["event"] == "work_step" and r["work_id"] == first["work_id"])
        # 下发前新送达的在线迹象会覆盖规划阶段签发的停源许可。
        stamp = (first["time_ns"] + dispatch["time_ns"]) // 2
        down = first["source_stop"]
        receipt = next(r for r in probes.values() if r["probe"]["observed_at_ns"] == down["sampled_at_ns"]
                       and r["probe"]["node_id"] == down["reporter_node_id"] and r["probe"]["link"]["link_id"] == down["link_id"])
        contrary = deepcopy(receipt)
        contrary["time_ns"] = stamp
        contrary["probe"].update(observed_at_ns=stamp, seq=100_000)
        contrary["probe"]["link"]["endpoint_online"] = True
        with patch.dict(probes, {(down["link_id"], down["reporter_node_id"], 100_000): contrary}):
            with self.assertRaisesRegex(ValueError, "latest delivered"):
                verify_source_stops(plans, actions, probes, demands, conditions)
