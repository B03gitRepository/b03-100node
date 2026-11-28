"""针对锁定技术知识版的只读查询实现，以及 RC 首单残余准入核查逻辑。"""
from copy import deepcopy
import hashlib
import json


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _entries(library, edition):
    if (not isinstance(library, dict) or library.get("schema") not in ("b03.knowledge.v1", "b03.knowledge.v2")
            or library.get("version") != edition or not isinstance(library.get("entries"), list)):
        raise ValueError("The configured technical knowledge edition differs from the source")
    fields = ("id", "topics", "content", "sources", "applicability")
    entries = {}
    for row in library["entries"]:
        if (not isinstance(row, dict) or any(not _text(row.get(k)) for k in ("id", "content", "applicability"))
                or any(not isinstance(row.get(k), list) or not row[k] or not all(_text(x) for x in row[k])
                       for k in ("topics", "sources")) or row["id"] in entries):
            raise ValueError("Incomplete or duplicate knowledge entry")
        keys = fields
        if library["schema"] == "b03.knowledge.v2":
            # 普通技术说明保持既有发布格式；诊断指引与历史案例条目
            # 额外标明所需证据依据以及结论的适用边界。
            if "kind" in row:
                keys = (*fields, "kind", "evidence", "limits")
                if (row["kind"] not in ("diagnosis", "case") or not _text(row.get("limits"))
                        or not isinstance(row.get("evidence"), list) or not row["evidence"]
                        or not all(_text(item) for item in row["evidence"])):
                    raise ValueError("Invalid diagnostic or case fields for " + row["id"])
            if set(row) != set(keys):
                raise ValueError("Unexpected knowledge fields for " + row["id"])
        entry = {key: deepcopy(row[key]) for key in keys}
        entries[entry["id"]] = {**entry, "version": edition}
    return entries


def query_library(library, edition, *, identifier=None, topic=None):
    if (identifier is None) == (topic is None) or not _text(identifier if topic is None else topic):
        raise ValueError("Select exactly one nonempty knowledge ID or topic")
    entries = _entries(library, edition)
    if identifier is not None:
        return [entries[identifier]] if identifier in entries else []
    return [row for row in entries.values() if any(topic.casefold() in t.casefold() for t in row["topics"])]


def _edition(components):
    reg = components.knowledge
    ranked = reg.get("contractVersion") == "b03.knowledge.lookup.v2"
    cached = getattr(components, "_ranked_knowledge", None) if ranked else None
    if cached is None:
        raw = components.knowledge_path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        library = json.loads(raw)
        entries = _entries(library, reg["version"])
        provider = None
        if reg.get("entrypoint", "builtin") != "builtin":
            config = {k: deepcopy(reg[k]) for k in ("id", "version", "entrypoint", "options") if k in reg}
            provider = components.factory("knowledge")(config=config, library=deepcopy(library))
            if not callable(getattr(provider, "lookup", None)):
                raise ValueError("A knowledge provider must implement lookup")
        cached = (source_hash, entries, provider)
        if ranked:
            if source_hash != components.identity["knowledge"]["sha256"]:
                raise ValueError("The knowledge source differs from its pinned edition")
            components._ranked_knowledge = cached
    return cached


def _lookup(components, identifier, topic):
    if (identifier is None) == (topic is None) or not _text(identifier if topic is None else topic):
        raise ValueError("Select exactly one nonempty knowledge ID or topic")
    source_hash, entries, provider = _edition(components)
    if provider is not None:
        ids = provider.lookup(identifier=identifier, topic=topic)
    elif identifier is not None:
        ids = [identifier] if identifier in entries else []
    else:
        ids = [key for key, row in entries.items() if any(topic.casefold() in t.casefold() for t in row["topics"])]
    if (not isinstance(ids, list) or not all(_text(i) and i in entries for i in ids)
            or len(ids) != len(set(ids))
            or identifier is not None and ids != ([identifier] if identifier in entries else [])):
        raise ValueError("Provider IDs are invalid for the query in the pinned edition")
    return [deepcopy(entries[i]) for i in ids], source_hash


def query(components, *, identifier=None, topic=None):
    return _lookup(components, identifier, topic)[0]


def _pending(diagnosis, mode):
    if mode != "residual":
        return []
    allowed = diagnosis["authority"]["executableServiceIds"]
    if any(not diagnosis["services"][sid]["authorized"] for sid in allowed):
        raise ValueError("Opening knowledge requires consistent OM write authority")
    return sorted(sid for sid in allowed if diagnosis["services"][sid]["status"] in
                  {"observed_pending_admission", "observed_pending_new_demand_revision"})


def _question(work, snapshot_id, state, diagnosis, mode, selected, pending):
    basis = {}
    for sid in pending:
        row = state["services"][sid]
        if (not (row["fresh"] and row["active"] and row["registered"])
                or not 0 <= row["observed_at_ns"] <= row["received_at_ns"] <= state["now_ns"]):
            raise ValueError("Opening knowledge requires a delivered, active demand")
        basis[sid] = {key: row[key] for key in ("revision", "observed_at_ns", "received_at_ns")}
        basis[sid]["status"] = diagnosis["services"][sid]["status"]
    return {"id": "K044", "work": work, "snapshotId": snapshot_id, "timeNs": state["now_ns"],
            "role": "RC", "mode": mode, "selected": selected, "pendingAuthorized": basis}


def _identity(identity):
    reg = identity["knowledge"]
    return {"id": reg["id"], "version": reg["version"], "entrypoint": reg.get("entrypoint", "builtin"),
            "source": reg["source"], "sourceSha256": reg["sha256"],
            "providerSha256": identity.get("providerModuleSha256", {}).get("knowledge")}


def opening_lookup(components, work, snapshot_id, state, diagnosis, mode, selected):
    pending = _pending(diagnosis, mode)
    if not pending:
        return None
    if components is None:
        from component_config import load_components
        components = load_components().activate("knowledge")
    question = _question(work, snapshot_id, state, diagnosis, mode, selected, pending)
    component = _identity(components.identity)
    result, source_hash = _lookup(components, question["id"], None)
    if source_hash != component["sourceSha256"]:
        raise ValueError("Knowledge source changed after component selection")
    return {"schema": "b03.packet.knowledge-io.v1", "input": question,
            "component": component, "output": result}


def check_opening_advice(receipt, before, after, snapshot, authority, admission):
    """命中 K044 知识条目时，对照原始工单复核残余容量补接提案的合规性。"""
    if not receipt["output"]:
        return {"status": "entry_not_in_edition"}
    if set(before) != set(after):
        raise ValueError("K044 residual check found a different service roster")
    additions = sorted(sid for sid, path in after.items() if path != before[sid])
    if (set(additions) != set(admission["applied"])
            or not set(additions) <= set(authority["executableServiceIds"]) & set(admission["missingAuthorized"])
            or any(before[sid] is not None or after[sid] is None for sid in additions)):
        raise ValueError("K044 residual check found a changed prior path or an unauthorized addition")
    pending = receipt["input"]["pendingAuthorized"]
    checked = {}
    for sid in sorted(set(additions) | set(pending)):
        if after[sid] is not None:
            rate = after[sid]["allocatedBps"]
            if rate != snapshot["requests"][sid]["requirement"]["bandwidthBps"]:
                raise ValueError("K044 residual check found a changed original wire rate")
            if sid in pending:
                checked[sid] = rate
    return {"status": "checked", "retainedPendingIds": sorted(sid for sid in pending if before[sid] is not None),
            "residualAddedIds": additions, "pendingWireBps": checked}


def opening_outcome(receipt, admission, choices, installs):
    """汇总 K044 建议与原求解器提案、残余搜索及最终设备计划的对应审计结果。"""
    searched = {row["id"] for row in admission["search"]}
    by_service = {}
    for sid in receipt["input"]["pendingAuthorized"]:
        by_service[sid] = {"proposedBySelectedRule": sid not in admission["missingAuthorized"],
                           "residualSearched": sid in searched,
                           "residualFeasible": sid in admission["feasibleAdditions"],
                           "addedByOpening": sid in admission["applied"],
                           "acceptedInFinalPlan": choices[sid]["accepted"],
                           "installRequested": sid in installs}
    return {"handler": "opening.check_opening", "matchedEditionEntry": bool(receipt["output"]),
            "heldBySelectedAction": admission["heldBySelectedAction"], "services": by_service}


def verify_work_order_queries(conditions, events):
    """兼容仅导入 src/ 原模块的旧调用方，转发工单知识回放核验入口。"""
    from knowledge_ports import verify_work_order_queries as replay
    return replay(conditions, events)
