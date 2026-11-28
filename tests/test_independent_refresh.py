"""在可控回包顺序下测试原生查询/ACK 的单业务独立配置刷新逻辑。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "src/packet")]
from scene import simulator_root
sys.path.insert(0, str(simulator_root() / "src"))
from packet_device import WorkLoop
from packet_refresh import IndependentRefresh
from packet_refresh_evidence import CONTRACT, verify_refresh
from drain_evidence import drain_receipt


class IndependentQueries(unittest.TestCase):
    def setUp(self):
        self.loop = loop = object.__new__(WorkLoop)
        loop.config = {"observation_ttl_ns": 10, "transaction_timeout_ns": 45, "tick_ns": 1,
                       "ack_wait_ns": 40, "max_retries": 2, "command_validity_ns": 45, "query_interval_ns": 3}
        loop.work = loop.readout = loop.transaction = loop.pending = None
        loop.refresh_blocks, loop.confirmed, loop.versions = {}, {}, {}
        loop.command_count = loop.now_ns = loop.refresh_failures = loop.refresh_completed = 0
        loop.force_review = False
        loop.independent_refresh = IndependentRefresh(loop)
        self.events, self.sent, self.demands = [], [], {}
        loop.emit = self.events.append
        loop.transport = lambda cmd, retry=False: self.sent.append(deepcopy(cmd))
        loop.budgets = lambda: {"wire": 123}
        loop.link_reports = SimpleNamespace(snapshot=lambda now: {})
        loop.service_reports = SimpleNamespace(snapshot=lambda now: {"services": self.demands})
        loop._snapshots = lambda now: ({}, {"services": self.demands})
        loop._refresh_key = lambda sid, links, demand: loop.confirmed[sid]["epoch"]
        self.reachable = False
        loop._port_witness = lambda *args, **kwargs: {"observed_at_ns": args[3]} if self.reachable else None

    def start(self, *ids):
        targets = {}
        for sid in ids:
            choice = {"service_id": sid, "node_path": ["shared", sid], "link_path": ["wire"], "wire_bps": 7,
                      "reserved_bps": 6, "demand_revision": 1}
            self.loop.confirmed[sid] = {"epoch": 1, "decision": choice}
            self.demands[sid] = {"active": False, "registered": True, "fresh": True, "revision": 2}
            targets[sid] = {"epoch": 1, "plan_id": "W/" + sid, "decision": choice}
        self.events.extend([
            {"event": "b03_observation_evidence", "time_ns": 0, "work_id": "W", "purpose": "plan",
             "state": {"confirmed": deepcopy(self.loop.confirmed)}},
            {"event": "b03_rc_plan", "time_ns": 0, "work_id": "W", "decision_wall_ns": 0, "refresh_ids": list(ids)},
            {"event": "config_refresh_requested", "time_ns": 0, "work_id": "W", "service_ids": list(ids)}])
        self.loop.independent_refresh.start(0, "W", targets)

    def command(self, sid, node):
        return next(c for c in reversed(self.sent) if c["target_node_id"] == node and c["action"]["service_id"] == sid)

    def ack(self, sid, node, at, version=1, command=None, epoch=1):
        cmd = command or self.command(sid, node)
        source = {"enabled": False, "epoch": 1, "active_epoch": 1, "rate_bps": 0, "registered": True,
                  "demand_active": False, "demand_revision": 2} if node == "shared" else None
        hop = {"next_node_id": sid, "link_id": "wire"} if source else None
        rule = dict(hop, reserved_bps=6) if source else None
        result = dict(retired=False, source=source, forwarding=hop, installed=rule, prepared=rule)
        payload = {"service_id": sid, "epoch": epoch, "request_packet_uid": 1, "observed_at_ns": at, "result": result}
        ack = dict(kind="ack", status="applied", version=version, node_id=node, plan_id=cmd["plan_id"],
                   command_id=cmd["command_id"], executed_at_ns=at, b03_readback=payload,
                   source_enabled=False, active_epoch=1)
        self.loop.receive_ack(ack, at, node)

    def test_one_replies_ahead_of_four_and_a_slow_peer_keeps_its_original_query(self):
        self.start("fast", "slow", "off1", "off2", "off3")
        for sid in self.loop.confirmed:
            self.ack(sid, "shared", 1)
        command = self.command("slow", "slow")
        self.ack("fast", "fast", 2)
        self.assertEqual(set(self.loop.independent_refresh.jobs), {"slow", "off1", "off2", "off3"})
        self.assertTrue(self.loop.force_review)
        self.loop.work = {"work_id": "another-work-order"}
        self.ack("slow", "slow", 9, command=command)
        results = [r for r in self.events if r["event"] == "config_refresh_service_result"]
        self.assertEqual([(r["service_id"], r["time_ns"], r["work_id"]) for r in results], [("fast",2,"W"), ("slow",9,"W")])
        self.assertTrue(all(r["retained_wire_bps"] == {"wire":123} and r["replan_before_write"] for r in results))
        self.assertTrue(all(c["action"]["op"] == "query" for c in self.sent))
        self.assertFalse(any(e["event"] in ("service_released", "work_verified") for e in self.events))

    def test_shared_node_newer_version_requeries_and_a_late_or_wrong_epoch_cannot_match(self):
        self.start("s")
        old = self.command("s", "shared")
        self.ack("s", "shared", 1)
        self.loop.versions["shared"] = 2
        self.ack("s", "s", 3)
        new = self.command("s", "shared")
        self.assertNotEqual(old["command_id"], new["command_id"])
        self.ack("s", "shared", 4, version=2, command=old)
        self.ack("s", "shared", 5, version=2, command=new, epoch=2)
        self.assertIn("s", self.loop.independent_refresh.jobs)
        self.ack("s", "shared", 6, version=2, command=new)
        self.assertEqual(self.loop.confirmed["s"]["query_ids"]["shared"], new["command_id"])

    def test_ttl_boundary_and_expired_peer(self):
        self.start("a", "b")
        self.ack("a", "shared", 1)
        self.ack("b", "shared", 1)
        self.ack("a", "a", 11)
        self.ack("b", "b", 12)
        self.assertNotIn("a", self.loop.independent_refresh.jobs)
        self.assertIn("b", self.loop.independent_refresh.jobs)
        self.ack("b", "shared", 13)
        self.assertTrue(self.loop.confirmed["b"]["configuration_observed"])

    def test_deadline_releases_reader_and_bounded_timer_or_fresh_reachability_wakes_it(self):
        self.start("s", "t")
        for sid in ("s", "t"):
            self.ack(sid, "shared", 42)
        self.ack("t", "t", 45)
        self.loop.independent_refresh.poll(46)
        self.assertTrue(self.loop.confirmed["t"]["configuration_observed"])
        self.assertFalse(self.loop.confirmed["s"]["configuration_observed"])
        self.assertEqual(self.loop.refresh_blocks, {"s":1})
        self.loop.force_review = False
        self.ack("s", "s", 47)
        self.loop.independent_refresh.poll(48)
        self.assertFalse(self.loop.force_review)
        self.reachable = True
        self.loop.independent_refresh.poll(49)
        self.assertTrue(self.loop.force_review)
        self.assertEqual(self.events[-1]["reason"], "reachable")
        self.loop.independent_refresh.retry_at["s"] = (95, 50, [])
        self.loop.refresh_blocks["s"] = 1
        self.loop.independent_refresh.poll(95)
        self.assertEqual(self.events[-1]["reason"], "retry_deadline")

    def test_joint_install_waits_for_all_services_and_requeries_advanced_shared_version(self):
        self.start("a", "b")
        self.loop.independent_refresh.jobs.clear()
        self.loop.work = {"work_id": "joint"}
        targets = {sid: dict(record, plan_id="joint/" + sid) for sid, record in self.loop.confirmed.items()}
        self.loop._start_readout(0, "install", final_targets=targets)
        for sid in ("a", "b"):
            self.ack(sid, "shared", 1)
        self.ack("a", "a", 2)
        self.assertIn(("b", "b"), self.loop.readout["waiting"])
        self.loop.versions["shared"] = 2
        self.ack("b", "b", 3)
        self.assertEqual(set(self.loop.readout["waiting"]), {("a","shared"), ("b","shared")})
        self.assertFalse(any(e["event"] in ("work_verified", "service_rechecked") for e in self.events))

    def test_foreground_write_cancels_only_its_own_epoch(self):
        self.start("a", "b")
        self.loop.work = {"work_id": "write"}
        write = deepcopy(self.command("a", "shared"))
        write["action"]["op"] = "deactivate"
        write["plan_id"] = "write/P"
        self.loop._send(write)
        self.ack("a", "shared", 4)
        self.assertEqual(set(self.loop.independent_refresh.jobs), {"b"})
        self.assertNotIn("configuration_observed", self.loop.confirmed["a"])

    def test_archive_rejects_missing_or_cross_associated_reads_and_early_writes(self):
        self.start("fast", "slow")
        self.ack("fast", "shared", 1)
        self.ack("fast", "fast", 2)
        cfg = {"configurationRefreshContract": CONTRACT, "simulation": {"control": self.loop.config}, "mode": "workorder"}
        packets = [{"event": "service_observation", "time_ns": 1, "result": {"accepted": True}, "message":
                    dict(self.demands["fast"], service={"service_id":"fast"}, observed_at_ns=1)}]
        def verify(events):
            return verify_refresh(cfg, {"drain_end_ns": 5}, packets, events)
        self.assertEqual(verify(self.events)["pendingServices"], ["slow"])
        for field, value in (("work_id", "next"), ("epoch", 2), ("status", "unconfirmed"), ("time_ns", 15), ("query_ids", {"fast": "wrong"})):
            rows = deepcopy(self.events)
            rows[-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify(rows)
        rows = deepcopy(self.events)
        rows.append({"event": "device_command", "time_ns": 3, "work_id": "W", "op": "deactivate", "service_id": "fast"})
        with self.assertRaisesRegex(ValueError, "write"):
            verify(rows)

    def test_drain_preserves_unresolved_read_only_asks_separately_from_release_duties(self):
        self.start("s")
        snapshot = {"event": "service_confirmed", "time_ns": 0, "service_id": "s", "epoch": 1}
        acts = [snapshot, *self.events]
        c = {"configurationRefreshContract": CONTRACT, "simulation": {"end_s":0, "drain_s":5e-9, "control": self.loop.config},
             "authority": {"allowedServiceIds":["s"]}}
        clk = {"end_ns":0,"drain_end_ns":5}
        sample = {"event":"service_observation", "time_ns": 4, "result":{"accepted":True},
                  "message":{"service":{"service_id":"s"}, "active":True, "registered":True, "observed_at_ns":4}}
        seen = drain_receipt(c,clk,[sample],acts)
        self.assertEqual((seen["status"], seen["pendingReleaseIds"], seen["continuingActiveIds"]), ("incomplete",[],[]))
        self.assertEqual(seen["unresolvedConfigurationRefreshes"][0]["missingNodes"], ["s","shared"])
        sample["message"]["active"] = False
        self.assertEqual(drain_receipt(c,clk,[sample],acts)["pendingReleaseIds"], ["s"])
        legacy = dict(c); del legacy["configurationRefreshContract"]
        self.assertNotIn("unresolvedConfigurationRefreshes",drain_receipt(legacy,clk,[sample],acts))
