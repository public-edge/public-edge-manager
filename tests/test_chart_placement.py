"""Exercise the rendered chart, including Kubernetes node-field constraints."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("helm"), "Helm is required for chart rendering")
class ChartPlacementTests(unittest.TestCase):
    def terms(self, names):
        rendered = subprocess.check_output([
            "helm", "template", "placement-test", str(ROOT),
            "-f", str(ROOT / "examples/values-example.yaml"), "--set", "enabled=true", "--set-json", f"workload.nodeNames={json.dumps(names)}",
        ], text=True)
        workload = next(obj for obj in yaml.safe_load_all(rendered)
                        if obj and obj.get("kind") == "DaemonSet"
                        and obj["metadata"]["name"] == "public-edge-manager")
        return workload["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]

    def test_explicit_nodes_are_independent_alternatives(self):
        for names in (["authority-a"], ["authority-a", "authority-b"]):
            with self.subTest(names=names):
                terms = self.terms(names)
                self.assertEqual(len(terms), len(names))
                for term, name in zip(terms, names):
                    self.assertNotIn("matchExpressions", term)
                    self.assertEqual(term["matchFields"], [{
                        "key": "metadata.name", "operator": "In", "values": [name]}])

    def test_empty_node_names_preserves_discovery_placement(self):
        terms = self.terms([])
        self.assertEqual(len(terms), 1)
        self.assertNotIn("matchFields", terms[0])
        self.assertEqual(terms[0]["matchExpressions"][0]["values"], ["true"])
