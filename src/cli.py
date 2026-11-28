"""Run and inspect official or custom scenarios with the pinned packet simulator."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

from scenario_manifest import parse_custom_manifest, read_custom_manifest, resolve_manifest_reference, resolve_preset, selected_channel
from scene import PROFILE, ROOT, has_bundled_data, prepare, sha256, simulator_root, verify_engine, write_json as json_file


def _network_source(args, parser):
    """Opt into a custom pack; otherwise preserve the official profile and B03_DATA precedence."""
    from knowledge.network import resolve_external, resolve_network
    if args.script is not None and (args.network is not None or args.scenario is not None):
        parser.error("--script selects an official script and cannot be combined with --network or --scenario")
    if args.network is not None:
        network = resolve_network(ROOT, args.network)
        args.scenario = network["manifest"]
        return network
    if args.scenario is not None:
        args.scenario = resolve_manifest_reference(ROOT, args.scenario)
    return resolve_external(ROOT, args.scenario)


def _select_scene(args, parser, engine):
    """Resolve the input source. B03_DATA supplies the default for official runs."""
    if args.scenario is not None:
        return args.scenario.resolve(strict=True).parent
    if args.script is None:
        args.script = PROFILE["script"]
    if f"scripts/script-{args.script}.json" not in PROFILE["pins"]:
        parser.error("Choose a registered official script from 01 through 10")
    if args.data is None and os.environ.get("B03_DATA"):
        args.data = Path(os.environ["B03_DATA"])
    if args.data is None and not has_bundled_data(engine, args.script):
        parser.error("Supply --data PATH or set B03_DATA to the official scenario folder or dataset archive.")
    return args.data.resolve() if args.data and args.data.is_dir() else None


def _run_options(args, parser, scenario, out):
    """Apply command-line overrides to the defaults from the archived scenario."""
    source_info = {"scene": scenario.scenario_id, "script": args.script, "sourceHashes": scenario.source_hashes}
    window, selection = PROFILE, PROFILE["exampleServices"]
    if args.scenario is not None:
        archived = out / "scenario-manifest.json"
        manifest = read_custom_manifest(archived)
        window, selection = manifest["window"], manifest["services"]
        source_info.update(scene="custom:" + scenario.scenario_id, sceneOrigin="custom",
                           sceneContract=manifest["schema"], manifestVersion=manifest["version"],
                           manifestSha256=sha256(archived), sourceFiles=scenario.raw_documents["scenario-config.json"]["files"])
        override = getattr(args, "channel_selection", None)
        if override is not None:
            source_info["channelSelection"] = override
        if selected_channel(manifest, override) is not None:
            model_file = out / "channel-model.json"
            profile = json.loads(model_file.read_bytes())
            source_info["sourceHashes"] = {name: sha256(out / "input" / name) for name in scenario.source_hashes}
            source_info["channelModel"] = {"id": profile["id"], "version": profile["version"], "model": profile["model"],
                                           "profileSha256": sha256(model_file), "effectiveHashes": scenario.source_hashes}
    args.start = window["start"] if args.start is None else args.start
    args.end = window["end"] if args.end is None else args.end
    args.drain = window["drain"] if args.drain is None else args.drain
    if args.drain < 0 or not 0 <= args.start < args.end <= scenario.duration_s:
        parser.error("Require 0 <= start < end <= the selected scenario horizon, and drain >= 0")
    if args.services is not None:
        selection = "all" if args.services == "all" else args.services.split(",")
    return source_info, selection


def main(argv=None, *, on_policy_ready=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    run = actions.add_parser("run", help="Run the native packet clock and B03 controls")
    run.add_argument("--simulator", help="Read-only resnet_py release (default: B03_SIMULATOR or simulator/resnet_py)")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--data", type=Path, help="Official dataset archive, extracted root or scenario folder")
    source.add_argument("--scenario", type=Path, metavar="MANIFEST", help="Custom scenario manifest")
    source.add_argument("--network", metavar="ID", help="Select a named synthetic network from knowledge/scenarios")
    run.add_argument("--foundation", action="store_true",
                     help="Archive and share the static opening map for an official run (included for custom scenes)")
    channel = run.add_mutually_exclusive_group()
    channel.add_argument("--channel-profile", metavar="ID", help="Override a custom manifest's channel edition")
    channel.add_argument("--no-channel", action="store_true", help="Use a custom scene's nominal link settings")
    run.add_argument("--script", help=f"Official script number (default: {PROFILE['script']})")
    run.add_argument("--mode", choices=["package", "single", "workorder"], default="workorder")
    run.add_argument("--initial-admission", choices=["residual","audit","original"], default="residual",
                     help="At the first admitted work order, check authorized omissions on residual links")
    run.add_argument("--policy", default="I",
                     help="Authorized RC tool, conditional D/K/J rule, or frozen checkpoint")
    run.add_argument("--checkpoint",type=Path,default=os.environ.get("B03_CHECKPOINT"))
    run.add_argument("--tokenizer",type=Path,default=os.environ.get("B03_TOKENIZER"))
    run.add_argument("--device",choices=["cpu","cuda"],default="cuda")
    run.add_argument("--services", help="Comma-separated active workload, or all; defaults to the selected scenario")
    run.add_argument("--include-arrivals", action="store_true",
                     help="Keep the selected startup workload and include all later registrations on delivery")
    run.add_argument("--writable-services", help="Subset that can receive writes")
    run.add_argument("--protected-services", default="")
    run.add_argument("--service-end", action="append", default=[], metavar="ID=SECONDS",
                     help="Schedule a project test demand end at an absolute second; repeat for separate services")
    run.add_argument("--start", type=int)
    run.add_argument("--end", type=int)
    run.add_argument("--drain", type=int)
    run.add_argument("--defer-unreachable-cleanup", action=argparse.BooleanOptionalAction, default=None,
                     help="Park unreachable work in workorder mode (default). Use --no-defer-unreachable-cleanup for v1.")
    run.add_argument("--drop-install-ack", action="store_true", help="Test one lost native install receipt")
    run.add_argument("--output", type=Path, required=True)
    check = actions.add_parser("verify", help="Validate causal evidence, native device writes, QoS, and packet accounts")
    check.add_argument("directory", type=Path)
    check.add_argument("--require-complete-drain", action="store_true",
                       help="Fail acceptance when work-order drain closure is incomplete or unrecorded")
    check.add_argument("--report", type=Path, help="New file for complete per-service verification results")
    run.add_argument("--components", type=Path, help="Component manifest (default: config/components.json)")
    run.add_argument("--record-component-io", action="store_true", help="Save exact solver input and output for local reproduction")
    lookup = actions.add_parser("knowledge", help="Read a versioned technical knowledge entry")
    lookup.add_argument("--components", type=Path)
    lookup.add_argument("--formulas", action="store_true", help="Query the versioned calculation edition")
    lookup.add_argument("--edition", help="Required formula edition version if specified")
    selector = lookup.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", dest="identifier")
    selector.add_argument("--topic")
    selector.add_argument("--channel-profile", metavar="ID", help="Read a channel edition and its configured capacity limits")
    sample = actions.add_parser("examples", help="Export actual model, solver and knowledge examples from a sealed run")
    sample.add_argument("directory", type=Path)
    sample.add_argument("--components", type=Path)
    sample.add_argument("--output", type=Path, help="Write the examples to a new local file")
    check.add_argument("--components", type=Path, help="Manifest used by the original run for a strict check")
    for command in (check, sample):
        command.add_argument("--strict-source", action="store_true",
                             help="Also require the original implementation and active component dependencies")
        command.add_argument("--checkpoint", type=Path, help="Original model weights for a strict source check")
        command.add_argument("--tokenizer", type=Path, help="Original tokenizer for a strict source check")
        command.add_argument("--simulator",
                             help="Original packet simulator for a strict source check")
    args = parser.parse_args(argv)
    if not __debug__:
        parser.error("Run without -O so all invariant checks execute")
    packet = ROOT / "src/packet"
    runtime = ROOT / "src/rules"
    sys.path[:0] = [str(runtime / "vendor/src"), str(runtime), str(packet), str(ROOT)]
    from component_config import load_components
    if args.command in ("verify", "examples"):
        manifest = load_components(args.components) if args.components else None
        checks = dict(strict_source=args.strict_source, components=manifest,
                      checkpoint=args.checkpoint, tokenizer=args.tokenizer, simulator=args.simulator)
    if args.command == "verify":
        from verify_packet import verify
        result = verify(args.directory.resolve(), **checks)
        if args.report:
            json_file(args.report, result)
        print(json.dumps({k: result[k] for k in ("scene", "nodes", "links", "mode", "acceptedPathChecks", "verifiedDeviceQueryCount", "actualModelCalls", "toolChoices", "business", "packets", "verification")}, ensure_ascii=False))
        if args.require_complete_drain and result["verification"].get("drainStatus", {}).get("status") != "complete":
            check.exit(1, "Work-order drain closure is incomplete or unrecorded. See the verification report.\n")
        return
    if args.command == "knowledge":
        if args.channel_profile is not None:
            if args.formulas or args.components:
                lookup.error("--channel-profile reads the channel catalog directly")
            from knowledge.channels.model import read_edition
            _, profile, ceilings = read_edition(ROOT / "knowledge/channels", args.channel_profile)
            if args.edition is not None and args.edition != profile["version"]:
                lookup.error("Requested edition differs from the selected channel profile version")
            print(json.dumps({"profile": profile, "capacityCeilingsBps": ceilings}, ensure_ascii=False, indent=2))
            return
        if args.formulas:
            import b03_formulas as formula
            if args.components:
                lookup.error("Formula editions are bound separately from advisory knowledge components")
            result = formula.lookup(identifier=args.identifier, topic=args.topic,
                                    version=args.edition if args.edition is not None else formula.EDITION_VERSION)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.edition is not None:
            lookup.error("--edition selects a --formulas edition")
        from tech_knowledge import query
        components = load_components(args.components).activate("knowledge")
        print(json.dumps(query(components, identifier=args.identifier, topic=args.topic), ensure_ascii=False, indent=2))
        return
    if args.command == "examples":
        from component_examples import examples
        result = examples(args.directory.resolve(), **checks)
        if args.output:
            json_file(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.defer_unreachable_cleanup is None:
        args.defer_unreachable_cleanup = args.mode == "workorder"
    elif args.mode != "workorder":
        run.error("Unreachable cleanup recovery options require --mode workorder")
    network = _network_source(args, run)
    args.channel_selection = None
    if args.no_channel or args.channel_profile is not None:
        if args.scenario is None:
            run.error("--channel-profile and --no-channel require --scenario or --network")
        args.channel_selection = {"source": "cli", "profile": args.channel_profile}
    preset = manifest_pin = preset_pin = None
    if args.scenario is not None:
        raw_manifest = args.scenario.read_bytes()
        manifest_pin = hashlib.sha256(raw_manifest).hexdigest()
        manifest = parse_custom_manifest(raw_manifest)
        selected_channel(manifest, args.channel_selection)
        preset = manifest.get("knowledgePreset")
        if preset:
            args.components = resolve_preset(ROOT, preset, args.components)
            preset_pin = sha256(args.components)
    components = load_components(args.components)
    if args.policy not in (*components.symbols, "model", "combined"):
        run.error("The requested policy is absent from the configured action catalog")
    dependency = simulator_root(args.simulator)
    raw_root = _select_scene(args, run, dependency)
    out = args.output.resolve()
    if out == dependency or dependency in out.parents or (raw_root and (out == raw_root or raw_root in out.parents)):
        run.error("Select an output directory outside the source inputs")
    from simulator_component import LOCK_MEMBER, runtime_identity, select_engine, simulation_type
    components.activate(args.policy if args.mode == "workorder" else args.mode)
    if args.mode == "workorder":
        from knowledge.work_order import BINDING_CONTRACT, validate_binding_edition
        validate_binding_edition(components)
    lock_blob, lock = select_engine(components)
    fingerprint = verify_engine(dependency, lock)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(exist_ok=False)
    (out / LOCK_MEMBER).write_bytes(lock_blob)
    os.environ["B03_PACKET_RUNTIME"] = str(runtime)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(dependency / "src"))
    import resnet
    if not Path(resnet.__file__).resolve().is_relative_to((dependency / "src").resolve()):
        raise ValueError("Native simulator import origin differs from the pinned external source")
    from resnet.control.policy import decide
    from b03 import solver
    from packet_measure import request_row
    engine_type = simulation_type(components)
    scenario, config, config_hash, _ = prepare(
        dependency, args.data, args.script, out, manifest_path=args.scenario, resources=network["resources"],
        channel_selection=args.channel_selection, archive_bundled_config=args.foundation)
    source_info, selection = _run_options(args, run, scenario, out)
    foundation = None
    source_info["foundationEnabled"] = args.foundation or args.scenario is not None
    if source_info["foundationEnabled"]:
        from scene import archive_network_foundation
        foundation, source_info["networkFoundation"] = archive_network_foundation(
            out, network, config, source_info, manifest=args.scenario)
    if manifest_pin is not None and manifest_pin != source_info["manifestSha256"]:
        raise ValueError("The scenario manifest changed while selecting components")

    if args.mode != "workorder" and args.policy != "I":
        run.error("--policy is available on the joint OM/RC work-order path")
    from frozen_policy import DecisionPolicy
    chooser = DecisionPolicy(args.policy, args.checkpoint, args.tokenizer, args.device, args.initial_admission,
                             components=components) if args.mode == "workorder" else None
    if chooser is not None:
        from component_config import work_order_components_digest
        chooser.identity["workOrderComponentsSha256"] = work_order_components_digest(components.identity)
        chooser.record_component_io = args.record_component_io
        if on_policy_ready is not None:
            on_policy_ready(chooser)
    specs = {x.service_id: x for x in scenario.services + scenario.added_services}
    ids = sorted(specs) if selection == "all" else sorted(set(selection))
    if args.include_arrivals:
        if args.mode != "workorder" or args.writable_services is not None or selection == "all":
            run.error("--include-arrivals requires a startup workload in workorder mode with discovered writes")
        startup_ids = {sid for sid, spec in specs.items() if spec.registered_at_us == 0}
        protected_ids = set(filter(None, args.protected_services.split(",")))
        if not set(ids) <= startup_ids or not protected_ids <= set(ids):
            run.error("The selected and protected sets must contain only the named startup services")
        source_info["workloadSelection"] = {"schema": "b03.startup-plus-arrivals.v1", "startup": ids}
        # Only the simulator receives the expanded traffic filter. Roles discover every ID from delivered reports.
        ids = sorted(set(ids) | (specs.keys() - startup_ids))
    if not ids or not set(ids) <= specs.keys():
        run.error("Unknown or empty original service selection")
    activity = {}
    if args.service_end:
        from activity_overlay import parse_service_ends, with_service_ends
        cutoffs = parse_service_ends(args.service_end, specs, set(ids), args.start, args.end)
        scenario = with_service_ends(scenario, cutoffs)
        activity = {"schema": "b03.demand-ends.v1", "origin": "experiment", "endSeconds": cutoffs}
        json_file(out / "input/b03-demand-ends.json", activity)
    writable = set(ids) if args.writable_services is None else set(filter(None, args.writable_services.split(",")))
    discover_writes = (selection == "all" or args.include_arrivals) and args.writable_services is None
    protected = set(filter(None, args.protected_services.split(",")))
    if not writable <= set(ids) or not protected <= set(ids):
        run.error("Write scope and protected IDs must be part of the original selected workload")
    if args.mode != "workorder" and (args.writable_services is not None or protected):
        run.error("Scoped B03 work-order permissions require --mode workorder")
    config.update(start_s=args.start, end_s=args.end, drain_s=args.drain, service_ids=ids)
    config["control"] = {**config["control"], "managed_service_ids": ids}
    pyenv = {pkg: importlib.metadata.version(pkg) for pkg in ["simpy"] + (["a2a-sdk"] if args.mode == "workorder" else [])}
    definitions = {sid: request_row(specs[sid]) for sid in ids}
    definition_key = "serviceDefinitions" if args.scenario is not None else "officialDefinitions"
    identity = {**source_info, "nodeCount": len(scenario.nodes), "linkCount": len(scenario.links), "initialServiceCount": len(scenario.services), "eventCount": len(scenario.events), "case":"packet_control",
                "components": components.identity, "rcPolicy": chooser.identity if chooser else None, "releaseManifestSha256": fingerprint, "originalConfigSha256": config_hash,
                "simulatorRuntime": runtime_identity(components.simulator, lock, lock_blob),
                "simulation": config, "services": ids, definition_key: definitions,
                "authority": {"discovery":"all_delivered_registered" if discover_writes else "explicit_roster", "allowedServiceIds": sorted(writable), "protectedServiceIds":sorted(protected), "effectiveWriteIds":sorted(writable - protected), "healthyMigration": False, "preemption": False,
                              "rateScale": config["rate_scale"], "scope": "B03 work orders; package/single use native stock review authority"},
                "coldStart": args.start != 0, "match": "same input bytes, reporting, start/end/drain, clocks, eligible IDs, full demand, physical queues and seed"}
    if args.mode == "workorder":
        from drain_evidence import CONTRACT as drain_contract
        identity["drainContract"] = drain_contract
        identity["knowledgeBindingContract"] = BINDING_CONTRACT
        identity["deviceRecovery"] = {"contractVersion": ("b03.deferred-cleanup.v2" if args.defer_unreachable_cleanup
                                                          else "b03.deferred-cleanup.v1"),
                                      "deferUnreachableCleanup": args.defer_unreachable_cleanup}
    if activity:
        identity["activityOverlay"] = activity
    from integrity import source_identity
    from provenance import capture_provenance
    proof = capture_provenance(ROOT, components, checkpoint=args.checkpoint, tokenizer=args.tokenizer,
                               policy=args.policy if args.mode == "workorder" else args.mode,
                               policy_identity=chooser.identity if chooser else None)
    implementation = source_identity()
    if preset:
        from integrity import preset_binding
        binding = preset_binding(preset, components.identity, implementation)
        if binding["sha256"] != preset_pin:
            raise ValueError("The knowledge preset changed while preparing the run")
        identity["knowledgePreset"] = binding
    from integrity import contract_for_mode
    import b03_formulas as formula
    identity["formulaBinding"] = formula.receipt_binding(config, implementation)
    (out / formula.MEMBER).write_bytes(formula.EDITION_BYTES)
    identity["receiptContract"] = contract_for_mode(args.mode)
    json_file(out / "CONDITIONS.json", {**identity, "provenance": proof, "implementationSha256": implementation, "mode": args.mode, "dropInstallAckAtDestination": args.drop_install_ack,
                                       "python": sys.version, "packages": pyenv})
    with ExitStack() as stack:
        traj = stack.enter_context((out / "b03.jsonl").open("x", encoding="utf-8"))
        def emit(row):
            traj.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n")
        rule = None
        if args.mode in ("single", "package"):
            if args.mode == "single":
                from packet_roles import single_policy
                rule_method = single_policy
            else:
                def rule_method(view, services, **kw):
                    return decide(view, services, qos_profile="official", **kw)
            def rule(snapshot, services, **kw):
                now = kw["now_ns"]
                if len(services) != 1 or snapshot["sampled_for_decision_at_ns"] != now:
                    raise ValueError("Single-policy contract mismatch")
                seen = {key: {"usable": x["usable"], "status": x["reason"],
                              "reports": {n: None if v is None else [v["probe"]["observed_at_ns"], v["received_at_ns"], v["probe"]["seq"]]
                                          for n, v in x["reporters"].items()}}
                        for key, x in snapshot["links"].items()}
                if any(v and not 0 <= v[0] <= v[1] <= now for x in seen.values() for v in x["reports"].values()):
                    raise ValueError("Future observation exposed to policy")
                start = time.perf_counter_ns()
                result = rule_method(snapshot, services, **kw)
                emit({"event": "single_query_and_route", "time_ns": now, "service_id": services[0].service_id,
                      "input": request_row(services[0]), "reports":seen,"retained_wire_bps":kw["retained_wire_bps"],
                      "decision":result["decisions"][0],"solver_wall_ns":time.perf_counter_ns()-start,
                      "solver_clock_charge_ns":0,"policy":args.mode})
                return result
        sim = engine_type(scenario, config, out, decision_policy=rule, drop_install_ack=args.drop_install_ack)
        stack.callback(sim.close)
        sim.start_meter(out,solver)
        stack.callback(sim.close_meter)
        if args.mode == "workorder":
            from packet_roles import PacketRoles
            from packet_device import install
            roles = PacketRoles(None if discover_writes else writable, emit, chooser=chooser, protected_ids=protected,
                                control_headroom_fraction=config["control"]["control_headroom_fraction"],
                                network_foundation=foundation)
            stack.callback(roles.close)
            workloop = install(sim, roles, emit, defer_unreachable_cleanup=args.defer_unreachable_cleanup)
        started = time.perf_counter()
        sim.advance(sim.end_ns)
        boundary = sim.summary()
        sim.advance(sim.stop_ns)
        drained = sim.summary()
        if not (drained["checks_passed"] and boundary["checks_passed"]):
            raise RuntimeError("Native packet conservation failed")
        evaluation = sim.finish_meter()
        requested = sum(drained["demand_bits_by_service"].values())
        summary = {"mode":args.mode,"seed":config["seed"],"business":evaluation,"demandBits":requested,
                  "endBoundary":boundary["conservation"],"afterDrain":drained["conservation"],
                  "packets":drained["totals"],"services":{i:drained["services"][i] for i in ids},
                  "links":drained["links"],"sourceDefinitions":definitions,
                  "fault":sim.dropped_ack,"clock":{"start_ns":sim.start_ns,"end_ns":sim.end_ns,"drain_end_ns":sim.stop_ns},
                  "control":drained["control"],
                  "b03LoopStatus":workloop.status() if args.mode == "workorder" and hasattr(workloop,"status") else None,
                  "timeAccounting":{"modelClock":"SimPy integer ns","deviceNetwork":"actual packet enqueue through arrival",
                                    "solver":"measured RC tool selection and solver ns charged before dispatch" if args.mode=="workorder" else "measured wall ns; stock policy hook charges zero simulated compute",
                                    "a2a":"loopback role RPC wall time, separate from simulated device network"},"wallSeconds":time.perf_counter()-started,
                  "releaseVerifiedAfterRun":verify_engine(dependency, lock)==fingerprint}
        print(json.dumps({"mode":args.mode,"window":[args.start,args.end], "U":evaluation["U"],"W":evaluation["W"],
                          "data":drained["conservation"]["data"],"fault":sim.dropped_ack is not None},ensure_ascii=False))

    from simulator_ports import VERSION as public_simulator_contract
    if components.simulator["contractVersion"] == public_simulator_contract:
        summary["simulatorPorts"] = sim.ports.receipt()
    if args.mode == "workorder":
        from competition_metrics import competition_metrics
        from drain_evidence import drain_receipt, file_rows
        summary["drainStatus"] = drain_receipt(identity, summary["clock"], file_rows(out / "controller.jsonl"),
                                              file_rows(out / "b03.jsonl"))
        if summary["drainStatus"]["status"] == "incomplete":
            print("WARNING: work-order drain incomplete at configured deadline",
                  json.dumps(summary["drainStatus"], ensure_ascii=False), file=sys.stderr)
        summary["competitionMetrics"] = competition_metrics(out, summary)
    json_file(out / "SUMMARY.json", summary)
    from integrity import seal
    seal(out)
