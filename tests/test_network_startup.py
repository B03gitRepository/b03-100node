"""验证场景选择、运行时资源设置与 OM/RC 开局底图事实严格一致。"""
from argparse import ArgumentParser, Namespace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "simulator/resnet_py"
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from cli import _network_source, _run_options, _select_scene
from knowledge.network_replay import check_startup_facts
from scene import PROFILE, archive_network_foundation, prepare, sha256
from scenario_manifest import resolve_manifest_reference


class NetworkStartupTests(unittest.TestCase):
    def test_selected_pack_controls_native_queues_and_published_facts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "path", [str(ENGINE / "src"), *sys.path]):
            from resnet.simulation.simulation import Simulation
            root = Path(tmp)
            catalog = root / "knowledge/scenarios"
            folder = catalog / "field-survey"
            shutil.copytree(ROOT / "knowledge/scenarios/field-survey", folder)
            spec_file = folder / "network.json"
            pack = json.loads(spec_file.read_bytes())
            limits = {"queue_packets": 7, "queue_bytes": 4096, "control_headroom_fraction": 0.2}
            pack["resources"] = limits
            spec_file.write_text(json.dumps(pack), encoding="utf-8")
            args = Namespace(network='field-survey', data=None, script=None, scenario=None, channel_selection=None,
                             start=None, end=None, drain=None, services=None)
            with patch("cli.ROOT", root):
                selected = _network_source(args, ArgumentParser())
            run = root / "run"
            run.mkdir()
            scenario, config, pin, _ = prepare(ENGINE, args.data, args.script, run,
                                                manifest_path=args.scenario, resources=selected["resources"])
            source, _ = _run_options(args, ArgumentParser(), scenario, run)
            facts, receipt = archive_network_foundation(run, selected, config, source, manifest=args.scenario)
            self.assertEqual(receipt["networkId"], "field-survey")
            self.assertEqual(facts["resources"], limits)
            self.assertEqual(json.loads((run / receipt["file"]).read_bytes()), facts)
            sim = Simulation(scenario, config)
            self.addCleanup(sim.close)
            self.assertTrue(sim.links)
            for link in sim.links.values():
                self.assertEqual((link.max_queue_packets, link.max_queue_bytes), (7, 4096))
            self.assertEqual(sim.control.options["control_headroom_fraction"], 0.2)

            cond = {**source, "originalConfigSha256": pin, "networkFoundation": receipt,
                    "foundationEnabled": True, "simulation": config}
            files = {p.relative_to(run).as_posix(): {"sha256": sha256(p)} for p in run.rglob("*") if p.is_file()}
            check_startup_facts(run, cond, files, installed_root=root)
            with self.assertRaisesRegex(ValueError, "foundation selection"):
                check_startup_facts(run, {**cond, "foundationEnabled": False}, files)
            # 重新绑定修改后的归档，绝不能把 40 秒后才到达的未来业务提前塞进底图。
            facts["initialServices"]["GAUGE"] = dict(facts["initialServices"]["MAP"])
            fact_path = run / receipt["file"]
            fact_path.write_text(json.dumps(facts), encoding="utf-8")
            pin = sha256(fact_path)
            files[receipt["file"]]["sha256"] = pin
            with self.assertRaisesRegex(ValueError, "Opening network knowledge"):
                check_startup_facts(run, {**cond, "networkFoundation": {**receipt, "sha256": pin}}, files)


    def test_old_manifests_reference_the_right_central_channel_variant(self):
        old = ROOT / "examples/custom_scenario"
        expected = ROOT / "knowledge/scenarios/field-survey"
        self.assertEqual(resolve_manifest_reference(ROOT, old / "manifest.json"), expected / "manifest-plain.json")
        self.assertEqual(resolve_manifest_reference(ROOT, old / "manifest-noise.json"), expected / "manifest.json")

    def test_implicit_official_selection_keeps_profile_script_environment_and_data_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "official-input"
            external.mkdir()
            explicit = root / "explicit-input"
            explicit.mkdir()
            catalog = root / "knowledge/scenarios"
            catalog.mkdir(parents=True)
            (catalog / "active.json").write_text('{"active":"nonexistent"}', encoding="utf-8")
            for data, script, expected_data in ((None, None, external), (None, "08", external),
                                                (explicit, None, explicit), (explicit, "01", explicit)):
                with self.subTest(data=data, script=script), patch("cli.ROOT", root), \
                        patch.dict(os.environ, {"B03_DATA": str(external)}):
                    args = Namespace(network=None, data=data, script=script, scenario=None)
                    network = _network_source(args, ArgumentParser())
                    self.assertEqual(_select_scene(args, ArgumentParser(), ENGINE), expected_data)
                    self.assertIsNone(args.scenario)
                    self.assertEqual(args.script, script or PROFILE["script"])
                    self.assertEqual(network["resources"], {})
                    self.assertEqual(network["nodeCapabilities"],
                                     {"*": {"maxInterfaces": None, "allowedLinkTypes": None}})

    def test_explicit_custom_overrides_unrelated_official_environment(self):
        with patch.dict(os.environ, {"B03_DATA": "/absent-official-directory"}):
            args = Namespace(network="field-survey", data=None, script=None, scenario=None)
            network = _network_source(args, ArgumentParser())
            self.assertEqual(_select_scene(args, ArgumentParser(), ENGINE), network["directory"])
            self.assertIsNone(args.data)
            self.assertIsNone(args.script)

    def test_legacy_and_opt_out_archives_need_no_opening_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            check_startup_facts(out, {}, {})
            check_startup_facts(out, {"foundationEnabled": False}, {})
            with self.assertRaises(ValueError):
                check_startup_facts(out, {"foundationEnabled": True}, {})
            with self.assertRaises(ValueError):
                check_startup_facts(out, {"sceneOrigin": "custom"}, {})
            (out / "network-profile.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                check_startup_facts(out, {"foundationEnabled": False}, {})
