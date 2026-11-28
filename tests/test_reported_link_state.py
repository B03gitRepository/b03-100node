"""验证只有经网络送达的报告才能影响队列拥堵判定与上报链路健康度。"""
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/p) for p in (".", "src", "src/packet", "simulator/resnet_py/src")]
from packet_roles import _project_links
from resnet.control.controller import NetworkController
from resnet.control.observation import ObservationCache
from resnet.simulation.traffic import TrafficSource


class ReportedLinkStateTests(unittest.TestCase):
    def setUp(self):
        self.up = True
        self.depth = 0
        self.online = {"a": True, "b": True}
        self.now = 0
        self.cache = ObservationCache({"edge": ("a","b")}, 20, queue_limit_packets=8)
        def snapshot():
            return {"parameters": dict(capacity_bps=800000, delay_ms=1, jitter_ms=0,
                                       packet_loss_rate=0, ber=0),
                    "effective_up": self.up and all(self.online.values()), "admin_up": self.up,
                    "endpoint_online": dict(self.online), "queue_packets": self.depth}
        def send(nid, target, probe):
            self.assertTrue(self.cache.receive(probe, self.now, nid)["accepted"])
        self.controller = SimpleNamespace(sim=SimpleNamespace(
            state=SimpleNamespace(node_online=self.online), links={"edge": SimpleNamespace(snapshot=snapshot)}),
            endpoints={"edge": ("a", "b")}, controller_id="a", probe_sequences=Counter(),
            node_executors={n: SimpleNamespace(version=0) for n in self.online},
            now=lambda: self.now, _send=send)

    def sample(self, time):
        self.now = time
        for nid in self.online:
            NetworkController.send_probes(self.controller, nid)
        return _project_links(time, self.cache.snapshot(time))["edge"]

    def test_closed_link_with_two_online_nodes(self):
        self.up = False
        row = self.sample(0)
        self.assertTrue(row["reports"]["a"]["endpoint_online"])
        self.assertEqual(row["reason"], "link_down")
        self.up = True
        self.assertTrue(self.sample(1)["usable"])
        self.online["b"] = False
        row = self.sample(5)
        self.assertEqual(row["reason"], "endpoint_offline")
        self.assertFalse(row["reports"]["a"]["endpoint_online"])

    def test_queue_samples_reach_roles_and_threshold_has_hysteresis(self):
        self.depth = 6
        self.assertFalse(self.sample(0)["queue_pressure"])
        self.depth = 8
        row = self.sample(5)
        self.assertTrue(row["queue_pressure"])
        self.assertEqual((row["queue_packets"],row["reports"]["a"]["queue_packets"]), (8,8))
        self.depth = 5
        self.assertTrue(self.sample(6)["queue_pressure"])
        self.depth = 4
        self.assertFalse(self.sample(7)["queue_pressure"])
        self.depth = 8
        self.assertFalse(self.sample(9)["queue_pressure"])
        self.assertFalse(self.sample(40)["queue_pressure"])

    def test_unchanged_rate_logged_once_at_same_time_but_clock_is_rescheduled(self):
        entries, timers = [], []
        sim = SimpleNamespace(now=0, config={"payload_bytes":1000},
                              state=SimpleNamespace(node_online={"a":True}),
                              log=lambda _, row: entries.append(row))
        src = TrafficSource(sim, SimpleNamespace(src_node_id="a",service_id="s"),0)
        src._reschedule = timers.append
        src.set_rate(0)
        src.set_rate(0)
        src.epoch = 1
        src.set_rate(0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(timers, [0,0,0])

    def test_single_fresh_report_preserves_depth_only(self):
        self.depth = 8
        self.sample(0)
        self.assertTrue(self.sample(5)["queue_pressure"])
        self.online["b"] = False
        self.depth = 1
        row = self.sample(30)
        self.assertEqual(row["queue_packets"], 1)
        self.assertFalse(row.get("queue_pressure", False))
        self.assertEqual(row["reports"]["b"]["status"], "stale")
