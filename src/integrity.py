"""对分组工单轨迹生成封存校验清单、核验归档完整性，以及对照本地源码环境。"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re

from component_config import digest, work_order_components_digest
from provenance import SCHEMA, active_kinds, changed, checkout_python_files, model_assets, revision, verify_provenance
from scenario_manifest import native_source_files, read_custom_manifest, selected_channel
from scene import DEPENDENCY, PROFILE, ROOT, sha256, simulator_root, verify_engine, write_json


def source_identity():
    """保留完整的历史源码清单结构，确保旧工具与归档复核兼容。"""
    # 场景输入另有逐次运行证据；历史用例索引仅用于离线复查。
    knowledge = ROOT / "knowledge"
    shared_json = [p for p in knowledge.rglob("*.json")
                   if p.relative_to(knowledge).parts[0] not in {"scenarios", "cases"}]
    paths = [ROOT / "run.py", *(ROOT / "config").glob("*.json"),
             *shared_json, *checkout_python_files(ROOT)]
    if (ROOT / "episode.py").is_file():
        paths.append(ROOT / "episode.py")
    if (ROOT / "train.py").is_file():
        paths.append(ROOT / "train.py")
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(paths)}


def contract_for_mode(mode, version=3):
    family = "workorder" if mode == "workorder" else "run"
    return f"b03.packet.{family}.v{version}"


def receipt_format(conditions):
    pins = conditions.get("implementationSha256", {})
    mode = conditions.get("mode")
    contract = conditions.get("receiptContract")
    from b03_formulas import DECLARATION, IMPLEMENTATION
    proof_files = (conditions.get("provenance") or {}).get("checkoutFiles", {})
    markers = {DECLARATION, IMPLEMENTATION}
    has_formula = bool(markers.intersection(pins) or markers.intersection(proof_files) or "formulaBinding" in conditions)
    if has_formula or contract == contract_for_mode(mode):
        if (contract != contract_for_mode(mode) or not markers.issubset(pins) or not isinstance(conditions.get("formulaBinding"), dict)):
            raise ValueError("Missing or inconsistent formula contract for v3 producer")
        return "b03.packet.run-files.v3"
    if contract is None:
        if "src/episode_contract.py" in pins:
            raise ValueError("Receipt from a v2 producer has no contract marker")
        return "b03.packet.run-files.v1"
    if contract != contract_for_mode(mode, 2):
        raise ValueError("Unsupported contract for this receipt mode")
    return "b03.packet.run-files.v2"


def seal(run):
    files = {p.relative_to(run).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(run.rglob("*")) if p.is_file()}
    conditions = json.loads((run / "CONDITIONS.json").read_bytes())
    write_json(run / "RUN_FILES.json", {"schema": receipt_format(conditions), "files": files})


def check_members(run, conditions):
    record = json.loads((run / "RUN_FILES.json").read_bytes())
    if record.get("schema") != receipt_format(conditions):
        raise ValueError("Unknown packet evidence schema")
    files = record["files"]
    required = {"CONDITIONS.json", "SUMMARY.json", "b03.jsonl", "business-intervals.jsonl"}
    if conditions.get("sceneOrigin") == "custom":
        required.update({"scenario-manifest.json", "input/scenario-config.json", "native-experiment.json", "native-common.json"})
        if "channelModel" in conditions:
            required.update({"channel-model.json", "batches.jsonl"})
    else:
        required.update({"input/net-topo.json", "input/bg-service.json", f"input/scripts/script-{conditions['script']}.json"})
    optional = {"controller.jsonl", "executions.jsonl", "routes.jsonl", "packets-control.jsonl",
                "input/b03-demand-ends.json"}
    required.update(name for name in optional if (run / name).exists())
    from b03_formulas import MEMBER
    if record["schema"] == "b03.packet.run-files.v3":
        required.add(MEMBER)
    elif MEMBER in files or (run / MEMBER).exists():
        raise ValueError("Formula edition on a legacy receipt")
    if not required.issubset(files):
        raise ValueError("Evidence index is missing: " + ", ".join(sorted(required - files.keys())))
    root = run.resolve()
    for name, info in files.items():
        rel = Path(name)
        path = (root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Missing or invalid evidence member: " + name)
        if path.stat().st_size != info["bytes"] or sha256(path) != info["sha256"]:
            raise ValueError("Packet evidence digest mismatch: " + name)
    if conditions != json.loads((run / "CONDITIONS.json").read_bytes()):
        raise ValueError("Supplied conditions differ from the sealed CONDITIONS.json")
    return files


def _reject_channel_artifacts(run, conditions, files):
    if ("channelModel" in conditions or "channel-model.json" in files or (run / "channel-model.json").exists()
            or (run / "effective-input").exists() or any(k.startswith("effective-input/") for k in files)):
        raise ValueError("Channel artifacts require an explicitly selected custom channel profile")


def check_official_inputs(run, conditions, files):
    _reject_channel_artifacts(run, conditions, files)
    if ("scenario-manifest.json" in files or (run / "scenario-manifest.json").exists()
            or any(k in conditions for k in ("sceneContract", "manifestVersion", "manifestSha256", "sourceFiles", "serviceDefinitions", "knowledgePreset", "channelSelection"))):
        raise ValueError("Custom scenario metadata found in an official receipt")
    if (conditions["nodeCount"], conditions["linkCount"]) != (PROFILE["nodes"], PROFILE["links"]):
        raise ValueError("Run belongs to a different network scale")
    script = conditions["script"]
    if conditions["scene"] != PROFILE["officialScenario"] + "/script-" + script:
        raise ValueError("Unknown official scenario for this network scale")
    for name in ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json"):
        if (name not in PROFILE["pins"] or conditions["sourceHashes"].get(name) != PROFILE["pins"][name]
                or files["input/" + name]["sha256"] != PROFILE["pins"][name]):
            raise ValueError("Original official input identity mismatch: " + name)


def preset_binding(name, components, implementation):
    from scenario_manifest import preset_member
    member = preset_member(name)
    knowledge = components["knowledge"]
    return {"id": name, "file": member, "sha256": implementation[member],
            "componentManifestSha256": components["manifestSha256"],
            "knowledge": {key: knowledge[key] for key in ("id", "version", "sha256")}}


def check_custom_inputs(run, conditions, files):
    manifest = read_custom_manifest(run / "scenario-manifest.json")
    config = json.loads((run / "input/scenario-config.json").read_bytes())
    if (conditions.get("sceneContract") != manifest["schema"] or conditions.get("manifestVersion") != manifest["version"]
            or conditions.get("manifestSha256") != files["scenario-manifest.json"]["sha256"]
            or conditions["scene"] != "custom:" + config["scenario_id"] or conditions["script"] is not None
            or "officialDefinitions" in conditions or config.get("official_dataset") is True):
        raise ValueError("Custom scene identity differs from its archived manifest or native config")
    if conditions.get("sourceFiles") != config["files"]:
        raise ValueError("Custom input roles differ from the archived native config")
    preset = manifest.get("knowledgePreset")
    if preset or "knowledgePreset" in conditions:
        if not preset or conditions.get("knowledgePreset") != preset_binding(
                preset, conditions["components"], conditions["implementationSha256"]):
            raise ValueError("Knowledge preset differs from the archived scenario or effective components")
    native_config_hash = digest([files[name]["sha256"] for name in ("native-experiment.json", "native-common.json")])
    if native_config_hash != conditions["originalConfigSha256"]:
        raise ValueError("Custom native experiment or common settings differ from their recorded identity")
    pins = conditions["sourceHashes"]
    if set(pins) != set(native_source_files(config)):
        raise ValueError("Custom scene source inventory differs from its native config")
    for name, pin in pins.items():
        if files.get("input/" + name, {}).get("sha256") != pin:
            raise ValueError("Original custom input identity mismatch: " + name)
    control = conditions["simulation"]["control"]
    for key in ("controller_id", "control_primary_link", "control_backup_link"):
        if control.get(key) != manifest["control"].get(key):
            raise ValueError("Archived controller setting differs from custom manifest: " + key)
    selected = selected_channel(manifest, conditions.get("channelSelection"))
    expected_dir = "effective-input" if selected is not None else "input"
    experiment = json.loads((run / "native-experiment.json").read_bytes())
    if experiment.get("scenario_dir") != expected_dir:
        raise ValueError("Native experiment refers to the wrong channel input directory")
    if selected is None:
        _reject_channel_artifacts(run, conditions, files)
    else:
        from knowledge.channels.replay import check_channel_derivation
        check_channel_derivation(run, conditions, files, selected)


def check_inputs(run, conditions, files):
    origin = conditions.get("sceneOrigin", "official")
    if origin == "custom":
        check_custom_inputs(run, conditions, files)
    elif origin == "official":
        check_official_inputs(run, conditions, files)
    else:
        raise ValueError("Unknown archived scene origin")
    from simulator_component import archived_engine_lock
    archived_engine_lock(run, conditions, files, default_lock=DEPENDENCY)
    overlay = conditions.get("activityOverlay")
    member = "input/b03-demand-ends.json"
    if member in files or overlay is not None:
        if (member not in files or not isinstance(overlay, dict) or overlay.get("schema") != "b03.demand-ends.v1"
                or overlay != json.loads((run / member).read_bytes())):
            raise ValueError("Activity overlay differs between CONDITIONS.json and " + member)


def valid_pins(value):
    return (isinstance(value, dict) and bool(value)
            and all(isinstance(k, str) and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin)
                    for k, pin in value.items()))


def native_receipt(conditions):
    policy = conditions.get("rcPolicy") or {}
    if policy.get("kind") != "frozen-model":
        return
    weights = policy.get("checkpointFilesSha256")
    if (not valid_pins(weights) or
            policy.get("modelSpecSha256") != conditions["implementationSha256"].get("config/model.json")):
        raise ValueError("Invalid archived native model specification or weight identity")
    public = ROOT / "config/model.json"
    if policy["modelSpecSha256"] == sha256(public):
        spec = json.loads(public.read_bytes())
        if spec["format"] == "hf-safetensors":
            pins = spec["weightFilesSha256"]
        else:
            record = spec["nativeVersions"].get(str(policy["version"]))
            if not record:
                raise ValueError("Unknown frozen policy version in the registered native model")
            pins = {"training.pt": record["checkpointSha256"], "metadata.json": record["metadataSha256"]}
        if pins != weights:
            raise ValueError("Frozen policy weight hashes differ from its registered public model specification")


def check_recorded(conditions):
    components = conditions.get("components")
    proof = conditions.get("provenance")
    if not valid_pins(conditions.get("implementationSha256")):
        raise ValueError("Missing or invalid implementation file inventory")
    if components is not None:
        expected = digest({k: v for k, v in components.items() if k != "manifestSha256"})
        if components.get("manifestSha256") != expected:
            raise ValueError("Archived component manifest fingerprint differs")
    policy = conditions.get("rcPolicy") or {}
    if "workOrderComponentsSha256" in policy:
        if components is None or policy["workOrderComponentsSha256"] != work_order_components_digest(components):
            raise ValueError("Work-order component fingerprint differs from the archived components")
    native_receipt(conditions)
    if proof is None:
        return
    if components is None or proof.get("schema") != SCHEMA:
        raise ValueError("Unsupported component provenance schema")
    if proof.get("fingerprint") != digest({k: v for k, v in proof.items() if k != "fingerprint"}):
        raise ValueError("Archived provenance fingerprint differs")
    rc = conditions.get("rcPolicy") or {}
    policy = (rc["method"] if rc.get("kind") == "rule" else "model") if conditions["mode"] == "workorder" else conditions["mode"]
    if proof["policy"] != policy or proof["nativeFrozen"] != (rc.get("kind") == "frozen-model"):
        raise ValueError("Archived active component path differs from the run policy")
    selected = active_kinds(policy, components)
    if set(proof["providers"]) != set(selected):
        raise ValueError("The provenance provider inventory differs from the selected policy")
    for kind in selected:
        entry = proof["providers"][kind].get("entrySha256")
        if entry != components["providerModuleSha256"].get(kind):
            raise ValueError(f"Recorded provider entry hash differs for {kind}")
        reg = dict(components[kind])
        if "assets" in reg.get("provenance", {}):
            reg["provenance"] = {**reg["provenance"], "assets": sorted(reg["provenance"]["assets"])}
        if reg != proof["activeComponents"][kind]:
            raise ValueError(f"Recorded effective settings differ for {kind}")
    if active_kinds(policy) and proof["activeComponents"]["catalog"] != components["catalog"]:
        raise ValueError("Recorded action catalog differs")
    for name, pin in proof["checkoutFiles"].items():
        if name in conditions["implementationSha256"] and conditions["implementationSha256"][name] != pin:
            raise ValueError("Recorded checkout hash differs for " + name)
    knowledge = proof["knowledge"]
    if (any(knowledge[k] != components["knowledge"][k] for k in ("id", "version", "sha256"))
            or hashlib.sha256(knowledge["text"].encode("utf-8")).hexdigest() != knowledge["sha256"]):
        raise ValueError("Archived knowledge edition or original bytes differ")
    if proof["nativeFrozen"] and proof["modelAssets"].get("checkpoint") != rc["checkpointFilesSha256"]:
        raise ValueError("Archived native checkpoint differs from the validated inference receipt")


def legacy_source(conditions, components, checkpoint, tokenizer):
    """v1 旧版回执仅能核对当时原始记录下来的源码与提供方文件。"""
    differences = changed(conditions["implementationSha256"], source_identity(), "checkoutFiles")
    recorded = conditions.get("components")
    if recorded is not None:
        try:
            if components is None:
                from component_config import load_components
                components = load_components()
            differences.extend(changed(recorded, components.identity, "components"))
        except (ValueError, OSError, ImportError) as exc:
            differences.append(str(exc))
    for name, expected in conditions.get("packages", {}).items():
        try:
            differences.extend(changed(expected, metadata.version(name), "distributions." + name))
        except metadata.PackageNotFoundError:
            differences.append(f"distributions.{name}: missing")
    rc = conditions.get("rcPolicy") or {}
    if rc.get("kind") == "frozen-model":
        try:
            model_assets(ROOT, True, rc, checkpoint, tokenizer)
        except (ValueError, OSError, KeyError) as exc:
            differences.append(str(exc))
    return differences


def check_archive(run, conditions, *, strict_source=False, components=None, checkpoint=None, tokenizer=None, simulator=None):
    files = check_members(run, conditions)
    if "formulaBinding" in conditions:
        from b03_formulas import MEMBER, verify_binding
        verify_binding(conditions["formulaBinding"], (run / MEMBER).read_bytes(),
                       conditions["simulation"], conditions["implementationSha256"])
    check_inputs(run, conditions, files)
    from knowledge.network_replay import check_startup_facts
    check_startup_facts(run, conditions, files, installed_root=ROOT if strict_source else None)
    if "channelModel" in conditions:
        from knowledge.channels.replay import check_channel_batches
        check_channel_batches(run, conditions)
    check_recorded(conditions)
    proof = conditions.get("provenance")
    simulator_check = "release-lock-only"
    if strict_source:
        if "knowledgePreset" in conditions:
            from component_config import load_components
            from scenario_manifest import resolve_preset
            binding = conditions["knowledgePreset"]
            path = resolve_preset(ROOT, binding["id"])
            if sha256(path) != binding["sha256"]:
                raise ValueError("Installed knowledge preset differs from the archived version")
            selected = load_components(path).activate(conditions["provenance"]["policy"])
            if selected.identity["manifestSha256"] != binding["componentManifestSha256"]:
                raise ValueError("Effective components from the installed knowledge preset differ from this run")
            if components is None:
                components = selected
            elif components.activate(conditions["provenance"]["policy"]).identity["manifestSha256"] != binding["componentManifestSha256"]:
                raise ValueError("--components differs from the scenario knowledgePreset")
        if "channelModel" in conditions:
            from knowledge.channels.model import read_edition
            model = conditions["channelModel"]
            edition, _, _ = read_edition(ROOT / "knowledge/channels", model["id"])
            if hashlib.sha256(edition).hexdigest() != model["profileSha256"]:
                raise ValueError("Installed channel profile differs from the archived edition")
        engine = simulator or os.environ.get("B03_SIMULATOR")
        if engine or conditions.get("simulatorRuntime"):
            from simulator_component import archived_engine_lock
            lock = archived_engine_lock(run, conditions, files, default_lock=DEPENDENCY)
            if conditions.get("simulatorRuntime") or lock is not DEPENDENCY:
                actual = verify_engine(simulator_root(engine), lock)
            else:
                actual = verify_engine(simulator_root(engine))
            if actual != conditions["releaseManifestSha256"]:
                raise ValueError("Installed packet engine differs from the archived release")
            simulator_check = "installed-files"
        if proof:
            differences = verify_provenance(proof, ROOT, components, checkpoint=checkpoint, tokenizer=tokenizer,
                                            policy_identity=conditions.get("rcPolicy"))
        else:
            differences = legacy_source(conditions, components, checkpoint, tokenizer)
        if differences:
            message = "\n".join(differences[:30])
            if len(differences) > 30:
                message += f"\n… and {len(differences) - 30} further differences"
            raise ValueError("Strict source verification failed. Use the original checkout and environment.\n" + message)
    return {"mode": "strict-source" if strict_source else "archive",
            "receiptContract": conditions.get("receiptContract", "legacy-v1"), "archiveIndexSha256": sha256(run / "RUN_FILES.json"),
            "provenanceSchema": proof["schema"] if proof else "legacy", "sourceCompared": strict_source,
            "simulatorCheck": simulator_check, "recordedGitCommit": proof.get("gitCommit") if proof else None,
            "checkoutGitCommit": revision(ROOT) if strict_source else None}
