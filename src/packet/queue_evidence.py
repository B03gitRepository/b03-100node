"""基于封存的分组与控制器日志，离线重放已送达端口样本及队列拥堵滞回状态。"""
from bisect import bisect_left
from collections import defaultdict
from operator import attrgetter
from typing import NamedTuple


# 便携式归档核验无需导入原仿真器代码即可独立运行。
PEER_V2 = "peer_endpoint_v2"
BUNDLED_PROBE_SEMANTICS = {
    "910891e496a8c16e6b9ba134abab1e0cbd6b7dd95c31fbb5da7d3ad190b27e30": None,
    "ca1010c9be21e2927a4edd95be35f33cf5e737ca71817fa11ea99331e4b00c98": PEER_V2,
}


class PortSample(NamedTuple):
    arrived: int
    sampled: int
    seq: int
    depth: int
    pressure: bool
    reporter_online: bool
    admin_up: bool
    peer_online: bool


def _bind_deliveries(controller, packets, control_node):
    receipts = {}
    for row in controller:
        if row["event"] == "observation":
            uid = row["packet_uid"]
            if uid in receipts:
                raise ValueError("Duplicate controller probe receipt")
            receipts[uid] = row
    for packet in packets:
        if (packet["event"] != "delivered" or packet["message"].get("kind") != "probe"
                or packet["destination_node_id"] != control_node):
            continue
        row = receipts.pop(packet["packet_uid"], None)
        if (row is None or row["time_ns"] != packet["time_ns"] or row["probe"] != packet["message"]
                or (row["result"]["accepted"] and row["probe"]["node_id"] != packet["source_node_id"])):
            raise ValueError("Controller probe differs from the delivered network packet")
    if receipts:
        raise ValueError("Controller probe has no delivered network packet")


def _port_histories(controller, endpoints, ttl, limit):
    history = defaultdict(list)
    for row in controller:
        if row["event"] != "observation" or not row["result"]["accepted"]:
            continue
        probe = row["probe"]
        link = probe["link"]
        lid, reporter = link["link_id"], probe["node_id"]
        pair = endpoints.get(lid, ())
        if len(pair) != 2 or set(pair) != {reporter, link["peer_node_id"]}:
            raise ValueError("Probe is not from either endpoint of its reported link")
        sampled, arrived, seq, depth = probe["observed_at_ns"], row["time_ns"], probe["seq"], link["queue_packets"]
        flags = (probe["node_online"], link["admin_up"], link["endpoint_online"])
        if (any(type(x) is not int for x in (sampled, arrived, seq, depth)) or not 0 <= depth <= limit
                or seq < 0 or not 0 <= sampled <= arrived <= sampled + ttl or any(type(x) is not bool for x in flags)):
            raise ValueError("Invalid accepted probe timing, depth or endpoint state")
        stream = history[lid, reporter]
        pressure = False
        if stream:
            previous = stream[-1]
            if seq <= previous.seq or sampled < previous.sampled or arrived < previous.arrived:
                raise ValueError("Accepted probes are out of sequence")
            age = sampled - previous.sampled
            if age <= ttl and depth * 2 > limit:
                pressure = previous.pressure or (age > 0 and min(previous.depth, depth) * 4 >= limit * 3)
        stream.append(PortSample(arrived, sampled, seq, depth, pressure, *flags))
    return history


def _visible_sample(stream, ref, now):
    # 分组日志与控制器日志顺序独立：严格早于快照时刻送达的探针必须已入账，
    # 同纳秒送达的探针则可能落在控制节拍之前或之后。
    boundary = bisect_left(stream, now, key=attrgetter("arrived"))
    before = stream[boundary - 1] if boundary else None
    if ref is None:
        if before is not None:
            raise ValueError("Snapshot omitted a previously delivered queue sample")
        return None
    if before is not None and before.seq == ref["seq"]:
        return before
    while boundary < len(stream) and stream[boundary].arrived == now:
        if stream[boundary].seq == ref["seq"]:
            return stream[boundary]
        boundary += 1
    raise ValueError("Snapshot selected a superseded or undelivered queue sample")


def verify_queue_evidence(conditions, summary, controller, actions, endpoints, packets):
    engine = conditions["releaseManifestSha256"]
    control = summary.get("control") or {}
    semantics = control.get("probe_semantics")
    if engine in BUNDLED_PROBE_SEMANTICS and BUNDLED_PROBE_SEMANTICS[engine] != semantics:
        raise ValueError("Probe semantics differ from the pinned simulator release")
    if semantics != PEER_V2:
        return False

    config = conditions["simulation"]
    ttl, limit = config["control"]["observation_ttl_ns"], config["queue_packets"]
    if any(type(x) is not int or x <= 0 for x in (ttl, limit)):
        raise ValueError("Invalid pinned observation TTL or queue limit")
    _bind_deliveries(controller, packets, config["control"]["controller_id"])
    streams = _port_histories(controller, endpoints, ttl, limit)
    for row in actions:
        if row["event"] != "b03_observation_evidence":
            continue
        now, state = row["time_ns"], row["state"]
        if state["now_ns"] != now or state["link_ttl_ns"] != ttl or set(state["links"]) != set(endpoints):
            raise ValueError("Snapshot link inventory, clock or TTL differs from the episode")
        for lid, link in state["links"].items():
            pair = endpoints[lid]
            if (tuple(pair) != (link["src_node_id"], link["dst_node_id"]) or set(link["reports"]) != set(pair)):
                raise ValueError("Snapshot endpoint identities differ from the topology")
            fresh = []
            for node, ref in link["reports"].items():
                sample = _visible_sample(streams.get((lid, node), ()), ref, now)
                if sample is None:
                    continue
                valid = now - sample.sampled <= ttl
                if (ref["received_at_ns"] != sample.arrived or ref["sampled_at_ns"] != sample.sampled
                        or ref["status"] != ("fresh" if valid else "stale")
                        or type(ref.get("queue_packets")) is not int or ref["queue_packets"] != sample.depth
                        or ref["node_online"] is not sample.reporter_online or ref["admin_up"] is not sample.admin_up
                        or ref["endpoint_online"] is not sample.peer_online):
                    raise ValueError("Snapshot port depth, endpoint state or freshness differs from delivered probes")
                if valid:
                    fresh.append(sample)
            if fresh:
                if type(link.get("queue_packets")) is not int or link["queue_packets"] != max(s.depth for s in fresh):
                    raise ValueError("Aggregate queue depth differs from fresh delivered probes")
            elif "queue_packets" in link:
                raise ValueError("Queue depth claimed without a fresh delivered probe")
            if len(fresh) == 2:
                if link.get("queue_pressure") is not any(s.pressure for s in fresh):
                    raise ValueError("Reported queue pressure differs from per-port hysteresis")
            elif "queue_pressure" in link:
                raise ValueError("Queue pressure claimed without two fresh endpoints")
    return True
