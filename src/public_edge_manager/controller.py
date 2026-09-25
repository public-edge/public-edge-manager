#!/usr/bin/env python3
"""Discover public edges from Kubernetes facts and reconcile derived state.

The controller deliberately has no node inventory.  Nodes become candidates
only when Kubernetes reports a global ExternalIP, fresh provider-neutral path
evidence exists, the selected Gateway is programmed, and the public listeners
are reachable.  PublicEdge objects and eligibility labels are derived outputs.
"""

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")
API_VERSION = os.getenv("API_VERSION", "v1alpha1")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
POD_NAME = os.getenv("POD_NAME", "public-edge-controller")
INTERVAL = max(5, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "15")))
PROBE_TIMEOUT = max(0.2, float(os.getenv("DISCOVERY_PROBE_TIMEOUT_SECONDS", "2")))
DEFAULT_CAPACITY = max(1, int(os.getenv("DEFAULT_CAPACITY_MBPS", "100")))
MINIMUM_CAPACITY = max(1, int(os.getenv("MINIMUM_CAPACITY_MBPS", "100")))
CAPACITY_INVENTORY_GROUP = os.getenv("CAPACITY_INVENTORY_API_GROUP", "")
CAPACITY_INVENTORY_VERSION = os.getenv("CAPACITY_INVENTORY_API_VERSION", "v1alpha1")
CAPACITY_INVENTORY_RESOURCE = os.getenv("CAPACITY_INVENTORY_RESOURCE", "advancedfabrics")
CAPACITY_INVENTORY_NAME = os.getenv("CAPACITY_INVENTORY_NAME", "")
ALLOWED_STATES = set(json.loads(os.getenv("FABRIC_EVIDENCE_ALLOWED_STATES_JSON", '["Ready","Partial"]')))
NPA_GROUP = os.getenv("FABRIC_EVIDENCE_API_GROUP", "networking.re8ch.com")
NPA_VERSION = os.getenv("FABRIC_EVIDENCE_API_VERSION", "v1alpha2")
NPA_RESOURCE = os.getenv("FABRIC_EVIDENCE_RESOURCE", "networkpathassessments")
GATEWAY_NAMESPACE = os.getenv("GATEWAY_NAMESPACE", "")
GATEWAY_SELECTOR = os.getenv("GATEWAY_SELECTOR", "networking.re8ch.com/public-edge-gateway=true")
AUTHORITY_LABEL = os.getenv("AUTHORITY_LABEL", f"{API_GROUP}/public-edge-authority-ready")
INGRESS_LABEL = os.getenv("INGRESS_LABEL", f"{API_GROUP}/public-edge-ready")
NAMESERVER_CONFIGMAP = os.getenv("NAMESERVER_CONFIGMAP", "public-edge-nameservers")
MAX_NAMESERVERS = max(1, min(3, int(os.getenv("MAX_NAMESERVERS", "3"))))
PARENT_ZONE = os.getenv("PARENT_ZONE", "").rstrip(".").lower()
CHILD_ZONES = [value.rstrip(".").lower() for value in json.loads(os.getenv("CHILD_ZONES_JSON", "[]"))]
CF_TOKEN = os.getenv("CF_API_TOKEN", "")
CF_SECRET_NAMESPACE = os.getenv("CF_SECRET_NAMESPACE", "")
CF_SECRET_NAME = os.getenv("CF_SECRET_NAME", "")
CF_SECRET_KEY = os.getenv("CF_SECRET_KEY", "CF_API_TOKEN")
LEASE_NAME = os.getenv("LEASE_NAME", "public-edge-discovery")


def now_rfc3339():
    # coordination.k8s.io MicroTime requires exactly six fractional digits.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return 0


def api(path, method="GET", payload=None, content_type=None):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    with open(f"{SA}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"https://{host}:{port}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type or "application/json"},
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        return {} if response.status == 204 else json.load(response)


def patch(path, payload, status=False):
    return api(path, "PATCH", payload, "application/merge-patch+json")


def object_name(uid):
    return "edge-" + hashlib.sha256(uid.encode()).hexdigest()[:16]


def nameserver_name(uid):
    return f"ns-{hashlib.sha256(uid.encode()).hexdigest()[:12]}.{PARENT_ZONE}."


def global_external_ip(node):
    for item in node.get("status", {}).get("addresses", []):
        if item.get("type") != "ExternalIP":
            continue
        try:
            address = ipaddress.ip_address(item["address"])
        except ValueError:
            continue
        if address.version == 4 and address.is_global:
            return str(address)
    return ""


def node_ready(node):
    return any(item.get("type") == "Ready" and item.get("status") == "True"
               for item in node.get("status", {}).get("conditions", []))


def assessment_by_node(payload):
    result = {}
    for item in payload.get("items", []):
        subject = item.get("spec", {}).get("subjectRef", {})
        if subject.get("kind") == "Node" and subject.get("name"):
            result[subject["name"]] = item
    return result


def assessment_ready(item, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    status = (item or {}).get("status", {})
    evidence = status.get("pathEvidence", {})
    condition = next((entry for entry in status.get("conditions", [])
                      if entry.get("type") == "EvidenceReady"), {})
    tolerated_not_ready = set(evidence.get("missingEvidence", [])) == {"node-ready"}
    return (status.get("state") in ALLOWED_STATES and
            parse_time(status.get("validUntil")) >= timestamp and
            (condition.get("status") == "True" or tolerated_not_ready) and
            evidence.get("currentPathMeasured") is True and evidence.get("reachable") is True)


def parse_selector(selector):
    return {key.strip(): value.strip() for key, value in
            (part.split("=", 1) for part in selector.split(",") if "=" in part)}


def select_gateway(items):
    required = parse_selector(GATEWAY_SELECTOR)
    candidates = []
    for item in items:
        if GATEWAY_NAMESPACE and item.get("metadata", {}).get("namespace") != GATEWAY_NAMESPACE:
            continue
        labels = item.get("metadata", {}).get("labels", {})
        if any(labels.get(key) != value for key, value in required.items()):
            continue
        programmed = any(condition.get("type") == "Programmed" and condition.get("status") == "True"
                         for condition in item.get("status", {}).get("conditions", []))
        if not programmed:
            continue
        addresses = [entry.get("value", "") for entry in item.get("status", {}).get("addresses", [])]
        for value in addresses:
            try:
                if ipaddress.ip_address(value).version == 4:
                    candidates.append((item, value))
            except ValueError:
                pass
    if not candidates:
        return None, ""
    candidates.sort(key=lambda pair: (pair[0]["metadata"].get("creationTimestamp", ""),
                                      pair[0]["metadata"]["namespace"], pair[0]["metadata"]["name"]))
    return candidates[0]


def tcp_probe(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def dns_query(ip, zone, tcp=False):
    ident = os.urandom(2)
    qname = b"".join(bytes([len(label)]) + label.encode() for label in zone.split(".")) + b"\0"
    query = ident + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + struct.pack("!HH", 6, 1)
    try:
        if tcp:
            with socket.create_connection((ip, 53), timeout=PROBE_TIMEOUT) as sock:
                sock.sendall(struct.pack("!H", len(query)) + query)
                size = sock.recv(2)
                if len(size) != 2:
                    return False
                expected = struct.unpack("!H", size)[0]
                packet = b""
                while len(packet) < expected:
                    part = sock.recv(expected - len(packet))
                    if not part:
                        return False
                    packet += part
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(PROBE_TIMEOUT)
                sock.sendto(query, (ip, 53))
                packet, _ = sock.recvfrom(4096)
        return len(packet) >= 12 and packet[:2] == ident and (struct.unpack("!H", packet[2:4])[0] & 0x840F) == 0x8400
    except OSError:
        return False


def capacity_inventory_by_node():
    if not CAPACITY_INVENTORY_GROUP or not CAPACITY_INVENTORY_NAME:
        return {}
    item = api(
        f"/apis/{CAPACITY_INVENTORY_GROUP}/{CAPACITY_INVENTORY_VERSION}/"
        f"{CAPACITY_INVENTORY_RESOURCE}/{CAPACITY_INVENTORY_NAME}"
    )
    result = {}
    for entry in item.get("spec", {}).get("nodes", []):
        try:
            result[entry["name"]] = {
                "capacityMbps": max(1, int(float(entry["uplinkMbps"]))),
                "region": entry.get("region", ""),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def capacity(node, inventory=None):
    metadata = node.get("metadata", {})
    annotations = metadata.get("annotations", {})
    for key in (f"{API_GROUP}/observed-capacity-mbps", f"{API_GROUP}/capacity-mbps"):
        try:
            return max(1, int(float(annotations[key])))
        except (KeyError, TypeError, ValueError):
            pass
    inventory = inventory or {}
    if metadata.get("name") in inventory:
        value = inventory[metadata["name"]]
        return value["capacityMbps"] if isinstance(value, dict) else value
    return DEFAULT_CAPACITY


def locality(node, inventory=None):
    labels = node.get("metadata", {}).get("labels", {})
    inventory = inventory or {}
    inventory_region = inventory.get(node.get("metadata", {}).get("name"), {})
    inventory_region = inventory_region.get("region", "") if isinstance(inventory_region, dict) else ""
    region = labels.get("topology.kubernetes.io/region") or inventory_region or "unknown"
    area = labels.get(f"{API_GROUP}/area", "")
    if not area:
        area = region.split("-", 1)[0].upper() if region != "unknown" else "GLOBAL"
    return area, region


def desired_edge(node, public_ip, gateway, gateway_ip, assessment, listeners, node_capacity,
                 inventory=None):
    uid = node["metadata"]["uid"]
    area, region = locality(node, inventory)
    protocols = ["http", "https", "tls-passthrough"]
    ports = [80, 443]
    service_classes = sorted({value for value in os.getenv("SERVICE_CLASSES", "api,web,registry,db-ro,db-rw").split(",") if value})
    return {
        "apiVersion": f"{API_GROUP}/{API_VERSION}", "kind": "PublicEdge",
        "metadata": {"name": object_name(uid), "labels": {
            f"{API_GROUP}/managed": "true", f"{API_GROUP}/node-uid": uid,
        }},
        "spec": {
            "area": area, "region": region, "nodeName": node["metadata"]["name"],
            "enabled": True, "draining": False,
            "endpoint": {"type": "PublicIP", "value": public_ip},
            "gatewayVIP": gateway_ip, "capacityMbps": node_capacity, "priority": 0,
            "protocols": protocols, "ports": ports, "serviceClasses": service_classes,
            "forwarding": {"mode": "DirectGateway"},
            "gatewayRef": {"namespace": gateway["metadata"]["namespace"], "name": gateway["metadata"]["name"]},
            "discovery": {"source": "Node+NetworkPathAssessment+Gateway", "nodeUID": uid},
        },
        "status": {
            "observedAt": now_rfc3339(),
            "networkEvidence": assessment.get("status", {}),
            "gatewayPath": {"address": gateway_ip, "reachable": tcp_probe(gateway_ip, 443)},
            "protocolProbes": listeners,
            "conditions": [{"type": "Ready", "status": "True", "reason": "PublicListenersAndGatewayReady",
                            "message": "public listeners and canonical Gateway are reachable",
                            "lastTransitionTime": now_rfc3339()}],
        },
    }


def reconcile_object(desired, existing):
    name = desired["metadata"]["name"]
    base = f"/apis/{API_GROUP}/{API_VERSION}/publicedges/{name}"
    body = {key: desired[key] for key in ("apiVersion", "kind", "metadata", "spec")}
    if name in existing:
        persisted = patch(base, body)
    else:
        persisted = api(f"/apis/{API_GROUP}/{API_VERSION}/publicedges", "POST", body)
    desired["status"]["observedGeneration"] = persisted["metadata"]["generation"]
    patch(base + "/status", {"status": desired["status"]})


def patch_node_labels(node, ingress, authority):
    labels = node.get("metadata", {}).get("labels", {})
    desired = {INGRESS_LABEL: "true" if ingress else None,
               AUTHORITY_LABEL: "true" if authority else None}
    if all(labels.get(key) == value for key, value in desired.items() if value) and \
       all(key not in labels for key, value in desired.items() if value is None):
        return
    patch(f"/api/v1/nodes/{node['metadata']['name']}", {"metadata": {"labels": desired}})


def ensure_nameserver_configmap(nameservers):
    if not nameservers:
        return
    payload = {"nameservers.json": json.dumps({"nameservers": [item["ns"] for item in nameservers]}, separators=(",", ":"))}
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{NAMESERVER_CONFIGMAP}"
    try:
        patch(path, {"data": payload})
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        api(f"/api/v1/namespaces/{NAMESPACE}/configmaps", "POST", {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": NAMESERVER_CONFIGMAP}, "data": payload,
        })


def cloudflare(method, path, payload=None):
    token = CF_TOKEN
    if not token and CF_SECRET_NAMESPACE and CF_SECRET_NAME:
        import base64
        secret = api(f"/api/v1/namespaces/{CF_SECRET_NAMESPACE}/secrets/{CF_SECRET_NAME}")
        token = base64.b64decode(secret["data"][CF_SECRET_KEY]).decode()
    if not token:
        raise RuntimeError("Cloudflare token is unavailable")
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("success"):
        raise RuntimeError(result.get("errors"))
    return result["result"]


def reconcile_cloudflare(selected):
    if not (CF_TOKEN or (CF_SECRET_NAMESPACE and CF_SECRET_NAME)) or not PARENT_ZONE or not CHILD_ZONES or not selected:
        return
    zones = cloudflare("GET", f"/zones?name={urllib.parse.quote(PARENT_ZONE)}&per_page=1")
    if len(zones) != 1:
        raise RuntimeError("parent zone is not uniquely active")
    prefix = f"/zones/{zones[0]['id']}/dns_records"

    def records(name):
        return cloudflare("GET", f"{prefix}?name={urllib.parse.quote(name)}&per_page=100")

    for item in selected:
        name = item["ns"].rstrip(".")
        current = [record for record in records(name) if record["type"] == "A"]
        if not any(record["content"] == item["ip"] for record in current):
            cloudflare("POST", prefix, {"type": "A", "name": name, "content": item["ip"], "ttl": 60,
                                         "proxied": False, "comment": "PublicEdge dynamic authority"})
        for record in current:
            if record["content"] != item["ip"]:
                cloudflare("DELETE", f"{prefix}/{record['id']}")
    desired = {item["ns"].rstrip(".") for item in selected}
    for child in CHILD_ZONES:
        current = [record for record in records(child) if record["type"] == "NS"]
        existing = {record["content"].rstrip(".") for record in current}
        for ns in desired - existing:
            cloudflare("POST", prefix, {"type": "NS", "name": child, "content": ns, "ttl": 300,
                                         "comment": "PublicEdge dynamic delegation"})
        for record in current:
            if record["content"].rstrip(".") not in desired:
                cloudflare("DELETE", f"{prefix}/{record['id']}")


def acquire_lease():
    path = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE_NAME}"
    timestamp = now_rfc3339()
    try:
        lease = api(path)
        spec = lease.get("spec", {})
        expired = parse_time(spec.get("renewTime")) + int(spec.get("leaseDurationSeconds", 30)) < time.time()
        if spec.get("holderIdentity") not in (POD_NAME, None, "") and not expired:
            return False
        # Include resourceVersion so competing replicas cannot both acquire an
        # expired Lease from the same read. Kubernetes returns 409 to the loser.
        patch(path, {"metadata": {"resourceVersion": lease["metadata"]["resourceVersion"]},
                     "spec": {"holderIdentity": POD_NAME, "leaseDurationSeconds": 30,
                              "renewTime": timestamp}})
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return False
        if exc.code != 404:
            raise
        try:
            api(f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases", "POST", {
                "apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"name": LEASE_NAME},
                "spec": {"holderIdentity": POD_NAME, "leaseDurationSeconds": 30,
                         "acquireTime": timestamp, "renewTime": timestamp},
            })
        except urllib.error.HTTPError as create_exc:
            if create_exc.code == 409:
                return False
            raise
    return True


def reconcile():
    if not acquire_lease():
        return
    nodes = api("/api/v1/nodes").get("items", [])
    inventory = capacity_inventory_by_node()
    assessments = assessment_by_node(api(f"/apis/{NPA_GROUP}/{NPA_VERSION}/{NPA_RESOURCE}"))
    gateway_path = "/apis/gateway.networking.k8s.io/v1/gateways"
    if GATEWAY_NAMESPACE:
        gateway_path = f"/apis/gateway.networking.k8s.io/v1/namespaces/{GATEWAY_NAMESPACE}/gateways"
    gateway, gateway_ip = select_gateway(api(gateway_path).get("items", []))
    if not gateway:
        raise RuntimeError("no programmed public-edge Gateway matched selector")
    existing_items = api(f"/apis/{API_GROUP}/{API_VERSION}/publicedges").get("items", [])
    existing = {item["metadata"]["name"]: item for item in existing_items
                if item.get("metadata", {}).get("labels", {}).get(f"{API_GROUP}/managed") == "true"}
    desired_names = set()
    authorities = []
    for node in nodes:
        name = node.get("metadata", {}).get("name", "")
        uid = node.get("metadata", {}).get("uid", "")
        public_ip = global_external_ip(node)
        node_capacity = capacity(node, inventory)
        capacity_ready = node_capacity >= MINIMUM_CAPACITY
        path_ready = bool(public_ip and uid and capacity_ready and assessment_ready(assessments.get(name)))
        gateway_ready = path_ready and tcp_probe(gateway_ip, 443)
        # The generic redirector is scheduled from bootstrap eligibility.  On
        # the next cycle its public listeners qualify the derived PublicEdge.
        listeners = {"http": tcp_probe(public_ip, 80), "https": tcp_probe(public_ip, 443)} if gateway_ready else {}
        ingress_ready = gateway_ready and all(listeners.values())
        dns_ready = False
        if ingress_ready and PARENT_ZONE:
            dns_ready = all(dns_query(public_ip, zone, tcp) for zone in CHILD_ZONES for tcp in (False, True))
        patch_node_labels(node, gateway_ready, ingress_ready)
        if not ingress_ready:
            continue
        desired = desired_edge(node, public_ip, gateway, gateway_ip, assessments[name], listeners,
                               node_capacity, inventory)
        desired_names.add(desired["metadata"]["name"])
        reconcile_object(desired, existing)
        if dns_ready:
            authorities.append({"uid": uid, "ip": public_ip, "ns": nameserver_name(uid),
                                "region": desired["spec"]["region"], "capacity": desired["spec"]["capacityMbps"]})
    for name in set(existing) - desired_names:
        api(f"/apis/{API_GROUP}/{API_VERSION}/publicedges/{name}", "DELETE")
    ordered = sorted(authorities, key=lambda item: (-item["capacity"], item["uid"]))
    selected = []
    for candidate in ordered:
        if candidate["region"] not in {item["region"] for item in selected}:
            selected.append(candidate)
        if len(selected) >= MAX_NAMESERVERS:
            break
    for candidate in ordered:
        if len(selected) >= MAX_NAMESERVERS:
            break
        if candidate not in selected:
            selected.append(candidate)
    if len(selected) == 1:
        print("ALERT severity=critical reason=SingleNameserverDegraded", flush=True)
    elif not selected:
        print("ALERT severity=critical reason=NoHealthyNameserver preserving=last-known-good", flush=True)
    ensure_nameserver_configmap(selected)  # Empty keeps the last-known-good set.
    reconcile_cloudflare(selected)
    print(json.dumps({"nodes": len(nodes), "edges": len(desired_names), "authorities": len(selected),
                      "gateway": gateway_ip}), flush=True)


def main():
    while True:
        try:
            reconcile()
        except Exception as exc:
            print(f"reconcile error={type(exc).__name__}: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
