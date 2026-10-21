import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("simulation", ROOT / "research" / "generate.py")
SIMULATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SIMULATION)


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "research" / "scenarios.json").read_text())
        cls.rows, cls.runs = SIMULATION.evaluate(cls.config)

    def test_all_nine_cells_are_covered(self):
        self.assertEqual({run["scenario"] for run in self.runs}, {
            f"{network}-{resource}"
            for network in SIMULATION.NETWORKS for resource in SIMULATION.RESOURCES
        })

    def test_failed_scarce_fails_closed(self):
        runs = [run for run in self.runs if run["scenario"] == "failed-scarce"]
        self.assertTrue(runs)
        self.assertTrue(all(run["failClosed"] for run in runs))

    def test_failed_adequate_uses_an_explainable_alternative(self):
        baseline = next(run for run in self.runs
                        if run["scenario"] == "failed-adequate" and run["scan"] == "baseline")
        self.assertEqual(baseline["selected"], "node-02")
        self.assertFalse(baseline["failClosed"])

    def test_ineligible_high_capacity_candidate_never_wins(self):
        affected_steps = {
            row["timeStep"] for row in self.rows
            if row["readiness"] == "ineligible" and row["capacityMbps"] == 1000
        }
        for step in affected_steps:
            selected = [row for row in self.rows if row["timeStep"] == step and row["selected"]]
            self.assertTrue(all(row["readiness"] == "ready" for row in selected))

    def test_failure_masks_cover_every_combination(self):
        for resource, candidates in self.config["resources"].items():
            masks = {run["failureMask"] for run in self.runs
                     if run["network"] == "healthy" and run["resources"] == resource
                     and run["scan"] == "failure-mask"}
            self.assertEqual(masks, set(range(1 << len(candidates))))

    def test_evaluation_is_deterministic(self):
        rows2, runs2 = SIMULATION.evaluate(self.config)
        self.assertEqual(self.rows, rows2)
        self.assertEqual(self.runs, runs2)


if __name__ == "__main__":
    unittest.main()
