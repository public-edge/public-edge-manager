#!/usr/bin/env python3
"""Verify release-manifest evidence files and their declared SHA-256 digests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def verify(manifest_path: Path, asset_directory: Path) -> dict[str, int]:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schemaVersion") != "public-edge.dev/release-manifest/v1":
        raise ValueError("unsupported release manifest schema")

    checked = 0
    artifact_ids = set()
    for artifact in manifest.get("artifacts", []):
        artifact_id = artifact.get("id")
        if not artifact_id or artifact_id in artifact_ids:
            raise ValueError(f"missing or duplicated artifact id: {artifact_id!r}")
        artifact_ids.add(artifact_id)
        evidence = artifact.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"{artifact_id}: evidence is required")
        for category in ("signatures", "provenance", "sboms"):
            entries = evidence.get(category)
            if not isinstance(entries, list):
                raise ValueError(f"{artifact_id}: {category} must be a list")
            for entry in entries:
                reference = entry.get("reference", "")
                declared = entry.get("digest")
                if not declared:
                    continue
                if reference.startswith(("ghcr.io/", "oci://")):
                    if not reference.endswith(f"@{declared}"):
                        raise ValueError(f"{artifact_id}: external evidence digest does not match reference")
                    checked += 1
                    continue
                if Path(reference).name != reference:
                    raise ValueError(f"{artifact_id}: evidence reference must be a release filename")
                path = asset_directory / reference
                if not path.is_file():
                    raise ValueError(f"{artifact_id}: missing evidence file {reference}")
                actual = sha256(path)
                if actual != declared:
                    raise ValueError(
                        f"{artifact_id}: digest mismatch for {reference}: {declared} != {actual}"
                    )
                checked += 1

    required = {"controller-image", "routeros-authority-image", "helm-chart"}
    if artifact_ids != required:
        raise ValueError(f"release manifest artifacts differ: {sorted(artifact_ids)}")
    return {"artifacts": len(artifact_ids), "evidenceFiles": checked}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_release_evidence.py MANIFEST ASSET_DIRECTORY", file=sys.stderr)
        return 2
    try:
        result = verify(Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release evidence verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
