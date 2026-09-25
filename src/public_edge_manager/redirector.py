#!/usr/bin/env python3
"""Generic L4 public listener forwarding to the discovered Gateway VIP."""

import asyncio
import json
import os
import ssl
import urllib.request


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NODE = os.environ["NODE_NAME"]
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")
TARGET = {"host": "", "ports": {80, 443}}


def kubernetes_get(path):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    with open(f"{SA}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    request = urllib.request.Request(f"https://{host}:{port}{path}",
                                     headers={"Authorization": f"Bearer {token}"})
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(request, context=context, timeout=5) as response:
        return json.load(response)


async def refresh_target():
    while True:
        try:
            payload = await asyncio.to_thread(
                kubernetes_get, f"/apis/{API_GROUP}/v1alpha1/publicedges"
            )
            match = next((item for item in payload.get("items", [])
                          if item.get("spec", {}).get("nodeName") == NODE and
                          item.get("metadata", {}).get("labels", {}).get(f"{API_GROUP}/managed") == "true"), None)
            TARGET["host"] = match.get("spec", {}).get("gatewayVIP", "") if match else ""
        except Exception as exc:
            print(f"target refresh failed: {exc}", flush=True)
        await asyncio.sleep(5)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(client_reader, client_writer, port):
    host = TARGET["host"]
    if not host:
        client_writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3
        )
    except (OSError, asyncio.TimeoutError):
        client_writer.close()
        return
    await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer))


async def main_async():
    asyncio.create_task(refresh_target())
    servers = [await asyncio.start_server(lambda r, w, p=port: handle(r, w, p), "0.0.0.0", port)
               for port in sorted(TARGET["ports"])]
    print(f"node={NODE} listeners=80,443", flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
