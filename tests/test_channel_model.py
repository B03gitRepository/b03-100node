"""信道噪声折算、有效物理输入生成及离线归档回放的回归测试。"""
from argparse import ArgumentParser, Namespace
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from channel_model import derive_inputs, validate_profile
from cli import _run_options
from integrity import check_custom_inputs
from scene import prepare, sha256


EXAMPLE = ROOT / "knowledge/scenarios/field-survey"
PROFILE = ROOT / "knowledge/channels/survey-noise-v1.json"


def read(path):
    return json.loads(path.read_bytes())


def update(time, link_id, **params):
    return {"time": time, "eventType": "link_param_change", "attrs": {"linkId": link_id, **params}}


class ChannelProjectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = read(PROFILE)
        self.topology = read(EXAMPLE / "net-topo.json")

    def test_capacity_delay_and_independent_loss_only_on_selected_type(self):
        radio = next(link for link in self.topology["links"] if link["linkType"] == "radio")
        radio["params"].update(packetLossRate=0.2, jitterMs=1, ber=0.00001)
        original = deepcopy(self.topology)
        effective, _ = derive_inputs(self.profile, self.topology, [])
        links = {link["linkId"]: link for link in effective["links"]}
        params = links[radio["linkId"]]["params"]
        self.assertEqual(self.topology, original)
        self.assertEqual(params["bandwidthBps"], 400000)
        self.assertEqual(params["delayMs"], radio["params"]["delayMs"] + 5)
        self.assertAlmostEqual(params["packetLossRate"], 0.208)
        self.assertEqual((params["jitterMs"], params["ber"]), (1, 0.00001))
        microwave = next(link for link in original["links"] if link["linkType"] == "microwave")
        self.assertEqual(links[microwave["linkId"]], microwave)

    def test_partial_updates_same_time_bandwidth_alias_and_recovery(self):
        link = "EAST-HUB"
        events = [
            update(10, link, bandwitdhBps=120000),
            update(10, link, delayMs=4),
            update(11, link, packetLossRate=0.2),
            update(12, link, delayMs=2, packetLossRate=0, bandwidthBps=2400000),
            update(13, "WEST-HUB", bandwidthBps=1700000),
            {"time": 14, "eventType": "link_state", "attrs": {"linkId": link, "state": "down"}},
        ]
        original = deepcopy(events)
        _, effective = derive_inputs(self.profile, self.topology, events)
        self.assertEqual(events, original)
        self.assertEqual([e["time"] for e in effective], [10, 10, 11, 12, 13, 14])
        first = dict(effective[0]["attrs"])
        first.pop("linkId")
        self.assertEqual(list(first.values()), [120000])
        self.assertIn(next(iter(first)), ("bandwidthBps", "bandwitdhBps"))
        self.assertEqual(effective[1]["attrs"], {"linkId": link, "delayMs": 9})
        self.assertEqual(set(effective[2]["attrs"]), {"linkId", "packetLossRate"})
        self.assertAlmostEqual(effective[2]["attrs"]["packetLossRate"], 0.208)
        recovered = effective[3]["attrs"]
        self.assertEqual((recovered["delayMs"], recovered["bandwidthBps"]), (7, 400000))
        self.assertAlmostEqual(recovered["packetLossRate"], 0.01)
        self.assertEqual(effective[4:], original[4:])

    def test_rejects_invalid_profiles(self):
        for field, value in (("spectrumHz", 0), ("spectrumHz", True), ("efficiency", 1.1),
                             ("extraDelayMs", -1), ("extraLossProbability", -0.1), ("noiseDbm", float("nan"))):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                profile = deepcopy(self.profile)
                profile["linkTypes"]["radio"][field] = value
                validate_profile(profile)
        with self.assertRaises(ValueError):
            validate_profile(self.profile, expected_id="different-profile")
        with self.assertRaises(ValueError):
            derive_inputs({**self.profile, "linkTypes": {"absent": self.profile["linkTypes"]["radio"]}},
                          self.topology, [])


class ChannelArchiveTests(unittest.TestCase):
    def test_explicit_channel_overrides_are_bound_to_the_engine_directory(self):
        from scenario_manifest import selected_channel
        engine = ROOT / "simulator/resnet_py"
        for name, selected in (("manifest.json", None), ("manifest-plain.json", "survey-noise-v1")):
            with self.subTest(manifest=name, selected=selected), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                source = EXAMPLE / name
                override = {"source": "cli", "profile": selected}
                with patch.object(sys, "path", [str(engine / "src"), *sys.path]):
                    spec, settings, native_pin, _ = prepare(engine, None, None, out, manifest_path=source,
                                                             channel_selection=override)
                args = Namespace(scenario=source, script=None, start=None, end=None, drain=None,
                                 services=None, channel_selection=override)
                conditions, _ = _run_options(args, ArgumentParser(), spec, out)
                conditions.update(originalConfigSha256=native_pin, simulation=settings)
                self.assertEqual((out / "scenario-manifest.json").read_bytes(), source.read_bytes())
                self.assertEqual(conditions["channelSelection"], override)
                self.assertEqual(selected_channel(read(source), conditions["channelSelection"]), selected)
                self.assertEqual(read(out / "native-experiment.json")["scenario_dir"],
                                 "input" if selected is None else "effective-input")
                self.assertEqual((out / "channel-model.json").is_file(), selected is not None)
                files = {f.relative_to(out).as_posix(): {"sha256": sha256(f)}
                         for f in out.rglob("*") if f.is_file()}
                check_custom_inputs(out, conditions, files)
                with self.assertRaises(ValueError):
                    check_custom_inputs(out, {k: v for k, v in conditions.items() if k != "channelSelection"}, files)

    def test_replay_compares_projected_parameters_to_native_packet_batches(self):
        from channel_replay import check_channel_batches
        engine = ROOT / "simulator/resnet_py"
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "path", [str(engine / "src"), *sys.path]):
            from resnet.simulation.simulation import Simulation
            out = Path(tmp)
            source = EXAMPLE / "manifest.json"
            spec, settings, _, _ = prepare(engine, None, None, out, manifest_path=source)
            args = Namespace(scenario=source, script=None, start=20, end=55, drain=0, services=None)
            conditions, _ = _run_options(args, ArgumentParser(), spec, out)
            settings.update(start_s=args.start, end_s=args.end, drain_s=args.drain, mode="fixed")
            conditions["simulation"] = settings
            sim = Simulation(spec, settings, out)
            try:
                sim.advance(sim.end_ns)
            finally:
                sim.close()
            check_channel_batches(out, conditions)
            log = out / "batches.jsonl"
            rows = [json.loads(line) for line in log.read_bytes().splitlines()]
            self.assertEqual(rows[-1]["time_ns"], 55_000_000_000)
            rows[-1]["state"]["link_params"]["EAST-HUB"]["capacity_bps"] += 1
            log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Packet engine link parameters"):
                check_channel_batches(out, conditions)

    def test_native_batches_and_archive_replay_use_effective_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            shutil.copytree(EXAMPLE, source)
            manifest = read(source / "manifest.json")
            manifest["channelProfile"] = "survey-noise-v1"
            (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            config_path = source / "scenario-config.json"
            config = read(config_path)
            run = root / "run"
            run.mkdir()
            engine = ROOT / "simulator/resnet_py"
            with patch.object(sys, "path", [str(engine / "src"), *sys.path]):
                from resnet.io import load_scenario
                native = load_scenario(source)
                # 验证时间线复述了脚本中的事件操作，引擎在加载时会交叉校验。
                timeline = {"schema": "project-execution-batches-v1", "batches": [
                    {"time_s": b.time_s, "sim_time_us": b.time_us, "apply_before_packet_generation": True,
                     "operations": list(b.operations), "profile": b.profile, "active_service_ids": list(b.active_service_ids)}
                    for b in native.batches]}
                (source / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
                config["files"]["validation_timeline"] = "timeline.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                spec, settings, config_hash, _ = prepare(engine, None, None, run, manifest_path=source / "manifest.json")

            inputs, effective = run / "input", run / "effective-input"
            for name in ["scenario-config.json", *config["files"].values()]:
                self.assertEqual((inputs / name).read_bytes(), (source / name).read_bytes())
            self.assertEqual((run / "channel-model.json").read_bytes(), PROFILE.read_bytes())
            self.assertNotEqual(read(inputs / "timeline.json"), read(effective / "timeline.json"))
            east = next(link for link in spec.links if link.link_id == "EAST-HUB")
            self.assertEqual((east.capacity_bps, east.delay_s, spec.events[-1].attrs["bandwidthBps"]),
                             (400000, 0.007, 400000))
            self.assertEqual(read(run / "native-experiment.json")["scenario_dir"], "effective-input")
            files = {f.relative_to(run).as_posix(): {"sha256": sha256(f), "bytes": f.stat().st_size}
                     for f in run.rglob("*") if f.is_file()}
            args = Namespace(scenario=source / "manifest.json", script=None, start=None, end=None,
                             drain=None, services=None)
            conditions, _ = _run_options(args, ArgumentParser(), spec, run)
            conditions.update(originalConfigSha256=config_hash, simulation=settings)
            channel = conditions["channelModel"]
            self.assertEqual(channel["effectiveHashes"], spec.source_hashes)
            self.assertNotEqual(conditions["sourceHashes"], spec.source_hashes)
            check_custom_inputs(run, conditions, files)
            with self.assertRaises(ValueError):
                check_custom_inputs(run, {k: v for k, v in conditions.items() if k != "channelModel"}, files)

            # 故意篡改有效带宽并重算两项摘要，独立回放仍必须识别并报错。
            name = config["files"]["topology"]
            target = effective / name
            forged = read(target)
            forged["links"][0]["params"]["bandwidthBps"] += 1
            target.write_text(json.dumps(forged), encoding="utf-8")
            files["effective-input/" + name] = {"sha256": sha256(target), "bytes": target.stat().st_size}
            channel["effectiveHashes"][name] = sha256(target)
            with self.assertRaises(ValueError):
                check_custom_inputs(run, conditions, files)


if __name__ == "__main__":
    unittest.main()
