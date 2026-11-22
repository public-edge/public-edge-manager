import importlib.util
import re
import unittest
from unittest import mock
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

    def test_old_default_image_cannot_ship_in_a_new_chart(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path == ROOT / "values.yaml":
                text = re.sub(r'^  tag:.*$', '  tag: "0.0.0"', text, flags=re.MULTILINE)
            return text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
            with self.assertRaisesRegex(ValueError, "release versions disagree"):
                MODULE.validate()


if __name__ == "__main__":
    unittest.main()
