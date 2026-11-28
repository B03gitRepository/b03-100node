from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from foundation_policy import FoundationActor, initial_parameters, probabilities, write_parameters
from foundation_training import update
from knowledge.agent_facts import evidence_digest


class FoundationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.ck = Path(self.sandbox.name) / "initial"
        write_parameters(self.ck, initial_parameters())
        self.catalog = {"nodes":{"oak":{},"elm":{}},"links":{"branch":{"effectiveParams":{"bandwidthBps":100}}},
                        "initialServices":{"voice":{"requirement":{"bandwidthBps":50}}}}

    def observation(self, *, drop=False, new=False):
        return {"legalActions":["B","I"], "links":{"columns":["id","usable"],"defaults":[True],"rows":[[0,0]]},
                "services":{"columns":["id","active","writable"],"defaults":[True,True],"rows":[[0,0]]},
                "networkReference":{"catalogDigest":evidence_digest(self.catalog),"serviceIds":["voice","image"],
                    "linksBelowStartupCapacity":{"branch":{"startupBps":100,"observedBps":40}} if drop else {},
                    "reportedOutsideInitialDefinitions":["image"] if new else []}}

    def actor(self):
        return FoundationActor(self.ck,{"mode":"online-ppo","options":{"drawSeed":137}})

    def test_catalogue_and_two_deltas_contribute_separately(self):
        actor = self.actor()
        actor.set_network_foundation(self.catalog)
        plain = actor.facts.encode(self.observation())
        drop = actor.facts.encode(self.observation(drop=True))
        new = actor.facts.encode(self.observation(new=True))
        self.assertGreater(plain[2],0)
        self.assertGreater(drop[6],plain[6])
        self.assertGreater(new[7],plain[7])
        broken = self.observation()
        broken["networkReference"]["catalogDigest"] = "0"*64
        with self.assertRaises(ValueError):
            actor.choose(broken)

    def test_restore_reproduces_next_actual_draw(self):
        actor = self.actor()
        actor.set_network_foundation(self.catalog)
        for _ in range(3):
            actor.choose(self.observation(drop=True))
        saved = self.ck.parent/"saved"
        actor.save_checkpoint(saved)
        expected = actor.choose(self.observation())
        rebuilt=self.actor()
        rebuilt.restore_checkpoint(saved)
        rebuilt.set_network_foundation(self.catalog)
        self.assertEqual(expected,rebuilt.choose(self.observation()))
        action, details = expected
        self.assertEqual(details["behaviorProbability"],details["behaviorProbabilities"][action])
        self.assertEqual(sum(details["behaviorProbabilities"].values()),1)

    def test_clipped_update_uses_return_and_keeps_legal_mask(self):
        actor=self.actor()
        actor.set_network_foundation(self.catalog)
        x=actor.facts.encode(self.observation(drop=True))
        samples=[(x,"B",0.5,["B","I"],1),(x,"I",0.5,["B","I"],-1)]
        before=initial_parameters()
        after,history=update(before,samples)
        self.assertEqual(after["version"],1)
        self.assertGreater(probabilities(after["weights"],x,["B","I"])["B"],0.5)
        self.assertGreater(history[0]["gradientNorm"],0)
        self.assertEqual(probabilities(after["weights"],x,["I"]),{"I":1.0})
        with self.assertRaises(ValueError):
            update(before,[(x,"B",0.5,["B","I"],0)])
