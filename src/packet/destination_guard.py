"""基于已送达端点证据，在目的节点确认离线时授权主控直通叫停源端发包。"""

from bisect import bisect_right
from collections import defaultdict
from operator import itemgetter


def _offline_destinations(services, ports):
    """为每个节点保留最新的在线与离线观测证据。

    只有管理态开启（admin_up）的在线邻居才能证明对端离线；
    一旦收到同时刻或更新的在线迹象，即撤销离线判定。
    """
    alive, missing = {}, {}
    for row in services.values():
        if row["fresh"]:
            nid = row["node_id"]
            alive[nid] = max(alive.get(nid, -1), row["observed_at_ns"])
    for lid, reporter, peer, stamp, admin_up, peer_online in ports:
        alive[reporter] = max(alive.get(reporter, -1), stamp)
        if not admin_up:
            continue
        if peer_online:
            alive[peer] = max(alive.get(peer, -1), stamp)
        else:
            candidate = (stamp, lid, reporter)
            if peer not in missing or candidate > missing[peer]:
                missing[peer] = candidate
    return {nid: {"sampled_at_ns": stamp, "link_id": lid, "reporter_node_id": reporter}
            for nid, (stamp, lid, reporter) in missing.items() if stamp > alive.get(nid, -1)}


def offline_destinations(state):
    """评估角色视图中的已送达观测，保留用于下发核验的离线证人探针。"""
    ports = ((lid, who, link["dst_node_id"] if who == link["src_node_id"] else link["src_node_id"],
              p["sampled_at_ns"], p["admin_up"], p["endpoint_online"])
             for lid,link in state["links"].items() for who,p in link.get("reports", {}).items()
             if p and p["status"] == "fresh" and p["node_online"])
    return _offline_destinations(state["services"], ports)


def offline_node_signature(links, demands, now, ttl):
    """在控制节拍上比较目的离线判定语义，避免把全部探针全量拷贝进角色视图。"""
    def delivered_ports():
        for lid, link in links["links"].items():
            for who, record in link["reporters"].items():
                if not record:
                    continue
                p = record["probe"]
                t = p["observed_at_ns"]
                if p["node_online"] and now - ttl <= t <= record["received_at_ns"] <= now:
                    q = p["link"]
                    yield lid, who, q["peer_node_id"], t, q["admin_up"], q["endpoint_online"]
    return tuple(sorted(_offline_destinations(demands["services"], delivered_ports())))


def choose_source_stop(state, writable, healthy):
    """每次只授权一条受保护的源端停发工单，优先叫停占用线速最高的无效流量。"""
    if not state["confirmed"]:
        return None
    offline = offline_destinations(state)
    candidates = []
    for sid in (writable & state["confirmed"].keys()) - set(healthy):
        old, row = state["confirmed"][sid], state["services"].get(sid)
        if not row or not (row["fresh"] and row["registered"] and row["active"]):
            continue
        choice = old["decision"]
        src, dst = choice["node_path"][0], choice["node_path"][-1]
        if (src == dst or dst not in offline or src in offline or row["revision"] != choice["demand_revision"]
                or row["service"]["src_node_id"] != src or row["service"]["dst_node_id"] != dst):
            continue
        candidates.append((-choice["wire_bps"], sid, src, dst, old["epoch"]))
    if not candidates:
        return None
    _, sid, src, dst, epoch = min(candidates)
    return {"service_id": sid, "epoch": epoch, "source_node_id": src,
            "destination_node_id": dst, **offline[dst]}


def port_witness(node, since, now, ttl, reports, *, online, last_service_sample=-1):
    """仅当不存在同样新鲜的反向在线样本时，才选取最新的端口离线证据。"""
    best = None
    opposed_at = -1 if online else last_service_sample
    for reporter, probe, arrived in reports:
        sampled = probe["observed_at_ns"]
        if not (probe["node_online"] and since < sampled <= arrived <= now and now - sampled <= ttl):
            continue
        link = probe["link"]
        if reporter == node:
            alive = True
        elif link["peer_node_id"] == node and link["admin_up"]:
            alive = link["endpoint_online"]
        else:
            continue
        if alive is not online:
            opposed_at = max(opposed_at, sampled)
            continue
        key = (sampled, link["link_id"], reporter, probe["seq"], arrived)
        if best is None or key > best:
            best = key
    if best is None or best[0] <= opposed_at:
        return None
    sampled, lid, reporter, seq, arrived = best
    return {"reporter_node_id": reporter, "link_id": lid, "seq": seq,
            "sampled_at_ns": sampled, "arrived_at_ns": arrived}


class DeliveredNodeEvidence:
    """为相关节点索引已接受的到达探针，并重建各端口最新上报状态。"""

    def __init__(self, nodes, probes, demands):
        self.ports = {node: defaultdict(list) for node in nodes}
        self.services = {node: defaultdict(list) for node in nodes}
        for (lid, reporter, _), receipt in probes.items():
            if not receipt["result"]["accepted"]:
                continue
            for node in (reporter, receipt["probe"]["link"]["peer_node_id"]):
                if node in self.ports:
                    self.ports[node][lid, reporter].append(receipt)
        for (sid, _), receipt in demands.items():
            if receipt["result"]["accepted"] and receipt["message"]["node_id"] in self.services:
                self.services[receipt["message"]["node_id"]][sid].append(receipt)
        for group in (self.ports, self.services):
            for streams in group.values():
                for timeline in streams.values():
                    timeline.sort(key=itemgetter("time_ns"))

    @staticmethod
    def _latest(stream, now):
        end = bisect_right(stream, now, key=itemgetter("time_ns"))
        return stream[end - 1] if end else None

    def demand(self, node, sid, now):
        return self._latest(self.services[node].get(sid, ()), now)

    def witness(self, node, since, now, ttl, *, online):
        ports = ((reporter, row["probe"], row["time_ns"])
                 for (_, reporter), stream in self.ports[node].items()
                 if (row := self._latest(stream, now)) is not None)
        service_sample = -1
        if not online:
            service_sample = max((row["message"]["observed_at_ns"] for stream in self.services[node].values()
                                  if (row := self._latest(stream, now)) is not None
                                  and 0 <= row["message"]["observed_at_ns"] <= row["time_ns"] <= now), default=-1)
        return port_witness(node, since, now, ttl, ports, online=online, last_service_sample=service_sample)


def verify_source_stops(plans, events, probe_index, demand_index, conditions):
    """依据最新已接受的网络送达记录，离线复核规划与下发阶段的停源授权。"""
    marked = [plan for plan in plans if plan.get("source_stop")]
    if not marked:
        return 0
    nodes = {plan["source_stop"][field] for plan in marked for field in ("source_node_id", "destination_node_id")}
    history = DeliveredNodeEvidence(nodes, probe_index, demand_index)
    snapshots = {row["work_id"]: row for row in events
                 if row["event"] == "b03_observation_evidence" and row.get("purpose") == "plan"}
    diagnoses = {row["work_id"]: row for row in events if row["event"] == "b03_om_diagnosis"}
    dispatch = defaultdict(list)
    for row in events:
        if row["event"] in ("release_authority_checked", "work_step"):
            dispatch[row["work_id"]].append(row)
    for plan in marked:
        wid, witness = plan["work_id"], plan["source_stop"]
        dx, snapshot = diagnoses[wid], snapshots[wid]
        state, sid = snapshot["state"], witness["service_id"]
        allowed = {key for key, row in dx["services"].items() if row["allowed"] and not row["protected"]}
        expected = choose_source_stop(state, allowed, dx["authority"]["frozenHealthyServiceIds"])
        if (snapshot["snapshot_id"] != plan["snapshot_id"] or expected != witness or dx.get("source_stop") != witness
                or sid not in conditions["authority"]["allowedServiceIds"]
                or sid in conditions["authority"].get("protectedServiceIds", [])
                or plan["authorized"] != [sid] or plan["release_ids"] != [sid] or plan["install_ids"]
                or plan["refresh_ids"] or plan["decisions"][sid]["accepted"]
                or plan["real_solver_calls"] or plan["tool_choice"]["modelCalled"]):
            raise ValueError("Invalid destination failure stop scope or observation")
        src, dst = witness["source_node_id"], witness["destination_node_id"]
        revision = plan["decisions"][sid]["demand_revision"]
        stages = [(state["now_ns"], witness)]
        checks = {row["time_ns"]: row for row in dispatch[wid] if row["event"] == "release_authority_checked"}
        for step in dispatch[wid]:
            if step["event"] != "work_step":
                continue
            check = checks.get(step["time_ns"])
            if (step["phase"] != "release" or step["service_id"] != sid or not check
                    or check["status"] != "destination_offline_source_stop"
                    or check["service_id"] != sid or check["demand_revision"] != revision
                    or any(check.get("source_stop", {}).get(key) != witness[key]
                           for key in ("service_id", "epoch", "source_node_id", "destination_node_id"))):
                raise ValueError("Dispatched source stop lacks independent release authority")
            stages.append((step["time_ns"], check["source_stop"]))
        ttl = conditions["simulation"]["control"]["observation_ttl_ns"]
        for now, permit in stages:
            down = history.witness(dst, -1, now, ttl, online=False)
            demand = history.demand(src, sid, now)
            msg = demand["message"] if demand else None
            if (not down or any(permit[key] != down[key] for key in ("link_id", "reporter_node_id", "sampled_at_ns"))
                    or history.witness(src, -1, now, ttl, online=False) or not msg
                    or not now - ttl <= msg["observed_at_ns"] <= demand["time_ns"] <= now
                    or not msg["active"] or not msg["registered"] or msg["revision"] != revision
                    or msg["service"]["src_node_id"] != src or msg["service"]["dst_node_id"] != dst):
                raise ValueError("Source stop contradicts the latest delivered source or neighbor evidence")
    return len(marked)
