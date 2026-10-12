import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from public_edge_manager.route_snapshot import atomic_write_json, sign_snapshot, verify_snapshot


def payload(version=2, serial=100):
    now = datetime.now(timezone.utc)
    return {
        "schemaVersion": "v1", "gitRevision": "abc123", "policyVersion": version,
        "generatedAt": now.isoformat(), "expiresAt": (now + timedelta(minutes=5)).isoformat(),
        "zones": {"service.re8ch.com.": {"serial": serial}},
        "regions": {"CN": [{"id": "edge-cn", "ip": "192.0.2.10", "healthy": False}]},
    }


def test_signed_snapshot_round_trip():
    private = Ed25519PrivateKey.generate()
    signed = sign_snapshot(payload(), private)
    assert verify_snapshot(signed, private.public_key())["gitRevision"] == "abc123"


def test_bad_signature_and_digest_are_rejected():
    private = Ed25519PrivateKey.generate()
    signed = sign_snapshot(payload(), private)
    signed["gitRevision"] = "tampered"
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_snapshot(signed, private.public_key())


def test_policy_and_zone_rollbacks_are_rejected():
    private = Ed25519PrivateKey.generate()
    current = sign_snapshot(payload(version=3, serial=101), private)
    with pytest.raises(ValueError, match="rollback"):
        verify_snapshot(sign_snapshot(payload(version=2, serial=101), private), private.public_key(), current=current)
    with pytest.raises(ValueError, match="serial rollback"):
        verify_snapshot(sign_snapshot(payload(version=3, serial=100), private), private.public_key(), current=current)


def test_expiry_and_atomic_last_known_good(tmp_path):
    private = Ed25519PrivateKey.generate()
    value = payload()
    now = datetime.now(timezone.utc)
    value["expiresAt"] = (now - timedelta(seconds=10)).isoformat()
    signed = sign_snapshot(value, private)
    with pytest.raises(ValueError, match="expired"):
        verify_snapshot(signed, private.public_key(), now=now.timestamp())
    assert verify_snapshot(signed, private.public_key(), now=now.timestamp(), max_stale_seconds=30)
    target = tmp_path / "snapshot.json"
    atomic_write_json(target, signed)
    assert json.loads(target.read_text())["digest"] == signed["digest"]
