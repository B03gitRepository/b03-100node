"""解析实验场景清单，定位属于该场景的全部输入与参考文件。"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

CUSTOM_SCHEMA = "b03.custom-scenario.v1"


def check_channel_id(key):
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key):
        raise ValueError("channelProfile must be a catalog ID of up to 64 lowercase letters, digits, '-' or '_'")



def selected_channel(manifest, override=None):
    """解析清单声明的默认信道版本，或命令行显式指定的覆盖版本（同步写入回执）。"""
    if override is None:
        return manifest.get("channelProfile")
    if not isinstance(override, dict) or set(override) != {"source", "profile"} or override["source"] != "cli":
        raise ValueError("Invalid channel selection override")
    profile = override["profile"]
    if profile is not None:
        check_channel_id(profile)
    return profile


def preset_member(name):
    """仅允许通过已登记的预设编号选取项目内置知识配置。"""
    if not isinstance(name, str) or not re.fullmatch(r"knowledge-[a-z0-9][a-z0-9_-]{0,53}", name):
        raise ValueError("knowledgePreset must be a local knowledge-* preset ID")
    return f"config/{name}.json"


def resolve_preset(root, name, explicit=None):
    member = root / preset_member(name)
    if member.resolve(strict=True) != member or not member.is_file():
        raise ValueError("knowledgePreset must select a regular file in the project config directory")
    if explicit is not None and explicit.resolve(strict=True) != member:
        raise ValueError("--components conflicts with the scenario knowledgePreset; select the same preset or remove knowledgePreset")
    return member


def parse_json_bytes(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate custom scenario field: " + key)
            value[key] = item
        return value
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)


def read_custom_manifest(path):
    """读取自建场景的默认实验参数；物理节点、流量与事件仍存放在原生场景文件中。"""
    return parse_custom_manifest(Path(path).read_bytes())


def resolve_manifest_reference(project_root, path):
    """兼容解析旧示例清单路径中使用的简写文件引用。"""
    path = Path(path).resolve(strict=True)
    obj = parse_json_bytes(path.read_bytes())
    if not isinstance(obj, dict) or obj.get("schema") != "b03.network-reference.v1":
        return path
    from knowledge.network import resolve_network
    name = obj.get("manifest")
    if (set(obj) != {"schema", "network", "manifest"} or not isinstance(obj["network"], str)
            or not isinstance(name, str) or not re.fullmatch(r"manifest(?:-[a-z0-9_-]+)?\.json", name)):
        raise ValueError("Invalid network reference; expected a catalog ID and a manifest filename")
    pack = resolve_network(project_root, obj["network"])
    folder = pack["directory"].resolve(strict=True)
    target = (folder / name).resolve(strict=True)
    if pack["origin"] != "custom" or target.parent != folder or not target.is_file():
        raise ValueError("Network reference must select a manifest in the custom network pack")
    return target


def parse_custom_manifest(raw):
    manifest = parse_json_bytes(raw)
    required = {"schema", "version", "control", "window", "services"}
    if (not isinstance(manifest, dict) or set(manifest) - {"channelProfile", "knowledgePreset"} != required
            or manifest["schema"] != CUSTOM_SCHEMA or not isinstance(manifest["version"], str)
            or not manifest["version"].strip()):
        raise ValueError("Expected a versioned b03.custom-scenario.v1 manifest")
    if "channelProfile" in manifest:
        check_channel_id(manifest["channelProfile"])
    if "knowledgePreset" in manifest:
        preset_member(manifest["knowledgePreset"])
    control = manifest["control"]
    if (not isinstance(control, dict) or set(control) - {"controller_id", "control_primary_link", "control_backup_link"}
            or not isinstance(control.get("controller_id"), str) or not control["controller_id"].strip()):
        raise ValueError("Custom control requires a controller_id and optional parallel control links")
    primary, backup = control.get("control_primary_link"), control.get("control_backup_link")
    if (primary, backup) != (None, None) and (
            not all(isinstance(x, str) and x.strip() for x in (primary, backup)) or primary == backup):
        raise ValueError("Specify two distinct control links or omit both")
    window = manifest["window"]
    if (not isinstance(window, dict) or set(window) != {"start", "end", "drain"}
            or any(type(t) is not int or t < 0 for t in window.values()) or window["start"] >= window["end"]):
        raise ValueError("Custom window requires integer seconds: 0 <= start < end and drain >= 0")
    services = manifest["services"]
    if services != "all" and (not isinstance(services, list) or not services
                              or not all(isinstance(s, str) and s.strip() for s in services)
                              or len(services) != len(set(services))):
        raise ValueError("Custom services must be 'all' or a nonempty list of distinct service IDs")
    return manifest


def native_source_files(config):
    """返回随自建运行一起归档的原生输入文件清单（含可选参考时间线）。"""
    if not isinstance(config, dict) or config.get("official_dataset", False) is not False:
        raise ValueError("A custom scenario cannot declare official dataset identity")
    files = config.get("files")
    required = {"topology", "initial_services", "events", "traffic"}
    references = config.get("reference_files", [])
    if (not isinstance(files, dict) or not required <= files.keys()
            or files.keys() - required - {"validation_timeline"} or not isinstance(references, list)):
        raise ValueError("Invalid native custom scenario file map")
    names = ["scenario-config.json"]
    seen = {names[0].casefold(): names[0]}
    for i, name in enumerate([*files.values(), *references]):
        if not isinstance(name, str):
            raise ValueError("Custom input names must be relative paths")
        rel = PurePosixPath(name)
        if (not rel.parts or rel.is_absolute() or rel.as_posix() != name or ".." in rel.parts
                or any(c in name for c in ('\\', ':', '\x00')) or name.casefold() == "b03-demand-ends.json"
                or any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in rel.parts)):
            raise ValueError("Invalid custom input path: " + repr(name))
        key = name.casefold()
        if key in seen:
            if seen[key] != name:
                raise ValueError("Custom input names collide on Windows: " + name)
            if i < len(files):
                raise ValueError("Each native file role needs a distinct path: " + name)
            continue
        seen[key] = name
        names.append(name)
    return names


def archive_input(directory, role, conditions, *, effective=False):
    """按文件角色定位归档中的原生输入，兼容未记录角色映射的旧版官方回执。"""
    directory = Path(directory)
    origin = conditions.get("sceneOrigin", "official")
    if origin == "custom":
        names = conditions.get("sourceFiles") or {}
    elif origin == "official":
        names = {"topology": "net-topo.json", "initial_services": "bg-service.json",
                 "traffic": "runtime-traffic.json"}
        if role == "events":
            names["events"] = f"scripts/script-{conditions['script']}.json"
    else:
        raise ValueError("Unknown archived scene origin")
    name = names.get(role)
    if not isinstance(name, str) or not name or PureWindowsPath(name).anchor or ".." in PureWindowsPath(name).parts:
        raise ValueError("Missing or invalid archived input for role: " + role)
    folder = "effective-input" if effective and origin == "custom" and "channelModel" in conditions else "input"
    root = (directory / folder).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not root.is_relative_to(directory.resolve()):
        raise ValueError("Archived input is outside its run directory: " + name)
    return path
