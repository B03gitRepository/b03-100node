"""Independent receipt replay for the native packet engine's delivered queue reports."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/packet"))
from queue_evidence import PEER_V2, verify_queue_evidence


PREVIOUS = "910891e496a8c16e6b9ba134abab1e0cbd6b7dd95c31fbb5da7d3ad190b27e30"
CURRENT = "ca1010c9be21e2927a4edd95be35f33cf5e737ca71817fa11ea99331e4b00c98"


class QueueReplay(unittest.TestCase):
    def setUp(self):
        self.conditions = {"releaseManifestSha256": CURRENT,
                           "simulation": {"queue_packets": 8, "control": {"observation_ttl_ns": 10, "controller_id": "a"}}}
        self.summary = {"control": {"probe_semantics": PEER_V2}}
        self.reports, self.deliveries, self.actions = [], [], []

    def probe(self, node, seq, sampled, arrived, depth, *, accepted=True, up=True, peer_online=True):
        probe = {"kind": "probe", "seq": seq, "node_id": node, "node_online": True, "observed_at_ns": sampled,
                 "link": {"link_id": "edge", "peer_node_id": "a" if node == "b" else "b", "admin_up": up,
                          "endpoint_online": peer_online, "queue_packets": depth}}
        uid = len(self.reports) + 1
        self.reports.append({"event": "observation", "packet_uid": uid, "time_ns": arrived, "probe": probe,
                             "result": {"accepted": accepted}})
        self.deliveries.append({"event": "delivered", "packet_uid": uid, "time_ns": arrived,
                                "source_node_id": node, "destination_node_id": "a", "message": deepcopy(probe)})
        return {"seq": seq, "sampled_at_ns": sampled, "received_at_ns": arrived, "status": "fresh",
                "queue_packets": depth, "node_online": True, "admin_up": up, "endpoint_online": peer_online}

    def sample(self, now, a, b, **queue):
        link = dict(src_node_id="a", dst_node_id="b", reports={"a": a, "b": b}, **queue)
        self.actions.append({"event": "b03_observation_evidence", "time_ns": now,
                             "state": {"now_ns": now, "link_ttl_ns": 10, "links": {"edge": link}}})
        return link

    def replay(self):
        return verify_queue_evidence(self.conditions, self.summary, self.reports, self.actions,
                                     {"edge": ("a", "b")}, iter(self.deliveries))

    def test_replays_hysteresis_from_delivered_samples_and_ttl(self):
        a = self.probe("a", 1, 1, 1, 6)
        b = self.probe("b", 1, 1, 2, 1)
        self.sample(2, a, b, queue_packets=6, queue_pressure=False)
        self.probe("a", 1, 1, 2, 0, accepted=False)
        a = self.probe("a", 2, 3, 4, 7)
        self.sample(5, a, b, queue_packets=7, queue_pressure=True)
        a = self.probe("a", 3, 5, 6, 5)
        self.sample(6, a, b, queue_packets=5, queue_pressure=True)
        a = self.probe("a", 4, 7, 8, 4)
        self.sample(8, a, b, queue_packets=4, queue_pressure=False)
        a = self.probe("a", 5, 9, 10, 8)
        self.sample(12, a, dict(b, status="stale"), queue_packets=8)
        b = self.probe("b", 2, 20, 20, 1)
        a = self.probe("a", 6, 20, 21, 8)
        self.sample(21, a, b, queue_packets=8, queue_pressure=False)
        self.assertTrue(self.replay())

    def test_rejects_forged_pressure_aggregate_endpoint_state_and_ttl(self):
        a = self.probe("a", 1, 2, 2, 8, up=False)
        b = self.probe("b", 1, 2, 3, 3)
        link = self.sample(4, a, b, queue_packets=8, queue_pressure=False)
        self.assertTrue(self.replay())
        for mapping, field, wrong in ((link, "queue_pressure", True), (link, "queue_packets", 7),
                                      (a, "queue_packets", 5), (a, "status", "stale"),
                                      (a, "endpoint_online", False), (a, "admin_up", True),
                                      (self.actions[0]["state"], "link_ttl_ns", 11)):
            with self.subTest(field=field):
                old = mapping[field]
                mapping[field] = wrong
                with self.assertRaises(ValueError):
                    self.replay()
                mapping[field] = old

    def test_rejects_superseded_omitted_and_undelivered_reports(self):
        old = self.probe("a", 1, 1, 2, 1)
        a = self.probe("a", 2, 3, 4, 1)
        link = self.sample(5, a, None, queue_packets=1)
        self.assertTrue(self.replay())
        for bad in (None, old):
            link["reports"]["a"] = bad
            with self.assertRaisesRegex(ValueError, "omitted|superseded"):
                self.replay()
        link["reports"]["a"] = a
        self.deliveries[-1]["message"]["link"]["queue_packets"] = 7
        with self.assertRaisesRegex(ValueError, "delivered network packet"):
            self.replay()
        self.deliveries.pop()
        with self.assertRaisesRegex(ValueError, "delivered network packet"):
            self.replay()

    def test_same_clock_receipt_can_follow_snapshot(self):
        a = self.probe("a", 1, 1, 2, 1)
        newer = self.probe("a", 2, 2, 3, 6)
        self.sample(2, None, None)
        self.sample(3, a, None, queue_packets=1)
        self.sample(3, newer, None, queue_packets=6)
        self.assertTrue(self.replay())

    def test_known_engine_cannot_claim_another_probe_edition(self):
        self.summary["control"] = {}
        with self.assertRaisesRegex(ValueError, "pinned simulator"):
            self.replay()
        self.conditions["releaseManifestSha256"] = PREVIOUS
        self.assertFalse(self.replay())
        self.summary["control"]["probe_semantics"] = PEER_V2
        with self.assertRaisesRegex(ValueError, "pinned simulator"):
            self.replay()
        self.conditions["releaseManifestSha256"] = "partner-runtime"
        self.assertTrue(self.replay())
