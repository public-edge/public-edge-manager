#!/usr/bin/env python3
"""Validate the public release inventory against repository build sources."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise ValueError(message)


def chart_versions() -> tuple[str, str]:
    text = (ROOT / "Chart.yaml").read_text()
    version = re.search(r"^version:\s*([^\s]+)\s*$", text, re.MULTILINE)
    app_version = re.search(r'^appVersion:\s*["\']?([^\s"\']+)', text, re.MULTILINE)
    if not version or not app_version:
        fail("Chart.yaml must declare version and appVersion")
    return version.group(1), app_version.group(1)


def validate(requested_version: str | None = None) -> dict[str, object]:
    inventory = json.loads((ROOT / "artifacts.json").read_text())
    if inventory.get("schemaVersion") != "public-edge.dev/artifacts/v1":
        fail("unsupported artifacts.json schemaVersion")

    chart_version, app_version = chart_versions()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    versions = {chart_version, app_version, project["version"]}
    if len(versions) != 1:
        fail(f"release versions disagree: {sorted(versions)}")
    version = versions.pop()
    if requested_version and requested_version.removeprefix("v") != version:
        fail(f"release version {requested_version!r} does not match {version!r}")
    readme = (ROOT / "README.md").read_text()
    if f"--version {version}" not in readme:
        fail(f"README install example does not use current version {version}")

    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        fail("artifacts.json must register at least one artifact")
    ids: set[str] = set()
    kinds: set[str] = set()
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        if not artifact_id or artifact_id in ids:
            fail(f"artifact id is missing or duplicated: {artifact_id!r}")
        ids.add(artifact_id)
        kinds.add(artifact.get("kind", ""))
        source = artifact.get("source")
        if not source or not (ROOT / source).is_file():
            fail(f"{artifact_id}: source does not exist: {source!r}")
        if "{version}" not in artifact.get("reference", ""):
            fail(f"{artifact_id}: reference must contain {{version}}")
        if not artifact.get("signed"):
            fail(f"{artifact_id}: signing method is not declared")

    required_kinds = {"oci-image", "helm-oci-chart", "github-release-assets"}
    if not required_kinds.issubset(kinds):
        fail(f"missing required artifact kinds: {sorted(required_kinds - kinds)}")

    for path in inventory.get("security", {}).get("automation", []):
        if not (ROOT / path).is_file():
            fail(f"security automation does not exist: {path}")

    release_workflow = (ROOT / inventory["release"]["workflow"]).read_text()
    for marker in (
        "ghcr.io/${{ github.repository }}",
        "routeros-authority",
        "charts/public-edge-manager",
        "release-manifest.json",
    ):
        if marker not in release_workflow:
            fail(f"release workflow does not publish registered artifact: {marker}")

    return {"version": version, "artifacts": sorted(ids), "status": "valid"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-version")
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.release_version), indent=2))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"artifact inventory validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
