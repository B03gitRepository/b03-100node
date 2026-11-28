"""Custom scenes use the native packet format without assuming official names or control links."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import scene
from integrity import check_custom_inputs, preset_binding
from scenario_manifest import native_source_files, parse_custom_manifest, resolve_preset


class CustomScenarioTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        shutil.copytree(ROOT / "knowledge/scenarios/field-survey", self.source)
        self.output = self.root / "run"
        self.output.mkdir()

    def read(self, name):
        return json.loads((self.source / name).read_text(encoding="utf-8"))

    def write(self, name, value):
        (self.source / name).write_text(json.dumps(value), encoding="utf-8")

    def prepare(self):
        engine = ROOT / "simulator/resnet_py"
        with patch.object(sys, "path", [str(engine / "src"), *sys.path]):
            return scene.prepare(engine, None, None, self.output, manifest_path=self.source / "manifest-plain.json")

    def test_custom_topology_control_and_native_activity(self):
        spec, config, _, _ = self.prepare()
        self.assertEqual({n.node_id for n in spec.nodes}, {"FIELD", "EAST", "WEST", "HUB"})
        self.assertEqual({l.link_id for l in spec.links}, {"FIELD-EAST", "EAST-HUB", "FIELD-WEST", "WEST-HUB"})
        control = config["control"]
        self.assertEqual(control["controller_id"], "HUB")
        self.assertIsNone(control["control_primary_link"])
        self.assertIsNone(control["control_backup_link"])
        self.assertEqual([s.service_id for s in spec.services], ["MAP"])
        self.assertEqual([(s.service_id, s.registered_at_us) for s in spec.added_services], [("GAUGE", 40_000_000)])
        self.assertEqual([e.time_us for e in spec.events], [25_000_000, 40_000_000, 55_000_000])
        self.assertEqual({b.time_s: set(b.active_service_ids) for b in spec.batches},
                         {0: {"MAP"}, 25: {"MAP"}, 40: {"MAP", "GAUGE"}, 55: {"MAP", "GAUGE"}, 80: set()})

    def test_native_file_map_supplies_the_archived_bytes(self):
        config = self.read("scenario-config.json")
        files = config["files"]
        for role, new_name in (("events", "scripts/local-events.json"), ("topology", "radio-ring.json")):
            (self.source / files[role]).rename(self.source / new_name)
            files[role] = new_name
        self.write("scenario-config.json", config)
        spec, _, _, archived = self.prepare()
        for name in ["scenario-config.json", *files.values()]:
            with self.subTest(name=name):
                self.assertEqual((archived / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual(spec.events[0].attrs["linkId"], "EAST-HUB")
        self.assertEqual((self.output / "scenario-manifest.json").read_bytes(),
                         (self.source / "manifest-plain.json").read_bytes())

    def test_custom_scene_rejects_official_origin_and_external_paths(self):
        config = self.read("scenario-config.json")
        with self.assertRaises(ValueError):
            native_source_files({**config, "official_dataset": True})
        for name in ("../outside.json", "C:/outside.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                native_source_files({**config, "files": {**config["files"], "events": name}})

    def test_archive_rejects_changes_to_recorded_inputs(self):
        spec, settings, identity, _ = self.prepare()
        manifest = self.read("manifest-plain.json")
        files = {p.relative_to(self.output).as_posix(): {"sha256": scene.sha256(p)}
                 for p in self.output.rglob("*") if p.is_file()}
        conditions = {"scene": "custom:" + spec.scenario_id, "script": None,
                      "sceneContract": manifest["schema"], "manifestVersion": manifest["version"],
                      "manifestSha256": files["scenario-manifest.json"]["sha256"],
                      "sourceFiles": self.read("scenario-config.json")["files"], "sourceHashes": spec.source_hashes,
                      "originalConfigSha256": identity, "simulation": settings}
        check_custom_inputs(self.output, conditions, files)
        altered = {
            "manifestVersion": "2.0",
            "sourceFiles": {**conditions["sourceFiles"], "events": "traffic.json"},
            "sourceHashes": {**spec.source_hashes, "net-topo.json": "0" * 64},
            "simulation": {**settings, "control": {**settings["control"], "controller_id": "EAST"}},
        }
        for field, value in altered.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_custom_inputs(self.output, {**conditions, field: value}, files)

    def test_unknown_selected_service_is_rejected(self):
        manifest = self.read("manifest-plain.json")
        self.write("manifest-plain.json", {**manifest, "services": ["UNKNOWN"]})
        with self.assertRaises(ValueError):
            self.prepare()

    def test_knowledge_preset_is_a_project_id_and_explicit_selection_must_agree(self):
        manifest = self.read("manifest-plain.json")
        for value in ("../knowledge-om", "knowledge-om.json", "/config/knowledge-om", "components", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_custom_manifest(json.dumps({**manifest, "knowledgePreset": value}).encode())
        selected = ROOT / "config/knowledge-v2.json"
        self.assertEqual(resolve_preset(ROOT, "knowledge-v2", selected), selected)
        with self.assertRaises(ValueError):
            resolve_preset(ROOT, "knowledge-v2", ROOT / "config/components.json")

    def test_archive_binds_preset_to_configuration_and_knowledge_edition(self):
        self.write("manifest-plain.json", {**self.read("manifest-plain.json"), "knowledgePreset": "knowledge-v2"})
        spec, settings, config_hash, _ = self.prepare()
        files = {p.relative_to(self.output).as_posix(): {"sha256": scene.sha256(p)}
                 for p in self.output.rglob("*") if p.is_file()}
        from component_config import load_components
        preset = resolve_preset(ROOT, "knowledge-v2")
        components = load_components(preset).activate("knowledge").identity
        conditions = {"scene": "custom:" + spec.scenario_id, "script": None, "sceneContract": "b03.custom-scenario.v1",
                      "manifestVersion": self.read("manifest-plain.json")["version"],
                      "manifestSha256": files["scenario-manifest.json"]["sha256"],
                      "sourceFiles": self.read("scenario-config.json")["files"], "sourceHashes": spec.source_hashes,
                      "originalConfigSha256": config_hash, "simulation": settings, "components": components,
                      "implementationSha256": {"config/knowledge-v2.json": scene.sha256(preset)}}
        bound = preset_binding("knowledge-v2", components, conditions["implementationSha256"])
        check_custom_inputs(self.output, {**conditions, "knowledgePreset": bound}, files)
        for change in ({"sha256": "0"*64}, {"id": "knowledge-v1"}, {"knowledge": {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_custom_inputs(self.output, {**conditions, "knowledgePreset": {**bound, **change}}, files)
        with self.assertRaises(ValueError):
            check_custom_inputs(self.output, conditions, files)

    def test_control_links_must_be_parallel(self):
        manifest = self.read("manifest-plain.json")
        manifest["control"].update(control_primary_link="FIELD-EAST", control_backup_link="WEST-HUB")
        self.write("manifest-plain.json", manifest)
        with self.assertRaises(ValueError):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
