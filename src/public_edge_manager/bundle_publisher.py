"""Publish the same desired DNS data to an authority outside Kubernetes.

The publisher is a control-plane process, not a DNS server.  It never asks a
regional authority for DNS answers or AXFR data.
"""

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .authority import candidates_from_public_edges
from .controller import api


NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
RECORDS_CONFIGMAP = os.environ.get("RECORDS_CONFIGMAP", "public-edge-manager-records")
RUNTIME_CONFIGMAP = os.environ.get("RUNTIME_CONFIGMAP", "public-edge-runtime")
NAMESERVER_CONFIGMAP = os.environ.get("NAMESERVER_CONFIGMAP", "public-edge-nameservers")
KEY_FILE = os.environ.get("BUNDLE_HMAC_KEY_FILE", "/run/bundle-key/key")


def configmap_json(name, key):
    obj = api(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{name}")
    return json.loads(obj["data"][key])


def build_payload(now=None):
    records = configmap_json(RECORDS_CONFIGMAP, "records.json")
    runtime = configmap_json(RUNTIME_CONFIGMAP, "runtime.json")
    nameservers = configmap_json(NAMESERVER_CONFIGMAP, "nameservers.json")
    definitions = dict(records["services"])
    for name, definition in runtime.get("services", {}).items():
        fqdn = f"{name.rstrip('.').lower()}."
        if definition is None:
            definitions.pop(fqdn, None)
        else:
            definitions[fqdn] = definition
    candidates = candidates_from_public_edges(definitions)
    group = os.getenv("FABRIC_EVIDENCE_API_GROUP", "networking.re8ch.com")
    version = os.getenv("FABRIC_EVIDENCE_API_VERSION", "v1alpha2")
    resource = os.getenv("FABRIC_EVIDENCE_RESOURCE", "networkpathassessments")
    items = api(f"/apis/{group}/{version}/{resource}")["items"]
    assessments = {
        item["spec"]["subjectRef"]["name"]: {
            "metadata": {"name": item["metadata"]["name"]},
            "spec": {"subjectRef": item["spec"]["subjectRef"],
                     "scope": item["spec"].get("scope", {})},
            "status": item.get("status", {}),
        }
        for item in items
        if item.get("spec", {}).get("subjectRef", {}).get("kind") == "Node"
        and item.get("spec", {}).get("subjectRef", {}).get("name")
        and item.get("spec", {}).get("scope", {}).get("plane") in ("host-and-pod", "pod")
    }
    nodes = api("/api/v1/nodes")["items"]
    node_readiness = {
        item["metadata"]["name"]: any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", []))
        for item in nodes if not item.get("metadata", {}).get("deletionTimestamp")
    }
    if not candidates or not nameservers.get("nameservers") or not records.get("zones"):
        raise ValueError("incomplete authority publication")
    return {
        "schemaVersion": 1,
        "generatedAt": int(time.time() if now is None else now),
        "records": records,
        "runtime": runtime,
        "nameservers": nameservers,
        "candidates": candidates,
        "fabric": {"assessments": assessments, "nodeReadiness": node_readiness},
        "policy": {
            "clientAreaCidrs": json.loads(os.getenv("CLIENT_AREA_CIDRS_JSON", "{}")),
            "fabricEvidenceMode": os.getenv("FABRIC_EVIDENCE_MODE", "Disabled"),
            "fabricAllowedStates": json.loads(os.getenv("FABRIC_EVIDENCE_ALLOWED_STATES_JSON", '["Ready","Partial"]')),
            "fabricRequireNodeReady": os.getenv("FABRIC_REQUIRE_NODE_READY", "true").lower() == "true",
        },
    }


def signed_bundle(payload, key):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"payload": payload,
            "hmacSha256": hmac.new(key, canonical, hashlib.sha256).hexdigest()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/authority-bundle":
            self.send_error(404)
            return
        try:
            with open(KEY_FILE, "rb") as stream:
                key = stream.read().strip()
            if len(key) < 32:
                raise ValueError("bundle signing key is too short")
            body = json.dumps(signed_bundle(build_payload(), key),
                              separators=(",", ":")).encode()
        except (OSError, KeyError, ValueError, TypeError) as exc:
            print(f"bundle publication failed: {type(exc).__name__}: {exc}", flush=True)
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    address = os.environ["BUNDLE_BIND_ADDRESS"]
    ThreadingHTTPServer((address, int(os.getenv("BUNDLE_PORT", "18553"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
