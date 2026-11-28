"""The deferred cleanup proof must protect a retained epoch without the simulator."""
from collections import defaultdict
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path[:0] = [str(SRC.parent), str(SRC), str(SRC / "packet")]
from packet_cleanup_evidence import cleanup_budgets
from scene import simulator_root


class DeferredCleanupTests(unittest.TestCase):
    def setUp(self):
        self.cond = {"deviceRecovery": {"contractVersion": "b03.deferred-cleanup.v1", "deferUnreachableCleanup": True},
                     "simulation": {"control":{"command_validity_ns":3,"transaction_timeout_ns":15,"observation_ttl_ns":8}}}
        self.choice={"service_id":"old","node_path":["source","lost"],"link_path":["one"],"wire_bps":7}
        def event(name,t,**fields):
            return {"event":name, "time_ns":t, "work_id":"first", **fields}
        self.rows=[
            event("work_started",0,release_ids=["old"],install_ids=[]),
            event("work_step",1,phase="release",service_id="old",plan_id="plan",decision=self.choice,retained_wire_bps={"one":7}),
            event("device_ack",2,op="deactivate",plan_id="plan",node_id="source",service_id="old",epoch=1,original_status="applied"),
            event("native_transaction",2,phase="source_stop_ack",plan_id="plan",reconciled_by_query=False),
            event("device_command",3,op="cleanup",service_id="old",command_id="c",node_id="lost",epoch=1,issued_at_ns=3,plan_id="plan",retry=False),
            event("device_command",8,op="query",service_id="old",command_id="q",node_id="lost",epoch=1,issued_at_ns=8,plan_id="plan",retry=False),
            event("native_command_timeout",14,operation="query",plan_id="plan",command_id="q"),
            event("cleanup_deferred",18,expired_command_id="c",timed_out_query_id="q",epoch=1,node_id="lost",protected_services=["old"],
                  protected_nodes=["lost"],protected_links=["one"],retained_wire_bps={"one":7})]
        self.probes={}
        self.demands={}

    def validate(self):
        groups=defaultdict(lambda:defaultdict(list))
        for row in self.rows:
            groups[row.get("work_id")][row["event"]].append(row)
        return cleanup_budgets(self.rows, groups, self.cond, self.probes, self.demands)

    def test_unresolved_epoch_remains_reserved(self):
        self.assertEqual(self.validate(),{})
        self.rows[-1]["retained_wire_bps"]={}
        with self.assertRaisesRegex(ValueError,"wire changed"):
            self.validate()

    def test_requires_opt_in_and_device_ack(self):
        self.cond["deviceRecovery"]["deferUnreachableCleanup"]=False
        with self.assertRaisesRegex(ValueError,"not selected"):
            self.validate()
        self.cond["deviceRecovery"]["deferUnreachableCleanup"]=True
        self.rows[2]["original_status"]="rejected"
        with self.assertRaisesRegex(ValueError,"source-stop acknowledgment"):
            self.validate()

    def test_wrong_query_timeout_cannot_release_the_device_slot(self):
        self.rows[-1]["timed_out_query_id"]="unrelated-query"
        with self.assertRaisesRegex(ValueError,"timed-out read-only"):
            self.validate()

    def test_native_unknown_without_a_matching_query_timeout_stays_attached(self):
        sys.path.insert(0, str(simulator_root() / "src"))
        from packet_device import WorkLoop
        loop=object.__new__(WorkLoop)
        old={"decision":self.choice,"epoch":1}
        original={"command_id":"c","issued_at_ns":3,"validity_ns":3,"target_node_id":"lost",
                  "action":{"op":"cleanup","service_id":"old","epoch":1}}
        loop.transaction={"decision":self.choice,"status":"unknown","source_confirmed":True,"epoch":1,
                          "old":old,"deadline_ns":15,"uncertain_command":original}
        loop.work={"work_id":"first","release_ids":["old"],"install_ids":[],"queue":[]}
        loop.readout=loop.pending=None
        loop.native_plan_id="plan"
        loop.confirmed={"old":old}
        loop.retained=[old]
        loop.deferred_cleanups={}
        loop.defer_unreachable_cleanup=True
        loop.native_log=lambda row: None
        loop._event=lambda *args, **kwargs: None
        loop._defer_expired_cleanup(18)
        self.assertIsNotNone(loop.work)  # A nonterminal readback can also produce 'unknown'.
        loop.pending={"command":{"command_id":"q","target_node_id":"lost",
                                 "action":{"op":"query","service_id":"old","epoch":1}}}
        row={"event":"command_timeout","operation":"query","time_ns":17,"plan_id":"plan","command_id":"other-q"}
        loop._log(row)
        self.assertNotIn("cleanup_query_timeout",loop.transaction)
        loop._log(dict(row,command_id="q"))
        self.assertEqual(loop.transaction["cleanup_query_timeout"],("c","q"))
        loop.pending=None
        loop._defer_expired_cleanup(18)
        self.assertIsNone(loop.work)
        self.assertNotIn("cleanup_query_timeout",loop.deferred_cleanups["first"]["transaction"])

    def test_unrelated_work_cannot_use_held_port_or_link(self):
        after={"event":"work_step","time_ns":19,"work_id":"second","service_id":"new",
               "decision":{"node_path":["third","fourth"],"link_path":["other"],"wire_bps":1}}
        self.rows.append(after)
        self.assertEqual(self.validate(),{})
        self.rows[-1]["decision"]["link_path"]=["one"]
        with self.assertRaisesRegex(ValueError,"conflicting foreign work"):
            self.validate()

    def test_recovery_requires_a_delivered_new_probe_and_readback(self):
        wake={"event":"cleanup_recovery_probe","work_id":"first","time_ns":25,"epoch":1,"node_id":"lost",
              "link_id":"one","seq":5,"sampled_at_ns":22,"arrived_at_ns":23}
        self.rows.append(wake)
        with self.assertRaisesRegex(ValueError,"delivered device probe"):
            self.validate()
        self.probes["one","lost",5]={"time_ns":23,"probe":{"observed_at_ns":22},"result":{"accepted":True}}
        self.rows.append({"event":"device_command","work_id":"first","time_ns":25,"op":"cleanup","node_id":"lost",
                          "epoch":1,"service_id":"old","plan_id":"plan","issued_at_ns":25,"command_id":"retry-write"})
        with self.assertRaisesRegex(ValueError,"before reconciliation"):
            self.validate()

    def test_park_unconfirmed_source_in_joint_work_and_keep_budget(self):
        sys.path.insert(0, str(simulator_root() / "src"))
        from packet_device import WorkLoop
        loop = object.__new__(WorkLoop)
        old = {"decision": self.choice, "epoch": 1}
        cmd = {"command_id": "stop", "issued_at_ns": 3, "validity_ns": 3,
               "target_node_id": "source", "action": {"op": "deactivate", "service_id": "old", "epoch": 1}}
        loop.work = {"work_id": "first", "release_ids": ["old","other"], "install_ids": ["new"],
                     "queue": [("release","other"),("install","new")], "failures": {}}
        loop.transaction = {"decision": self.choice, "status": "unknown", "source_confirmed": False,
                            "uncertain_command": cmd, "epoch": 1, "deadline_ns": 18, "old": old,
                            "cleanup_query_timeout": ("stop","q")}
        loop.pending = loop.readout = None
        loop.native_plan_id = "plan"
        loop.confirmed, loop.retained, loop.deferred_cleanups = {"old":old}, [], {}
        loop.config = {"observation_ttl_ns":8}
        loop.link_reports = SimpleNamespace(snapshot=lambda _: {"links":{}})
        loop.service_reports = SimpleNamespace(snapshot=lambda _: {"services":{}})
        events = []
        loop.emit = events.append
        loop._defer_expired_cleanup(17)
        self.assertIsNotNone(loop.transaction)
        loop._defer_expired_cleanup(18)
        self.assertEqual(loop.budgets(), {"one":7})
        self.assertIsNone(loop.work)
        record = loop.deferred_cleanups["first"]
        self.assertFalse(record["transaction"]["source_confirmed"])
        self.assertEqual(record["work"]["queue"], [])
        self.assertEqual(set(record["work"]["failures"]), {"new","other"})
        self.assertEqual(events[-1]["event"],"release_parked")
        # Only a probe that was sampled after parking and reached the cache may wake this transaction.
        def snapshot(stamp):
            return {"links":{"one":{"reporters":{"source":{"received_at_ns":22, "probe":{
                "observed_at_ns":stamp, "node_online":True, "seq":5, "link":{"link_id":"one"}}}}}}}
        self.assertFalse(loop._resume_reachable_cleanup(23,snapshot(17)))
        issued = []
        loop._query = lambda now, original: issued.append((now,original))
        self.assertTrue(loop._resume_reachable_cleanup(23,snapshot(21)))
        self.assertEqual(issued,[(23,cmd)])
        self.assertEqual(loop.budgets(), {"one":7})

    def test_v2_replays_unconfirmed_stop_and_allows_other_epoch_writes(self):
        self.cond["deviceRecovery"]["contractVersion"] = "b03.deferred-cleanup.v2"
        self.rows[0]["install_ids"] = ["new"]
        del self.rows[2:4]
        self.rows[2].update(op="deactivate",node_id="source")
        self.rows[3]["node_id"]="source"
        self.rows[-1].update(event="release_parked",plan_id="plan",node_id="source",operation="deactivate",service_id="old",
                             source_stop_confirmed=False,reserved_links=["one"],abandoned_steps=[["install","new"]])
        self.rows.append({"event":"device_command","work_id":"second","time_ns":19,"service_id":"new",
                          "node_id":"source","op":"cleanup"})
        self.assertEqual(self.validate(),{})
        self.rows[-1]["service_id"]="old"
        with self.assertRaisesRegex(ValueError,"conflicting foreign write"):
            self.validate()
        self.rows.pop()
        self.rows[-1]["source_stop_confirmed"]=True
        with self.assertRaisesRegex(ValueError,"expired write scope"):
            self.validate()

    def test_early_park_requires_fresh_specific_unreachable_port_evidence(self):
        self.cond["deviceRecovery"]["contractVersion"] = "b03.deferred-cleanup.v2"
        w = {"reporter_node_id":"source","link_id":"one","seq":7,"sampled_at_ns":10,"arrived_at_ns":11}
        self.rows[-1].update(event="release_parked", plan_id="plan", operation="cleanup", time_ns=15, service_id="old",
                             source_stop_confirmed=True,reserved_links=["one"],offline_probe=w)
        with self.assertRaisesRegex(ValueError,"early park"):
            self.validate()
        probe={"node_id":"source","node_online":True,"seq":7,"observed_at_ns":10,
               "link":{"link_id":"one","peer_node_id":"lost","admin_up":True,"endpoint_online":False}}
        self.probes["one","source",7]={"time_ns":11,"probe":probe,"result":{"accepted":True}}
        self.assertEqual(self.validate(), {})
        probe["link"]["endpoint_online"]=True
        with self.assertRaisesRegex(ValueError,"early park"):
            self.validate()

    def test_contradictory_recovery_and_early_park_are_suppressed(self):
        from destination_guard import DeliveredNodeEvidence
        sys.path.insert(0, str(simulator_root() / "src"))
        from packet_device import WorkLoop

        def port(reporter, peer, lid, stamp, online):
            return {"seq": 1, "node_id": reporter, "node_online": True, "observed_at_ns": stamp,
                    "link": {"link_id": lid, "peer_node_id": peer, "admin_up": True, "endpoint_online": online}}

        loop = object.__new__(WorkLoop)
        loop.config = {"observation_ttl_ns": 8}
        down = port("adjacent", "lost", "one", 10, False)
        up = port("neighbor", "lost", "two", 11, True)

        def evaluate(t, wanted, *, direct_service=None):
            probes, links = {}, {}
            for p in (down, up):
                reporter, lid = p["node_id"], p["link"]["link_id"]
                arrived = p["observed_at_ns"] + 1
                probes[lid, reporter, 1] = {"time_ns": arrived, "probe": p, "result": {"accepted": True}}
                links[lid] = {"reporters": {reporter: {"probe": p, "received_at_ns": arrived}}}
            rows = {}
            delivered = {}
            if direct_service is not None:
                rows["s"] = {"node_id": "lost", "observed_at_ns": direct_service, "fresh": True}
                delivered["s", 1] = {"message": rows["s"], "time_ns": direct_service + 1, "result": {"accepted": True}}
            loop.service_reports = SimpleNamespace(snapshot=lambda _: {"services": rows})
            live = loop._port_witness("lost", 4, {"links": links}, t, online=wanted)
            replay = DeliveredNodeEvidence({"lost"}, probes, delivered).witness("lost", 4, t, 8, online=wanted)
            self.assertEqual(live, replay)
            return live

        self.assertIsNone(evaluate(12, False))
        self.assertEqual(evaluate(12, True)["reporter_node_id"], "neighbor")
        down["observed_at_ns"] = 11
        self.assertIsNone(evaluate(12, True))
        self.assertIsNone(evaluate(12, False))
        down["observed_at_ns"] = 12
        self.assertIsNone(evaluate(13, True))
        self.assertEqual(evaluate(13, False)["reporter_node_id"], "adjacent")
        self.assertIsNone(evaluate(13, False, direct_service=12))
        self.assertIsNone(evaluate(25, False))

    def test_v2_abandoned_operations_must_reenter_as_a_new_order(self):
        self.cond["deviceRecovery"]["contractVersion"] = "b03.deferred-cleanup.v2"
        self.rows[0]["install_ids"] = ["new"]
        self.rows[-1].update(event="release_parked", plan_id="plan", operation="cleanup",
                             service_id="old", source_stop_confirmed=True, reserved_links=["one"],
                             abandoned_steps=[["install", "new"]])
        self.assertEqual(self.validate(), {})
        self.rows[-1]["abandoned_steps"] = []
        with self.assertRaisesRegex(ValueError, "abandoned work"):
            self.validate()
        self.rows[-1]["abandoned_steps"] = [["install", "new"]]
        self.rows.append({"event": "work_step", "time_ns": 20, "work_id": "first", "plan_id":"new-plan",
                          "service_id": "new", "phase": "install"})
        with self.assertRaisesRegex(ValueError, "abandoned step"):
            self.validate()

    def test_stale_aggregate_can_still_change_offline_node_signature(self):
        from destination_guard import offline_node_signature
        port = {"peer_node_id":"lost","admin_up":True,"endpoint_online":False}
        report = {"received_at_ns":10,"probe":{"observed_at_ns":10,"node_online":True,"link":port}}
        links = {"links":{"one":{"usable":False,"reason":"stale_endpoint_observation","reporters":{"neighbor":report,"lost":None}}}}
        demands = {"services":{}}
        self.assertEqual(offline_node_signature(links,demands,10,8), ("lost",))
        # A fresh repeat and a new sequence/time do not change the decision key.
        report["received_at_ns"]=report["probe"]["observed_at_ns"]=13
        self.assertEqual(offline_node_signature(links,demands,14,8), ("lost",))
        demands["services"]["svc"]={"fresh":True,"node_id":"lost","observed_at_ns":13}
        self.assertEqual(offline_node_signature(links,demands,14,8), ())
        demands["services"].clear()
        port["endpoint_online"]=True
        self.assertEqual(offline_node_signature(links,demands,14,8), ())


if __name__=="__main__":
    unittest.main()
