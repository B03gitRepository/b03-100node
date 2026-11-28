"""Portable validation of independent pre-dispatch configuration queries."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict

from packet_device_readback import refresh_matches


CONTRACT = "b03.independent-config-refresh.v1"


def require(ok, message):
    if not ok:
        raise ValueError("Independent refresh: " + message)


def refresh_obligation(pending, row):
    event = row["event"]
    key = row.get("service_id"), row.get("epoch")
    if event == "readback_requested" and row.get("independent"):
        pending[key] = {"serviceId": key[0], "epoch": key[1], "workId": row["work_id"], "planId": row["plan_id"],
                        "state": "awaiting_readback", "nodes": list(row["nodes"]), "received": set()}
    elif key in pending:
        item = pending[key]
        if event == "device_readback" and row["phase"] == "config_refresh" and row["plan_id"] == item["planId"]:
            item["received"].add(row["node_id"])
        elif event == "config_refresh_service_result":
            if row["status"] == "matched" or row["status"] == "superseded" and row["reason"] != "dispatch_recheck":
                del pending[key]
            else:
                item["state"] = "dispatch_recheck" if row["status"] == "superseded" else "unconfirmed"
                item["received"] = set(row["query_ids"])
        elif event in ("service_released", "service_aborted"):
            del pending[key]
    if event == "config_refresh_result" and row.get("recheck_original_authority"):
        for sid in row["matched"]:
            for stored in [k for k in pending if k[0] == sid and pending[k]["state"] == "dispatch_recheck"]:
                del pending[stored]


def verify_refresh(conditions, clock, controller, actions):
    identifier = conditions.get("configurationRefreshContract")
    member = "src/packet/packet_refresh.py"
    producer = member in conditions.get("implementationSha256", {}) or member in conditions.get("provenance", {}).get("checkoutFiles", {})
    if identifier is None and not (producer and conditions["mode"] == "workorder"):
        require(not any(r.get("independent") or r["event"] == "config_refresh_service_result" for r in actions), "missing edition")
        return None
    require(identifier == CONTRACT and conditions["mode"] == "workorder", "unknown or missing edition")
    cfg = conditions["simulation"]["control"]
    ttl, timeout, tick = cfg["observation_ttl_ns"], cfg["transaction_timeout_ns"], cfg["tick_ns"]
    snapshots = {r["work_id"]: r["state"] for r in actions if r["event"] == "b03_observation_evidence" and r["purpose"] == "plan"}
    plans = {r["work_id"]: r for r in actions if r["event"] == "b03_rc_plan"}
    demand, probe_versions = defaultdict(list), defaultdict(list)
    for row in controller:
        if row["event"] == "service_observation" and row["result"]["accepted"]:
            msg = row["message"]
            demand[msg["service"]["service_id"]].append((row["time_ns"], msg))
        elif row["event"] == "observation" and row["result"]["accepted"]:
            probe = row["probe"]
            series = probe_versions[probe["node_id"]]
            series.append((row["time_ns"], max(probe["version"], series[-1][1] if series else 0)))
    windows, requested, active, commands = {}, {}, {}, {}
    latest, settled_queries = {}, {}
    superseding = {(r["service_id"], r["time_ns"], "device_write") for r in actions
                   if r["event"] == "device_command" and r["op"] != "query"}
    superseding.update((r["service_id"], r["time_ns"], "dispatch_recheck") for r in actions
                       if r["event"] == "readback_requested" and r["phase"] == "config_refresh" and r["joint_refresh"])
    versions, outcomes = Counter(), Counter()
    for row in actions:
        kind, wid, now = row["event"], row.get("work_id"), row["time_ns"]
        sid, pid = row.get("service_id"), row.get("plan_id")
        if kind == "b03_observation_evidence" and row["purpose"] == "plan":
            for member, record in row["state"].get("confirmed", {}).items():
                if not record.get("configuration_evidence", {}).get("verified"):
                    continue
                for sample in record.get("readback_nodes", {}).values():
                    qid = sample["query_id"]
                    if qid in commands:
                        proof = settled_queries.get(qid)
                        require(proof is not None and proof[:2] == (member, record["epoch"]) and proof[2] <= now,
                                "next authorization uses an unsettled read-only sample")
        if kind == "config_refresh_requested":
            require(wid not in windows and set(row["service_ids"]) <= set(plans[wid]["refresh_ids"]), "request outside plan")
            windows[wid] = (set(row["service_ids"]), now + plans[wid]["decision_wall_ns"])
            requested[wid] = set()
        elif kind == "readback_requested" and row.get("independent"):
            require(wid in windows and sid in windows[wid][0] and sid not in requested[wid] and sid not in active,
                    "overlapping or unauthorized service query")
            target = snapshots[wid]["confirmed"][sid]
            require(target["epoch"] == row["epoch"] and row["nodes"] == target["decision"]["node_path"]
                    and row["phase"] == "config_refresh" and row["joint_refresh"] is False
                    and row["deadline_ns"] == now + timeout and now >= windows[wid][1], "wrong path, epoch or deadline")
            requested[wid].add(sid)
            active[sid] = (row, target["decision"], {})
        elif kind == "device_command" and wid in windows:
            require(row["op"] == "query" and sid in active, "write or out-of-round query under pre-refresh authority")
            start, _, reads = active[sid]
            require(pid == start["plan_id"] and wid == start["work_id"] and row["epoch"] == start["epoch"]
                    and row["node_id"] in start["nodes"] and now <= start["deadline_ns"], "query belongs to another round")
            if not row["retry"]:
                require(row["command_id"] not in commands, "command ID reused")
                latest[pid, row["node_id"]] = row["command_id"]
                commands[row["command_id"]] = (pid, row["node_id"])
                reads.pop(row["node_id"], None)
        elif kind == "native_command_timeout" and row.get("command_id") in commands:
            key = commands[row["command_id"]]
            if latest.get(key) == row["command_id"]:
                del latest[key]
        elif kind == "device_readback" and wid in windows:
            require(sid in active, "late readback accepted by a closed round")
            start, _, reads = active[sid]
            require(pid == start["plan_id"] and wid == start["work_id"] and row["epoch"] == start["epoch"]
                    and row["phase"] == "config_refresh" and row["joint_refresh"] is False
                    and latest.get((pid, row["node_id"])) == row["query_id"] and row["network_source"] == row["node_id"]
                    and start["time_ns"] <= row["issued_at_ns"] <= row["observed_at_ns"] <= row["arrived_at_ns"] == now <= start["deadline_ns"],
                    "readback identity or clock differs from its own round")
            del latest[pid, row["node_id"]]
            reads[row["node_id"]] = row
        elif kind == "config_refresh_service_result":
            require(sid in active, "duplicate or unrequested settlement")
            start, choice, reads = active.pop(sid)
            state = row["status"]
            require((wid, pid, row["epoch"], row["deadline_ns"]) ==
                    (start["work_id"], start["plan_id"], start["epoch"], start["deadline_ns"])
                    and row["query_ids"] == {n: r["query_id"] for n, r in reads.items()}
                    and row["replan_before_write"] is True and state in ("matched", "unconfirmed", "superseded"),
                    "settlement differs from device receipts")
            if row["reason"] == "all_replied":
                require(state in ("matched", "unconfirmed") and set(reads) == set(start["nodes"])
                        and now == max(r["arrived_at_ns"] for r in reads.values()) and now <= start["deadline_ns"],
                        "incomplete or delayed service settlement")
                stream = demand[sid]
                left = bisect_left(stream, now, key=lambda x: x[0])
                right = bisect_right(stream, now, key=lambda x: x[0])
                # Equal-clock probe and role logs lack a common sequence. Account for both possible arrival orders.
                candidates = [dict(m, fresh=now - m["observed_at_ns"] <= ttl)
                              for _, m in stream[max(0, left - 1):right]]
                proofs = [all(refresh_matches(choice, row["epoch"], n, r["result"], sample) for n,r in reads.items())
                          for sample in candidates]
                possible = any(proofs)
                certain = bool(proofs) and all(proofs) and (left > 0 or not right)
                for node, proof in reads.items():
                    series = probe_versions[node]
                    lower = bisect_left(series, now, key=lambda x: x[0])
                    upper = bisect_right(series, now, key=lambda x: x[0])
                    fresh = now - proof["observed_at_ns"] <= ttl
                    possible &= fresh and proof["version"] >= max(versions[node], series[lower-1][1] if lower else 0)
                    certain &= fresh and proof["version"] >= max(versions[node], series[upper-1][1] if upper else 0)
                require(possible if state == "matched" else not certain, "settlement contradicts service and version evidence")
                if state == "matched":
                    settled_queries.update((r["query_id"], (sid, row["epoch"], now)) for r in reads.values())
            elif row["reason"] == "deadline":
                require(state == "unconfirmed" and set(reads) != set(start["nodes"]) and start["deadline_ns"] < now <= start["deadline_ns"] + tick, "invalid incomplete query deadline")
            elif state == "superseded":
                require((sid, now, row["reason"]) in superseding or (row["reason"] == "epoch_or_path_changed"
                        and any(r["event"] in ("service_confirmed", "service_released", "service_aborted") and r["service_id"] == sid
                                and start["time_ns"] <= r["time_ns"] <= now for r in actions)), "unproved supersession")
            else:
                raise ValueError("Independent refresh: unknown settlement reason")
            outcomes[state] += 1
        if kind in ("device_readback", "device_ack"):
            versions[row["node_id"]] = max(versions[row["node_id"]], row["version"])
    for wid, (ids, ready) in windows.items():
        if ready + tick <= clock["drain_end_ns"]:
            require(requested[wid] == ids, "missing planned per-service query")
    for start, _, reads in active.values():
        require(clock["drain_end_ns"] <= start["deadline_ns"] + tick and set(reads) != set(start["nodes"]),
                "unsettled completed or overdue read-only round")
    return {"contractVersion": CONTRACT, "requested": sum(map(len, requested.values())), "matched": outcomes["matched"],
            "unconfirmed": outcomes["unconfirmed"], "superseded": outcomes["superseded"], "pendingServices": sorted(active)}
