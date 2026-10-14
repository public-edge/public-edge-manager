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
EXTRA_AUTHORITIES = json.loads(os.getenv("GLOBALPING_AUTHORITY_ADDRESSES_JSON", "[]"))
MAX_TARGETS = max(1, int(os.getenv("GLOBALPING_MAX_TARGETS_PER_KIND", "8")))
MAX_MEASUREMENTS = max(1, int(os.getenv("GLOBALPING_MAX_MEASUREMENTS_PER_CYCLE", "8")))
OUTPUT_CONFIGMAP = os.getenv("GLOBALPING_OUTPUT_CONFIGMAP", "public-edge-globalping-quality")
USER_AGENT = os.getenv("PROBE_USER_AGENT", "public-edge-manager/0.7")


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
               "limit": max(1, len(LOCATIONS)), "timeout": 15}
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


def summarize(measurement):
    samples = []
    measurement_type = measurement.get("type", "")
    for item in measurement.get("results", []):
        result = item.get("result", {})
        probe = item.get("probe", {})
        stats = result.get("stats", {})
        timings = result.get("timings", {})
        timing_total = timings.get("total") if isinstance(timings, dict) else None
        usable = result.get("status") == "finished"
        if measurement_type == "ping":
            usable = usable and isinstance(stats.get("loss"), (int, float)) and stats["loss"] < 100
        elif measurement_type == "dns":
            usable = usable and result.get("statusCode") == 0 and bool(result.get("answers"))
        elif measurement_type == "http":
            usable = usable and isinstance(result.get("statusCode"), int) and result["statusCode"] < 500
        samples.append({
            "status": result.get("status", "unknown"),
            "ready": usable,
            "country": probe.get("country", ""),
            "region": probe.get("region", ""),
            "latencyMs": stats.get("avg", timing_total),
            "lossPercent": stats.get("loss"),
            "statusCode": result.get("statusCode"),
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


def measure(kind, target, options=None):
    try:
        return summarize(create_measurement(kind, target, options))
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
    kubernetes_api(
        f"/apis/{API_GROUP}/{API_VERSION}/publicedges/{edge_name}/status", "PATCH",
        {"status": {"externalQuality": {"provider": "globalping", "observedAt": observed_at,
                                        **{key: value for key, value in result.items()
                                           if key != "samples"}}}},
        "application/merge-patch+json",
    )


def observe_once():
    observed_at = now_rfc3339()
    edges = public_edges()[:MAX_TARGETS]
    candidate_results = {}
    measurements = 0
    per_kind = max(1, MAX_MEASUREMENTS // 3)
    for edge in edges[:per_kind]:
        result = measure("ping", edge["address"], {"packets": 3, "protocol": "ICMP"})
        measurements += 1
        candidate_results[edge["name"]] = {"target": edge["address"], **result}
        publish_edge_quality(edge["name"], result, observed_at)
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
        hostname = target.rstrip(".")
        http_results[hostname] = measure("http", hostname, {
            "protocol": "HTTPS", "request": {"method": "GET"}})
        measurements += 1
    document = {"provider": "globalping", "observedAt": observed_at,
                "locations": LOCATIONS, "candidates": candidate_results,
                "authorities": dns_results, "services": http_results}
    publish_configmap(document)
    print(json.dumps({"observedAt": observed_at, "candidates": len(candidate_results),
                      "authorities": len(dns_results), "services": len(http_results),
                      "measurements": measurements}), flush=True)
    return document


def main():
    if not TOKEN:
        raise RuntimeError("GLOBALPING_API_TOKEN is required")
    while True:
        started = time.monotonic()
        try:
            observe_once()
        except Exception as exc:
            print(f"globalping_observer error={type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1, INTERVAL - int(time.monotonic() - started)))


if __name__ == "__main__":
    main()
