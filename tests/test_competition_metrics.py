"""脱离官方数据集和模型权重的轻量化赛题五指标时钟单测。"""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from competition_metrics import accept_report, distribution, records, sensed_events


def probe(node, peer, sampled, arrived, *, delay=7, link="edge"):
    return {"event": "observation", "time_ns": arrived, "result": {"accepted": True},
            "probe": {"node_id": node, "observed_at_ns": sampled, "node_online": True,
                      "link": {"link_id": link, "peer_node_id": peer, "delay_ns": delay}}}


class CompetitionMetricsTests(unittest.TestCase):
    def test_change_requires_two_matching_fresh_endpoint_reports(self):
        rec = {"index": 0, "entity": "edge", "type": "link_param_change", "eventNs": 100,
               "cutoffNs": 230, "attrs": {"delayMs": .000007}, "samples": {}, "evidenceArrivedNs": None}
        windows = {"edge": [rec]}
        for row in [probe("a", "b", 99, 102), probe("a", "b", 104, 108, link="other"),
                    probe("a", "b", 106, 108), probe("b", "a", 109, 111, delay=3),
                    probe("b", "a", 136, 140)]:
            accept_report(windows, row, 20)
        self.assertIsNone(rec["evidenceArrivedNs"])
        accept_report(windows, probe("a", "b", 141, 144), 20)
        result = sensed_events(windows, 143)[0]
        self.assertEqual(result["delayNs"], 44)
        self.assertTrue(result["duringDrain"])
        self.assertEqual(result["evidence"], {"a": [141, 144], "b": [136, 140]})

    def test_superseded_changes_and_unobservable_motion_remain_in_denominator(self):
        changes = {"index": 0, "entity": "edge", "type": "link_state", "eventNs": 10,
                   "cutoffNs": 20, "attrs": {"state":"up"}, "samples": {}, "evidenceArrivedNs": None}
        motion = dict(changes, index=1, type="node_move", entity="a")
        windows = {"edge": [changes], "a": [motion]}
        accept_report(windows, probe("a", "b", 22, 25), 30)
        result = sensed_events(windows, 50)
        self.assertEqual([x["delayNs"] for x in result], [None, None])
        self.assertEqual(result[1]["status"], "coordinates_absent_from_control_reports")
        self.assertEqual(distribution([None]), {"n":0, "meanNs":None, "p95Ns":None, "maxNs":None})

    def test_native_engine_omits_device_file_until_first_execution(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "executions.jsonl"
            self.assertEqual(list(records(path, optional=True)), [])
            with self.assertRaises(FileNotFoundError):
                list(records(path))

    def test_peer_evidence_is_fresh_attributed_and_requires_a_live_port(self):
        rec = {"index": 0, "type": "node_offline", "entity": "b", "eventNs": 10,
               "cutoffNs": 100, "samples": {}, "evidenceArrivedNs": None, "attrs": {}}
        windows = {"b": [rec]}
        row = probe("a", "b", 12, 14)
        row["probe"]["link"].update(admin_up=False, endpoint_online=False)
        accept_report(windows, row, 10, peer_evidence=True)
        self.assertIsNone(rec["evidenceArrivedNs"])
        row["probe"]["link"]["admin_up"] = True
        accept_report(windows, row, 10)  # 早期版本仅对直接送达证据计分。
        accept_report(windows, {**row, "time_ns": 24}, 10, peer_evidence=True)
        self.assertIsNone(rec["evidenceArrivedNs"])
        accept_report(windows, row, 10, peer_evidence=True)
        result = sensed_events(windows, 30)[0]
        self.assertEqual(result["delayNs"], 4)
        self.assertEqual(result["status"], "observed_by_neighbor")
        self.assertEqual(result["neighborLink"], "edge")
        self.assertEqual(result["evidence"], {"a": [12, 14]})

    def test_business_and_connectivity_are_recomputed_from_scored_intervals(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "packet"))
        from verify_packet import verify_business_denominators
        row = {"start_ns": 0, "end_ns": 2_000_000_000, "active": {"s": 3}, "U_service_ns": 1_000_000_000,
               "onlinePairDenominator": 2, "connectedPairFraction": .5,
               "activeEndpointDenominator": 1, "activeEndpointReachableFraction": 1}
        ref = {"requestSeconds": 2, "priorityRequestSeconds": 6, "UOverRequests": .5,
               "onlinePairConnectedTimeMean": .5, "activeEndpointReachableTimeMean": 1}
        verify_business_denominators([row], ref)
        for field in ref:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_business_denominators([row], {**ref, field: 0})
        empty = dict(row, active={}, U_service_ns=0, activeEndpointDenominator=0, activeEndpointReachableFraction=None)
        verify_business_denominators([empty], dict(ref, requestSeconds=0, priorityRequestSeconds=0,
                                                    UOverRequests=None, activeEndpointReachableTimeMean=None))

    def test_p95_uses_nearest_rank(self):
        self.assertEqual(distribution(range(1, 21))["p95Ns"], 19)
