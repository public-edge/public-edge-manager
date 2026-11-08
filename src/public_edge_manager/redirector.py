#!/usr/bin/env python3
"""L4 public listener with optional exact-SNI upstream overrides."""

import asyncio
import json
import os
import ssl
import urllib.request


SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NODE = os.environ["NODE_NAME"]
API_GROUP = os.getenv("API_GROUP", "networking.re8ch.com")
TARGET = {"host": "", "ports": {80, 443}}
SNI_ROUTES = json.loads(os.getenv("SNI_ROUTES", "{}"))
CLIENT_HELLO_LIMIT = 65536
CLIENT_HELLO_TIMEOUT = 3


def client_hello_sni(data):
    """Return the lower-case SNI from buffered TLS records, if complete."""
    payload = bytearray()
    offset = 0
    while offset + 5 <= len(data):
        content_type = data[offset]
        length = int.from_bytes(data[offset + 3:offset + 5], "big")
        end = offset + 5 + length
        if end > len(data):
            return None
        if content_type == 22:
            payload.extend(data[offset + 5:end])
        offset = end
        if len(payload) >= 4:
            message_length = int.from_bytes(payload[1:4], "big")
            if len(payload) >= 4 + message_length:
                break
    if len(payload) < 4 or payload[0] != 1:
        return None
    message_length = int.from_bytes(payload[1:4], "big")
    hello = memoryview(payload)[4:4 + message_length]
    if len(hello) < 35:
        return None
    cursor = 34
    session_length = hello[cursor]
    cursor += 1 + session_length
    if cursor + 2 > len(hello):
        return None
    cipher_length = int.from_bytes(hello[cursor:cursor + 2], "big")
    cursor += 2 + cipher_length
    if cursor >= len(hello):
        return None
    compression_length = hello[cursor]
    cursor += 1 + compression_length
    if cursor + 2 > len(hello):
        return None
    extensions_length = int.from_bytes(hello[cursor:cursor + 2], "big")
    cursor += 2
    extensions_end = min(cursor + extensions_length, len(hello))
    while cursor + 4 <= extensions_end:
        extension_type = int.from_bytes(hello[cursor:cursor + 2], "big")
        extension_length = int.from_bytes(hello[cursor + 2:cursor + 4], "big")
        cursor += 4
        extension_end = cursor + extension_length
        if extension_end > extensions_end:
            return None
        if extension_type == 0 and extension_length >= 5:
            names = hello[cursor:extension_end]
            names_cursor = 2
            while names_cursor + 3 <= len(names):
                name_type = names[names_cursor]
                name_length = int.from_bytes(names[names_cursor + 1:names_cursor + 3], "big")
                names_cursor += 3
                if names_cursor + name_length > len(names):
                    return None
                if name_type == 0:
                    try:
                        return bytes(names[names_cursor:names_cursor + name_length]).decode("ascii").lower()
                    except UnicodeDecodeError:
                        return None
                names_cursor += name_length
        cursor = extension_end
    return None


async def tls_prefix(reader):
    data = bytearray()
    while len(data) < CLIENT_HELLO_LIMIT:
        chunk = await asyncio.wait_for(reader.read(min(4096, CLIENT_HELLO_LIMIT - len(data))),
                                       timeout=CLIENT_HELLO_TIMEOUT)
        if not chunk:
            break
        data.extend(chunk)
        hostname = client_hello_sni(data)
        if hostname:
            return bytes(data), hostname
        if len(data) >= 5:
            record_length = int.from_bytes(data[3:5], "big")
            if len(data) >= 5 + record_length and data[0] != 22:
                break
    return bytes(data), None


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
    upstream_port = port
    prefix = b""
    if port == 443 and SNI_ROUTES:
        try:
            prefix, hostname = await tls_prefix(client_reader)
        except (asyncio.TimeoutError, ConnectionError):
            client_writer.close()
            return
        route = SNI_ROUTES.get(hostname, {})
        host = route.get("host", host)
        upstream_port = route.get("port", port)
    if not host:
        client_writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, upstream_port), timeout=3
        )
    except (OSError, asyncio.TimeoutError):
        client_writer.close()
        return
    if prefix:
        upstream_writer.write(prefix)
        await upstream_writer.drain()
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
