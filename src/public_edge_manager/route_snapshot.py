"""Signed RouteSnapshot publication and last-known-good validation."""

import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load_private_key(path):
    with open(path, "rb") as stream:
        return serialization.load_pem_private_key(stream.read(), password=None)


def load_public_key(path):
    with open(path, "rb") as stream:
        return serialization.load_pem_public_key(stream.read())


def sign_snapshot(payload, private_key):
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("RouteSnapshot signing key must be Ed25519")
    document = dict(payload)
    document.pop("digest", None)
    document.pop("signature", None)
    digest = hashlib.sha256(canonical_json(document)).hexdigest()
    document["digest"] = f"sha256:{digest}"
    document["signature"] = base64.b64encode(private_key.sign(canonical_json(document))).decode()
    return document


def verify_snapshot(document, public_key, now=None, current=None, max_stale_seconds=0):
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("RouteSnapshot public key must be Ed25519")
    unsigned = dict(document)
    signature = base64.b64decode(unsigned.pop("signature", ""), validate=True)
    expected_digest = unsigned.pop("digest", "")
    digest = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    if expected_digest != f"sha256:{digest}":
        raise ValueError("RouteSnapshot digest mismatch")
    signed = dict(unsigned, digest=expected_digest)
    public_key.verify(signature, canonical_json(signed))
    if unsigned.get("schemaVersion") != "v1":
        raise ValueError("unsupported RouteSnapshot schema")
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    expires = _timestamp(unsigned["expiresAt"])
    if now > expires + max(0, max_stale_seconds):
        raise ValueError("RouteSnapshot expired beyond max stale interval")
    if current:
        old_version = int(current.get("policyVersion", 0))
        new_version = int(unsigned.get("policyVersion", 0))
        if new_version < old_version:
            raise ValueError("RouteSnapshot rollback rejected")
        old_serials = current.get("zones", {})
        for zone, item in unsigned.get("zones", {}).items():
            if zone in old_serials and int(item["serial"]) < int(old_serials[zone]["serial"]):
                raise ValueError(f"zone serial rollback rejected for {zone}")
    return document


def atomic_write_json(path, value):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".route-snapshot-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
