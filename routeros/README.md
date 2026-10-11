# Independent RouterOS DNS authority

The ARM32 image runs the same Public Edge authoritative engine used by the
Kubernetes authorities. It does not relay DNS queries or request AXFR from
another authority. It independently probes published service candidates and
selects addresses using the common geographic policy. Queries outside the
published zones receive REFUSED; it is not a recursive resolver.

The bundle publisher is a separate control-plane workload. It reads the same
records, runtime ConfigMap, nameserver election, PublicEdge candidate catalog,
and AdvFab path assessments used by the Kubernetes authorities. It signs a canonical JSON bundle
with an HMAC key stored in a Kubernetes Secret. The RouterOS container polls a
private publication URL, verifies the signature and freshness, and atomically
updates live files in `/state` (tmpfs). A signed checkpoint in `/data` survives
a publisher outage and router reboot, so
the router can continue serving DNS and probing candidates without any other
DNS authority. `/healthz` becomes 503 after the publication age limit;
`/livez` remains 200 while the process is alive.

Build with `docker buildx build --platform linux/arm/v7 -f routeros/Dockerfile .`.
Mount `authority.example.json` as `/config/authority.json` after replacing the
example URL, and mount the HMAC key separately as `/config/bundle.key`. Mount a
writable `/state` tmpfs for live data and a small writable `/data` directory
for the last signed checkpoint. Keep the publisher
restricted to the RouterOS container over a private path; for a plain HTTP
private feed, set `allowPrivateHttp: true` explicitly. The HMAC key must be at
least 32 random bytes and must never enter Git. Pin both images by digest.
Keep RouterOS's built-in `/ip dns` for LAN recursion only, and publish the
container as an external authority only after its UDP/TCP SOA and address
answers pass from outside the LAN.
