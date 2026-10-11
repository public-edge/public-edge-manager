"""Run the full Public Edge authority on RouterOS from a signed shared bundle."""

import hashlib
import hmac
import ipaddress
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path


CONFIG = json.loads(Path(os.getenv("AUTHORITY_CONFIG", "/config/authority.json")).read_text())
DATA = Path(CONFIG.get("bundleDirectory", "/state"))
CACHE = Path(CONFIG.get("bundleCacheDirectory", "/data"))
KEY = Path(CONFIG.get("bundleKeyFile", "/config/bundle.key"))
URL = CONFIG["bundleUrl"]
INTERVAL = max(30, int(CONFIG.get("bundleRefreshSeconds", 60)))
MAX_AGE = max(300, int(CONFIG.get("bundleMaxAgeSeconds", 86400)))
FRESHNESS_MARKER = Path("/dev/shm/portable-bundle-fresh")


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def verify_bundle(envelope, key, now=None, check_freshness=True):
    payload = envelope["payload"]
    signature = envelope["hmacSha256"]
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("unsupported authority bundle")
    expected = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise ValueError("invalid authority bundle signature")
    age = (time.time() if now is None else now) - payload["generatedAt"]
    if check_freshness and (age < -300 or age > MAX_AGE):
        raise ValueError("authority bundle is not fresh")
    for name in ("records", "runtime", "nameservers", "candidates", "fabric", "policy"):
        if name not in payload:
            raise ValueError(f"authority bundle is missing {name}")
    if not payload["records"].get("zones") or not payload["nameservers"].get("nameservers"):
        raise ValueError("authority bundle has no zones or nameservers")
    if not isinstance(payload["candidates"], list) or not payload["candidates"]:
        raise ValueError("authority bundle has no candidates")
    return payload


def write_if_changed(path, value):
    raw = canonical(value)
    if path.exists() and path.read_bytes() == raw:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(raw)
        temporary = stream.name
    os.replace(temporary, path)
    return True


def install_bundle(payload):
    changed = []
    for name in ("records", "runtime", "nameservers", "candidates", "fabric", "policy"):
        if write_if_changed(DATA / f"{name}.json", payload[name]):
            changed.append(name)
    if "policy" in changed and "public_edge_manager.authority" in sys.modules:
        authority = sys.modules["public_edge_manager.authority"]
        authority.CLIENT_AREA_CIDRS = payload["policy"]["clientAreaCidrs"]
        authority.FABRIC_EVIDENCE_MODE = payload["policy"]["fabricEvidenceMode"]
        authority.FABRIC_EVIDENCE_ALLOWED_STATES = set(payload["policy"]["fabricAllowedStates"])
        authority.FABRIC_REQUIRE_NODE_READY = payload["policy"]["fabricRequireNodeReady"]
    # This timestamp is volatile and only drives freshness checks.  It does
    # not create flash writes for every unchanged publication.
    return changed


def fetch_once():
    parsed = urllib.parse.urlparse(URL)
    if parsed.scheme == "http":
        try:
            private = ipaddress.ip_address(parsed.hostname).is_private
        except (TypeError, ValueError):
            private = False
        if not CONFIG.get("allowPrivateHttp", False) or not private:
            raise ValueError("HTTP bundle URL requires a private IP and explicit opt-in")
    elif parsed.scheme != "https":
        raise ValueError("authority bundle URL must use HTTPS")
    with urllib.request.urlopen(URL, timeout=5) as response:
        if response.status != 200 or response.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("invalid authority bundle response")
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("authority bundle exceeds size limit")
    key = KEY.read_bytes().strip()
    if len(key) < 32:
        raise ValueError("bundle verification key is too short")
    envelope = json.loads(raw)
    payload = verify_bundle(envelope, key)
    checkpoint = CACHE / "envelope.json"
    critical_changed = any(
        not (DATA / f"{name}.json").exists() or
        (DATA / f"{name}.json").read_bytes() != canonical(payload[name])
        for name in ("records", "runtime", "nameservers", "policy"))
    if critical_changed or not checkpoint.exists() or time.time() - checkpoint.stat().st_mtime >= 21600:
        write_if_changed(checkpoint, envelope)
    changed = install_bundle(payload)
    FRESHNESS_MARKER.touch()
    return changed


def refresh_loop():
    while True:
        try:
            changed = fetch_once()
            if changed:
                print(f"authority bundle updated: {','.join(changed)}", flush=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"authority bundle refresh failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(INTERVAL)


def main():
    try:
        fetch_once()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"initial bundle fetch failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            envelope = json.loads((CACHE / "envelope.json").read_text())
            cached = verify_bundle(envelope, KEY.read_bytes().strip(), check_freshness=False)
            install_bundle(cached)
            print("using last signed authority publication", flush=True)
        except (OSError, ValueError, KeyError, TypeError) as cached_exc:
            raise RuntimeError("no verified authority publication available") from cached_exc
    if not all((DATA / f"{name}.json").exists()
               for name in ("records", "runtime", "nameservers", "candidates", "fabric", "policy")):
        raise RuntimeError("no complete authority bundle available")
    os.environ.update({
        "RECORDS_FILE": str(DATA / "records.json"),
        "RUNTIME_FILE": str(DATA / "runtime.json"),
        "NAMESERVERS_FILE": str(DATA / "nameservers.json"),
        "CANDIDATES_FILE": str(DATA / "candidates.json"),
        "NODE_NAME": CONFIG.get("nodeName", "routeros-r640"),
        "BIND_ADDRESS": CONFIG.get("listenAddress", "0.0.0.0"),
        "DNS_PORT": str(CONFIG.get("listenPort", 53)),
        "HTTP_PORT": str(CONFIG.get("healthPort", 8080)),
        "SOA_RNAME": CONFIG.get("soaRname", "hostmaster.re8ch.com."),
        "CLIENT_AREA_CIDRS_JSON": json.dumps(json.loads((DATA / "policy.json").read_text())["clientAreaCidrs"]),
        "FABRIC_BUNDLE_FILE": str(DATA / "fabric.json"),
        "FABRIC_EVIDENCE_MODE": json.loads((DATA / "policy.json").read_text())["fabricEvidenceMode"],
        "FABRIC_EVIDENCE_ALLOWED_STATES_JSON": json.dumps(json.loads((DATA / "policy.json").read_text())["fabricAllowedStates"]),
        "FABRIC_REQUIRE_NODE_READY": str(json.loads((DATA / "policy.json").read_text())["fabricRequireNodeReady"]).lower(),
        "SECONDARY_NAMESERVERS_BY_ZONE_JSON": json.dumps(CONFIG.get("secondaryNameserversByZone", {})),
        "NAMESERVERS": "bootstrap.invalid.",
        "PUBLICATION_ENABLED": "false",
        "BUNDLE_FRESHNESS_FILE": str(FRESHNESS_MARKER),
        "BUNDLE_MAX_AGE_SECONDS": str(MAX_AGE),
    })
    threading.Thread(target=refresh_loop, daemon=True).start()
    from public_edge_manager.authority import main as authority_main
    authority_main()


if __name__ == "__main__":
    main()
