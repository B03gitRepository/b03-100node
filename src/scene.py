"""为锁定的分组仿真引擎统一准备官方场景或版本化自建场景输入。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

from scenario_manifest import archive_input, native_source_files, parse_json_bytes, read_custom_manifest, resolve_manifest_reference, selected_channel

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "config/profile.json").read_bytes())
DEPENDENCY = json.loads((ROOT / "config/simulator.lock.json").read_bytes())
# 首个内置引擎版本早于便携式引擎包元数据上线，此处做向后兼容。
PREVIOUS_ENGINE_SHA256 = "910891e496a8c16e6b9ba134abab1e0cbd6b7dd95c31fbb5da7d3ad190b27e30"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signed_inputs(raw, script):
    names = ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json")
    if set(raw) != set(names) or any(name not in PROFILE["pins"] for name in names):
        raise ValueError("This official script is outside the registered profile")
    for name in names:
        if hashlib.sha256(raw[name]).hexdigest() != PROFILE["pins"][name]:
            raise ValueError(f"Official input identity mismatch: {name}")


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def archive_network_foundation(output, network, config, source_info, *, manifest=None):
    """向 OM/RC 交付与构造仿真引擎完全一致的开局拓扑清单及生效资源上限。"""
    from knowledge.network import build_foundation
    resources = {"queue_packets": config["queue_packets"], "queue_bytes": config["queue_bytes"],
                 "control_headroom_fraction": config["control"]["control_headroom_fraction"]}
    profile = {"resources": resources, "nodeCapabilities": network["nodeCapabilities"]}
    topology = parse_json_bytes(archive_input(output, "topology", source_info).read_bytes())
    initial = parse_json_bytes(archive_input(output, "initial_services", source_info).read_bytes())
    channel = parse_json_bytes((output / "channel-model.json").read_bytes()) if "channelModel" in source_info else None
    foundation = build_foundation(topology, initial, channel, resources, profile["nodeCapabilities"])
    profile_path, foundation_path = output / "network-profile.json", output / "network-foundation.json"
    write_json(profile_path, profile)
    write_json(foundation_path, foundation)
    network_id = network.get("id")
    identity = {"file": foundation_path.name, "sha256": sha256(foundation_path),
                "networkId": network_id, "origin": "catalog" if network_id is not None else "external",
                "profileSha256": sha256(profile_path)}
    if network_id is not None and manifest is not None:
        identity["manifest"] = manifest.name
    return foundation, identity


def simulator_root(value=None):
    """按“命令行参数 > 环境变量 > 仓内内置引擎”的优先级定位仿真引擎目录。"""
    selected = value or os.environ.get("B03_SIMULATOR") or ROOT / "simulator/resnet_py"
    path = Path(selected).expanduser().resolve(strict=True)
    return path / "resnet_py" if (path / "resnet_py/src/resnet").is_dir() else path


def validate_engine_lock(lock):
    """无需导入仿真引擎，直接校验便携式引擎源码锁的文件清单与摘要。"""
    if not isinstance(lock, dict) or any(not isinstance(lock.get(k), str) or not lock[k].strip()
                                         for k in ("distribution", "version")):
        raise ValueError("The simulator lock requires a distribution and a version")
    files = lock.get("files")
    required = {"configs/common.json", "src/resnet/__init__.py", "src/resnet/simulation/simulation.py"}
    if not isinstance(files, dict) or not required <= files.keys():
        raise ValueError("The simulator lock omits required native configuration or packet source")
    for name, fingerprint in files.items():
        if not isinstance(name, str):
            raise ValueError("Simulator lock members require relative paths")
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or path.as_posix() != name or ".." in path.parts
                or chr(92) in name or ":" in name or not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint))):
            raise ValueError(f"Invalid simulator lock member: {name}")
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if lock.get("runtimeSha256") != hashlib.sha256(canonical).hexdigest():
        raise ValueError("Packet simulator fingerprint does not match its file table")
    identity = (lock["runtimeSha256"], lock["distribution"], lock["version"])
    if identity not in {(DEPENDENCY["runtimeSha256"], DEPENDENCY["distribution"], DEPENDENCY["version"]),
                        (PREVIOUS_ENGINE_SHA256, "resnet-simulation", "1.1.0")}:
        if "pyproject.toml" not in files:
            raise ValueError("A separately versioned engine must pin its pyproject.toml metadata")
    return lock


def verify_engine(path, lock=None):
    """核验声明的引擎文件字节；若指定自定义锁，一并盘点所有可导入源码文件。"""
    path = Path(path).resolve(strict=True)
    if lock is None:
        lock = DEPENDENCY  # 历史回执沿用当时代码仓已验收的引擎锁。
    else:
        validate_engine_lock(lock)
        source = path / "src"
        code_suffixes = {".py", ".pyc", ".pyd", ".so"}
        unpinned = sorted(p.relative_to(path).as_posix() for p in source.rglob("*")
                          if p.is_file() and p.suffix in code_suffixes
                          and "__pycache__" not in p.relative_to(source).parts
                          and p.relative_to(path).as_posix() not in lock["files"])
        if unpinned:
            raise ValueError("Unpinned simulator source: " + ", ".join(unpinned))
    for name, expected in lock["files"].items():
        member = (path / name).resolve()
        if not member.is_relative_to(path) or not member.is_file() or sha256(member) != expected:
            raise ValueError(f"Unexpected simulator source: {name}. Supply the selected packet release.")
    if "pyproject.toml" in lock["files"]:
        import tomllib
        with (path / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
        if (project.get("name"), project.get("version")) != (lock["distribution"], lock["version"]):
            raise ValueError("Installed engine metadata differs from its registered distribution/version")
    return lock["runtimeSha256"]


def official_bytes(value, script):
    if not re.fullmatch(r"[0-9]{2}", script):
        raise ValueError("Use a two-digit official script number")
    src = Path(value).expanduser().resolve(strict=True)
    names = ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json")
    prefix = "data/scenarios/" + PROFILE["officialScenario"]
    if src.is_dir():
        choices = [src, src / prefix, src / "scenarios" / PROFILE["officialScenario"]]
        folder = next((p for p in choices if (p / "net-topo.json").is_file()), None)
        if folder is None:
            raise ValueError("Supply the raw official scenario directory or the extracted dataset root")
        return {name: (folder / name).read_bytes() for name in names}
    expected = {prefix + "/" + name: name for name in names}
    found = {}
    with tarfile.open(src, "r:*") as bundle:
        for member in bundle:
            key = PurePosixPath(member.name).as_posix()
            if key not in expected:
                continue
            name = expected[key]
            if name in found or not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError(f"Unexpected official archive member: {name}")
            with bundle.extractfile(member) as handle:
                found[name] = handle.read()
    if set(found) != set(names):
        raise ValueError(f"Missing official archive members: {sorted(set(names) - set(found))}")
    return found


def has_bundled_data(engine, script):
    assets = DEPENDENCY.get("bundled20", {})
    return (PROFILE["nodes"] == 20 and script == "09" and bool(assets)
            and all((engine / name).is_file() for name in assets))


def _load_local(engine, output, control, *, input_folder="input", resources=None):
    """复用锁定引擎自身的配置合并、原生 schema 校验与 ScenarioSpec 构建流程。"""
    from resnet.application.config import load_config
    shutil.copyfile(engine / "configs/common.json", output / "native-common.json")
    # 在原生引擎校验配置之前，先注入本次选定的资源上限参数。
    options = {"control": dict(control)}
    if resources:
        options.update(queue_packets=resources["queue_packets"], queue_bytes=resources["queue_bytes"])
        options["control"]["control_headroom_fraction"] = resources["control_headroom_fraction"]
    selection = {"schema": "resnet-experiment-v1", "extends": "native-common.json", "scenario_dir": input_folder,
                 "simulation": options, "cases": [{"case_id": "packet_control", "mode":"control","start_s":0,"end_s":None}]}
    write_json(output / "native-experiment.json", selection)
    return load_config(output / "native-experiment.json", "packet_control")


def _prepare_channel(output, profile_id):
    """先校验原始原生场景合法性，再按所选信道模型生成交付引擎的实际输入。"""
    from knowledge.channels.model import project_channel_inputs, read_edition
    from resnet.io import load_scenario

    original = output / "input"
    raw = load_scenario(original)
    edition, profile, _ = read_edition(ROOT / "knowledge/channels", profile_id)
    (output / "channel-model.json").write_bytes(edition)
    roles = raw.raw_documents["scenario-config.json"]["files"]
    projected = project_channel_inputs(profile, roles, raw.raw_documents.__getitem__)
    effective = output / "effective-input"
    for name in raw.source_hashes:
        target = effective / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in projected:
            write_json(target, projected[name])
        else:
            shutil.copyfile(original / name, target)
    return effective.name


def _prepare_custom(engine, manifest_path, output, resources=None, channel_selection=None):
    root = Path(manifest_path).resolve(strict=True).parent
    archived = output / "scenario-manifest.json"
    shutil.copyfile(manifest_path, archived)
    manifest = read_custom_manifest(archived)
    native = root / "scenario-config.json"
    if not native.resolve(strict=True).is_relative_to(root):
        raise ValueError("Native scenario config is outside the manifest directory")
    raw = native.read_bytes()
    members = native_source_files(parse_json_bytes(raw))
    local = output / "input"
    local.mkdir()
    (local / "scenario-config.json").write_bytes(raw)
    for name in members[1:]:
        source = (root / name).resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(root):
            raise ValueError("Custom input is outside the manifest directory: " + name)
        target = local / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # 显式置空以关闭官方拓扑的平行链路默认合并逻辑。
    control = {"control_primary_link": None, "control_backup_link": None, **manifest["control"]}
    profile = selected_channel(manifest, channel_selection)
    folder = _prepare_channel(output, profile) if profile is not None else "input"
    scenario, config, _, identity = _load_local(engine, output, control, input_folder=folder, resources=resources)
    if manifest["window"]["end"] > scenario.duration_s:
        raise ValueError("Custom window ends after the scenario horizon")
    ids = {s.service_id for s in (*scenario.services, *scenario.added_services)}
    if manifest["services"] != "all" and set(manifest["services"]) - ids:
        raise ValueError("Custom manifest selects an unknown service")
    if control["control_primary_link"] is not None:
        endpoints = {l.link_id: {l.src_node_id, l.dst_node_id} for l in scenario.links}
        primary, backup = control["control_primary_link"], control["control_backup_link"]
        if primary not in endpoints or endpoints[primary] != endpoints.get(backup):
            raise ValueError("Custom control links must exist and join the same two nodes")
    return scenario, config, identity, local


def prepare(engine, data, script, output, *, manifest_path=None, resources=None, channel_selection=None,
            archive_bundled_config=False):
    """在推进任何分组事件之前，将归档输入字节与当前输入契约完成绑定校验。"""
    from resnet.application.config import load_config
    if manifest_path is not None:
        if data is not None or script is not None:
            raise ValueError("Select a custom manifest or an official script")
        manifest_path = resolve_manifest_reference(ROOT, manifest_path)
        if resources is None:
            from knowledge.network import resolve_external
            resources = resolve_external(ROOT, manifest_path)["resources"]
        return _prepare_custom(engine, manifest_path, output, resources, channel_selection)
    if channel_selection is not None:
        raise ValueError("Channel overrides require a custom scene")
    if data is None:
        if resources:
            raise ValueError("An explicit network resource profile requires a selected scenario directory")
        if not has_bundled_data(engine, script):
            raise ValueError("Supply --data PATH or set B03_DATA to the official scenario folder or dataset archive.")
        scenario_dir = engine / "data/OFFICIAL20-script09"
        for name, expected in DEPENDENCY["bundled20"].items():
            if sha256(engine / name) != expected:
                raise ValueError(f"Unexpected bundled official input: {name}")
        scenario, config, _, identity = load_config(engine / "configs/official20.json", "official_control")
        if archive_bundled_config:
            shutil.copyfile(engine / "configs/common.json", output / "native-common.json")
            shutil.copyfile(engine / "configs/official20.json", output / "native-experiment.json")
        # 离线审计始终使用实际驱动本次运行的原始三份输入文件。
        raw = official_bytes(scenario_dir, script)
        signed_inputs(raw, script)
        local = output / "input"
        local.mkdir()
        (local / "scripts").mkdir()
        for name, blob in raw.items():
            (local / name).write_bytes(blob)
        for n in ("scenario-config.json", "runtime-traffic.json", "provenance.json"):
            shutil.copyfile(scenario_dir / n, local / n)
    else:
        raw = official_bytes(data, script)
        signed_inputs(raw, script)
        local = output / "input"
        local.mkdir()
        (local / "scripts").mkdir()
        for name, blob in raw.items():
            (local / name).write_bytes(blob)
        topology = json.loads(raw["net-topo.json"])
        if len(topology["nodes"]) != PROFILE["nodes"]:
            raise ValueError(f"Expected {PROFILE['nodes']} physical nodes, found {len(topology['nodes'])}")
        initial = json.loads(raw["bg-service.json"])
        events = json.loads(raw[f"scripts/script-{script}.json"])
        duration = PROFILE["horizonSeconds"]
        if any(row.get("startAtSec") != 0 for row in initial):
            raise ValueError("This official activity profile requires all initial demands to start at zero")
        if any(row["time"] > duration for row in events):
            raise ValueError("The scenario horizon ends before an official event")
        additions = [{"service_id": row["attrs"]["serviceId"], "start_s": row["time"], "end_s": duration}
                     for row in events if row["eventType"] == "service_adjust" and row["attrs"].get("op") == "add"]
        traffic = {"schema": "project-traffic-activity-v1", "project_extension": True,
                   "definition": "Original initial requests are active from zero. Added requests start at their official event; the experiment horizon is explicit.",
                   "realtime_types": ["voice", "video"], "profiles": {"official_all": {"label": "full demand", "active_service_ids": [s["serviceId"] for s in initial]}},
                   "profile_windows": [{"start_s": 0, "end_s": duration, "profile": "official_all"}], "temporary_windows": additions}
        write_json(local / "runtime-traffic.json", traffic)
        write_json(local / "scenario-config.json", {"scenario_id": PROFILE["officialScenario"] + "/script-" + script,
                   "source_scenario": PROFILE["officialScenario"], "official_dataset": True, "duration_s": duration,
                   "input_mode": "official_script", "movement_semantics": "endpoint_at_event",
                   "files": {"topology": "net-topo.json", "initial_services": "bg-service.json",
                             "events": f"scripts/script-{script}.json", "traffic": "runtime-traffic.json"}})
        scenario, config, _, identity = _load_local(engine, output, PROFILE["control"], resources=resources)
    if len(scenario.nodes) != PROFILE["nodes"] or len(scenario.links) != PROFILE["links"]:
        raise ValueError(f"Wrong physical topology: {len(scenario.nodes)} nodes and {len(scenario.links)} links")
    return scenario, config, identity, local
