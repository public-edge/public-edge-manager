# RouterOS authoritative DNS front end

The RouterOS built-in `/ip dns` remains the LAN caching resolver. This image
runs on RouterOS's ARM32 container package behind a dedicated `veth` address.
Only WAN UDP/TCP 53 should DNAT to that address. Do not expose the container's
health port publicly or turn the router resolver into an open recursive DNS
service.

`authority.example.json` uses documentation addresses and names. Replace them
with the deployed independent public authorities and delegated child zones. At
startup the front end loads saved AXFR copies,
refreshes them from `axfrPrimary`, and forwards normal queries to `upstreams`.
It carries existing ECS and adds a /24 IPv4 or /56 IPv6 ECS for a public client
that did not supply one. If every upstream fails, it answers from an AXFR copy
for up to `maxStaleSeconds`; outside configured zones it refuses. `/healthz`
returns 200 only while every configured zone has a fresh copy.

Build for RouterOS ARM32 with `docker buildx build --platform linux/arm/v7 -f
routeros/Dockerfile routeros`. Keep `/config/authority.json` read-only and
`/state` writable and persistent. Pin the image digest when importing it into
RouterOS. The container binds privileged port 53 as root inside its RouterOS
container namespace. The production authority must allow AXFR from the router's actual
egress address. Confirm the source address, zone serials, SOA/NS answers and
health endpoint before adding it to `discovery.externalAuthorities`.
