#!/usr/bin/env python3
"""Cluster-local operator CLI for PublicEdge runtime intent and status."""

import argparse
import json
import os
import ssl
import urllib.error
import urllib.request


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = os.getenv("POD_NAMESPACE", "regional-routing")
CONFIGMAP = os.getenv("RUNTIME_CONFIGMAP", "public-edge-runtime")
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")


def api(path, method="GET", payload=None):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    with open(f"{SA}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    request = urllib.request.Request(
        f"https://{host}:{port}{path}", method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/merge-patch+json"},
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        return {} if response.status == 204 else json.load(response)


def load_runtime():
    obj = api(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP}")
    return obj, json.loads(obj.get("data", {}).get("runtime.json", "{}"))


def save_runtime(obj, document):
    payload = {"metadata": {"resourceVersion": obj["metadata"]["resourceVersion"]},
               "data": {"runtime.json": json.dumps(document, sort_keys=True, separators=(",", ":"))}}
    api(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP}", "PATCH", payload)


def json_value(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def mutate(args):
    obj, document = load_runtime()
    document.setdefault("services", {})
    document.setdefault("model", {})
    document.setdefault("routes", {})
    if args.command == "service-register":
        definition = {"service": args.service, "class": args.service_class, "probePath": args.probe_path}
        if args.accepted_statuses:
            definition["acceptedStatuses"] = [int(value) for value in args.accepted_statuses.split(",")]
        document["services"][f"{args.domain.rstrip('.').lower()}."] = definition
    elif args.command == "service-delete":
        document["services"][f"{args.domain.rstrip('.').lower()}."] = None
    elif args.command == "model-set":
        document["model"][args.key] = json_value(args.value)
    elif args.command == "model-unset":
        document["model"].pop(args.key, None)
    elif args.command == "route-set":
        document["routes"][args.service] = [json_value(value) for value in args.paths]
    elif args.command == "route-delete":
        document["routes"].pop(args.service, None)
    save_runtime(obj, document)
    print(json.dumps(document, indent=2, sort_keys=True))


def status(_args):
    _, runtime = load_runtime()
    edges = api(f"/apis/{API_GROUP}/v1alpha1/publicedges").get("items", [])
    gateways = api("/apis/gateway.networking.k8s.io/v1/gateways").get("items", [])
    output = {
        "runtime": runtime,
        "edges": [{"name": item["metadata"]["name"], "spec": item.get("spec", {}),
                   "status": item.get("status", {})} for item in edges],
        "gateways": [{"namespace": item["metadata"]["namespace"], "name": item["metadata"]["name"],
                      "addresses": item.get("status", {}).get("addresses", []),
                      "conditions": item.get("status", {}).get("conditions", [])} for item in gateways
                     if item.get("metadata", {}).get("labels", {}).get("networking.re8ch.com/public-edge-gateway") == "true"],
    }
    print(json.dumps(output, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(prog="edgectl")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("service-register")
    register.add_argument("domain"); register.add_argument("--service", required=True)
    register.add_argument("--class", dest="service_class", default="web")
    register.add_argument("--probe-path", default="/"); register.add_argument("--accepted-statuses", default="")
    delete = sub.add_parser("service-delete"); delete.add_argument("domain")
    model = sub.add_parser("model-set"); model.add_argument("key"); model.add_argument("value")
    unset = sub.add_parser("model-unset"); unset.add_argument("key")
    route = sub.add_parser("route-set"); route.add_argument("service"); route.add_argument("paths", nargs="+")
    route_delete = sub.add_parser("route-delete"); route_delete.add_argument("service")
    sub.add_parser("status")
    args = parser.parse_args()
    status(args) if args.command == "status" else mutate(args)


if __name__ == "__main__":
    main()
