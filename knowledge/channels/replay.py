"""离线重算所选信道模型的折算结果，并与分组引擎实际记录的链路状态逐批比对。"""
import json

from .model import project_channel_inputs, validate_profile
from component_config import digest
from scenario_manifest import archive_input, parse_json_bytes


def check_channel_derivation(run, conditions, files, selected):
    """根据归档的信道版本和原始标称场景，重新推导出有效物理输入。"""
    model_file = "channel-model.json"
    if model_file not in files:
        raise ValueError("Channel profile is missing from the sealed run")
    profile = parse_json_bytes((run / model_file).read_bytes())
    validate_profile(profile, expected_id=selected)
    fields = ("id", "version", "model")
    bound = conditions.get("channelModel")
    hashes = {k.removeprefix("effective-input/"): v["sha256"] for k, v in files.items() if k.startswith("effective-input/")}
    if (set(hashes) != set(conditions["sourceHashes"])
            or bound != {**{k: profile[k] for k in fields}, "profileSha256": files[model_file]["sha256"],
                         "effectiveHashes": hashes}):
        raise ValueError("Channel identity or effective input inventory differs from the sealed files")
    def read_nominal(name):
        return parse_json_bytes((run / "input" / name).read_bytes())
    derived = project_channel_inputs(profile, conditions["sourceFiles"], read_nominal)
    for name, pin in hashes.items():
        if name in derived:
            actual = parse_json_bytes((run / "effective-input" / name).read_bytes())
            if digest(actual) != digest(derived[name]):
                raise ValueError("Effective input differs from channel replay: " + name)
        elif pin != conditions["sourceHashes"][name]:
            raise ValueError("Untransformed input differs between nominal and effective scenes: " + name)


def check_channel_batches(run, conditions):
    """将推导出的生效参数与分组引擎在每个事件批次后记录的链路状态逐项核对。"""
    from b03_formulas import seconds_to_ns
    topology = json.loads(archive_input(run, "topology", conditions, effective=True).read_bytes())
    events = json.loads(archive_input(run, "events", conditions, effective=True).read_bytes())
    fields = {"bandwidthBps": "capacity_bps", "bandwitdhBps": "capacity_bps", "delayMs": "delay_ms",
              "jitterMs": "jitter_ms", "ber": "ber", "packetLossRate": "packet_loss_rate"}
    state = {}
    for link in topology["links"]:
        values = {fields[k]: v for k, v in link["params"].items() if k in fields}
        # 仿真引擎构造时会把初始秒数重新换算回毫秒。
        for key in ("delay_ms", "jitter_ms"):
            values[key] = values[key] / 1000 * 1000
        state[link["linkId"]] = values
    end = seconds_to_ns(conditions["simulation"]["end_s"])
    pending = {0: []}  # 即使采用冷启动，原生时钟也会从 0 秒起回放开窗前的历史事件批次。
    for event in events:
        at = seconds_to_ns(event["time"])
        if event["eventType"] == "link_param_change" and at <= end:
            pending.setdefault(at, []).append(event)
    previous = -1
    with (run / "batches.jsonl").open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            batch = json.loads(line)
            at = batch["time_ns"]
            changes = pending.pop(at, [])
            executed = [op["payload"] for op in batch["operations"] if op["kind"] == "official_event"
                        and op["payload"]["eventType"] == "link_param_change"]
            if batch["sequence"] != index or not previous < at <= end or executed != changes:
                raise ValueError("Channel event sequence differs from the packet engine batch")
            for event in changes:
                attrs = event["attrs"]
                state[attrs["linkId"]].update({fields[k]: v for k, v in attrs.items() if k in fields})
            if batch["state"]["link_params"] != state:
                raise ValueError(f"Packet engine link parameters differ from the effective scene at {at} ns")
            previous = at
    if pending:
        raise ValueError("Missing packet engine batch for the initial state or a channel change")
