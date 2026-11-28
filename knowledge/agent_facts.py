"""供 OM 与 RC 在首条探针到达前读取的开局静态网络底图及实时差量对比。"""
from copy import deepcopy
import hashlib
import json


def evidence_digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AgentNetworkFacts:
    """每次仿真独立维护一份底图目录，对外只返回深拷贝副本以防篡改。"""

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
        """按原生编号查询节点、链路、初始业务、信道模型或资源上限。"""
        values = self._catalog[section]
        return deepcopy(values if identifier is None else values.get(identifier))

    def export(self):
        """在初始化阶段向模型适配器交付一份独立的开局底图副本。"""
        return deepcopy(self._catalog)

    def compare_reports(self, state):
        """将已送达的实时观测与开局底图做差量对比，不自行臆测链路通断。"""
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
    """把匿名化观测表下标映射回公开编号，供启用了底图的模型适配器使用。"""
    links = state["links"]
    nodes = {row[key] for row in links.values() for key in ("src_node_id", "dst_node_id")}
    return {**deepcopy(comparison), "nodeIds": sorted(nodes), "linkIds": sorted(links),
            "serviceIds": [sid if sid in state["services"] or sid in state["confirmed"] else None
                           for sid in sorted(diagnosis["services"])]}


def order_records(events, event, purpose=None):
    """按工单索引指定事件类型，出现重复或歧义回执时直接拒收。"""
    rows = {}
    for row in events:
        if row["event"] == event and (purpose is None or row.get("purpose") == purpose):
            work = row["work_id"]
            if work in rows:
                raise ValueError("Duplicate " + event + " for a work order")
            rows[work] = row
    return rows


def verify_agent_facts(catalog, events, policy, model=None, authority=None):
    """基于封存底图和对应工单观测，离线重算 OM 与 RC 两侧的底图差量输入。"""
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
        visible = state["services"].keys() | state["confirmed"].keys()
        roster = set(diag["services"])
        if not visible.issubset(roster):
            raise ValueError("Network reference omits a service present in the delivered snapshot")
        if authority is not None:
            permitted = () if authority["discovery"] == "all_delivered_registered" else authority["allowedServiceIds"]
            if roster != visible.union(permitted):
                raise ValueError("Network reference service roster differs from the snapshot and grant")
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
