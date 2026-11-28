"""将标称链路配置按信道模型折算为分组仿真引擎使用的五项物理参数。"""
from copy import deepcopy
import math
from pathlib import Path

from scenario_manifest import check_channel_id, parse_json_bytes


FIELDS = {"signalDbm", "noiseDbm", "spectrumHz", "efficiency", "extraDelayMs", "extraLossProbability"}
BANDWIDTH_KEYS = ("bandwidthBps", "bandwitdhBps")  # 原生输入契约中两种带宽字段拼写均可能出现。


def read_edition(catalog, key):
    """读取信道版本的原始字节、参数设置和链路容量上限，供查询或运行归档使用。"""
    check_channel_id(key)
    root = Path(catalog).resolve(strict=True)
    path = (root / (key + ".json")).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Channel profile resolves outside the channel catalog")
    raw = path.read_bytes()
    profile = parse_json_bytes(raw)
    ceilings = validate_profile(profile, expected_id=key)
    return raw, profile, ceilings


def _finite(value, name):
    try:
        valid = type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number")
    return value


def validate_profile(profile, expected_id=None):
    """校验信道版本配置，并按香农公式计算各类链路的容量上限（单位：bit/s）。"""
    keys = {"schema", "id", "version", "model", "linkTypes"}
    if (not isinstance(profile, dict) or set(profile) != keys
            or profile["schema"] != "b03.channel-model.v1" or profile["model"] != "noise-cap.v1"):
        raise ValueError("Expected a b03.channel-model.v1 profile using noise-cap.v1")
    if any(not isinstance(profile[k], str) or not profile[k].strip() for k in ("id", "version")):
        raise ValueError("Channel id and version must be nonempty strings")
    if expected_id is not None and expected_id != profile["id"]:
        raise ValueError("Selected channel id differs from the archived profile")
    models = profile["linkTypes"]
    if not isinstance(models, dict) or not models:
        raise ValueError("Channel linkTypes must be a nonempty mapping")
    ceilings = {}
    for kind, cfg in models.items():
        if not isinstance(kind, str) or not kind.strip() or not isinstance(cfg, dict) or set(cfg) != FIELDS:
            raise ValueError(f"Expected six channel parameters for link type {kind!r}")
        for key, value in cfg.items():
            _finite(value, key)
        if (cfg["spectrumHz"] <= 0 or not 0 < cfg["efficiency"] <= 1 or cfg["extraDelayMs"] < 0
                or not 0 <= cfg["extraLossProbability"] <= 1):
            raise ValueError(f"Invalid spectrum, efficiency, delay or loss for {kind}")
        # 信号与噪声功率均为同一接收带宽下的总功率；在对数域计算 log2(1 + S/N)，
        # 防止信噪比差值很大时直接求 exp() 发生浮点溢出。
        log_ratio = (float(cfg["signalDbm"]) - float(cfg["noiseDbm"])) * (math.log(10) / 10)
        bits_per_hz = (max(log_ratio, 0) + math.log1p(math.exp(-abs(log_ratio)))) / math.log(2)
        capacity = cfg["efficiency"] * float(cfg["spectrumHz"]) * bits_per_hz
        if not math.isfinite(capacity) or capacity < 1:
            raise ValueError(f"Channel capacity for {kind} must support at least one bit/s and be finite")
        ceilings[kind] = math.floor(capacity)
    return ceilings


def _project(fields, cfg, ceiling):
    """仅就地折算当前拓扑行或事件中实际给出的标称字段。"""
    rates = [fields[key] for key in BANDWIDTH_KEYS if key in fields]
    if rates and (any(type(v) is not int or v <= 0 for v in rates) or rates[0] != rates[-1]):
        raise ValueError("Nominal bandwidth must be a positive integer and aliases must agree")
    for key in BANDWIDTH_KEYS:
        if key in fields:
            fields[key] = min(fields[key], ceiling)
    if "delayMs" in fields:
        delay = _finite(fields["delayMs"], "delayMs")
        if delay < 0:
            raise ValueError("Nominal delayMs must be nonnegative")
        fields["delayMs"] = _finite(float(delay) + float(cfg["extraDelayMs"]), "Effective delayMs")
    if "packetLossRate" in fields:
        base = _finite(fields["packetLossRate"], "packetLossRate")
        if not 0 <= base <= 1:
            raise ValueError("Nominal packetLossRate must be between zero and one")
        # 只有未在标称丢包中丢失的分组，才会继续经受额外信道丢包。
        fields["packetLossRate"] = base + (1 - base) * cfg["extraLossProbability"]


def derive_inputs(profile, topology, events):
    """生成独立的引擎原生输入文件；业务 QoS、抖动、误码率及事件顺序原样透传。"""
    ceilings = validate_profile(profile)
    effective_topology, effective_events = deepcopy(topology), deepcopy(events)
    targeted = {}
    for link in effective_topology["links"]:
        kind = link["linkType"]
        if kind in ceilings:
            targeted[link["linkId"]] = kind
            _project(link["params"], profile["linkTypes"][kind], ceilings[kind])
    if not targeted:
        raise ValueError("No topology link matches a type in the selected channel profile")
    for event in effective_events:
        if event["eventType"] == "link_param_change":
            fields = event["attrs"]
            kind = targeted.get(fields["linkId"])
            if kind is not None:
                # 事件中变更的字段均为绝对值；未列出的字段由仿真引擎保持原值。
                _project(fields, profile["linkTypes"][kind], ceilings[kind])
    return effective_topology, effective_events


def derive_timeline(timeline, effective_events):
    """按加载器的事件编号和原始顺序，同步更新可选的原生批次对照时间线。"""
    result = deepcopy(timeline)
    links = {f"E{i:02d}": row for i, row in enumerate(effective_events, 1)
             if row["eventType"] == "link_param_change"}
    for batch in result["batches"]:
        for op in batch["operations"]:
            if op["kind"] == "official_event" and op["payload"]["eventType"] == "link_param_change":
                op["payload"] = deepcopy(links[op["event_id"]])
    return result


def project_channel_inputs(profile, role_files, read_document):
    """折算物理场景输入；若附带对照时间线则一并折算。"""
    topo, events = role_files["topology"], role_files["events"]
    topology, projected_events = derive_inputs(profile, read_document(topo), read_document(events))
    documents = {topo: topology, events: projected_events}
    timeline = role_files.get("validation_timeline")
    if timeline:
        documents[timeline] = derive_timeline(read_document(timeline), projected_events)
    return documents
