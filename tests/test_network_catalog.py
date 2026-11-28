"""Checks on the editable knowledge packs used by simulation startup."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from knowledge.network import build_foundation, resolve_external, resolve_network


class NetworkCatalogTests(unittest.TestCase):
    def setUp(self):
        self.pack = resolve_network(ROOT, "field-survey")
        self.topology = json.loads((self.pack["directory"] / "net-topo.json").read_bytes())
        self.services = json.loads((self.pack["directory"] / "bg-service.json").read_bytes())
        self.profile = json.loads((ROOT / "knowledge/channels/survey-noise-v1.json").read_bytes())

    def foundation(self):
        return build_foundation(self.topology, self.services, self.profile,
                                self.pack["resources"], self.pack["nodeCapabilities"])

    def test_static_links_project_channel_and_services_hide_schedule(self):
        # A later declaration could reveal workload changes before the reporting node is heard.
        future = dict(self.services[0], serviceId="LATER", startAtSec=4)
        self.services.append(future)
        original = deepcopy(self.topology)
        view = self.foundation()
        link = view["links"]["FIELD-EAST"]
        self.assertEqual(link["params"]["bandwidthBps"], 2400000)
        self.assertEqual(link["effectiveParams"]["bandwidthBps"], 400000)
        self.assertEqual(link["effectiveParams"]["delayMs"], 7.0)
        self.assertEqual(view["nodes"]["FIELD"]["attachedLinkTypes"], ["microwave", "radio"])
        self.assertEqual(set(view["initialServices"]), {"MAP"})
        self.assertNotIn("startAtSec", view["initialServices"]["MAP"])
        self.assertNotIn("media", view["initialServices"]["MAP"])
        self.assertEqual(self.topology, original)

    def test_external_native_inputs_with_alias_and_optional_category(self):
        sys.path.insert(0, str(ROOT / "simulator/resnet_py/src"))
        from resnet.io import load_scenario
        link_qos = self.topology["links"][0]["params"]
        link_qos["bandwitdhBps"] = link_qos.pop("bandwidthBps")
        service_qos = self.services[0]["requirement"]
        service_qos["bandwitdhBps"] = service_qos.pop("bandwidthBps")
        del self.services[0]["category"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "arbitrary-scene"
            shutil.copytree(self.pack["directory"], root)
            for name, body in (("net-topo.json", self.topology), ("bg-service.json", self.services)):
                (root / name).write_text(json.dumps(body), encoding="utf-8")
            # Verify the external fixture with the native reader before checking its public projection.
            native = load_scenario(root)
            external = resolve_external(ROOT, root / "manifest.json")
            view = build_foundation(self.topology, self.services, None, self.pack["resources"],
                                    external["nodeCapabilities"])
        service = view["initialServices"]["MAP"]
        self.assertEqual(service["requirement"]["bandwidthBps"], native.services[0].required_bps)
        self.assertIsNone(service["category"])
        self.assertEqual(view["links"]["FIELD-EAST"]["params"]["bandwidthBps"], native.links[0].capacity_bps)
        self.assertEqual(view["nodes"]["FIELD"]["initialPosition"], {"x": 0, "y": 0})
        self.assertEqual(view["nodes"]["FIELD"]["ip"], "192.0.2.1")

    def test_pack_owns_node_constraints(self):
        profile = self.pack["nodeCapabilities"]["sampling_platform"]
        profile["allowedLinkTypes"] = ["radio"]
        with self.assertRaisesRegex(ValueError, "Link type.*FIELD"):
            self.foundation()
        profile["allowedLinkTypes"].append("microwave")
        profile["maxInterfaces"] = 1
        with self.assertRaisesRegex(ValueError, "interfaces at FIELD"):
            self.foundation()

    def test_explicit_input_keeps_unknown_capability_distinct_from_zero(self):
        legacy = resolve_external(ROOT)
        view = build_foundation(self.topology, self.services, None,
                                self.pack["resources"], legacy["nodeCapabilities"])
        node = view["nodes"]["FIELD"]
        self.assertIsNone(node["capabilities"]["maxInterfaces"])
        self.assertEqual(node["interfaceCount"], 2)
        self.assertEqual(view["channelModel"]["model"], "scenario-link-parameters")

    def test_external_foundation_only_records_the_measured_interface_count(self):
        # External competition inputs do not declare allowed media or maximum interfaces.
        self.topology["links"].extend(dict(self.topology["links"][0], linkId=f"parallel-{i}") for i in range(12))
        external = resolve_external(ROOT)
        foundation = build_foundation(self.topology, self.services, None,
                                      self.pack["resources"], external["nodeCapabilities"])
        field = foundation["nodes"]["FIELD"]
        self.assertEqual(field["interfaceCount"], 14)
        self.assertEqual(field["capabilities"], {"allowedLinkTypes": None, "maxInterfaces": None})

    def test_invalid_resources_and_parent_paths_fail_early(self):
        for changes in ({"queue_bytes": 0}, {"queue_packets": True}, {"control_headroom_fraction": 1.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_foundation(self.topology, self.services, None,
                                 {**self.pack["resources"], **changes}, self.pack["nodeCapabilities"])
        for key in ("../field-survey", "a/b"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                resolve_network(ROOT, key)
