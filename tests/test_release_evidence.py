import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_release_evidence", ROOT / "scripts" / "verify_release_evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ReleaseEvidenceTests(unittest.TestCase):
    def test_verifies_declared_evidence_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for name in ("controller.sbom.json", "routeros.sbom.json", "chart.provenance.json"):
                path = root / name
                path.write_text(json.dumps({"name": name}))
                files[name] = MODULE.sha256(path)
            artifacts = []
            for artifact_id, name in (
                ("controller-image", "controller.sbom.json"),
                ("routeros-authority-image", "routeros.sbom.json"),
                ("helm-chart", "chart.provenance.json"),
            ):
                artifacts.append({
                    "id": artifact_id,
                    "reference": artifact_id,
                    "evidence": {
                        "signatures": [],
                        "provenance": [{"reference": name, "digest": files[name]}],
                        "sboms": [],
                    },
                })
            manifest = root / "release-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": "public-edge.dev/release-manifest/v1",
                "artifacts": artifacts,
            }))
            self.assertEqual(MODULE.verify(manifest, root), {"artifacts": 3, "evidenceFiles": 3})

    def test_rejects_modified_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("original")
            digest = MODULE.sha256(evidence)
            evidence.write_text("changed")
            manifest = root / "release-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": "public-edge.dev/release-manifest/v1",
                "artifacts": [
                    {"id": artifact_id, "reference": artifact_id, "evidence": {
                        "signatures": [],
                        "provenance": ([{"reference": "evidence.json", "digest": digest}]
                                       if artifact_id == "controller-image" else []),
                        "sboms": [],
                    }}
                    for artifact_id in ("controller-image", "routeros-authority-image", "helm-chart")
                ],
            }))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                MODULE.verify(manifest, root)


if __name__ == "__main__":
    unittest.main()
