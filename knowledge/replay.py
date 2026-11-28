"""Replay the knowledge delivered to each agent against the sealed work-order inputs."""
import hashlib
import json

from .agent_facts import evidence_digest, order_records
from tech_knowledge import _entries, _identity, _pending, _question, opening_outcome, query_library
from .work_order import (BINDING_CONTRACT, accepts_model, accepts_rules, check_binding_ids, configured,
                         diagnosis_ids, diagnosis_query)


def _check_pending_cleanups(events):
    # A plan can see a parked release only after the executor records its deferral.
    # Recovery resumes that work order before the next planning snapshot is taken.
    pending = {}
    last = -1
    for row in events:
        event = row["event"]
        if event not in ("cleanup_deferred", "cleanup_recovery_probe", "release_parked",
                         "release_recovery_probe", "b03_observation_evidence"):
            continue
        if event == "b03_observation_evidence" and row.get("purpose") != "plan":
            continue
        if row["time_ns"] < last:
            raise ValueError("Cleanup observations are not in simulation time order")
        last = row["time_ns"]
        work = row["work_id"]
        if event in ("cleanup_deferred", "release_parked"):
            if work in pending:
                raise ValueError("Cleanup was already deferred for this work order")
            # A missing acknowledgment for a stop request leaves the source state unknown.
            # Only confirmed stops can serve as evidence for cleanup-specific OM guidance.
            pending[work] = row["service_id"] if event == "cleanup_deferred" or row["source_stop_confirmed"] else None
        elif event in ("cleanup_recovery_probe", "release_recovery_probe"):
            if work not in pending:
                raise ValueError("Cleanup recovery has no deferred work order")
            del pending[work]
        elif row["state"].get("controller_pending_cleanups", []) != sorted({sid for sid in pending.values() if sid is not None}):
            raise ValueError("Pending cleanup snapshot differs from the executor record")


def verify(conditions, events, entries, identity):
    bindings = configured(conditions["components"]["knowledge"])
    if "knowledgeBindingContract" in conditions:
        if conditions["knowledgeBindingContract"] != BINDING_CONTRACT:
            raise ValueError("Unknown knowledge binding contract")
        check_binding_ids(bindings, entries)
    registered = conditions["components"]["rules"]
    proof = []
    diagnoses = order_records(events, "b03_om_diagnosis")
    states = order_records(events, "b03_observation_evidence", "plan")
    plans = order_records(events, "b03_rc_plan")
    dynamic_om = bindings.get("om", {}).get("from") == "diagnosis"
    if dynamic_om:
        if states.keys() != plans.keys() or diagnoses.keys() != plans.keys():
            raise ValueError("Dynamic OM query lacks its work-order role pair or planning snapshot")
        _check_pending_cleanups(events)
    for plan in plans.values():
        work, snap = plan["work_id"], plan["snapshot_id"]
        records = plan.get("knowledge_contexts",{})
        choice = plan["tool_choice"]
        rules_port = plan["real_solver_calls"] and accepts_rules(registered, choice["selected"])
        model_port = choice["modelCalled"] and accepts_model(conditions["components"]["model"],conditions["rcPolicy"])
        enabled = ({"om"} | ({"rules"} if rules_port else set()) | ({"model"} if model_port else set())) & bindings.keys()
        if not isinstance(records,dict) or set(records) != enabled:
            raise ValueError("A knowledge port receipt is missing or outside its work-order trigger")
        diagnosis = diagnoses[work]
        observation = choice.get("observation",{})
        evidence = states.get(work)
        if enabled and (evidence is None or evidence["snapshot_id"] != snap or evidence_digest(evidence["state"]) != snap):
            raise ValueError("Knowledge context differs from its planning snapshot")
        if dynamic_om:
            from rule_replay import replay_diagnosis
            if (not evidence or evidence["snapshot_id"] != snap or evidence["time_ns"] != plan["time_ns"]
                    or evidence["state"]["now_ns"] != plan["time_ns"]):
                raise ValueError("Dynamic OM query lacks its sealed planning snapshot")
            state = evidence["state"]
            _, _, _, _, rebuilt, policy_hash = replay_diagnosis(conditions, state)
            if (diagnosis["time_ns"] != plan["time_ns"] or plan["policy_hash"] != policy_hash
                    or any(diagnosis.get(key) != value for key, value in rebuilt.items())
                    or plan["authorized"] != rebuilt["authority"]["executableServiceIds"]):
                raise ValueError("Dynamic OM diagnosis differs from the delivered evidence and grant")
            question = diagnosis_query(bindings["om"], rebuilt, state)
            expected_ids = diagnosis_ids(bindings["om"], question)
        for port in sorted(enabled):
            rec = records[port]
            binding = bindings[port]
            dynamic = "from" in binding
            expected_query = question if dynamic and port == "om" else binding
            schema = "b03.knowledge.context.v3" if dynamic else "b03.knowledge.context.v2"
            if (not isinstance(rec,dict) or set(rec) != {"schema","work","snapshotId","timeNs","port","query","component","output","wallNs"}
                    or rec["schema"] != schema or rec["work"] != work or rec["port"] != port
                    or rec["snapshotId"] != snap or diagnosis["snapshot_id"] != snap
                    or rec["timeNs"] != plan["time_ns"] or rec["query"] != expected_query
                    or rec["component"] != identity or type(rec["wallNs"]) is not int or rec["wallNs"] < 0):
                raise ValueError("Knowledge context differs from the sealed observation, query or edition")
            rows = rec["output"]
            if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
                raise ValueError("Invalid knowledge result")
            ids = [r.get("id") for r in rows]
            if (not all(isinstance(k,str) for k in ids) or len(set(ids)) != len(ids)
                    or any(r != entries.get(k) for k,r in zip(ids,rows))):
                raise ValueError("Ranked results contain duplicates or differ from the pinned edition")
            key = rec["query"].get("identifier")
            if key is not None and ids != ([key] if key in entries else []):
                raise ValueError("Exact knowledge lookup changed the identifier")
            if dynamic and ids != expected_ids:
                raise ValueError("Dynamic knowledge results differ from the diagnosis-bound edition entries")
            audit = {"work":work,"port":port,"rankedIds":ids,"version":identity["version"],"wallNs":rec["wallNs"]}
            if dynamic:
                audit["query"] = rec["query"]
            proof.append(audit)
        def match_slot(target, port, delivered):
            if ("knowledgeContext" in target) != delivered or delivered and target["knowledgeContext"] != records[port]["output"]:
                raise ValueError("Consumer context differs from delivered knowledge")
        match_slot(diagnosis, "om", "om" in enabled)
        # Earlier archives predate the RC input record. New producers include it for every OM handoff.
        has_input = "rc_knowledge_input_ids" in plan
        new_producer = ("knowledge/work_order.py" in conditions.get("implementationSha256", {})
                        or conditions.get("knowledgeBindingContract") == BINDING_CONTRACT)
        if has_input or new_producer:
            if has_input != ("om" in enabled) or (has_input and plan["rc_knowledge_input_ids"] !=
                    [entry["id"] for entry in records["om"]["output"]]):
                raise ValueError("RC decision input differs from the delivered OM knowledge")
        match_slot(observation, "model", "model" in enabled)
        patch = plan.get("solver_audit",{}).get("rulePatch")
        cfg = registered.get("options", {})
        plugin_tools = set(cfg.get("providedNative",[])) | set(cfg.get("customTools",{}))
        expected_patch = (registered.get("contractVersion") == "b03.rules.patch.v2" and bool(plan["real_solver_calls"])
                          and choice.get("selected") in plugin_tools)
        if bool(patch) != expected_patch:
            raise ValueError("Missing or unexpected rule patch receipt")
        if patch:
            from rule_component import verify_patch_receipt
            verify_patch_receipt(conditions, registered, plan, diagnosis, states[work], patch, records.get("rules",{}).get("output"))
    return proof


def verify_work_order_queries(conditions, events):
    """Replay lookup results and decision evidence from the sealed run, without importing providers."""
    proof = conditions.get("provenance", {})
    required = conditions.get("mode") == "workorder" and "knowledge" in proof.get("activeComponents", {})
    plans = [r for r in events if r["event"] == "b03_rc_plan"]
    if not required:
        if any("knowledge_io" in r or "knowledge_contexts" in r for r in plans):
            raise ValueError("Unregistered knowledge receipt")
        return []
    diagnoses = {r["work_id"]: r for r in events if r["event"] == "b03_om_diagnosis"}
    observations = {r["work_id"]: r for r in events if r["event"] == "b03_observation_evidence"
                    and r.get("purpose") == "plan"}
    identity = _identity(conditions["components"])
    edition = proof["knowledge"]
    raw = edition["text"].encode("utf-8")
    if (any(edition[k] != identity[k] for k in ("id", "version"))
            or hashlib.sha256(raw).hexdigest() != identity["sourceSha256"]):
        raise ValueError("Archived knowledge version or source differs")
    wanted = query_library(json.loads(raw), identity["version"], identifier="K044")
    mode = conditions["rcPolicy"]["initialAdmission"]
    first = True
    results = []
    for plan in plans:
        work = plan["work_id"]
        admission = plan.get("solver_audit", {}).get("openingAdmission")
        opening = first and bool(plan.get("real_solver_calls"))
        if opening != (admission is not None):
            raise ValueError("Opening evidence differs from the first authorized solver call")
        if opening:
            first = False
        diagnosis = diagnoses[work]
        pending = _pending(diagnosis, mode) if opening else []
        record = plan.get("knowledge_io")
        if bool(pending) != (record is not None):
            raise ValueError("Opening knowledge query missing or outside its delivered trigger")
        if not pending:
            continue
        observation = observations.get(work)
        if (not observation or diagnosis["snapshot_id"] != plan["snapshot_id"]
                or observation["snapshot_id"] != plan["snapshot_id"]
                or observation["time_ns"] != plan["time_ns"] or diagnosis["time_ns"] != plan["time_ns"]
                or set(plan["authorized"]) != set(diagnosis["authority"]["executableServiceIds"])
                or admission["mode"] != mode):
            raise ValueError("Opening knowledge query differs from its authorized observation")
        question = _question(work, plan["snapshot_id"], observation["state"], diagnosis, mode,
                             plan["tool_choice"]["selected"], pending)
        expected = {"schema": "b03.packet.knowledge-io.v1", "input": question, "component": identity,
                    "output": wanted}
        if wanted:
            retained = sorted(set(pending) - set(admission["missingAuthorized"]))
            added = sorted(admission["applied"])
            if (not set(added) <= set(plan["authorized"]) & set(admission["missingAuthorized"])
                    or not set(added) <= set(admission["feasibleAdditions"]) or added and admission["heldBySelectedAction"]):
                raise ValueError("K044 residual additions disagree with the authorized opening")
            sample = plan.get("component_io")
            if sample and retained != sorted(s for s in pending if sample["returned"]["target"][s] is not None):
                raise ValueError("K044 retained paths differ from the recorded rule proposal")
            checked = {s: plan["decisions"][s]["required_wire_bps"] for s in set(pending) & (set(retained) | set(added))}
            expected["procedureCheck"] = {"status": "checked", "retainedPendingIds": retained,
                                          "residualAddedIds": added, "pendingWireBps": checked}
        else:
            expected["procedureCheck"] = {"status": "entry_not_in_edition"}
        expected["disposition"] = opening_outcome(expected, admission, plan["decisions"], plan["install_ids"])
        if record != expected:
            raise ValueError("Archived knowledge input, source, result or actual disposition differs")
        results.append({"work": work, "id": question["id"], "version": identity["version"],
                        "sourceSha256": identity["sourceSha256"], "pending": pending,
                        "disposition": expected["disposition"]})
    if conditions.get("receiptContract") in ("b03.packet.workorder.v2", "b03.packet.workorder.v3"):
        results.extend(verify(conditions, events, _entries(json.loads(raw), identity["version"]), identity))
    elif any("knowledge_contexts" in plan or "rulePatch" in plan.get("solver_audit", {}) for plan in plans):
        raise ValueError("A v2 component record cannot be interpreted as a v1 work order")
    return results
