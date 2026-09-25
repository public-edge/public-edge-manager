#!/usr/bin/env python3
import http.client
import ipaddress
import json
import os
import hashlib
import socket
import socketserver
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.getenv("NODE_NAME", "unknown")
PROBE_CONNECT_TIMEOUT = float(os.getenv("PROBE_CONNECT_TIMEOUT_SECONDS", "5"))
PROBE_RESPONSE_TIMEOUT = float(os.getenv("PROBE_RESPONSE_TIMEOUT_SECONDS", "12"))
PROBE_INTERVAL_SECONDS = max(1, int(os.getenv("PROBE_INTERVAL_SECONDS", "3")))
PROBE_MAX_WORKERS = max(1, int(os.getenv("PROBE_MAX_WORKERS", "8")))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
DNS_PORT = int(os.getenv("DNS_PORT", "53"))
NAMESERVERS = os.getenv("NAMESERVERS", "").split()
NAMESERVERS_FILE = os.getenv("NAMESERVERS_FILE", "")
NAMESERVERS_DIGEST = ""
AUTHORITY_ZONES = sorted({
    f"{str(zone).rstrip('.').lower()}."
    for zone in json.loads(os.getenv("AUTHORITY_ZONES_JSON", "[]"))
    if str(zone).strip()
}, key=len, reverse=True)
EXTERNAL_RECORDS = {
    f"{name.rstrip('.').lower()}.": records
    for name, records in json.loads(os.getenv("EXTERNAL_RECORDS_JSON", "{}")).items()
}
CANDIDATES = json.loads(os.getenv("CANDIDATES_JSON", "[]"))
SERVICE_DEFINITIONS = json.loads(os.getenv("SERVICES_JSON", "{}"))
SERVICES = {name: definition["service"] for name, definition in SERVICE_DEFINITIONS.items()}
REGION = "unknown"
AREA = "GLOBAL"
CLIENT_AREA_CIDRS = json.loads(os.getenv("CLIENT_AREA_CIDRS_JSON", "{}"))
LOCK = threading.Lock()
HEALTH = {service: {} for service in SERVICES.values()}
KUBERNETES_API = os.getenv("KUBERNETES_SERVICE_HOST", "")
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")
SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
PUBLICATION_REFS = json.loads(os.getenv("PUBLICATION_REFS_JSON", "{}"))
PUBLICATION_ADAPTERS = json.loads(os.getenv("PUBLICATION_ADAPTERS_JSON", "{}"))
PUBLICATION_ENABLED = os.getenv("PUBLICATION_ENABLED", "false").lower() == "true"
READINESS_GATES = json.loads(os.getenv("READINESS_GATES_JSON", "{}"))
SOA_RNAME = os.getenv("SOA_RNAME", "hostmaster.invalid.")
USER_AGENT = os.getenv("PROBE_USER_AGENT", "public-edge-manager/0.3")
CANDIDATE_CAPACITY_WEIGHT = max(0, int(os.getenv("CANDIDATE_CAPACITY_WEIGHT", "10")))
CANDIDATE_LOCAL_AREA_BONUS = max(0, int(os.getenv("CANDIDATE_LOCAL_AREA_BONUS", "100000")))
CANDIDATE_PRIORITY_WEIGHT = max(0, int(os.getenv("CANDIDATE_PRIORITY_WEIGHT", "1")))
CANDIDATE_LATENCY_DIVISOR_MS = max(1, int(os.getenv("CANDIDATE_LATENCY_DIVISOR_MS", "20")))
CANDIDATE_LATENCY_PENALTY_CAP = max(0, int(os.getenv("CANDIDATE_LATENCY_PENALTY_CAP", "50")))
CANDIDATE_LOCAL_NODE_BONUS = max(0, int(os.getenv("CANDIDATE_LOCAL_NODE_BONUS", "5")))
CANDIDATE_FAILOVER_GRACE_SECONDS = max(0, int(os.getenv("CANDIDATE_FAILOVER_GRACE_SECONDS", "300")))
CANDIDATE_MIN_READY_SECONDS = max(0, int(os.getenv("CANDIDATE_MIN_READY_SECONDS", "120")))
CANDIDATE_MIN_HOLD_SECONDS = max(0, int(os.getenv("CANDIDATE_MIN_HOLD_SECONDS", "600")))
SELECTIONS = {}
FABRIC_EVIDENCE_MODE = os.getenv("FABRIC_EVIDENCE_MODE", "Disabled")
FABRIC_EVIDENCE_API_GROUP = os.getenv("FABRIC_EVIDENCE_API_GROUP", "networking.re8ch.com")
FABRIC_EVIDENCE_API_VERSION = os.getenv("FABRIC_EVIDENCE_API_VERSION", "v1alpha2")
FABRIC_EVIDENCE_RESOURCE = os.getenv("FABRIC_EVIDENCE_RESOURCE", "networkpathassessments")
FABRIC_EVIDENCE_ALLOWED_STATES = set(json.loads(os.getenv("FABRIC_EVIDENCE_ALLOWED_STATES_JSON", '["Ready","Partial"]')))
FABRIC_REQUIRE_NODE_READY = os.getenv("FABRIC_REQUIRE_NODE_READY", "true").lower() == "true"
FABRIC_ASSESSMENTS = {}
FABRIC_API_AVAILABLE = False
FABRIC_NODE_READINESS = {}
FABRIC_NODE_API_AVAILABLE = False
RECORDS_FILE = os.getenv("RECORDS_FILE", "")
RUNTIME_FILE = os.getenv("RUNTIME_FILE", "")
RECORDS_DIGEST = ""
RUNTIME_MODEL = {}


def reload_nameservers():
    """Accept a projected election result without restarting DNS listeners."""
    global NAMESERVERS, NAMESERVERS_DIGEST
    if not NAMESERVERS_FILE:
        return False
    try:
        with open(NAMESERVERS_FILE, "rb") as stream:
            raw = stream.read()
        digest = hashlib.sha256(raw).hexdigest()
        if digest == NAMESERVERS_DIGEST:
            return False
        payload = json.loads(raw)
        nameservers = payload["nameservers"]
        if not isinstance(nameservers, list) or not 1 <= len(nameservers) <= 3:
            raise ValueError("expected one to three nameservers")
        if len(set(nameservers)) != len(nameservers):
            raise ValueError("duplicate nameserver")
        for name in nameservers:
            if not isinstance(name, str) or not name.endswith("."):
                raise ValueError("nameserver must be an absolute DNS name")
            encode_name(name)
        with LOCK:
            NAMESERVERS = nameservers
            NAMESERVERS_DIGEST = digest
        print(f"nameservers reloaded digest={digest[:12]} count={len(nameservers)}", flush=True)
        return True
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"nameservers reload failed: {exc}", flush=True)
        return False


def reload_records():
    """Atomically accept a projected ConfigMap update; retain last good data on error."""
    global RECORDS_DIGEST, SERVICE_DEFINITIONS, SERVICES, AUTHORITY_ZONES, EXTERNAL_RECORDS, RUNTIME_MODEL
    if not RECORDS_FILE:
        return False
    try:
        with open(RECORDS_FILE, "rb") as stream:
            raw = stream.read()
        payload = json.loads(raw)
        runtime = {"services": {}, "routes": {}, "model": {}}
        runtime_raw = b""
        if RUNTIME_FILE:
            try:
                with open(RUNTIME_FILE, "rb") as stream:
                    runtime_raw = stream.read()
                runtime.update(json.loads(runtime_raw or b"{}"))
            except FileNotFoundError:
                pass
        digest = hashlib.sha256(raw + b"\0" + runtime_raw).hexdigest()
        if digest == RECORDS_DIGEST:
            return False
        definitions = dict(payload["services"])
        for name, definition in runtime.get("services", {}).items():
            fqdn = f"{name.rstrip('.').lower()}."
            if definition is None:
                definitions.pop(fqdn, None)
            else:
                definitions[fqdn] = definition
        for service, paths in runtime.get("routes", {}).items():
            for definition in definitions.values():
                if definition.get("service") == service:
                    definition["paths"] = paths
        zones = payload["zones"]
        external = payload["externalRecords"]
        if not isinstance(definitions, dict) or not definitions or not isinstance(zones, list) or not isinstance(external, dict):
            raise ValueError("invalid record configuration")
        services = {name: definition["service"] for name, definition in definitions.items()}
        authority_zones = sorted({f"{str(zone).rstrip('.').lower()}." for zone in zones if str(zone).strip()}, key=len, reverse=True)
        external_records = {f"{name.rstrip('.').lower()}.": records for name, records in external.items()}
        for name, records in external_records.items():
            zone = next((zone for zone in authority_zones if name == zone or name.endswith(f".{zone}")), None)
            if not zone or name == zone or name in services or not isinstance(records, list) or not records:
                raise ValueError(f"external record {name} conflicts with a zone or managed service")
            if not all(isinstance(item, dict) for item in records):
                raise ValueError(f"external record {name} must contain record objects")
            if any(item.get("type") not in ("A", "AAAA", "CNAME") or not item.get("externalCDN") for item in records):
                raise ValueError(f"external record {name} requires supported type and externalCDN marker")
            if any(item["type"] == "CNAME" for item in records) and len(records) != 1:
                raise ValueError(f"external CNAME {name} must be the only record at its name")
            for item in records:
                if item["type"] == "CNAME":
                    encode_name(item["value"])
                elif item["type"] == "A":
                    ipaddress.IPv4Address(item["value"])
                else:
                    ipaddress.IPv6Address(item["value"])
        if not isinstance(runtime.get("model", {}), dict):
            raise ValueError("runtime model must be an object")
        with LOCK:
            unchanged_services = {
                definition["service"] for name, definition in definitions.items()
                if SERVICE_DEFINITIONS.get(name) == definition
            }
            previous_health = {service: HEALTH[service] for service in unchanged_services if service in HEALTH}
            SERVICE_DEFINITIONS = definitions
            SERVICES = services
            AUTHORITY_ZONES = authority_zones
            EXTERNAL_RECORDS = external_records
            RUNTIME_MODEL = runtime.get("model", {})
            HEALTH.clear()
            HEALTH.update({service: previous_health.get(service, {}) for service in services.values()})
            RECORDS_DIGEST = digest
        print(f"records reloaded digest={digest[:12]} services={len(services)}", flush=True)
        return True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"records reload failed: {exc}", flush=True)
        return False


def kubernetes_get(path):
    if not KUBERNETES_API:
        return None
    try:
        with open(f"{SERVICE_ACCOUNT}/token", encoding="utf-8") as stream:
            token = stream.read().strip()
        context = ssl.create_default_context(cafile=f"{SERVICE_ACCOUNT}/ca.crt")
        request = urllib.request.Request(
            f"https://{KUBERNETES_API}:{os.getenv('KUBERNETES_SERVICE_PORT_HTTPS', '443')}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, context=context, timeout=3) as response:
            return json.load(response)
    except Exception as exc:
        print(f"kubernetes_get path={path} error={exc}", flush=True)
        return None


def kubernetes_patch(path, payload):
    with open(f"{SERVICE_ACCOUNT}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    context = ssl.create_default_context(cafile=f"{SERVICE_ACCOUNT}/ca.crt")
    request = urllib.request.Request(
        f"https://{KUBERNETES_API}:{os.getenv('KUBERNETES_SERVICE_PORT_HTTPS', '443')}{path}",
        data=json.dumps(payload).encode(), method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/merge-patch+json"},
    )
    with urllib.request.urlopen(request, context=context, timeout=5) as response:
        return json.load(response)


def readiness_gate_ready(service):
    """Evaluate an optional, deployment-defined authority gate for a service."""
    gate = READINESS_GATES.get(service)
    if not gate:
        return True
    try:
        config = gate["configMap"]
        endpoint_ref = gate["endpointSlice"]
        config_map = kubernetes_get(
            f"/api/v1/namespaces/{config['namespace']}/configmaps/{config['name']}"
        )
        endpoint_slice = kubernetes_get(
            f"/apis/discovery.k8s.io/v1/namespaces/{endpoint_ref['namespace']}"
            f"/endpointslices/{endpoint_ref['name']}"
        )
        document = json.loads(config_map["data"][config["dataKey"]])
        if any(document.get(key) != value for key, value in gate.get("requiredFields", {}).items()):
            return False
        selected = document
        for key in gate["addressPath"]:
            selected = selected[key]
        ready_addresses = [
            address
            for endpoint in endpoint_slice.get("endpoints", [])
            if endpoint.get("conditions", {}).get("ready", True)
            for address in endpoint.get("addresses", [])
        ]
        if gate.get("requireSingleReadyAddress", True) and len(ready_addresses) != 1:
            return False
        return selected in ready_addresses
    except (KeyError, TypeError, ValueError):
        return False


def candidates_from_public_edges():
    payload = kubernetes_get(f"/apis/{API_GROUP}/v1alpha1/publicedges")
    if payload is None:
        return None
    candidates = []
    for item in payload.get("items", []):
        spec = item.get("spec", {})
        endpoint = spec.get("endpoint", {})
        if not spec.get("enabled") or spec.get("draining"):
            continue
        service_classes = set(spec.get("serviceClasses", []))
        probes = {}
        for hostname, definition in SERVICE_DEFINITIONS.items():
            service = definition["service"]
            service_class = definition.get("class", "web")
            if service_class not in service_classes:
                continue
            probes[service] = f"https://{hostname.rstrip('.')}{definition.get('probePath', '/')}"
        candidates.append({
            "id": item["metadata"]["name"],
            "generation": item["metadata"].get("generation", 0),
            "nodeName": spec.get("nodeName", ""),
            "region": spec["region"],
            "area": spec.get("area", spec["region"]),
            "ip": endpoint["value"],
            "probeIp": endpoint.get("probeAddress", endpoint["value"]),
            "endpointType": endpoint["type"],
            "gatewayVip": spec["gatewayVIP"],
            "priority": spec.get("priority", 0),
            "capacityMbps": spec.get("capacityMbps", 1),
            "priorityByRegion": spec.get("priorityByRegion", {}),
            "forwarding": spec.get("forwarding", {"mode": "DirectGateway"}),
            "edgeReady": (item.get("status", {}).get("gatewayPath", {}).get("reachable") is True and
                          all(item.get("status", {}).get("protocolProbes", {}).get(protocol) is True
                              for protocol in ("http", "https"))),
            "probes": probes,
        })
    return candidates


def refresh_candidates():
    if not KUBERNETES_API:
        return
    discovered = candidates_from_public_edges()
    # An empty list (or an API failure) must never retain a previously selected
    # public endpoint. Keep static candidates only in the explicit non-K8s mode.
    with LOCK:
        CANDIDATES[:] = discovered or []
        active = {item["id"] for item in CANDIDATES}
        for service in SERVICES.values():
            HEALTH.setdefault(service, {})
            HEALTH[service] = {edge: result for edge, result in HEALTH[service].items()
                               if edge in active}


def refresh_fabric_assessments():
    """Cache provider-neutral network evidence once per probe cycle."""
    global FABRIC_API_AVAILABLE, FABRIC_NODE_API_AVAILABLE
    if FABRIC_EVIDENCE_MODE == "Disabled":
        with LOCK:
            FABRIC_ASSESSMENTS.clear()
            FABRIC_API_AVAILABLE = False
            FABRIC_NODE_READINESS.clear()
            FABRIC_NODE_API_AVAILABLE = False
        return
    payload = kubernetes_get(
        f"/apis/{FABRIC_EVIDENCE_API_GROUP}/{FABRIC_EVIDENCE_API_VERSION}/{FABRIC_EVIDENCE_RESOURCE}"
    )
    available = payload is not None
    assessments = {}
    for item in (payload or {}).get("items", []):
        subject = item.get("spec", {}).get("subjectRef", {})
        scope = item.get("spec", {}).get("scope", {})
        if subject.get("kind") != "Node" or not subject.get("name"):
            continue
        if scope.get("plane") not in ("host-and-pod", "pod"):
            continue
        assessments[subject["name"]] = item
    node_payload = kubernetes_get("/api/v1/nodes") if FABRIC_REQUIRE_NODE_READY else None
    node_available = node_payload is not None if FABRIC_REQUIRE_NODE_READY else True
    node_readiness = {}
    for item in (node_payload or {}).get("items", []):
        name = item.get("metadata", {}).get("name")
        if not name or item.get("metadata", {}).get("deletionTimestamp"):
            continue
        node_readiness[name] = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", [])
        )
    with LOCK:
        FABRIC_ASSESSMENTS.clear()
        FABRIC_ASSESSMENTS.update(assessments)
        FABRIC_API_AVAILABLE = available
        FABRIC_NODE_READINESS.clear()
        FABRIC_NODE_READINESS.update(node_readiness)
        FABRIC_NODE_API_AVAILABLE = node_available


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def fabric_evidence(candidate, now=None):
    """Evaluate cached evidence without assuming an Advanced Fabric release."""
    if FABRIC_EVIDENCE_MODE == "Disabled":
        return {"eligible": True, "mode": "Disabled", "state": "Disabled"}
    with LOCK:
        available = FABRIC_API_AVAILABLE
        node_api_available = FABRIC_NODE_API_AVAILABLE
        node_name = candidate.get("nodeName", "")
        node_ready = FABRIC_NODE_READINESS.get(node_name)
        item = FABRIC_ASSESSMENTS.get(node_name)
    if FABRIC_REQUIRE_NODE_READY and (not node_api_available or node_ready is not True):
        reason = "NodeNotReady" if node_api_available and node_ready is False else "NodeReadinessUnavailable"
        eligible = FABRIC_EVIDENCE_MODE in ("Shadow", "Optional") and not node_api_available
        return {"eligible": True if FABRIC_EVIDENCE_MODE == "Shadow" else eligible,
                "wouldReject": True, "mode": FABRIC_EVIDENCE_MODE,
                "state": "Unavailable", "reason": reason, "nodeReady": node_ready,
                "pathEvidence": {}}
    if not item:
        eligible = FABRIC_EVIDENCE_MODE in ("Shadow", "Optional")
        reason = "AssessmentNotFound" if available else "ProviderUnavailable"
        return {"eligible": eligible, "mode": FABRIC_EVIDENCE_MODE,
                "state": "Unavailable", "reason": reason, "pathEvidence": {}}
    status = item.get("status", {})
    state = status.get("state", "Unknown")
    valid_until = parse_timestamp(status.get("validUntil"))
    current = time.time() if now is None else now
    condition = next((value for value in status.get("conditions", [])
                      if value.get("type") == "EvidenceReady"), {})
    reason = condition.get("reason", state)
    fresh = valid_until is not None and current <= valid_until
    path_evidence = status.get("pathEvidence", {})
    # The fabric collector may mark an otherwise measured, reachable path
    # Partial solely because the Kubernetes Node is NotReady. Public delivery
    # is qualified by the observed path, not the kubelet's scheduling state.
    condition_ready = condition.get("status") == "True" or (
        not FABRIC_REQUIRE_NODE_READY and
        set(path_evidence.get("missingEvidence", [])) == {"node-ready"}
    )
    evidence_eligible = (fresh and state in FABRIC_EVIDENCE_ALLOWED_STATES and
                         condition_ready and
                         (not FABRIC_REQUIRE_NODE_READY or status.get("nodeReady") is True) and
                         path_evidence.get("currentPathMeasured") is True and
                         path_evidence.get("reachable") is True)
    if not fresh:
        reason = "EvidenceExpired" if valid_until is not None else "ValidityMissing"
    return {
        "eligible": True if FABRIC_EVIDENCE_MODE == "Shadow" else evidence_eligible,
        "wouldReject": not evidence_eligible,
        "mode": FABRIC_EVIDENCE_MODE,
        "nodeReady": node_ready if FABRIC_REQUIRE_NODE_READY else None,
        "assessment": item.get("metadata", {}).get("name", ""),
        "state": state,
        "reason": reason,
        "observedAt": status.get("observedAt", ""),
        "validUntil": status.get("validUntil", ""),
        "pathEvidence": path_evidence,
    }


def bind_address():
    per_node = json.loads(os.getenv("BIND_ADDRESSES_JSON", "{}"))
    if NODE in per_node:
        return per_node[NODE]
    configured = os.getenv("BIND_ADDRESS", "").strip()
    if configured:
        return configured
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    finally:
        sock.close()


def accepted_probe_status(service, status):
    definition = next(
        (item for item in SERVICE_DEFINITIONS.values() if item["service"] == service),
        {},
    )
    accepted_statuses = definition.get("acceptedStatuses")
    if accepted_statuses is not None:
        return status in accepted_statuses
    return 200 <= status < 400 or status in (401, 403)


def probe_fresh(observed, now=None):
    age = (time.time() if now is None else now) - int(observed.get("observedAt", 0))
    return 0 <= age <= max(30, PROBE_CONNECT_TIMEOUT + PROBE_RESPONSE_TIMEOUT +
                           2 * PROBE_INTERVAL_SECONDS)


def probe(candidate, service, url):
    parsed = urllib.parse.urlparse(url)
    started = time.monotonic()
    status = 0
    failure = ""
    try:
        context = ssl.create_default_context()
        probe_ip = candidate.get("probeIp", candidate.get("localIp", candidate["ip"]))
        sock = socket.create_connection((probe_ip, 443), timeout=PROBE_CONNECT_TIMEOUT)
        tls = context.wrap_socket(sock, server_hostname=parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, timeout=PROBE_RESPONSE_TIMEOUT)
        connection.sock = tls
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Host": parsed.hostname, "User-Agent": USER_AGENT})
        response = connection.getresponse()
        status = response.status
        response.read(4096)
        connection.close()
    except Exception as exc:
        failure = str(exc)
    latency = int((time.monotonic() - started) * 1000)
    with LOCK:
        previous = HEALTH[service].get(candidate["id"], {})
        if accepted_probe_status(service, status):
            successes = previous.get("successes", 0) + 1
            failures = 0
            ready = previous.get("ready", False) or successes >= 2
        else:
            successes = 0
            failures = previous.get("failures", 0) + 1
            ready = previous.get("ready", False) and failures < 2
        HEALTH[service][candidate["id"]] = {
            "ready": ready,
            "statusCode": status,
            "latencyMs": latency,
            "successes": successes,
            "failures": failures,
            "observedAt": int(time.time()),
            "failure": failure,
        }


def probe_all():
    reload_records()
    reload_nameservers()
    refresh_candidates()
    refresh_fabric_assessments()
    jobs = [
        (candidate, service, url)
        for candidate in CANDIDATES
        for service, url in candidate.get("probes", {}).items()
    ]
    # A release may expose dozens of services. Creating one thread per
    # candidate/service pair can exhaust a small CPU quota and starve the HTTP
    # health endpoint. A per-round bounded pool preserves parallel endpoint
    # sampling while ensuring a new round cannot overlap unfinished probes.
    with ThreadPoolExecutor(max_workers=min(PROBE_MAX_WORKERS, len(jobs) or 1),
                            thread_name_prefix="edge-probe") as executor:
        futures = [executor.submit(probe, candidate, service, url)
                   for candidate, service, url in jobs]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                print(f"probe_worker error={exc}", flush=True)


def probe_loop():
    while True:
        probe_all()
        publish_edge_statuses()
        publish_default_area()
        time.sleep(PROBE_INTERVAL_SECONDS)


def publish_edge_statuses():
    """Publish backend observations without redefining edge transport readiness."""
    if not KUBERNETES_API:
        return
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with LOCK:
        candidates = list(CANDIDATES)
        health = {service: dict(results) for service, results in HEALTH.items()}
    for candidate in candidates:
        edge_id = candidate["id"]
        service_health = {
            service: {
                "ready": bool(results.get(edge_id, {}).get("ready")) and
                         probe_fresh(results.get(edge_id, {})) and
                         fabric_evidence(candidate)["eligible"],
                "statusCode": int(results.get(edge_id, {}).get("statusCode", 0)),
                "latencyMs": int(results.get(edge_id, {}).get("latencyMs", 0)),
                "observedAt": int(results.get(edge_id, {}).get("observedAt", 0)),
                "reason": results.get(edge_id, {}).get("failure", ""),
            }
            for service, results in health.items()
            if service in candidate.get("probes", {})
        }
        ready_services = sorted(name for name, result in service_health.items() if result["ready"])
        network_evidence = fabric_evidence(candidate)
        # A merge-patch does not remove keys omitted by a newer publisher. Send
        # an explicit null once so v0.4's legacy O/S/I-derived score cannot be
        # mistaken for part of the v1alpha2 eligibility contract.
        network_evidence["score"] = None
        ready = bool(ready_services) and network_evidence["eligible"]
        condition = {
            "type": "ServiceBackendReady",
            "status": "True" if ready else "False",
            "reason": ("ServiceAndNetworkEvidenceReady" if ready else
                       network_evidence.get("reason", "NetworkEvidenceRejected")
                       if ready_services else "NoServiceProbeSucceeded"),
            "message": ("ready services: " + ", ".join(ready_services) if ready else
                        "network evidence rejected candidate" if ready_services else
                        "no configured service probe is ready"),
            "lastTransitionTime": observed_at,
        }
        try:
            kubernetes_patch(
                f"/apis/{API_GROUP}/v1alpha1/publicedges/{edge_id}/status",
                {"status": {"observedAt": observed_at,
                            "observedGeneration": candidate.get("generation", 0),
                            "services": service_health,
                            "networkEvidence": network_evidence, "backendConditions": [condition]}},
            )
        except Exception as exc:
            print(f"publicedge_status edge={edge_id} error={exc}", flush=True)


def publish_default_area():
    """Update provider-scoped publication objects for non-delegated names.

    Regional NS answers stay request-area aware. A global DNS A record cannot,
    so one elected authority publishes its configured area. Each adapter owns
    a distinct set of ExternalDNS-only Ingress objects and provider credentials
    remain outside Public Edge Manager.
    """
    if not PUBLICATION_ENABLED:
        return
    adapters = PUBLICATION_ADAPTERS or {
        "legacy": {"provider": "external-dns", "refs": PUBLICATION_REFS}
    }
    for adapter_name, adapter in adapters.items():
        if not adapter.get("enabled", True):
            continue
        provider = adapter.get("provider", adapter_name)
        for service, ref in adapter.get("refs", {}).items():
            try:
                selected = stable_selected(service, ranked(service), AREA)
                if not selected:
                    continue
                path = f"/apis/networking.k8s.io/v1/namespaces/{ref['namespace']}/ingresses/{ref['name']}"
                current = kubernetes_get(path)
                annotations = (current or {}).get("metadata", {}).get("annotations", {})
                desired = {
                    "external-dns.alpha.kubernetes.io/target": selected["ip"],
                    f"{API_GROUP}/selected-public-edge": selected["id"],
                    f"{API_GROUP}/publication-adapter": adapter_name,
                    f"{API_GROUP}/dns-provider": provider,
                }
                if all(annotations.get(key) == value for key, value in desired.items()):
                    continue
                kubernetes_patch(path, {"metadata": {"annotations": desired}})
            except Exception as exc:
                print(
                    f"publication adapter={adapter_name} provider={provider} "
                    f"service={service} error={exc}", flush=True,
                )


def validate_publication_adapters():
    """Reject ambiguous sinks before any provider-owned object is mutated."""
    if not PUBLICATION_ADAPTERS:
        return
    claimed = {}
    known_services = set(SERVICES.values())
    for adapter_name, adapter in PUBLICATION_ADAPTERS.items():
        if not adapter.get("enabled", True):
            continue
        for service, ref in adapter.get("refs", {}).items():
            if service not in known_services:
                raise RuntimeError(
                    f"publication adapter {adapter_name!r} references unknown service {service!r}"
                )
            identity = (ref["namespace"], ref["name"])
            if identity in claimed:
                raise RuntimeError(
                    f"publication object {identity[0]}/{identity[1]} is shared by adapters "
                    f"{claimed[identity]!r} and {adapter_name!r}"
                )
            claimed[identity] = adapter_name


def client_area(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "GLOBAL"
    matches = []
    for area, cidrs in CLIENT_AREA_CIDRS.items():
        for value in cidrs:
            try:
                network = ipaddress.ip_network(value)
            except ValueError:
                continue
            if ip in network:
                matches.append((network.prefixlen, area))
    return max(matches, default=(0, "GLOBAL"))[1]


def ranked(service, request_area=None):
    gate_ready = readiness_gate_ready(service)
    with LOCK:
        snapshot = dict(HEALTH.get(service, {}))
    result = []
    with LOCK:
        candidates = list(CANDIDATES)
    for candidate in candidates:
        if service not in candidate.get("probes", {}):
            continue
        observed = snapshot.get(candidate["id"], {})
        network_evidence = fabric_evidence(candidate)
        fresh_probe = probe_fresh(observed)
        # Edge transport and backend health are separate.  A reachable edge
        # remains in DNS when a service has no ready Pod; Gateway API then
        # terminates that request with an explicit 503 instead of DNS NODATA.
        edge_ready = candidate.get("edgeReady")
        if edge_ready is None:  # Compatibility for externally managed v1alpha1 objects.
            edge_ready = bool(observed.get("ready")) and fresh_probe and gate_ready
        ready = bool(edge_ready) and network_evidence["eligible"]
        score = 0
        if ready:
            area = request_area or AREA
            regional_priority = candidate.get("priorityByRegion", {}).get(area, candidate.get("priority", 0))
            # Area is the hard locality boundary: a US authority should publish a
            # healthy US relay even when the origin is in CN. Inside one area,
            # capacity is intentionally the dominant signal so clients reach
            # the strongest local edge instead of hairpinning through a remote one.
            capacity_weight = int(RUNTIME_MODEL.get("capacityWeight", CANDIDATE_CAPACITY_WEIGHT))
            local_area_bonus = int(RUNTIME_MODEL.get("localAreaBonus", CANDIDATE_LOCAL_AREA_BONUS))
            priority_weight = int(RUNTIME_MODEL.get("priorityWeight", CANDIDATE_PRIORITY_WEIGHT))
            latency_divisor = max(1, int(RUNTIME_MODEL.get("latencyDivisorMs", CANDIDATE_LATENCY_DIVISOR_MS)))
            latency_cap = max(0, int(RUNTIME_MODEL.get("latencyPenaltyCap", CANDIDATE_LATENCY_PENALTY_CAP)))
            local_node_bonus = int(RUNTIME_MODEL.get("localNodeBonus", CANDIDATE_LOCAL_NODE_BONUS))
            score = int(candidate.get("capacityMbps", 1)) * capacity_weight
            if candidate.get("area", candidate["region"]) == area:
                score += local_area_bonus
            score -= int(regional_priority) * priority_weight
            score -= min(int(observed.get("latencyMs", 0)) // latency_divisor, latency_cap)
            if candidate["id"] == NODE:
                score += local_node_bonus
        result.append({
            "id": candidate["id"], "region": candidate["region"],
            "area": candidate.get("area", candidate["region"]), "ip": candidate["ip"],
            "capacityMbps": candidate.get("capacityMbps", 1),
            "endpointType": candidate.get("endpointType", "PublicIP"),
            "gatewayVip": candidate.get("gatewayVip", ""),
            "forwarding": candidate.get("forwarding", {"mode": "DirectGateway"}),
            "paths": candidate.get("paths", {}).get(service, []),
            "score": score, "state": "ready" if ready else "unavailable",
            "statusCode": observed.get("statusCode", 0), "latencyMs": observed.get("latencyMs", 0),
            "observedAt": observed.get("observedAt", 0),
            "serviceBackendReady": bool(observed.get("ready")) and fresh_probe and gate_ready,
            "networkEvidence": network_evidence,
            "reason": ("ServiceProbeStale" if not fresh_probe else
                       network_evidence.get("reason", "network evidence rejected candidate")
                       if not network_evidence["eligible"] else
                       observed.get("failure", "") if gate_ready else
                       "configured readiness gate is not satisfied"),
        })
    return sorted(result, key=lambda item: (-item["score"], item["id"]))


def stable_selected(service, candidates, request_area=None, timestamp=None):
    """Return a sticky last-known-good candidate with qualified failover.

    Ranking changes never move a healthy selection. A failed selection remains
    the last-known-good answer during the grace and replacement qualification
    windows. This keeps DNS and provider publication on the same stable target.
    """
    timestamp = time.time() if timestamp is None else timestamp
    area = request_area or AREA
    key = (service, area)
    ready = [item for item in candidates if item["state"] == "ready"]
    by_id = {item["id"]: item for item in candidates}
    with LOCK:
        state = SELECTIONS.get(key)
        if state is None:
            if not ready:
                return None
            selected = dict(ready[0])
            SELECTIONS[key] = {
                "selected": selected["id"], "snapshot": selected,
                "selectedAt": timestamp, "unavailableAt": None,
                "pending": None, "pendingAt": None,
            }
            return selected

        current = by_id.get(state["selected"])
        if current and current["state"] == "ready":
            state.update(snapshot=dict(current), unavailableAt=None, pending=None, pendingAt=None)
            return dict(current)

        if state["unavailableAt"] is None:
            state["unavailableAt"] = timestamp
        replacement = ready[0] if ready else None
        if replacement is None:
            state.update(pending=None, pendingAt=None)
            return dict(state["snapshot"])
        if state["pending"] != replacement["id"]:
            state.update(pending=replacement["id"], pendingAt=timestamp)

        grace_elapsed = timestamp - state["unavailableAt"] >= CANDIDATE_FAILOVER_GRACE_SECONDS
        ready_elapsed = timestamp - state["pendingAt"] >= CANDIDATE_MIN_READY_SECONDS
        hold_elapsed = timestamp - state["selectedAt"] >= CANDIDATE_MIN_HOLD_SECONDS
        if grace_elapsed and ready_elapsed and hold_elapsed:
            selected = dict(replacement)
            state.update(selected=selected["id"], snapshot=selected, selectedAt=timestamp,
                         unavailableAt=None, pending=None, pendingAt=None)
            return selected
        return dict(state["snapshot"])


def encode_name(name):
    output = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("ascii")
        output.append(len(encoded))
        output.extend(encoded)
    output.append(0)
    return bytes(output)


def parse_name(packet, offset):
    labels = []
    while offset < len(packet):
        size = packet[offset]
        offset += 1
        if size == 0:
            return ".".join(labels).lower() + ".", offset
        if size > 63 or offset + size > len(packet):
            raise ValueError("invalid DNS name")
        labels.append(packet[offset:offset + size].decode("ascii"))
        offset += size
    raise ValueError("unterminated DNS name")


def skip_name(packet, offset):
    """Skip a possibly compressed DNS name and return the wire resume offset."""
    while offset < len(packet):
        size = packet[offset]
        offset += 1
        if size == 0:
            return offset
        if size & 0xC0 == 0xC0:
            return offset + 1
        if size > 63 or offset + size > len(packet):
            raise ValueError("invalid DNS name")
        offset += size
    raise ValueError("unterminated DNS name")


def ecs_address(packet, question_end):
    """Return RFC 7871 ECS address when present in an OPT additional RR."""
    try:
        _, _, _, answers, authorities, additional = struct.unpack("!HHHHHH", packet[:12])
        offset = question_end
        for _ in range(answers + authorities + additional):
            offset = skip_name(packet, offset)
            rrtype, _, _, length = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            end = offset + length
            if rrtype == 41:
                option = offset
                while option + 4 <= end:
                    code, size = struct.unpack("!HH", packet[option:option + 4])
                    option += 4
                    data = packet[option:option + size]
                    option += size
                    if code != 8 or len(data) < 4:
                        continue
                    family, prefix, _ = struct.unpack("!HBB", data[:4])
                    width = 4 if family == 1 else 16 if family == 2 else 0
                    if not width or prefix > width * 8:
                        continue
                    packed = data[4:] + b"\0" * (width - len(data[4:]))
                    return str(ipaddress.ip_address(packed[:width]))
            offset = end
    except (IndexError, struct.error, ValueError):
        return ""
    return ""


def rr(name, qtype, ttl, data):
    return encode_name(name) + struct.pack("!HHIH", qtype, 1, ttl, len(data)) + data


def matching_authority_zone(name):
    """Return the most-specific configured zone containing a DNS name."""
    return next(
        (zone for zone in AUTHORITY_ZONES if name == zone or name.endswith(f".{zone}")),
        None,
    )


def soa_rr(zone):
    serial = int(time.strftime("%Y%m%d") + "01")
    data = (
        encode_name(NAMESERVERS[0])
        + encode_name(SOA_RNAME)
        + struct.pack("!IIIII", serial, 60, 60, 86400, 30)
    )
    return rr(zone, 6, 300, data)


def service_address_records(name, service, request_area=None):
    selected = stable_selected(service, ranked(service, request_area), request_area)
    if not selected:
        return []
    records = []
    if selected.get("endpointType") == "HostnameTunnel":
        records.append(rr(name, 5, 30, encode_name(selected["ip"])))
        return records
    address = ipaddress.ip_address(selected["ip"])
    if address.version == 4:
        records.append(rr(name, 1, 30, address.packed))
    return records


def external_address_records(name, qtype):
    """Serve explicitly declared CDN/static records without health-based retargeting."""
    records = []
    for item in EXTERNAL_RECORDS.get(name, []):
        kind = item["type"]
        if kind == "CNAME":
            records.append(rr(name, 5, 30, encode_name(item["value"])))
        elif kind == "A" and qtype in (1, 255):
            records.append(rr(name, 1, 30, ipaddress.IPv4Address(item["value"]).packed))
        elif kind == "AAAA" and qtype in (28, 255):
            records.append(rr(name, 28, 30, ipaddress.IPv6Address(item["value"]).packed))
    return records


def dns_response(query, source_ip=""):
    if len(query) < 12:
        return b""
    ident, flags, qdcount = struct.unpack("!HHH", query[:6])
    if qdcount != 1:
        return b""
    try:
        name, offset = parse_name(query, 12)
    except ValueError:
        return b""
    if offset + 4 > len(query):
        return b""
    qtype, _ = struct.unpack("!HH", query[offset:offset + 4])
    question = query[12:offset + 4]
    request_area = client_area(ecs_address(query, offset + 4) or source_ip)
    answers = []
    authorities = []
    rcode = 0
    service = SERVICES.get(name)
    zone = matching_authority_zone(name) if AUTHORITY_ZONES else None
    authoritative = not AUTHORITY_ZONES or zone is not None

    if not authoritative:
        rcode = 5
    elif not AUTHORITY_ZONES:
        # Preserve the original service-name authority model for existing
        # installations until they explicitly configure zones.
        if not service:
            rcode = 3
        elif qtype in (1, 255):
            answers.extend(service_address_records(name, service, request_area))
        elif qtype == 2:
            answers.extend(rr(name, 2, 300, encode_name(ns)) for ns in NAMESERVERS)
        elif qtype == 6:
            answers.append(soa_rr(name))
    else:
        name_exists = service is not None or name == zone or name in EXTERNAL_RECORDS
        if not name_exists:
            rcode = 3
        elif name == zone and qtype == 2:
            answers.extend(rr(zone, 2, 300, encode_name(ns)) for ns in NAMESERVERS)
        elif name == zone and qtype == 6:
            answers.append(soa_rr(zone))
        elif name in EXTERNAL_RECORDS:
            answers.extend(external_address_records(name, qtype))
        elif service and qtype in (1, 255):
            answers.extend(service_address_records(name, service, request_area))
        if not answers:
            authorities.append(soa_rr(zone))

    response_flags = 0x8000 | (0x0400 if authoritative else 0) | (flags & 0x0100) | rcode
    header = struct.pack("!HHHHHH", ident, response_flags, 1, len(answers), len(authorities), 0)
    return header + question + b"".join(answers) + b"".join(authorities)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        response = dns_response(data, self.client_address[0])
        if response:
            sock.sendto(response, self.client_address)


class ReusableUDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        length_data = self.request.recv(2)
        if len(length_data) != 2:
            return
        length = struct.unpack("!H", length_data)[0]
        data = b""
        while len(data) < length:
            part = self.request.recv(length - len(data))
            if not part:
                return
            data += part
        response = dns_response(data, self.client_address[0])
        if response:
            self.request.sendall(struct.pack("!H", len(response)) + response)


class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"ok\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if parsed.path != "/v1/discovery":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        service = query.get("service", [""])[0]
        if service == "db":
            service = "db-" + query.get("mode", [""])[0]
        if service not in set(SERVICES.values()):
            self.send_error(400, "unknown service")
            return
        client = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        area = client_area(client)
        candidates = ranked(service, area)
        selected_item = stable_selected(service, candidates, area)
        selected = selected_item["id"] if selected_item else ""
        payload = json.dumps({
            "version": 1, "generation": int(time.time() // 10 * 10), "service": service,
            "paths": next((item.get("paths", []) for item in SERVICE_DEFINITIONS.values() if item["service"] == service), []),
            "selected": selected, "ttlSeconds": 30, "servedBy": NODE,
            "client": client, "clientArea": area,
            "candidates": candidates,
            "runtimeModel": RUNTIME_MODEL,
        }, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=20, stale-while-revalidate=60")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print("http", self.address_string(), fmt % args, flush=True)


def main():
    if RECORDS_FILE and not reload_records():
        raise RuntimeError("unable to load record configuration")
    if NAMESERVERS_FILE and not reload_nameservers():
        raise RuntimeError("unable to load nameserver election")
    for name, records in EXTERNAL_RECORDS.items():
        zone = matching_authority_zone(name)
        if not zone or name == zone or name in SERVICES or not records:
            raise RuntimeError(f"external record {name} conflicts with a zone or managed service")
        if any(item.get("type") not in ("A", "AAAA", "CNAME") or
               not item.get("externalCDN") for item in records):
            raise RuntimeError(f"external record {name} requires supported type and externalCDN marker")
        if any(item["type"] == "CNAME" for item in records) and len(records) != 1:
            raise RuntimeError(f"external CNAME {name} must be the only record at its name")
        for item in records:
            if item["type"] == "CNAME":
                encode_name(item["value"])
            elif item["type"] == "A":
                ipaddress.IPv4Address(item["value"])
            else:
                ipaddress.IPv6Address(item["value"])
    if not SERVICE_DEFINITIONS:
        raise RuntimeError("SERVICES_JSON must configure at least one public service")
    if not NAMESERVERS:
        raise RuntimeError("NAMESERVERS must configure at least one authoritative nameserver")
    if not CANDIDATES and not KUBERNETES_API:
        raise RuntimeError("no PublicEdge API or CANDIDATES_JSON configured")
    validate_publication_adapters()
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HTTPHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    probe_all()
    publish_edge_statuses()
    publish_default_area()
    threading.Thread(target=probe_loop, daemon=True).start()
    dns_bind = bind_address()
    udp = ReusableUDPServer((dns_bind, DNS_PORT), UDPHandler)
    tcp = ReusableTCPServer((dns_bind, DNS_PORT), TCPHandler)
    threading.Thread(target=udp.serve_forever, daemon=True).start()
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    print(f"node={NODE} area={AREA} region={REGION} dns={dns_bind}:{DNS_PORT} http=:{HTTP_PORT}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
