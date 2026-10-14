#!/usr/bin/env python3
"""Periodically collect Globalping evidence for PublicEdge candidates and services."""

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")
API_VERSION = os.getenv("API_VERSION", "v1alpha1")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
INTERVAL = max(300, int(os.getenv("GLOBALPING_INTERVAL_SECONDS", "300")))
TOKEN = os.getenv("GLOBALPING_API_TOKEN", "")
BASE_URL = os.getenv("GLOBALPING_API_URL", "https://api.globalping.io").rstrip("/")
LOCATIONS = json.loads(os.getenv("GLOBALPING_LOCATIONS_JSON", "[]"))
DNS_TARGETS = json.loads(os.getenv("GLOBALPING_DNS_TARGETS_JSON", "[]"))
HTTP_TARGETS = json.loads(os.getenv("GLOBALPING_HTTP_TARGETS_JSON", "[]"))
EDGE_HTTP_TARGETS = json.loads(os.getenv("GLOBALPING_EDGE_HTTP_TARGETS_JSON", "[]"))
EXTRA_AUTHORITIES = json.loads(os.getenv("GLOBALPING_AUTHORITY_ADDRESSES_JSON", "[]"))
MAX_TARGETS = max(1, int(os.getenv("GLOBALPING_MAX_TARGETS_PER_KIND", "8")))
MAX_MEASUREMENTS = max(1, int(os.getenv("GLOBALPING_MAX_MEASUREMENTS_PER_CYCLE", "8")))
PROBE_LIMIT = max(1, min(50, int(os.getenv("GLOBALPING_PROBE_LIMIT", "10"))))
OUTPUT_CONFIGMAP = os.getenv("GLOBALPING_OUTPUT_CONFIGMAP", "public-edge-globalping-quality")
USER_AGENT = os.getenv("PROBE_USER_AGENT", "public-edge-manager/0.8.2")


def now_rfc3339():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def request_json(url, method="GET", payload=None, headers=None, timeout=45):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": USER_AGENT, **(headers or {}),
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def kubernetes_api(path, method="GET", payload=None, content_type="application/json"):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    with open(f"{SA}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"https://{host}:{port}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        return {} if response.status == 204 else json.load(response)


def create_measurement(kind, target, options=None):
    payload = {"type": kind, "target": target, "locations": LOCATIONS,
               "limit": PROBE_LIMIT, "timeout": 15}
    if options:
        payload["measurementOptions"] = options
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    created = request_json(f"{BASE_URL}/v1/measurements", "POST", payload, headers)
    measurement_id = created["id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = request_json(f"{BASE_URL}/v1/measurements/{measurement_id}", headers=headers)
        if result.get("status") != "in-progress":
            return result
        time.sleep(0.6)
    raise TimeoutError(f"measurement {measurement_id} did not finish")


def summarize(measurement, accepted_statuses=None):
    samples = []
    measurement_type = measurement.get("type", "")
    for item in measurement.get("results", []):
        result = item.get("result", {})
        probe = item.get("probe", {})
        stats = result.get("stats", {})
        timings = result.get("timings", {})
        if not isinstance(timings, dict):
            timings = {}
        timing_total = timings.get("total") if isinstance(timings, dict) else None
        usable = result.get("status") == "finished"
        if measurement_type == "ping":
            usable = usable and isinstance(stats.get("loss"), (int, float)) and stats["loss"] < 100
        elif measurement_type == "dns":
            usable = usable and result.get("statusCode") == 0 and bool(result.get("answers"))
        elif measurement_type == "http":
            status_code = result.get("statusCode")
            usable = usable and isinstance(status_code, int) and (
                status_code in accepted_statuses if accepted_statuses is not None
                else 200 <= status_code < 400 or status_code in (401, 403)
            )
        samples.append({
            "status": result.get("status", "unknown"),
            "ready": usable,
            "country": probe.get("country", ""),
            "region": probe.get("region", ""),
            "latencyMs": stats.get("avg", timing_total),
            "lossPercent": stats.get("loss"),
            "statusCode": result.get("statusCode"),
            "resolvedAddress": result.get("resolvedAddress", ""),
            "timings": {
                key: timings.get(key) for key in
                ("dns", "tcp", "tls", "firstByte", "download", "total")
                if isinstance(timings.get(key), (int, float))
            },
        })
    finished = [item for item in samples if item["status"] == "finished"]
    latencies = [item["latencyMs"] for item in finished if isinstance(item["latencyMs"], (int, float))]
    losses = [item["lossPercent"] for item in finished if isinstance(item["lossPercent"], (int, float))]
    return {
        "measurementId": measurement.get("id", ""),
        "ready": bool(samples) and all(item["ready"] for item in samples),
        "probes": len(samples),
        "finished": len(finished),
        "latencyAvgMs": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "latencyMaxMs": round(max(latencies), 2) if latencies else None,
        "lossMaxPercent": max(losses) if losses else None,
        "samples": samples,
    }


def measure(kind, target, options=None, accepted_statuses=None):
    try:
        return summarize(create_measurement(kind, target, options), accepted_statuses)
    except Exception as exc:
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"[:240]}


def public_edges():
    payload = kubernetes_api(f"/apis/{API_GROUP}/{API_VERSION}/publicedges")
    result = []
    for item in payload.get("items", []):
        endpoint = item.get("spec", {}).get("endpoint", {})
        if item.get("spec", {}).get("enabled") and endpoint.get("value"):
            result.append({"name": item["metadata"]["name"], "address": endpoint["value"]})
    return result


def http_target(value):
    """Normalize a service probe contract while retaining string compatibility."""
    if isinstance(value, str):
        return {"hostname": value.rstrip("."), "path": "/", "method": "GET",
                "acceptedStatuses": [200, 201, 202, 204, 301, 302, 307, 308, 401, 403]}
    if not isinstance(value, dict) or not value.get("hostname"):
        raise ValueError("HTTP target requires hostname")
    statuses = value.get("acceptedStatuses", [200])
    if not isinstance(statuses, list) or not statuses or any(
            not isinstance(status, int) or isinstance(status, bool) for status in statuses):
        raise ValueError("HTTP target acceptedStatuses must be a non-empty integer list")
    return {"hostname": value["hostname"].rstrip("."),
            "path": value.get("path", "/"), "method": value.get("method", "GET"),
            "acceptedStatuses": statuses}


def measure_http(contract, address=None):
    target = http_target(contract)
    options = {"protocol": "HTTPS", "request": {
        "method": target["method"], "path": target["path"]}}
    if address:
        # Globalping's host option preserves TLS SNI and the HTTP Host header
        # while connecting to the selected PublicEdge address.
        options["request"]["host"] = target["hostname"]
    result = measure("http", address or target["hostname"], options,
                     set(target["acceptedStatuses"]))
    return target, result


def publish_configmap(document):
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{OUTPUT_CONFIGMAP}"
    payload = {"data": {"quality.json": json.dumps(document, separators=(",", ":"), sort_keys=True)}}
    try:
        kubernetes_api(path, "PATCH", payload, "application/merge-patch+json")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        kubernetes_api(f"/api/v1/namespaces/{NAMESPACE}/configmaps", "POST", {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": OUTPUT_CONFIGMAP},
            **payload,
        })


def publish_edge_quality(edge_name, result, observed_at):
    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items() if key != "samples"}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    kubernetes_api(
        f"/apis/{API_GROUP}/{API_VERSION}/publicedges/{edge_name}/status", "PATCH",
        {"status": {"externalQuality": {"provider": "globalping", "observedAt": observed_at,
                                        **compact(result)}}},
        "application/merge-patch+json",
    )


def observe_once():
    observed_at = now_rfc3339()
    edges = public_edges()[:MAX_TARGETS]
    candidate_results = {
        edge["name"]: {"target": edge["address"], "ping": {
            "ready": False, "error": "measurement budget exhausted"}, "services": {}}
        for edge in edges
    }
    measurements = 0
    per_kind = max(1, MAX_MEASUREMENTS // 3)
    for edge in edges[:per_kind]:
        result = measure("ping", edge["address"], {"packets": 3, "protocol": "ICMP"})
        measurements += 1
        candidate_results[edge["name"]]["ping"] = result
    authority_addresses = list(dict.fromkeys(
        [edge["address"] for edge in edges] + [str(value) for value in EXTRA_AUTHORITIES]
    ))[:MAX_TARGETS]
    dns_results = {}
    for address in authority_addresses[:per_kind]:
        for target in DNS_TARGETS[:1]:
            if measurements >= MAX_MEASUREMENTS:
                break
            key = f"{address}/{target}"
            dns_results[key] = measure("dns", target, {
                "resolver": address, "protocol": "UDP", "query": {"type": "SOA"},
            })
            measurements += 1
    http_results = {}
    for target in HTTP_TARGETS[:per_kind]:
        if measurements >= MAX_MEASUREMENTS:
            break
        contract, result = measure_http(target)
        http_results[contract["hostname"]] = {"path": contract["path"], **result}
        measurements += 1
    # Probe configured critical services through each concrete edge address.
    # This detects SNI/TLS failures hidden by ordinary DNS load balancing.
    for edge in edges:
        for target in EDGE_HTTP_TARGETS:
            if measurements >= MAX_MEASUREMENTS:
                break
            contract, result = measure_http(target, edge["address"])
            candidate_results[edge["name"]]["services"][contract["hostname"]] = {
                "path": contract["path"], **result}
            measurements += 1
        combined = candidate_results[edge["name"]]
        service_results = list(combined["services"].values())
        combined["ready"] = combined["ping"].get("ready", False) and all(
            item.get("ready", False) for item in service_results)
        publish_edge_quality(edge["name"], combined, observed_at)
    document = {"provider": "globalping", "observedAt": observed_at,
                "locations": LOCATIONS, "candidates": candidate_results,
                "authorities": dns_results, "services": http_results}
    publish_configmap(document)
    print(json.dumps({"observedAt": observed_at, "candidates": len(candidate_results),
                      "authorities": len(dns_results), "services": len(http_results),
                      "measurements": measurements}), flush=True)
    return document


def main():
    while True:
        started = time.monotonic()
        try:
            observe_once()
        except Exception as exc:
            print(f"globalping_observer error={type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1, INTERVAL - int(time.monotonic() - started)))


if __name__ == "__main__":
    main()
