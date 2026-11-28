"""Static network facts available to both work-order roles before the first report arrives."""
from copy import deepcopy
import hashlib
import json


def evidence_digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AgentNetworkFacts:
    """Keep one independent catalogue per simulation. Callers receive detached records."""

    def __init__(self, catalog):
        fields = {"schema", "nodes", "links", "initialServices", "channelModel", "resources"}
        if not isinstance(catalog, dict) or set(catalog) != fields or catalog["schema"] != "b03.network-foundation.v1":
            raise ValueError("Expected only the sealed startup network facts")
        static_fields = {
            "nodes": {"nodeType", "capabilities", "attachedLinkTypes", "interfaceCount", "ip", "mask", "initialPosition"},
            "links": {"srcNodeId", "dstNodeId", "linkType", "params", "effectiveParams"},
            "initialServices": {"srcNodeId", "dstNodeId", "serviceType", "category", "priority", "requirement"},
        }
        for section, permitted in static_fields.items():
            rows = catalog[section]
            if not isinstance(rows, dict) or any(not isinstance(row, dict) or row.keys() - permitted for row in rows.values()):
                raise ValueError("Unexpected startup fields in " + section)
        self._catalog = deepcopy(catalog)
        self.digest = evidence_digest(catalog)

    def lookup(self, section, identifier=None):
        """Look up nodes, links, initialServices, channelModel or resources by their native IDs."""
        values = self._catalog[section]
        return deepcopy(values if identifier is None else values.get(identifier))

    def export(self):
        """Hand a model adapter a detached copy once at initialization."""
        return deepcopy(self._catalog)

    def compare_reports(self, state):
        """Compare delivered measurements with the startup baseline; never infer link availability."""
        below = {}
        for lid, report in state["links"].items():
            link = self._catalog["links"].get(lid)
            if not link or not report["usable"]:
                continue
            baseline = link["effectiveParams"]["bandwidthBps"]
            observed = report["capacity_bps"]
            if observed < baseline:
                below[lid] = {"startupBps": baseline, "observedBps": observed}
        return {"catalogDigest": self.digest, "linksBelowStartupCapacity": below,
                "reportedOutsideInitialDefinitions": sorted(
                    sid for sid, row in state["services"].items()
                    if row["registered"] and sid not in self._catalog["initialServices"])}


def model_network_reference(comparison, state, diagnosis):
    """Join anonymized table indices to known public IDs for adapters using the catalogue."""
    links = state["links"]
    nodes = {row[key] for row in links.values() for key in ("src_node_id", "dst_node_id")}
    return {**deepcopy(comparison), "nodeIds": sorted(nodes), "linkIds": sorted(links),
            "serviceIds": [sid if sid in state["services"] or sid in state["confirmed"] else None
                           for sid in sorted(diagnosis["services"])]}


def order_records(events, event, purpose=None):
    """Index an event kind once, refusing ambiguous work-order receipts."""
    rows = {}
    for row in events:
        if row["event"] == event and (purpose is None or row.get("purpose") == purpose):
            work = row["work_id"]
            if work in rows:
                raise ValueError("Duplicate " + event + " for a work order")
            rows[work] = row
    return rows


def verify_agent_facts(catalog, events, policy, model=None):
    """Recompute both role inputs from the sealed catalogue and the matching observation."""
    hook = policy.get("networkFoundationHook") is True
    if model is not None:
        registered = (policy.get("kind") == "model-adapter" and model.get("entrypoint") != "builtin"
                      and model.get("options", {}).get("acceptNetworkFoundation") is True)
        if hook != registered:
            raise ValueError("Model network foundation hook differs from its registered option")
    if catalog is None:
        if hook or any("networkBaseline" in row or "rc_network_baseline_digest" in row
                       or "networkReference" in row.get("tool_choice", {}).get("observation", {}) for row in events):
            raise ValueError("Network reference found without a startup foundation")
        return {"catalogDigest": None, "verifiedOrders": 0}
    facts = AgentNetworkFacts(catalog)
    states = order_records(events, "b03_observation_evidence", "plan")
    diagnoses = order_records(events, "b03_om_diagnosis")
    plans = order_records(events, "b03_rc_plan")
    if states.keys() != plans.keys() or diagnoses.keys() != plans.keys():
        raise ValueError("Network foundation handoff lacks a role pair or planning snapshot")
    for work, plan in plans.items():
        diag, evidence = diagnoses[work], states[work]
        state = evidence["state"]
        snap = evidence_digest(state)
        if any(row.get("snapshot_id") != snap or row.get("time_ns") != state["now_ns"]
               for row in (diag, evidence, plan)):
            raise ValueError("Network foundation handoff differs from its sealed planning snapshot")
        expected = facts.compare_reports(state)
        if diag.get("networkBaseline") != expected or plan.get("rc_network_baseline_digest") != evidence_digest(expected):
            raise ValueError("Agent network baseline differs from its catalogue or the delivered reports")
        choice = plan["tool_choice"]
        observation = choice.get("observation", {})
        if choice["modelCalled"] and hook:
            if observation.get("networkReference") != model_network_reference(expected, state, diag):
                raise ValueError("Model network reference differs from the startup catalogue")
        elif "networkReference" in observation:
            raise ValueError("Unexpected model network reference")
    return {"catalogDigest": facts.digest, "verifiedOrders": len(plans)}
