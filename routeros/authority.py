"""Small authoritative DNS front end for a RouterOS ARM container.

The normal path relays wire queries to independent Public Edge authorities so
ECS-based answers stay live. AXFR copies provide an authoritative global-view
fallback if those authorities are temporarily unreachable.
"""

import ipaddress
import json
import os
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dns.edns
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rdata
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import dns.zone


CONFIG = json.loads(Path(os.getenv("AUTHORITY_CONFIG", "/config/authority.json")).read_text())
UPSTREAMS = tuple(CONFIG["upstreams"])
PRIMARY = CONFIG["axfrPrimary"]
ZONES = tuple(dns.name.from_text(name).canonicalize() for name in CONFIG["zones"])
STATE_DIR = Path(CONFIG.get("stateDirectory", "/state"))
REFRESH_SECONDS = max(30, int(CONFIG.get("refreshSeconds", 300)))
MAX_STALE_SECONDS = max(300, int(CONFIG.get("maxStaleSeconds", 86400)))
TIMEOUT = max(0.2, float(CONFIG.get("upstreamTimeoutSeconds", 2)))
LOCK = threading.RLock()
COPIES = {}
LAST_REFRESH = {}


def zone_path(origin):
    return STATE_DIR / (origin.to_text().rstrip(".") + ".zone")


def load_copies():
    for origin in ZONES:
        path = zone_path(origin)
        if not path.exists():
            continue
        try:
            zone = dns.zone.from_file(str(path), origin=origin, relativize=True)
            with LOCK:
                COPIES[origin] = zone
                LAST_REFRESH[origin] = path.stat().st_mtime
        except (OSError, dns.exception.DNSException) as exc:
            print(f"invalid saved zone {origin}: {exc}", flush=True)


def refresh_zone(origin):
    messages = dns.query.xfr(PRIMARY, origin, lifetime=20, relativize=True)
    zone = dns.zone.from_xfr(messages, relativize=True)
    if zone is None or zone.get_rdataset("@", dns.rdatatype.SOA) is None:
        raise ValueError(f"AXFR for {origin} has no SOA")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = zone_path(origin)
    old_text = target.read_text() if target.exists() else None
    new_text = zone.to_text(relativize=True)
    if new_text != old_text:
        with tempfile.NamedTemporaryFile("w", dir=STATE_DIR, prefix=".zone-", delete=False) as stream:
            stream.write(new_text)
            temporary = stream.name
        os.replace(temporary, target)
    else:
        target.touch()
    with LOCK:
        COPIES[origin] = zone
        LAST_REFRESH[origin] = time.time()


def refresh_loop():
    while True:
        for origin in ZONES:
            try:
                refresh_zone(origin)
            except (OSError, ValueError, dns.exception.DNSException) as exc:
                print(f"AXFR failed zone={origin} reason={exc}", flush=True)
        time.sleep(REFRESH_SECONDS)


def add_client_subnet(query, client_ip):
    if any(isinstance(option, dns.edns.ECSOption) for option in query.options):
        return
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return
    if not address.is_global:
        return
    prefix = 24 if address.version == 4 else 56
    options = list(query.options) + [dns.edns.ECSOption(str(address), prefix)]
    query.use_edns(edns=0, ednsflags=query.ednsflags, payload=max(query.payload, 1232), options=options)


def absolute_rrset(name, rdataset, origin):
    result = dns.rrset.RRset(name, rdataset.rdclass, rdataset.rdtype)
    for rdata in rdataset:
        absolute = dns.rdata.from_text(
            rdataset.rdclass, rdataset.rdtype,
            rdata.to_text(origin=origin, relativize=False), origin=origin, relativize=False,
        )
        result.add(absolute, rdataset.ttl)
    return result


def local_answer(query):
    if len(query.question) != 1:
        answer = dns.message.make_response(query)
        answer.set_rcode(dns.rcode.FORMERR)
        return answer
    question = query.question[0]
    origin = next((zone for zone in ZONES if question.name.is_subdomain(zone)), None)
    if origin is None:
        answer = dns.message.make_response(query)
        answer.set_rcode(dns.rcode.REFUSED)
        return answer
    with LOCK:
        zone = COPIES.get(origin)
        refreshed = LAST_REFRESH.get(origin, 0)
    if zone is None or time.time() - refreshed > MAX_STALE_SECONDS:
        answer = dns.message.make_response(query)
        answer.set_rcode(dns.rcode.SERVFAIL)
        return answer
    answer = dns.message.make_response(query)
    answer.flags |= dns.flags.AA
    answer.flags &= ~dns.flags.RA
    relative = question.name.relativize(origin)
    node = zone.get_node(relative)
    if node is None:
        answer.set_rcode(dns.rcode.NXDOMAIN)
    else:
        types = (dns.rdatatype.CNAME, question.rdtype)
        if question.rdtype == dns.rdatatype.ANY:
            types = tuple(rdataset.rdtype for rdataset in node.rdatasets)
        for rdtype in dict.fromkeys(types):
            rdataset = node.get_rdataset(dns.rdataclass.IN, rdtype)
            if rdataset:
                answer.answer.append(absolute_rrset(question.name, rdataset, origin))
    if not answer.answer:
        soa = zone.get_rdataset("@", dns.rdatatype.SOA)
        if soa:
            answer.authority.append(absolute_rrset(origin, soa, origin))
    return answer


def answer_wire(wire, client_ip, tcp=False):
    try:
        query = dns.message.from_wire(wire)
    except dns.exception.DNSException:
        return b""
    if len(query.question) != 1:
        return local_answer(query).to_wire()
    if not any(query.question[0].name.is_subdomain(origin) for origin in ZONES):
        return local_answer(query).to_wire()
    forwarded = dns.message.from_wire(wire)
    add_client_subnet(forwarded, client_ip)
    for upstream in UPSTREAMS:
        try:
            response = (dns.query.tcp if tcp else dns.query.udp)(forwarded, upstream, timeout=TIMEOUT)
            if (response.flags & dns.flags.AA and not response.flags & dns.flags.RA and
                    response.rcode() in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN)):
                response.id = query.id
                return response.to_wire()
        except (OSError, dns.exception.DNSException):
            continue
    return local_answer(query).to_wire()


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        response = answer_wire(data, self.client_address[0])
        if response:
            sock.sendto(response, self.client_address)


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        size = self.request.recv(2)
        if len(size) != 2:
            return
        expected = int.from_bytes(size, "big")
        data = bytearray()
        while len(data) < expected:
            part = self.request.recv(expected - len(data))
            if not part:
                return
            data.extend(part)
        response = answer_wire(bytes(data), self.client_address[0], tcp=True)
        if response:
            self.request.sendall(len(response).to_bytes(2, "big") + response)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        with LOCK:
            healthy = all(origin in COPIES and time.time() - LAST_REFRESH.get(origin, 0) < MAX_STALE_SECONDS
                          for origin in ZONES)
        self.send_response(200 if healthy else 503)
        self.end_headers()
        self.wfile.write(b"ready\n" if healthy else b"stale\n")

    def log_message(self, *_):
        pass


class ThreadingUDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not UPSTREAMS or not ZONES:
        raise ValueError("upstreams and zones must be nonempty")
    if any(address == CONFIG.get("publicAddress") for address in UPSTREAMS):
        raise ValueError("RouterOS public address cannot be its own upstream")
    load_copies()
    threading.Thread(target=refresh_loop, daemon=True).start()
    address = CONFIG.get("listenAddress", "0.0.0.0")
    port = int(CONFIG.get("listenPort", 53))
    udp = ThreadingUDPServer((address, port), UDPHandler)
    tcp = ThreadingTCPServer((address, port), TCPHandler)
    health = ThreadingHTTPServer((address, int(CONFIG.get("healthPort", 8080))), HealthHandler)
    for server in (udp, tcp, health):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"RouterOS authority listening on {address}:{port}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
