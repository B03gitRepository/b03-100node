"""选择版本化场景网络，并构建交付给 OM/RC 的开局静态底图。"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import math
from pathlib import Path
import re

from scenario_manifest import parse_json_bytes
from .channels.model import derive_inputs


_RESOURCE_FIELDS = {"queue_packets", "queue_bytes", "control_headroom_fraction"}
_QOS_FIELDS = ("delayMs", "jitterMs", "ber", "packetLossRate")
_UNDECLARED = {"allowedLinkTypes": None, "maxInterfaces": None}


def check_resources(resources):
    """校验这三项公开资源上限，并同步传给底层分组仿真引擎。"""
    if not isinstance(resources, dict) or set(resources) != _RESOURCE_FIELDS:
        raise ValueError("Network resources require queue_packets, queue_bytes and control_headroom_fraction")
    for name in ("queue_packets", "queue_bytes"):
        if type(resources[name]) is not int or resources[name] < 1:
            raise ValueError(f"Network resource {name} must be a positive integer")
    fraction = resources["control_headroom_fraction"]
    if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction < 1:
        raise ValueError("Network control_headroom_fraction must be in [0, 1)")
    return dict(resources)


def _capabilities(profiles, *, declared):
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Declare nodeCapabilities by node type")
    for node_type, row in profiles.items():
        if not isinstance(node_type, str) or not node_type.strip() or (declared and node_type == "*"):
            raise ValueError("Declare capabilities under the scenario node type")
        if not isinstance(row, dict) or set(row) != set(_UNDECLARED):
            raise ValueError("Node capabilities require allowedLinkTypes and maxInterfaces")
        allowed, limit = row["allowedLinkTypes"], row["maxInterfaces"]
        if allowed is not None and (not isinstance(allowed, list)
                or not all(isinstance(v, str) and v.strip() for v in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValueError("allowedLinkTypes must be a list of distinct nonempty link types or null")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("maxInterfaces must be a nonnegative integer or null")
    return profiles


def _check_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Network ID must be one scenario folder name")


def _load_custom_profile(directory):
    member = (directory / "network.json").resolve(strict=True)
    if member.parent != directory or not member.is_file():
        raise ValueError("Network profile must be a file in the scenario directory")
    spec = parse_json_bytes(member.read_bytes())
    fields = {"schema", "id", "origin", "resources", "nodeCapabilities"}
    if (not isinstance(spec, dict) or set(spec) != fields or spec["schema"] != "b03.network-pack.v1"
            or spec["origin"] != "custom"):
        raise ValueError("A custom network profile requires a b03.network-pack.v1 declaration")
    _check_id(spec["id"])
    check_resources(spec["resources"])
    _capabilities(spec["nodeCapabilities"], declared=True)
    return spec


def resolve_network(project_root, network_id):
    """只加载调用方指定的仓内合成网络；官方场景数据始终保留在代码仓外。"""
    _check_id(network_id)
    catalog = (Path(project_root) / "knowledge/scenarios").resolve(strict=True)
    directory = (catalog / network_id).resolve(strict=True)
    if directory.parent != catalog:
        raise ValueError("Network pack resolves outside the selected scenario catalog")
    spec = _load_custom_profile(directory)
    if spec["id"] != network_id:
        raise ValueError("Network pack ID must match its catalog folder")
    manifest = (directory / "manifest.json").resolve(strict=True)
    if manifest.parent != directory or not manifest.is_file():
        raise ValueError("Network manifest must be a file inside the selected pack")
    return dict(spec, directory=directory, manifest=manifest)


def resolve_external(project_root, manifest=None):
    """仅自建场景清单可声明节点硬件能力上限；外部官方场景只记录实测接口数。"""
    if manifest is not None:
        directory = Path(manifest).resolve(strict=True).parent
        catalog = (Path(project_root) / "knowledge/scenarios").resolve()
        if directory.parent == catalog:
            return resolve_network(project_root, directory.name)
        if (directory / "network.json").is_file():
            spec = _load_custom_profile(directory)
            return {key: spec[key] for key in ("resources", "nodeCapabilities")}
    return {"resources": {}, "nodeCapabilities": {"*": dict(_UNDECLARED)}}


def _qos(fields):
    """保留官方五项 QoS 字段，同时兼容旧版带宽字段拼写。"""
    capacity = fields["bandwidthBps"] if "bandwidthBps" in fields else fields["bandwitdhBps"]
    return {"bandwidthBps": capacity, **{key: fields[key] for key in _QOS_FIELDS}}


def build_foundation(topology, initial_services, channel_profile, resources, node_capabilities):
    """仅基于 0 秒开局配置生成静态底图，严禁读取后续事件脚本、活跃列表或仿真内部状态。"""
    constraints = check_resources(resources)
    profiles = _capabilities(node_capabilities, declared=False)
    effective = topology
    if channel_profile is not None:
        effective, _ = derive_inputs(channel_profile, topology, [])
    links = {}
    attached = defaultdict(set)
    interfaces = Counter()
    for raw, actual in zip(topology["links"], effective["links"], strict=True):
        lid, kind = raw["linkId"], raw["linkType"]
        links[lid] = {"srcNodeId": raw["srcNodeId"], "dstNodeId": raw["dstNodeId"], "linkType": kind,
                      "params": _qos(raw["params"]), "effectiveParams": _qos(actual["params"])}
        for endpoint in (raw["srcNodeId"], raw["dstNodeId"]):
            attached[endpoint].add(kind)
            interfaces[endpoint] += 1

    nodes = {}
    for raw in topology["nodes"]:
        nid, kind = raw["nodeId"], raw["nodeType"]
        capability = profiles.get(kind, profiles.get("*"))
        if capability is None:
            raise ValueError(f"Missing nodeCapabilities for type {kind!r} at node {nid}")
        allowed, limit = capability["allowedLinkTypes"], capability["maxInterfaces"]
        if allowed is not None and not attached[nid].issubset(allowed):
            raise ValueError(f"Link type exceeds declared node capabilities at {nid}")
        if limit is not None and interfaces[nid] > limit:
            raise ValueError(f"Topology requires {interfaces[nid]} interfaces at {nid}, limit is {limit}")
        nodes[nid] = {"nodeType": kind, "capabilities": deepcopy(capability),
                      "attachedLinkTypes": sorted(attached[nid]), "interfaceCount": interfaces[nid]}
        # 将声明的开局初始坐标与后续动态位置上报分开存放。
        for field in ("ip", "mask"):
            if field in raw:
                nodes[nid][field] = raw[field]
        if "position" in raw:
            nodes[nid]["initialPosition"] = {axis: raw["position"][axis] for axis in ("x", "y")}

    services = {}
    for raw in initial_services:
        if raw.get("startAtSec") != 0:
            continue
        services[raw["serviceId"]] = {"srcNodeId": raw["srcNodeId"], "dstNodeId": raw["dstNodeId"],
                                      "serviceType": raw["serviceType"], "category": raw.get("category"),
                                      "priority": raw["priority"], "requirement": _qos(raw["requirement"])}
    return {"schema": "b03.network-foundation.v1", "nodes": nodes, "links": links,
            "initialServices": services, "resources": constraints,
            "channelModel": deepcopy(channel_profile) if channel_profile is not None
                            else {"model": "scenario-link-parameters"}}
