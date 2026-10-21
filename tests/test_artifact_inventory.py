import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_artifacts", ROOT / "scripts" / "validate_artifacts.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ArtifactInventoryTests(unittest.TestCase):
    def test_inventory_matches_release_sources(self):
        result = MODULE.validate()
        self.assertEqual(result["status"], "valid")
        self.assertIn("controller-image", result["artifacts"])
        self.assertIn("helm-chart", result["artifacts"])


if __name__ == "__main__":
    unittest.main()
