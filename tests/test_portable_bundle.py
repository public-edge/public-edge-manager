import importlib.util
import json
from pathlib import Path

import pytest

from public_edge_manager import bundle_publisher
from public_edge_manager import authority


def portable(tmp_path, monkeypatch):
    config = tmp_path / "authority.json"
    config.write_text(json.dumps({"bundleUrl": "https://bundle.example.test/v1/authority-bundle",
                                  "bundleDirectory": str(tmp_path / "data")}))
    monkeypatch.setenv("AUTHORITY_CONFIG", str(config))
    spec = importlib.util.spec_from_file_location(
        "portable_under_test", Path(__file__).parents[1] / "routeros" / "portable.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_payload():
    return {
        "schemaVersion": 1, "generatedAt": 1000,
        "records": {"zones": ["api.example.test."], "services": {}, "externalRecords": {}},
        "runtime": {}, "nameservers": {"nameservers": ["ns.example.test."]},
        "candidates": [{"id": "edge-1", "ip": "192.0.2.1", "region": "test", "area": "test",
                        "probes": {}}],
        "fabric": {"assessments": {}, "nodeReadiness": {"edge-1": True}},
        "policy": {"clientAreaCidrs": {}, "fabricEvidenceMode": "Required",
                   "fabricAllowedStates": ["Ready", "Partial"], "fabricRequireNodeReady": True},
    }


def test_signed_bundle_rejects_tampering_and_stale_data(tmp_path, monkeypatch):
    module = portable(tmp_path, monkeypatch)
    key = b"test-key" * 8
    envelope = bundle_publisher.signed_bundle(sample_payload(), key)
    assert module.verify_bundle(envelope, key, now=1001)["records"]["zones"]
    envelope["payload"]["records"]["zones"] = ["other.example.test."]
    with pytest.raises(ValueError, match="signature"):
        module.verify_bundle(envelope, key, now=1001)
    envelope = bundle_publisher.signed_bundle(sample_payload(), key)
    with pytest.raises(ValueError, match="fresh"):
        module.verify_bundle(envelope, key, now=1000 + module.MAX_AGE + 1)


def test_bundle_writes_only_changed_data(tmp_path, monkeypatch):
    module = portable(tmp_path, monkeypatch)
    payload = sample_payload()
    assert set(module.install_bundle(payload)) == {"records", "runtime", "nameservers", "candidates", "fabric", "policy"}
    assert module.install_bundle(payload) == []
    payload["candidates"][0]["ip"] = "192.0.2.2"
    assert module.install_bundle(payload) == ["candidates"]


def test_publisher_uses_shared_records_and_live_candidate_catalog(monkeypatch):
    source = {"records.json": sample_payload()["records"],
              "runtime.json": {"services": {}},
              "nameservers.json": sample_payload()["nameservers"]}
    monkeypatch.setattr(bundle_publisher, "configmap_json", lambda _name, key: source[key])
    monkeypatch.setattr(bundle_publisher, "candidates_from_public_edges", lambda definitions: sample_payload()["candidates"])
    assessment = {
        "metadata": {"name": "path-edge-1"},
        "spec": {"subjectRef": {"kind": "Node", "name": "edge-1"},
                 "scope": {"plane": "host-and-pod"}},
        "status": {"state": "Ready", "validUntil": "2099-01-01T00:00:00Z",
                   "nodeReady": True, "conditions": [{"type": "EvidenceReady", "status": "True"}],
                   "pathEvidence": {"currentPathMeasured": True, "reachable": True}},
    }
    monkeypatch.setattr(bundle_publisher, "api", lambda path: {
        "items": [assessment] if "networkpathassessments" in path else
        [{"metadata": {"name": "edge-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]})
    payload = bundle_publisher.build_payload(now=1000)
    assert payload["records"] == source["records.json"]
    assert payload["candidates"][0]["id"] == "edge-1"
    assert payload["fabric"]["assessments"]["edge-1"]["status"]["pathEvidence"]["reachable"]
    assert payload["fabric"]["nodeReadiness"]["edge-1"]


def test_portable_authority_evaluates_published_advfab_evidence(tmp_path, monkeypatch):
    module = portable(tmp_path, monkeypatch)
    payload = sample_payload()
    payload["fabric"]["assessments"]["edge-1"] = {
        "metadata": {"name": "path-edge-1"},
        "status": {"state": "Ready", "validUntil": "2099-01-01T00:00:00Z",
                   "nodeReady": True, "conditions": [{"type": "EvidenceReady", "status": "True"}],
                   "pathEvidence": {"currentPathMeasured": True, "reachable": True}},
    }
    module.install_bundle(payload)
    monkeypatch.setattr(authority, "FABRIC_BUNDLE_FILE", str(module.DATA / "fabric.json"))
    monkeypatch.setattr(authority, "FABRIC_EVIDENCE_MODE", "Required")
    monkeypatch.setattr(authority, "FABRIC_REQUIRE_NODE_READY", True)
    authority.refresh_fabric_assessments()
    assert authority.fabric_evidence({"nodeName": "edge-1"})["eligible"]
