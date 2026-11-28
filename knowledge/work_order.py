"""Optional work-order contexts sourced from a pinned, read-only knowledge edition."""
from copy import deepcopy
import hashlib
import json
from time import perf_counter_ns

from tech_knowledge import _entries, _identity, _lookup, _text


BINDING_CONTRACT = "b03.knowledge.bindings.v1"


def configured(reg):
    bindings = reg.get("bindings", {})
    if not isinstance(bindings, dict) or bindings.keys() - {"om","rules","model"}:
        raise ValueError("Knowledge bindings must name om, rules or model")
    for port, q in bindings.items():
        if isinstance(q, dict) and set(q) in ({"identifier"}, {"topic"}) and _text(next(iter(q.values()))):
            continue
        if port == "om" and isinstance(q, dict) and set(q) == {"from", "topics"} and q["from"] == "diagnosis":
            topics = q["topics"]
            if (isinstance(topics, dict) and topics
                    and all(_text(tag) and isinstance(ids, list) and ids and all(_text(i) for i in ids)
                            and len(set(ids)) == len(ids) for tag, ids in topics.items())):
                continue
        om = bindings.get("om")
        if port != "om" and q == {"from": "om"} and isinstance(om, dict) and om.get("from") == "diagnosis":
            continue
        raise ValueError("Invalid knowledge binding for " + port)
    return bindings


def check_binding_ids(bindings, entries):
    """Exact references must exist even when their diagnostic event never occurs."""
    for port, binding in bindings.items():
        references = binding.get("topics", {})
        if "identifier" in binding:
            references = {"identifier": [binding["identifier"]]}
        for condition, identifiers in references.items():
            missing = [key for key in identifiers if key not in entries]
            if missing:
                raise ValueError(f"Unknown knowledge ID for {port}.{condition}: {', '.join(missing)}")


def _bound_entries(components):
    """Read and pin exact entries without running the configured retrieval plugin."""
    entries = getattr(components, "_diagnostic_entries", None)
    if entries is None:
        raw = components.knowledge_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != components.identity["knowledge"]["sha256"]:
            raise ValueError("Knowledge edition changed during setup")
        entries = _entries(json.loads(raw), components.knowledge["version"])
        components._diagnostic_entries = entries
    return entries


def validate_binding_edition(components):
    bindings = configured(components.knowledge)
    if bindings:
        check_binding_ids(bindings, _bound_entries(components))


def diagnosis_query(binding, diagnosis, state):
    """Group only configured findings by service, using this work order's evidence."""
    matches = {}
    for sid, row in diagnosis["services"].items():
        tags = {row["status"], row.get("potential_action_status")}
        tags.update(item["reason"] for item in row["evidence"])
        for tag in tags.intersection(binding["topics"]):
            matches.setdefault(tag, set()).add(sid)
    cleanup = "retaining_until_cleanup_readback"
    if cleanup in binding["topics"] and state.get("controller_pending_cleanups"):
        matches.setdefault(cleanup, set()).update(state["controller_pending_cleanups"])
    return {"from": "diagnosis", "matches": {k: sorted(matches[k]) for k in sorted(matches)}}


def diagnosis_ids(binding, question):
    # Resolve each ID once even if several services or phenomena use the same guidance.
    return list(dict.fromkeys(key for tag in sorted(question["matches"]) for key in binding["topics"][tag]))


def lookup(components, port, work, snap, stamp, *, diagnosis=None, state=None, om_advice=None):
    if not components or port not in configured(components.knowledge):
        return None
    start = perf_counter_ns()
    binding = components.knowledge["bindings"][port]
    question = binding
    component = _identity(components.identity)
    if binding.get("from") == "diagnosis":
        question = diagnosis_query(binding, diagnosis, state)
        keys = diagnosis_ids(binding, question)
        entries = _bound_entries(components)
        for key in keys:
            if key not in entries:
                raise ValueError(f"Unknown knowledge ID for {port}: {key}")
        output = [deepcopy(entries[key]) for key in keys]
    elif binding.get("from") == "om":
        if (not om_advice or om_advice["work"] != work or om_advice["snapshotId"] != snap
                or om_advice["port"] != "om" or om_advice["timeNs"] != stamp or om_advice["component"] != component):
            raise ValueError("Missing OM knowledge for this work order")
        output = deepcopy(om_advice["output"])
    else:
        output, sha = _lookup(components, binding.get("identifier"), binding.get("topic"))
        if component["sourceSha256"] != sha:
            raise ValueError("Knowledge edition changed after selection")
    schema = "b03.knowledge.context.v3" if "from" in binding else "b03.knowledge.context.v2"
    return {"schema":schema, "work":work, "snapshotId":snap, "timeNs":stamp,
            "port":port, "query":deepcopy(question), "component":component, "output":output,
            "wallNs":perf_counter_ns()-start}


def check_om_advice(components, work, snap, state, diagnosis, receipt):
    """RC independently checks a dynamic OM handoff before any consumer can use it."""
    binding = components.knowledge.get("bindings", {}).get("om") if components else None
    if binding is None:
        if receipt is not None:
            raise ValueError("Unexpected OM knowledge on a disabled port")
        return
    component = _identity(components.identity)
    entries = _bound_entries(components)
    dynamic = binding.get("from") == "diagnosis"
    question = diagnosis_query(binding, diagnosis, state) if dynamic else binding
    schema = "b03.knowledge.context.v3" if dynamic else "b03.knowledge.context.v2"
    if (not isinstance(receipt, dict)
            or set(receipt) != {"schema", "port", "work", "snapshotId", "timeNs", "query", "component", "output", "wallNs"}
            or receipt["schema"] != schema or receipt["port"] != "om" or receipt["work"] != work
            or receipt["snapshotId"] != snap or receipt["timeNs"] != state["now_ns"] or receipt["query"] != question
            or receipt["component"] != component or type(receipt["wallNs"]) is not int or receipt["wallNs"] < 0):
        raise ValueError("OM knowledge differs from this diagnosis, snapshot or edition")
    rows = receipt["output"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not _text(row.get("id")) for row in rows):
        raise ValueError("Invalid OM knowledge rows")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids) or any(row != entries.get(key) for key, row in zip(ids, rows)):
        raise ValueError("OM knowledge contains a duplicate or an unpinned entry")
    if dynamic:
        expected = diagnosis_ids(binding, question)
    elif "identifier" in binding:
        key = binding["identifier"]
        expected = [key] if key in entries else []
    elif component["entrypoint"] == "builtin":
        topic = binding["topic"].casefold()
        expected = [key for key, row in entries.items() if any(topic in t.casefold() for t in row["topics"])]
    else:
        # Legacy topic plugins own their recall/rank. Their returned rows must still match the edition.
        return
    if ids != expected:
        raise ValueError("OM knowledge differs from the results for this work order")


def accepts_model(model, policy):
    return (policy.get("kind") == "model-adapter" and model.get("entrypoint") != "builtin"
            and model.get("options",{}).get("acceptKnowledge") is True)


def accepts_rules(reg, symbol):
    opts = reg.get("options", {})
    return (reg.get("contractVersion") == "b03.rules.patch.v2" and opts.get("acceptKnowledge") is True
            and (symbol in opts.get("customTools", {}) or symbol in opts.get("providedNative", [])))
