"""Reconstruct the opening inventory from the sealed nominal inputs and native configuration."""
from component_config import digest
from scenario_manifest import archive_input, parse_json_bytes
from .network import build_foundation, check_resources, resolve_network


def _resources(settings):
    return check_resources({"queue_bytes": settings["queue_bytes"], "queue_packets": settings["queue_packets"],
                            "control_headroom_fraction": settings["control"]["control_headroom_fraction"]})


def _native_resources(run, files, conditions):
    names = ("native-experiment.json", "native-common.json")
    if any(name not in files for name in names):
        raise ValueError("Startup knowledge requires the selected native experiment and common settings")
    if digest([files[name]["sha256"] for name in names]) != conditions["originalConfigSha256"]:
        raise ValueError("Startup native configuration differs from its recorded configuration identity")
    experiment, common = [parse_json_bytes((run / name).read_bytes()) for name in names]
    cases = {case["case_id"]: case for case in experiment["cases"]}
    case = conditions.get("case", "packet_control")
    if case not in cases and conditions.get("sceneOrigin", "official") == "official":
        case = "official_control"  # Earlier installations also support the external bundled experiment.
    if case not in cases:
        raise ValueError("Startup experiment does not contain the selected case")
    merged = {}
    control = {}
    for options in (common["simulation"], experiment.get("simulation", {}), cases[case]):
        merged.update(options)
        control.update(options.get("control", {}))
    return _resources({**merged, "control": control})


def check_startup_facts(run, conditions, files, *, installed_root=None):
    binding = conditions.get("networkFoundation")
    facts_file, profile_file = "network-foundation.json", "network-profile.json"
    custom = conditions.get("sceneOrigin", "official") == "custom"
    if "foundationEnabled" in conditions and conditions["foundationEnabled"] is not (binding is not None):
        raise ValueError("Startup foundation selection differs from its recorded identity")
    if binding is None:
        if custom or any(name in files or (run / name).exists() for name in (facts_file, profile_file)):
            raise ValueError("Missing identity for the startup network knowledge")
        return
    if (not isinstance(binding, dict) or binding.get("file") != facts_file
            or facts_file not in files or profile_file not in files
            or binding.get("sha256") != files[facts_file]["sha256"]
            or binding.get("profileSha256") != files[profile_file]["sha256"]):
        raise ValueError("Startup network knowledge differs from its sealed files")
    profile = parse_json_bytes((run / profile_file).read_bytes())
    resources = _resources(conditions["simulation"])
    if (not isinstance(profile, dict) or set(profile) != {"resources", "nodeCapabilities"}
            or check_resources(profile["resources"]) != resources or _native_resources(run, files, conditions) != resources):
        raise ValueError("Startup profile differs from the native queue limits or control reservation")
    if not custom and profile["nodeCapabilities"] != {"*": {"maxInterfaces": None, "allowedLinkTypes": None}}:
        raise ValueError("External official scenes use observed connectivity without assumed hardware limits")
    channel = parse_json_bytes((run / "channel-model.json").read_bytes()) if "channelModel" in conditions else None
    topology = parse_json_bytes(archive_input(run, "topology", conditions).read_bytes())
    initial = parse_json_bytes(archive_input(run, "initial_services", conditions).read_bytes())
    expected = build_foundation(topology, initial, channel, resources, profile["nodeCapabilities"])
    if digest(expected) != digest(parse_json_bytes((run / facts_file).read_bytes())):
        raise ValueError("Opening network knowledge differs from the original physical inputs")
    origin, network_id = binding.get("origin"), binding.get("networkId")
    if origin == "external" and network_id is None and "manifest" not in binding:
        return
    if origin != "catalog" or not custom or not isinstance(network_id, str) or not network_id:
        raise ValueError("Invalid network catalog identity")
    name = binding.get("manifest")
    if not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in ("/", chr(92), ":")):
        raise ValueError("Invalid catalog manifest name")
    if installed_root is None:
        return
    from scene import sha256
    pack = resolve_network(installed_root, network_id)
    if pack["resources"] != resources or pack["nodeCapabilities"] != profile["nodeCapabilities"]:
        raise ValueError("Installed network resource or capability profile differs from this run")
    folder = pack["directory"]
    candidate = (folder / name).resolve(strict=True)
    if candidate.parent != folder or sha256(candidate) != conditions["manifestSha256"]:
        raise ValueError("Installed network manifest differs from this run")
    for name in conditions["sourceHashes"]:
        path = (folder / name).resolve(strict=True)
        if not path.is_relative_to(folder) or sha256(path) != files["input/" + name]["sha256"]:
            raise ValueError("Installed network input differs from the archived input: " + name)
