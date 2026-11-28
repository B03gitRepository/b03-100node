"""Reconcile held packet-device cleanups against chronological work and device receipts."""
from collections import Counter

from destination_guard import DeliveredNodeEvidence


def _require(value, message):
    if not value:
        raise ValueError("Deferred cleanup: " + message)


def _wire(ledger, decision, factor):
    for link in decision["link_path"]:
        ledger[link] += factor * decision["wire_bps"]
        _require(ledger[link] >= 0, "negative link reservation")


def cleanup_budgets(acts, groups, conditions, probes, demands=None):
    """Return the checked complete ledger immediately before each recovered retirement."""
    policy = conditions.get("deviceRecovery", {})
    modern = policy.get("contractVersion") == "b03.deferred-cleanup.v2"
    parked = "release_parked" if modern else "cleanup_deferred"
    wake = "release_recovery_probe" if modern else "cleanup_recovery_probe"
    _require(not any(row["event"] in {"release_parked", "cleanup_deferred"} - {parked} for row in acts),
             "unexpected parking contract")
    owners = {row["work_id"] for row in acts if row["event"] == parked}
    _require(not any(row["event"] in {"release_recovery_probe", "cleanup_recovery_probe"}
                     and (row["event"] != wake or row.get("work_id") not in owners) for row in acts),
             "recovery without a matching parked order")
    result = {}
    if not owners:
        return result
    _require(policy.get("contractVersion") in {"b03.deferred-cleanup.v1", "b03.deferred-cleanup.v2"}
             and policy.get("deferUnreachableCleanup") is True, "cleanup isolation was not selected")
    controls = conditions["simulation"]["control"]
    nodes = {row["node_id"] for row in acts if row["event"] == parked}
    history = DeliveredNodeEvidence(nodes, probes, demands or {}) if modern else None
    for wid in owners:
        events = groups[wid]
        start, = events["work_started"]
        first = events[parked][0]
        step, = [s for s in events["work_step"] if s["plan_id"] == first.get("plan_id", s["plan_id"])]
        old = step["decision"]
        sid = step["service_id"]
        _require(step["phase"] == "release" and sid in start["release_ids"]
                 and (modern or (start["release_ids"] == [sid] and not start["install_ids"])), "invalid release scope")
        remaining = []
        if modern:
            queue = [("release", s) for s in start["release_ids"]] + [("install", s) for s in start["install_ids"]]
            remaining = queue[queue.index(("release", sid)) + 1:]
        balance = Counter(step["retained_wire_bps"])
        _require(old["wire_bps"] > 0 and all(balance[link] >= old["wire_bps"] for link in old["link_path"]),
                 "old epoch lacks its full wire reservation")
        # No other operation can execute until the original transaction is parked.
        scan = False
        woken = False
        held = None
        pending_query = set()
        ready = False
        source = old["node_path"][0]
        stop_acks = {e["time_ns"] for e in events["device_ack"] if e["plan_id"] == step["plan_id"]
                     and e["op"] == "deactivate" and e["node_id"] == source and e["service_id"] == sid
                     and e["epoch"] == first["epoch"] and e["original_status"] == "applied"}
        stop_reads = {e["time_ns"] for e in events["device_readback"] if e["phase"] == "recovery"
                      and e.get("op") == "deactivate" and e["node_id"] == e["network_source"] == source
                      and e["service_id"] == sid and e["epoch"] == first["epoch"] and e["matched"]}
        stopped = any(e.get("phase") == "source_stop_ack" and e["plan_id"] == step["plan_id"]
                      and step["time_ns"] <= e["time_ns"] <= first["time_ns"]
                      and e["time_ns"] in (stop_reads if e["reconciled_by_query"] else stop_acks)
                      for e in events["native_transaction"])
        _require((stopped or (modern and first["operation"]=="deactivate")) and
                 (modern or first["time_ns"] >= step["time_ns"] + controls["transaction_timeout_ns"]),
                 "missing source-stop acknowledgment or transaction timeout")
        for row in acts:
            if row is first:
                scan = True
            if not scan:
                continue
            event, now = row["event"], row["time_ns"]
            if row.get("work_id") != wid:
                if event == "work_step":
                    decision = row["decision"]
                    _require(row["service_id"] != sid and (modern or
                             (held["node_id"] not in decision["node_path"] and set(old["link_path"]).isdisjoint(decision["link_path"]))),
                             "conflicting foreign work")
                if event == "device_command" and row["op"] != "query":
                    _require(row["service_id"] != sid and (modern or row["node_id"] != held["node_id"]), "conflicting foreign write")
                if event == "native_transaction" and row["phase"] == "begin":
                    _wire(balance, row["decision"], 1)
                if event in ("service_released", "service_aborted"):
                    _wire(balance, row["decision"], -1)
            elif event == parked:
                previous = held
                if modern:
                    _require(row.get("service_id") == sid
                             and [tuple(p) for p in row.get("abandoned_steps", ())] == remaining
                             and (previous is None or woken), "incorrect parked or abandoned work")
                    remaining = []
                held = row
                woken = False
                operation = row["operation"] if modern else "cleanup"
                original = next((c for c in events["device_command"] if c["command_id"] == row["expired_command_id"]
                                 and c["plan_id"] == step["plan_id"] and c["op"] == operation and not c["retry"]), None)
                if modern:
                    stopped_now = any(e.get("phase") == "source_stop_ack" and e["plan_id"] == step["plan_id"]
                                      and e["time_ns"] in (stop_reads if e["reconciled_by_query"] else stop_acks)
                                      and step["time_ns"] <= e["time_ns"] <= now for e in events["native_transaction"])
                    scope_ok = (row["plan_id"] == step["plan_id"] and row["reserved_links"] == old["link_path"]
                                and row["source_stop_confirmed"] is stopped_now and
                                ((operation == "deactivate" and not stopped_now and row["node_id"] == source) or
                                 (operation == "cleanup" and stopped_now)))
                else:
                    scope_ok = (row["protected_services"] == [sid] and row["protected_nodes"] == [row["node_id"]]
                                and row["protected_links"] == old["link_path"])
                _require(original and original["service_id"] == sid and original["epoch"] == row["epoch"]
                         and original["node_id"] == row["node_id"] and scope_ok
                         and original["issued_at_ns"] + controls["command_validity_ns"] < now, "invalid expired write scope")
                if modern and now < step["time_ns"] + controls["transaction_timeout_ns"]:
                    w = row.get("offline_probe") or {}
                    expected = history.witness(row["node_id"], original["issued_at_ns"], now,
                                               controls["observation_ttl_ns"], online=False)
                    _require(expected and w == expected, "invalid or contradicted early park evidence")
                elif modern:
                    _require(row.get("offline_probe") is None, "unexpected early park evidence after timeout")
                sent_queries = {q["command_id"] for q in events["device_command"]
                                if q["op"] == "query" and (q["plan_id"], q["node_id"], q["service_id"], q["epoch"]) ==
                                (step["plan_id"], row["node_id"], sid, row["epoch"])
                                and original["issued_at_ns"] <= q["issued_at_ns"] < now
                                and (not modern or previous is None or q["issued_at_ns"] >= previous["time_ns"])}
                timed_out = {r["command_id"] for r in events["native_command_timeout"]
                             if r["operation"] == "query" and r["command_id"] in sent_queries
                             and original["issued_at_ns"] < r["time_ns"] <= now and r["plan_id"] == step["plan_id"]}
                # Earlier receipts omitted the query ID on the hold event. Resolve those from native timeouts.
                _require(timed_out and ("timed_out_query_id" not in row or row["timed_out_query_id"] in timed_out),
                         "missing timed-out read-only reconciliation for the expired cleanup")
                ready = False
                pending_query.clear()
            elif event == wake:
                _require(held and row["node_id"] == held["node_id"] and row["epoch"] == held["epoch"], "wrong restart target")
                reporter = row.get("reporter_node_id", row["node_id"]) if modern else row["node_id"]
                receipt = probes.get((row["link_id"], reporter, row["seq"]))
                _require(receipt and receipt["result"]["accepted"] and receipt["probe"]["observed_at_ns"] == row["sampled_at_ns"]
                         and receipt["time_ns"] == row["arrived_at_ns"] and held["time_ns"] < row["sampled_at_ns"]
                         <= row["arrived_at_ns"] <= now and now-row["sampled_at_ns"] <= controls["observation_ttl_ns"],
                         "restart lacks a fresh delivered device probe")
                if modern:
                    expected = history.witness(row["node_id"], held["time_ns"], now,
                                               controls["observation_ttl_ns"], online=True)
                    _require(not woken and expected and all(row.get(k) == v for k, v in expected.items()),
                             "missing or contradicted recovery probe")
                woken = True
                pending_query.add(None)
            elif event == "device_command":
                if not ready:
                    _require(None in pending_query and row["op"] == "query" and row["service_id"] == sid
                             and row["node_id"] == held["node_id"] and row["epoch"] == held["epoch"],
                             "write or wrong query before reconciliation")
                    pending_query.add(row["command_id"])
            elif event == "device_readback" and row["phase"] == "recovery" and not ready:
                _require(row["query_id"] in pending_query and row["reconciles_command_id"] == held["expired_command_id"]
                         and row["network_source"] == row["node_id"] == held["node_id"] and row["service_id"] == sid
                         and row["epoch"] == held["epoch"] and row["issued_at_ns"] <= row["observed_at_ns"]
                         <= row["arrived_at_ns"] == now, "invalid reconciliation receipt")
                ready = True
            elif event in {"work_verified", "work_incomplete"}:
                _require(False, "order ended before reconciliation and retirement")
            elif event == "service_released":
                _require(ready and row["service_id"] == sid and row["epoch"] == held["epoch"], "premature retirement")
                if modern:
                    reads = {e["query_id"]: e for e in events["device_readback"] if e["phase"] == "release"
                             and e["matched"] and e["epoch"] == held["epoch"] and e["service_id"] == sid
                             and e["network_source"] == e["node_id"] and step["time_ns"] <= e["arrived_at_ns"] <= now}
                    query_ids = row["query_ids"]
                    _require(row["decision"] == old and set(query_ids) == set(old["node_path"])
                             and all(qid in reads and reads[qid]["node_id"] == node for node, qid in query_ids.items()),
                             "retirement lacks full release readback")
                result[(wid, sid) if modern else wid] = balance.copy()
                _wire(balance, old, -1)
                _require(balance == Counter(row["retained_wire_bps"]), "reservation after retirement differs")
                break
            if "retained_wire_bps" in row:
                _require(balance == Counter(row["retained_wire_bps"]), "wire changed outside verified transitions")
        if modern:
            after_park = False
            for row in acts:
                if row is first:
                    after_park = True
                if after_park and row.get("work_id") == wid:
                    _require(row["event"] != "work_step", "abandoned step ran on the parked order")
    return result
