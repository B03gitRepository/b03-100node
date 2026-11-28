from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from foundation_policy import FEATURES, initial_parameters
from foundation_training import feature_evidence, matched_observations, update


class FoundationTrainingSignalTest(unittest.TestCase):
    def test_observed_feature_can_have_no_advantage_or_cancelled_gradient(self):
        base = initial_parameters()
        outage = [int(n in ("bias", "unusable_link_share")) for n in FEATURES]
        arrival = [int(n in ("bias", "new_request_share")) for n in FEATURES]
        regular = [int(n == "bias") for n in FEATURES]
        both = ["B", "I"]
        examples = [(outage, "I", 0.5, both, 1), (outage, "I", 0.5, both, -1),
                    (arrival, "B", 0.5, both, 0), (arrival, "I", 1.0, ["I"], 5),
                    (regular, "B", 0.5, both, 1)]
        after, history = update(base, examples)
        report = feature_evidence(base, after, examples, history[0]["gradientByFeature"])
        self.assertEqual(report["unusable_link_share"]["nonzeroAdvantageByAction"], {"B": 0, "I": 2})
        self.assertEqual(report["unusable_link_share"]["firstEpochNetGradient"], 0)
        self.assertEqual(report["new_request_share"]["presentBothLegalByAction"], {"B": 1, "I": 0})
        self.assertEqual(report["new_request_share"]["nonzeroAdvantageByAction"], {"B": 0, "I": 0})
        self.assertGreater(report["bias"]["weightAfter"], report["bias"]["weightBefore"])

    def test_matched_states_require_same_observation_and_conditions(self):
        def episode(key, action, remaining_u, state):
            obs = {"timeNs": 0, "state": state}
            return {"key": key, "ends": [5], "counts": [0], "prefix": [0, 0],
                    "result": {"startNs": 0, "unmetServiceNs": remaining_u,
                               "steps": [{"observation": obs, "action": action}]}}
        pairs = [episode("a", "B", 4, 1), episode("a", "I", 2, 1),
                 episode("a", "B", 3, 2), episode("a", "I", 3, 2),
                 episode("a", "B", 1, 3), episode("b", "I", 2, 3)]
        self.assertEqual(matched_observations(pairs),
                         {"statesWithBothActions": 2, "statesWithDifferentRemainingU": 1})
