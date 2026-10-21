# Dependency and responsibility map

Public Edge Manager is usable without every integration listed below. Start
with Kubernetes, Gateway API, and delegated DNS; add other components only for
the evidence or publication path you need.

| Component | Required | What Public Edge Manager consumes or produces | What remains outside this project |
| --- | --- | --- | --- |
| Kubernetes API | Yes | Reads Nodes and Gateways; writes derived `PublicEdge`, labels, Lease, and runtime ConfigMaps | Node lifecycle, scheduling, Service and Pod reconciliation |
| Gateway API | Yes | Reads a selected Programmed Gateway and its canonical VIP | Listener, certificate, route, and backend reconciliation |
| Delegated DNS | Yes for authoritative service | Serves NS, SOA, A/AAAA/CNAME and negative answers for configured zones | Registrar and parent-zone ownership |
| ExternalDNS | Optional | Writes the selected target to a dedicated Ingress annotation | Provider authentication and DNS-record reconciliation |
| PowerDNS | Optional, through ExternalDNS | No direct API call; receives RFC 2136 changes from an isolated ExternalDNS instance | PowerDNS operation, TSIG keys, zones, replication, and backups |
| Globalping | Optional | Consumes bounded external DNS, HTTP, and concrete-edge observations | Probe infrastructure and Globalping account/token lifecycle |
| NetworkPathAssessment producer | Optional | Reads provider-neutral, fresh path evidence | Network measurement and dataplane configuration |
| Cloudflare | Optional | Can reconcile configured parent-delegation records | Zone ownership, token provisioning, and unrelated records |
| RouterOS or another authority | Optional | Qualifies an independent authority with public UDP/TCP probes; can publish a signed bundle | Router configuration, NAT, BGP, firewall, and credentials |
| Flux | Recommended, not runtime-required | Applies reviewed durable Helm/GitOps configuration | Git reconciliation and repository policy |

## PowerDNS publication path

```text
PublicEdge health and ranking
            │
            ▼
Public Edge Manager selects one target
            │ writes ExternalDNS annotation
            ▼
Dedicated publication Ingress
            │ watched by an isolated ExternalDNS
            ▼
ExternalDNS RFC 2136 provider
            │ authenticated with its own TSIG secret
            ▼
PowerDNS authoritative zone
```

This separation is intentional:

- Public Edge Manager decides **which edge is eligible and selected**.
- ExternalDNS decides **how to translate the annotation into a provider
  change**.
- PowerDNS decides **how the zone is stored and served**.

Do not mount a PowerDNS API key or RFC 2136 TSIG secret into Public Edge
Manager. Put those credentials only in the isolated ExternalDNS deployment.

## Example publication adapter

The referenced Ingress must be dedicated to one adapter; two adapters cannot
claim the same object.

```yaml
publication:
  enabled: true
  adapters:
    powerdns:
      provider: external-dns-rfc2136
      enabled: true
      refs:
        app:
          namespace: public-edge-publication
          name: app-powerdns-target
```

Public Edge Manager patches only these annotations on that object:

```yaml
external-dns.alpha.kubernetes.io/target: 203.0.113.10
networking.re8ch.com/selected-public-edge: edge-example
networking.re8ch.com/publication-adapter: powerdns
networking.re8ch.com/dns-provider: external-dns-rfc2136
```

Enable `publication.enabled` and `rbac.mutateIngresses` only after the adapter,
its dedicated object, and its rollback path have been reviewed.
