"""Boundary, independent readback and recovery-selection checks for bounded work-order drain."""
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/packet")]
from drain_evidence import CONTRACT, drain_receipt, verify_drain

SECOND = 1_000_000_000


class BoundedDrain(unittest.TestCase):
    def setUp(self):
        self.conditions = {"drainContract": CONTRACT, "mode": "workorder",
                           "deviceRecovery": {"contractVersion": "b03.deferred-cleanup.v2",
                                              "deferUnreachableCleanup": True},
                           "authority": {"allowedServiceIds": ["s", "protected"], "protectedServiceIds": ["protected"]},
                           "simulation": {"end_s": 10, "drain_s": 2, "control": {"controller_id": "a", "observation_ttl_ns": 4 * SECOND}}}
        self.clock = {"end_ns": 10 * SECOND, "drain_end_ns": 12 * SECOND}
        self.installed = {"event": "service_confirmed", "time_ns": SECOND, "service_id": "s", "epoch": 1}
        self.open = {"event": "work_started", "time_ns": 10 * SECOND, "work_id": "W", "release_ids": ["s"]}

    def demand(self, active, sampled=10 * SECOND, arrived=None, accepted=True):
        return {"event": "service_observation", "time_ns": sampled if arrived is None else arrived,
                "result": {"accepted": accepted}, "message": {"service": {"service_id": "s"},
                "registered": True, "active": active, "observed_at_ns": sampled}}

    def receipt(self, actions, demands=()):
        return drain_receipt(self.conditions, self.clock, demands, actions)

    def closed(self):
        stop = self.clock["drain_end_ns"]
        reads = []
        for node in ("a", "b"):
            reads.append({"event": "device_readback", "time_ns": stop, "observed_at_ns": stop, "arrived_at_ns": stop,
                          "phase": "release", "work_id": "W", "service_id": "s", "epoch": 1, "node_id": node,
                          "query_id": "Q" + node, "matched": True, "result": {"source": {"enabled": False, "rate_bps": 0}
                          if node == "a" else None, "forwarding": None, "retired": True, "prepared": None, "installed": None}})
        return [self.installed, self.open, *reads,
                {"event": "service_released", "work_id": "W", "service_id": "s", "epoch": 1, "time_ns": stop,
                 "decision": {"node_path": ["a", "b"]}, "query_ids": {"a": "Qa", "b": "Qb"}},
                {"event": "work_verified", "time_ns": stop, "work_id": "W", "om": {"matched": ["s"]}}]

    def test_inclusive_boundary_and_incomplete_unsigned_work(self):
        result = self.receipt(self.closed())
        self.assertEqual((result["status"], result["confirmedDeviceReleases"]), ("complete", 1))
        unsigned = self.receipt(self.closed()[:-1])
        self.assertEqual((unsigned["pendingWorkIds"], unsigned["awaitingOmReleaseIds"]), (["W"], ["s"]))
        mismatch = self.closed()
        mismatch[-1].update(event="work_incomplete", om={"matched": []})
        self.assertEqual(self.receipt(mismatch)["status"], "incomplete")
        short = self.receipt([self.installed, self.open], [self.demand(False)])
        self.assertEqual((short["status"], short["confirmedDeviceReleases"], short["pendingReleaseIds"]),
                         ("incomplete", 0, ["s"]))
        self.clock["drain_end_ns"] += 1
        with self.assertRaisesRegex(ValueError, "configured business window"):
            self.receipt([])

    def test_release_requires_actual_all_hop_pre_deadline_evidence(self):
        records = self.closed()
        for field, value in (("matched", False), ("epoch", 2), ("node_id", "a"), ("phase", "recovery"),
                             ("arrived_at_ns", self.clock["drain_end_ns"] + 1),
                             ("result", dict(records[3]["result"], forwarding={"next_node_id": "a"}))):
            broken = deepcopy(records)
            broken[3][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "device evidence"):
                self.receipt(broken)
        records[-2]["time_ns"] += 1
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.receipt(records)

    def test_park_and_wake_require_new_readbacks(self):
        park = {"event": "release_parked", "work_id": "W", "time_ns": 11 * SECOND, "service_id": "s"}
        wake = {"event": "release_recovery_probe", "work_id": "W", "time_ns": 11 * SECOND + 1}
        pending = [self.installed, self.open, park]
        self.assertEqual(self.receipt(pending)["parkedReleaseIds"], ["s"])
        pending.append(wake)
        state = self.receipt(pending)
        self.assertEqual((state["parkedReleaseIds"], state["pendingReleaseIds"], state["status"]), ([], ["s"], "incomplete"))
        self.assertEqual(self.receipt(pending + self.closed()[2:])["status"], "complete")

    def test_fresh_demands_and_write_authority(self):
        retained = [self.installed, dict(self.installed, service_id="protected")]
        state = self.receipt(retained, [self.demand(True)])
        self.assertEqual((state["status"], state["continuingActiveIds"], state["outsideWriteAuthorityIds"]),
                         ("complete", ["s"], ["protected"]))
        for sample in (self.demand(True, 9 * SECOND), self.demand(False, 2 * SECOND), self.demand(True, accepted=False),
                       self.demand(True, arrived=self.clock["drain_end_ns"] + 1)):
            self.assertEqual(self.receipt(retained, [sample])["unresolvedDemandIds"], ["s"])
        self.assertEqual(self.receipt(retained, [self.demand(False)])["pendingReleaseIds"], ["s"])

    def test_unread_written_epoch_survives_incomplete_work_and_active_report(self):
        sent = {"event": "device_command", "work_id": "W", "time_ns": 10 * SECOND + 1, "op": "deactivate",
                "service_id": "s", "epoch": 1}
        end = {"event": "work_incomplete", "work_id": "W", "time_ns": 11 * SECOND, "om": {"matched": []}}
        skip = {"event": "work_step_skipped", "work_id": "W", "phase": "release", "service_id": "s",
                "time_ns": 10 * SECOND + 2, "reason": "release_authority_changed"}
        healthy = {"event": "b03_om_diagnosis", "time_ns": 11 * SECOND + 1, "services": {
            "s": {"status": "configuration_qos_passed", "configuration_evidence": {"verified": True}}}}
        active = [self.demand(True, 11 * SECOND)]
        delayed = self.receipt([self.installed, self.open, sent, skip, end, healthy], active)
        self.assertEqual((delayed["status"], delayed["pendingReleaseIds"], delayed["continuingActiveIds"]),
                         ("incomplete", ["s"], []))
        self.assertEqual(delayed["unconfirmedDeviceEpochs"], [{"serviceId": "s", "epoch": 1}])
        # A failed pre-dispatch check needs positive authority to retain the original config.
        failed = [self.installed, self.open, skip, end]
        self.assertEqual(self.receipt(failed, active)["pendingReleaseIds"], ["s"])
        self.assertEqual(self.receipt([*failed, healthy], active)["status"], "complete")
        skip = dict(skip, reason="lifecycle_demand_reactivated")
        self.assertEqual(self.receipt([self.installed, self.open, skip, end], active)["continuingActiveIds"], ["s"])

    def test_historical_receipt_and_missing_or_falsified_new_contract(self):
        self.assertIsNone(verify_drain({"mode": "workorder"}, {}, [], [], []))
        summary = {"clock": self.clock, "drainStatus": self.receipt(self.closed())}
        summary["drainStatus"]["status"] = "incomplete"
        with self.assertRaisesRegex(ValueError, "differs"):
            verify_drain(self.conditions, summary, [], self.closed(), [])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            verify_drain({"mode": "workorder", "implementationSha256": {"src/packet/drain_evidence.py": "pin"}}, {}, [], [], [])

    def test_terminal_active_report_requires_its_delivered_packet(self):
        report = self.demand(True)
        report["packet_uid"] = 17
        report["message"].update(kind="service_status", node_id="a")
        packet = {"packet_uid": 17, "event": "delivered", "time_ns": report["time_ns"], "source_node_id": "a",
                  "destination_node_id": "a", "message": deepcopy(report["message"])}
        summary = {"clock": self.clock, "drainStatus": self.receipt([self.installed], [report])}
        self.assertEqual(verify_drain(self.conditions, summary, [report], [self.installed], [packet])["status"], "complete")
        packet["message"]["active"] = False
        with self.assertRaisesRegex(ValueError, "delivered service packet"):
            verify_drain(self.conditions, summary, [report], [self.installed], [packet])

    def test_cli_recovery_default_and_historical_override(self):
        import cli
        class Resolved(Exception):
            pass
        def stop(args, parser):
            raise Resolved(args.defer_unreachable_cleanup)
        for argv, expected in (([], True), (["--defer-unreachable-cleanup"], True),
                               (["--no-defer-unreachable-cleanup"], False), (["--mode", "package"], False)):
            with self.subTest(argv=argv), patch.object(cli, "_network_source", stop), self.assertRaises(Resolved) as got:
                cli.main(["run", "--output", "unused", *argv])
            self.assertIs(got.exception.args[0], expected)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main(["run", "--output", "unused", "--mode", "single", "--defer-unreachable-cleanup"])
