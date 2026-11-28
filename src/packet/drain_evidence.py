"""Report outstanding device work at the configured inclusive drain boundary."""
from __future__ import annotations

import json

from packet_device_readback import phase_matches
from packet_refresh_evidence import CONTRACT as REFRESH_CONTRACT, refresh_obligation


CONTRACT = "b03.bounded-drain.v1"


def file_rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def drain_receipt(conditions, clock, controller, actions):
    cfg = conditions["simulation"]
    end = clock["end_ns"]
    stop = clock["drain_end_ns"]
    if end != round(cfg["end_s"] * 1e9) or stop != end + round(cfg["drain_s"] * 1e9) or end > stop:
        raise ValueError("Drain deadline differs from the configured business window")
    demand = {}
    for row in controller:
        if row["event"] == "service_observation" and row["result"]["accepted"] and row["time_ns"] <= stop:
            sample = row["message"]
            demand[sample["service"]["service_id"]] = sample

    installed = {}
    open_orders = {}
    release_intent = set()
    sent_release = set()
    parked = {}
    reads = {}
    awaiting_signoff = {}
    release_count = 0
    independent = conditions.get("configurationRefreshContract") == REFRESH_CONTRACT
    refreshes = {}
    for row in actions:
        if independent:
            refresh_obligation(refreshes, row)
        event = row["event"]
        if row.get("time_ns", 0) > stop:
            raise ValueError("A work-order receipt exceeds the configured drain deadline")
        wid = row.get("work_id")
        if event == "work_started":
            open_orders[wid] = set(row["release_ids"])
            release_intent.update((sid, installed[sid]) for sid in row["release_ids"] if sid in installed)
        elif event in ("work_verified", "work_incomplete"):
            open_orders.pop(wid, None)
            confirmation = row["om"]
            matched = confirmation.get("matched", ()) if isinstance(confirmation, dict) else ()
            for sid in matched:
                awaiting_signoff.pop((wid, sid), None)
        elif event == "release_parked":
            parked[wid] = row["service_id"]
            open_orders[wid] = {row["service_id"]}
        elif event == "release_recovery_probe":
            parked.pop(wid, None)
        elif event == "work_step_skipped" and row["phase"] == "release":
            sid = row["service_id"]
            open_orders[wid].discard(sid)
            if row["reason"] in ("lifecycle_demand_reactivated", "release_no_longer_authorized:configuration_qos_passed"):
                release_intent.discard((sid, installed.get(sid)))
        elif event == "b03_om_diagnosis":
            for sid, state in row["services"].items():
                if state["status"] == "configuration_qos_passed" and state["configuration_evidence"]["verified"]:
                    release_intent.discard((sid, installed.get(sid)))
        elif event == "device_command" and row["op"] in ("deactivate", "cleanup", "abort"):
            sent_release.add((row["service_id"], row["epoch"]))
        elif event == "device_readback" and row["phase"] in ("release", "abort"):
            reads[row["query_id"]] = row
        elif event == "service_confirmed":
            installed[row["service_id"]] = row["epoch"]
        elif event in ("service_released", "service_aborted"):
            sid, epoch, decision = row["service_id"], row["epoch"], row["decision"]
            phase = "release" if event == "service_released" else "abort"
            if set(row["query_ids"]) != set(decision["node_path"]) or not decision["node_path"]:
                raise ValueError("Drain closure lacks a device path readback")
            for node, query in row["query_ids"].items():
                proof = reads.get(query)
                if (not proof or (proof["work_id"], proof["service_id"], proof["epoch"], proof["node_id"], proof["phase"])
                        != (wid, sid, epoch, node, phase) or not proof["matched"]
                        or not proof["observed_at_ns"] <= proof["arrived_at_ns"] <= row["time_ns"]
                        or not phase_matches(phase, decision, epoch, node, proof["result"])):
                    raise ValueError("Drain closure lacks matching device evidence")
            release_intent.discard((sid, epoch))
            sent_release.discard((sid, epoch))
            if installed.get(sid) == epoch:
                del installed[sid]
            if event == "service_released":
                awaiting_signoff[wid, sid] = epoch
                release_count += 1
                open_orders[wid].discard(sid)

    ttl = cfg["control"]["observation_ttl_ns"]
    protected = set(conditions["authority"].get("protectedServiceIds", ()))
    allowed = set(conditions["authority"]["allowedServiceIds"]) - protected
    due = {sid for sid, _ in release_intent | sent_release}.union(*open_orders.values())
    refresh_ids = {sid for sid, _ in refreshes}
    active, unknown, outside = [], [], []
    for sid in sorted(installed):
        sample = demand.get(sid)
        if sid not in allowed:
            outside.append(sid)
        elif sid in due:
            continue
        elif sample is None or not 0 <= stop - sample["observed_at_ns"] <= ttl:
            unknown.append(sid)
        elif sample["registered"] and sample["active"] and sample["observed_at_ns"] < end:
            unknown.append(sid)
        elif sample["registered"] and sample["active"]:
            if sid not in refresh_ids:
                active.append(sid)
        else:
            due.add(sid)
    pending = bool(due or unknown or open_orders or parked or awaiting_signoff or refreshes)
    observers = [{"serviceId": v["serviceId"], "epoch": v["epoch"], "workId": v["workId"], "planId": v["planId"],
                  "state": v["state"], "missingNodes": sorted(set(v["nodes"]) - v["received"])}
                 for _, v in sorted(refreshes.items())]
    return {"contractVersion": CONTRACT, "status": "incomplete" if pending else "complete",
            "scoreEndNs": end, "deadlineNs": stop, "confirmedDeviceReleases": release_count,
            "pendingReleaseIds": sorted(due), "pendingWorkIds": sorted(open_orders),
            "unconfirmedDeviceEpochs": [{"serviceId": sid, "epoch": epoch} for sid, epoch in sorted(sent_release)],
            "parkedReleaseIds": sorted(parked.values()), "awaitingOmReleaseIds": sorted({sid for _, sid in awaiting_signoff}),
            **({"unresolvedConfigurationRefreshes": observers} if independent else {}),
            "continuingActiveIds": active,
            "unresolvedDemandIds": unknown, "outsideWriteAuthorityIds": outside}


def _verify_demand_deliveries(controller, packets, destination):
    rows = {row["packet_uid"]: row for row in controller if row["event"] == "service_observation"}
    for packet in packets:
        if (packet["event"] != "delivered" or packet["message"].get("kind") != "service_status"
                or packet["destination_node_id"] != destination):
            continue
        row = rows.pop(packet["packet_uid"], None)
        if (not row or row["time_ns"] != packet["time_ns"] or row["message"] != packet["message"]
                or (row["result"]["accepted"] and row["message"]["node_id"] != packet["source_node_id"])):
            raise ValueError("Drain demand evidence differs from the delivered service packet")
    if rows:
        raise ValueError("Drain demand report lacks a delivered service packet")


def verify_drain(conditions, summary, controller, actions, packets):
    contract = conditions.get("drainContract")
    marker = "src/packet/drain_evidence.py"
    producer = conditions.get("mode") == "workorder" and (marker in conditions.get("implementationSha256", {})
              or marker in conditions.get("provenance", {}).get("checkoutFiles", {}))
    if contract is None and "drainStatus" not in summary and not producer:
        return None
    if contract != CONTRACT or conditions["mode"] != "workorder":
        raise ValueError("Unknown work-order drain contract")
    recovery = conditions.get("deviceRecovery", {})
    flag = recovery.get("deferUnreachableCleanup")
    if type(flag) is not bool or recovery.get("contractVersion") != ("b03.deferred-cleanup.v2" if flag else "b03.deferred-cleanup.v1"):
        raise ValueError("Drain contract has an inconsistent device recovery edition")
    _verify_demand_deliveries(controller, packets, conditions["simulation"]["control"]["controller_id"])
    expected = drain_receipt(conditions, summary["clock"], controller, actions)
    if expected != summary.get("drainStatus"):
        raise ValueError("Drain status differs from the bounded device receipts")
    return expected
