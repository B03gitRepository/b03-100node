"""Configuration and read-only knowledge regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from component_config import load_components
from tech_knowledge import query, query_library


class KnowledgeContract(unittest.TestCase):
    def test_switch_edition_without_touching_work_order_components(self):
        default = load_components()
        newer = load_components(ROOT / "config/knowledge-v2.json")
        for kind in ("catalog", "model", "rules"):
            self.assertEqual(getattr(default, kind), getattr(newer, kind))
        old = query(default, identifier="K045")
        new = query(newer, topic="clock")
        self.assertEqual((old[0]["id"], new[0]["id"]), ("K045", "K045"))
        self.assertEqual((old[0]["version"], new[0]["version"]), ("1.0.0", "1.1.0"))
        self.assertNotEqual(old[0]["content"], new[0]["content"])
        self.assertTrue(all((ROOT / p).is_file() for p in new[0]["sources"]))
        self.assertEqual(query(default, identifier="unknown"), [])

    def test_lookup_is_independent_of_provider_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"extends":str(ROOT / "config/components.json"),
                            "model":{"entrypoint":"not_installed_b03_model:build"}}), encoding="utf-8")
            self.assertEqual(query(load_components(path), topic="admission")[0]["id"], "K044")

    def test_a_different_mapping_requires_a_catalog_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"extends":str(ROOT / "config/components.json"),
                            "catalog":{"symbols":["A","C","B"]}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "immutable"):
                load_components(path)

    def test_operations_edition_preserves_existing_knowledge_and_exposes_evidence(self):
        previous = load_components(ROOT / "config/knowledge-v2.json")
        current = load_components(ROOT / "config/knowledge-v3.json").activate("knowledge")
        for key in ("K044", "K045"):
            old = query(previous, identifier=key)[0]
            new = query(current, identifier=key)[0]
            self.assertEqual(new["version"], "1.2.0")
            self.assertEqual({k: new[k] for k in old if k != "version"},
                             {k: v for k, v in old.items() if k != "version"})

        for key in ("K046", "K047", "K048", "K049", "K050", "CASE001", "CASE002"):
            with self.subTest(key=key):
                row = query(current, identifier=key)[0]
                self.assertEqual(row["id"], key)
                self.assertEqual(row["kind"], "case" if key.startswith("CASE") else "diagnosis")
                self.assertTrue(all((ROOT / path).is_file() for path in row["sources"]))
                self.assertEqual(set(row), {"id", "topics", "content", "sources", "applicability",
                                            "version", "kind", "evidence", "limits"})
                self.assertTrue(row["evidence"])
                self.assertTrue(all(isinstance(item, str) and item.strip() for item in row["evidence"]))
                self.assertTrue(isinstance(row["limits"], str) and row["limits"].strip())

    def test_cleanup_query_returns_independent_evidence_and_case(self):
        current = load_components(ROOT / "config/knowledge-v3.json").activate("knowledge")
        rows = query(current, topic="retaining_until_cleanup_readback")
        self.assertCountEqual([row["id"] for row in rows], ["K050", "CASE002"])
        case = next(row for row in rows if row["id"] == "CASE002")
        evidence = list(case["evidence"])
        case["evidence"].clear()
        self.assertEqual(query(current, identifier="CASE002")[0]["evidence"], evidence)

    def test_structured_knowledge_rejects_incomplete_or_unknown_fields(self):
        current = load_components(ROOT / "config/knowledge-v3.json")
        library = json.loads(current.knowledge_path.read_bytes())
        edition = library["version"]
        row = next(entry for entry in library["entries"] if entry["id"] == "K050")
        for key in ("kind", "evidence", "limits"):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                incomplete = {k: v for k, v in row.items() if k != key}
                query_library({**library, "entries": [incomplete]}, edition, identifier="K050")
        for key, value in (("kind", "unrecognized"), ("evidence", "not a list"),
                           ("evidence", []), ("evidence", [" "]), ("limits", " "),
                           ("offlineOutcome", "available only in replay")):
            with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                query_library({**library, "entries": [{**row, key: value}]}, edition, identifier="K050")
        with self.assertRaises(ValueError):
            query_library({**library, "entries": [row, dict(row)]}, edition, identifier="K050")

    def test_legacy_knowledge_ignores_unpublished_fields_during_replay(self):
        components = load_components(ROOT / "config/knowledge-v2.json")
        library = json.loads(components.knowledge_path.read_bytes())
        row = next(entry for entry in library["entries"] if entry["id"] == "K045")
        expected = {**row, "version": library["version"]}
        row["privateNote"] = "test input"
        self.assertEqual(query_library(library, library["version"], identifier="K045"), [expected])

    def test_child_profile_replaces_one_selector_and_preserves_other_ports(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            query = {"from": "diagnosis", "topics": {"demand_stale": ["K047"]}}
            parent = str(ROOT / "config/collab-opt-in.json")
            path.write_text(json.dumps({"extends": parent, "knowledge": {"bindings": {"om": query}}}), encoding="utf-8")
            ports = load_components(path).knowledge["bindings"]
            self.assertEqual(ports, {"om": query, "rules": {"topic": "detour guidance"},
                                     "model": {"topic": "detour guidance"}})
            for binding in ({"om": {"from": "diagnosis", "topics": {"demand_stale": []}}},
                            {"rules": {"from": "om"}}):
                path.write_text(json.dumps({"extends": parent, "knowledge": {"bindings": binding}}), encoding="utf-8")
                with self.subTest(binding=binding), self.assertRaisesRegex(ValueError, "knowledge binding"):
                    load_components(path)

    def test_all_configured_ids_are_validated_without_waiting_for_a_match(self):
        from knowledge.replay import verify
        from knowledge.work_order import BINDING_CONTRACT, validate_binding_edition
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            missing = "K046_typo"
            for om in ({"from": "diagnosis", "topics": {"never_triggered": [missing]}},
                       {"identifier": missing}):
                path.write_text(json.dumps({"extends": str(ROOT / "config/knowledge-v3.json"),
                                            "knowledge": {"bindings": {"om": om}}}), encoding="utf-8")
                components = load_components(path).activate("I")
                with self.subTest(om=om), self.assertRaisesRegex(ValueError, missing):
                    validate_binding_edition(components)
                conditions = {"components": components.identity, "knowledgeBindingContract": BINDING_CONTRACT}
                with self.assertRaisesRegex(ValueError, missing):
                    verify(conditions, [], {}, {})
                self.assertEqual(query(components, identifier=missing), [])
