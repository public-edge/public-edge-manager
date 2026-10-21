# Public Edge Manager

[![CI](https://github.com/public-edge/public-edge-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/public-edge/public-edge-manager/actions/workflows/ci.yml)
[![Security governance](https://github.com/public-edge/public-edge-manager/actions/workflows/security.yml/badge.svg)](https://github.com/public-edge/public-edge-manager/actions/workflows/security.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/public-edge/public-edge-manager/badge)](https://securityscorecards.dev/viewer/?uri=github.com/public-edge/public-edge-manager)
[![License](https://img.shields.io/github/license/public-edge/public-edge-manager)](LICENSE)

Public Edge Manager is an open-source Kubernetes controller and authoritative
DNS service for selecting healthy public ingress edges by locality, capacity,
priority, and observed application latency. It is application- and DNS-provider
agnostic: operators supply zones, services and model parameters. Nodes,
endpoints and Gateway VIPs are discovered from Kubernetes facts.

The repository is the complete build context. It contains the controller source,
tests, container definition, Helm chart, license, and CI/release workflows. It
does not require code or ConfigMaps from a separate private repository.

## Start here

- **Install or evaluate:** follow [Install](#install) with
  [`examples/values-example.yaml`](examples/values-example.yaml).
- **Understand the model:** read [How it works](#how-it-works) and the
  [exposure and security model](#exposure-and-security-model).
- **Contribute:** use [`CONTRIBUTING.md`](CONTRIBUTING.md); pull requests run
  tests, chart validation, dependency review and CodeQL.
- **Report a vulnerability:** use the private process in
  [`SECURITY.md`](SECURITY.md), not a public issue.
- **Operate a release:** select immutable package digests from the GitHub
  Release's signed `release-manifest.json`.

### Published artifacts

Every public artifact produced by this repository is registered in
[`artifacts.json`](artifacts.json). CI rejects missing sources, inconsistent
versions, unregistered signing methods and release workflows that no longer
publish a registered artifact.

| Artifact | Public location | Platform | Verification |
| --- | --- | --- | --- |
| Controller | `ghcr.io/public-edge/public-edge-manager` | `linux/amd64` | keyless Cosign image signature |
| RouterOS authority | `ghcr.io/public-edge/routeros-authority` | `linux/arm/v7` | keyless Cosign image signature |
| Helm chart | `oci://ghcr.io/public-edge/charts/public-edge-manager` | Kubernetes | checksum and Cosign bundle in the GitHub Release |
| Release manifest | [GitHub Releases](https://github.com/public-edge/public-edge-manager/releases) | JSON | checksum and Cosign bundle |

The repository-level inventory describes what may be published. Each GitHub
Release contains a signed `release-manifest.json` recording what was actually
published, including immutable image and chart digests.

To inspect and verify a release before installing it:

```sh
VERSION=0.8.3
gh release download "v${VERSION}" \
  --repo public-edge/public-edge-manager \
  --pattern 'release-manifest.json*' \
  --pattern "public-edge-manager-${VERSION}.tgz*"

sha256sum -c release-manifest.json.sha256
sha256sum -c "public-edge-manager-${VERSION}.tgz.sha256"
cosign verify-blob \
  --bundle release-manifest.json.bundle.json \
  --certificate-identity-regexp '^https://github.com/public-edge/public-edge-manager/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  release-manifest.json

# These are the immutable identities that were published by this release.
jq -r '.artifacts[] | [.id, .reference] | @tsv' release-manifest.json
```

Verify an image from the manifest before mirroring or deploying it:

```sh
IMAGE=$(jq -r '.artifacts[] | select(.id == "controller-image") | .reference' release-manifest.json)
cosign verify \
  --certificate-identity-regexp '^https://github.com/public-edge/public-edge-manager/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "${IMAGE}"
```

## How it works

1. Nodes with a global ExternalIP and fresh reachable NetworkPathAssessment are discovered automatically.
2. Programmed Gateway status supplies the canonical VIP; public listeners are verified before an edge is published.
3. Capacity, priority, application latency, and a small node-local preference
   provide deterministic ordering inside an area.
4. Optional provider-neutral `NetworkPathAssessment/v1alpha2` evidence excludes
   candidates whose node, freshness, executed-path or reachability evidence is
   unusable. It is an eligibility gate and never contributes a ranking score.
5. ECS, or resolver CIDR locality when ECS is absent, selects the nearest healthy area consistently on every authority.
6. A generic L4 redirector preserves HTTP/TLS traffic and sends it to the Gateway VIP; Gateway API owns Pod selection and 503 termination.

### Authoritative DNS zones

Set `dns.zones` to every zone delegated to Public Edge Manager. The server then
answers NS and SOA queries at each zone apex, returns NXDOMAIN only for unknown
names inside a configured zone, and includes the zone SOA in NXDOMAIN and NODATA
responses so recursive resolvers can apply negative caching correctly. Queries
outside the configured zones are refused.

```yaml
nameservers: [ns1.edge.example., ns2.edge.example.]
dns:
  zones: [edge.example.]
  soaRname: hostmaster.edge.example.
```

An empty `dns.zones` list retains the legacy service-name authority behavior for
backwards compatibility. New delegated DNS installations should configure zones
explicitly.

Names handled by an external CDN can be declared as static records inside a
delegated zone. They are returned by this authority but are not health-ranked or
retargeted by PublicEdge; changing the CDN target remains a GitOps change.

```yaml
dns:
  zones: [edge.example.]
  externalRecords:
    assets.edge.example.:
      - {type: CNAME, value: customer.cdn.example., externalCDN: true, provider: example-cdn}
```

Public Edge Manager does not configure provider routers, NAT, BGP, certificates
or application routes. It derives `PublicEdge` objects from existing Node,
NetworkPathAssessment and Gateway resources and removes them when evidence
expires. Public IPs and Pod addresses are never configured in chart values. An
optional shared cluster-node ConfigMap supplies membership, capacity and region:

```yaml
discovery:
  clusterInventoryConfigMap:
    namespace: flux-system
    name: cluster-node-inventory
    key: nodes.json
```

The key contains a JSON object keyed by Kubernetes Node name, with
`capacityMbps` and `region` in each value. When configured, unlisted Nodes are
ineligible even if they have a public IP; inventory attributes take precedence
over stale Node annotations and labels. Candidate health still comes from live
path, Gateway and listener evidence. The controller does not read any
vendor-specific network CR for inventory.

### Optional network evidence

PublicEdge can consume a provider-neutral, cluster-scoped assessment API without
depending on an Advanced Fabric namespace, release name, ConfigMap or internal
implementation. The built-in defaults keep this integration disabled:

```yaml
fabricEvidence:
  mode: Optional
  apiGroup: networking.re8ch.com
  apiVersion: v1alpha2
  resource: networkpathassessments
  requireNodeReady: true
  allowedStates: [Ready, Partial]
```

`Shadow` reads and reports evidence without changing eligibility or ranking and
is suitable for an initial observation window. `Optional` preserves probe-only
operation when the provider API or matching
node assessment is absent. When a matching assessment exists, it must be fresh,
report a Ready node and an executed reachable current path, carry
`EvidenceReady=True`, and use an allowed state. `Required` additionally
fails closed when the API or assessment is absent. `Disabled` neither reads the
API nor renders its RBAC permission.

With `requireNodeReady`, the consumer also applies the Kubernetes eligibility
fact used by Advanced Fabric rankings: absent, deleting, and non-`Ready=True`
Nodes are excluded. `Required` fails closed if either Nodes or assessments
cannot be read, preventing a fresh historical NPA from keeping a NotReady edge
eligible.

Production validation of the `Required` contract confirmed that a candidate
becomes eligible only after the producer advances a fresh assessment to
`Partial` or `Ready`; an `Unknown` candidate stays isolated even when its Node
is Ready. Assessment timestamps and `validUntil` must continue advancing across
producer sampling cycles.

Candidates match assessments through `PublicEdge.spec.nodeName` and
`NetworkPathAssessment.spec.subjectRef` with kind `Node`. Assessment scope must
be `pod` or `host-and-pod`. The controller records the evidence disposition in
its API and `PublicEdge` status but never configures the evidence producer or
network dataplane.

### Optional Globalping quality evidence

The chart can run one authenticated observer every five minutes. It measures
three independent layers from configured Globalping regions: PublicEdge public
IPs with ICMP, authoritative DNS addresses with SOA queries, and managed service
hostnames with HTTPS. Results are written to the
`public-edge-globalping-quality` ConfigMap, while candidate summaries are also
merged into `PublicEdge.status.externalQuality`. The evidence is observe-only;
it cannot withdraw an edge or change DNS ranking.

An API token raises provider-side quotas. Keep it in a pre-existing Secret and
reference it from values when available; an empty Secret name uses the public
API quota:

```yaml
globalping:
  enabled: true
  apiTokenSecretRef:
    name: globalping-api
    key: GLOBALPING_API_TOKEN
  intervalSeconds: 300
  locations:
    - {magic: World}
  probeLimit: 10
  httpTargets:
    - hostname: db-rw.example.com
      path: /ready
      acceptedStatuses: [200]
  edgeHttpTargets:
    - hostname: db-rw.example.com
      path: /ready
      acceptedStatuses: [200]
  extraAuthorityAddresses: [192.0.2.53]
```

Empty `dnsTargets` and `httpTargets` inherit `discovery.childZones` and the
configured service hostnames. `probeLimit` is the exact probe count requested
for each measurement (10 by default). `edgeHttpTargets` are sent to every
concrete PublicEdge address while retaining the configured TLS SNI and HTTP
Host; only declared response statuses are healthy. `maxTargetsPerKind` and
`maxMeasurementsPerCycle` bound API usage; the defaults stay within the
authenticated 500-tests/hour allowance at five locations. The token is
read only by the singleton observer and is never copied into a ConfigMap,
PublicEdge status or logs.

## Install

Start from [`examples/values-example.yaml`](examples/values-example.yaml), replace
all documentation addresses and names, then install the OCI chart:

```sh
helm install public-edge-manager \
  oci://ghcr.io/public-edge/charts/public-edge-manager \
  --version 0.8.3 \
  --namespace public-edge-system --create-namespace \
  --values values-production.yaml
```

The chart defaults to `enabled: false`; enabling it requires a service directory
and a startup nameserver fallback. `api.group` is configurable for
organizations that own a Kubernetes API group. Existing installations can keep
`networking.re8ch.com` for API compatibility without using any RE8CH service
domain or infrastructure.

## Exposure and security model

### Cluster-local runtime CLI

Service domains, scoring overrides and routing metadata can be changed at
runtime without editing Helm values. The chart deploys a least-privilege CLI
pod and preserves its runtime ConfigMap across upgrades:

```sh
kubectl -n regional-routing exec deploy/public-edge-manager-cli -- \
  python -m public_edge_manager.edgectl service-register app.example.com \
  --service app --class web --probe-path /healthz --accepted-statuses 200,401
kubectl -n regional-routing exec deploy/public-edge-manager-cli -- \
  python -m public_edge_manager.edgectl model-set capacityWeight 12
kubectl -n regional-routing exec deploy/public-edge-manager-cli -- \
  python -m public_edge_manager.edgectl route-set app '{"via":"canonical-gateway"}'
kubectl -n regional-routing exec deploy/public-edge-manager-cli -- \
  python -m public_edge_manager.edgectl status
```

The discovery Deployment runs at least two replicas and elects one writer with
a Kubernetes Lease. It derives generic ingress and authority labels only after
the relevant path and public listener probes succeed. The authority DaemonSet
uses that generic label and contains no hostname affinity.

For an elected NS set, set `dns.nameserversConfigMap` to a ConfigMap in the
release namespace with a `nameservers.json` key, for example
`{"nameservers":["ns-sh.example.","ns-gz.example."]}`. The manager accepts one
to three unique absolute names and reloads projected updates during its probe
loop. Invalid updates leave the last valid set in service. The configured
`nameservers` value remains the startup fallback when no election ConfigMap is
used. The election controller must update parent delegation and glue as well.

The chart creates two Services:

- `public-edge-manager-dns` carries only authoritative UDP/TCP 53 and may be
  configured as `LoadBalancer`.
- `public-edge-manager` is always `ClusterIP` and carries the health/discovery
  HTTP API on port 8080.

### Authorities outside Kubernetes

`discovery.externalAuthorities` can register an independently operated public
authority, including a RouterOS container or PowerDNS host. Each entry supplies
`id`, global IPv4 `address`, absolute `nameserver` under `parentZone`, `region`,
`capacityMbps`, and `enabled`. The default is an empty list. An enabled entry
joins the existing nameserver election only after independent UDP and TCP SOA
probes pass for every delegated child zone and A probes pass for every
`addressZones` apex. The response must be authoritative and must not advertise
recursion. These entries are never treated as Kubernetes Nodes or PublicEdge
ingress candidates; no Node labels, routing state, or Gateway ownership are
created for them. Keep an external authority disabled until its zone data and
public transport are ready, and remove it from election before retiring its
listener.

The optional `routeros/` ARM32 image runs the same authoritative DNS engine as
the Kubernetes authorities in non-Kubernetes mode. A separate bundle publisher
exports the shared records, runtime changes, nameserver election, public edge
candidates, AdvFab path assessments, node readiness, and client-area policy.
The RouterOS process verifies its HMAC,
stores the last good bundle, probes candidates independently, and answers UDP
and TCP DNS locally. Normal answers do not depend on another DNS authority or
AXFR. Its health endpoint fails when no signed bundle has been refreshed within
the configured age. `routeros/authority.example.json` documents the consumer;
the HMAC key is provisioned separately and never stored in chart values.

Ingress mutation is disabled by default. Enable `publication.enabled` and
`rbac.mutateIngresses` together only for provider publication. Normal delegated
authoritative DNS requires read-only Ingress access.

Publication adapters consume the same sticky selection as authoritative DNS.
For PowerDNS, point a dedicated ExternalDNS RFC2136 instance at PowerDNS and
configure an adapter with `provider: powerdns-rfc2136` whose refs are owned only
by that instance. Public Edge Manager updates the referenced Ingress target;
it never receives the RFC2136 TSIG secret or PowerDNS credentials. A healthy
selection is retained across ranking changes. Failover waits for
`candidateSelection.failoverGraceSeconds`, `minReadySeconds`, and
`minHoldSeconds`, preserving the last-known-good target while a replacement is
qualified.

Cloudflare parent delegation is optional. When configured, the Lease holder
publishes stable hash-based NS names and glue immediately after complete UDP and
TCP probes. A single NS is an allowed degraded state; zero healthy authorities
preserves the last-known-good ConfigMap and parent delegation.

`readinessGates` can fail a sensitive service closed unless JSON authority
evidence in a ConfigMap agrees with the ready addresses of an EndpointSlice.
The mechanism is generic and opt-in; database product names and resource names
remain solely in deployment values.

The controller runs as UID/GID 65532 with a read-only root filesystem, no
privilege escalation, and only `NET_BIND_SERVICE`. Never put credentials or
private topology in chart defaults.

## Artifact verification

Tagged releases publish multi-architecture images and OCI Helm charts to GHCR.
Images include GitHub provenance and SBOM attestations and are signed keylessly
with Sigstore. Pin the resolved image digest in production:

```sh
cosign verify \
  --certificate-identity-regexp '^https://github.com/public-edge/public-edge-manager/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/public-edge/public-edge-manager@sha256:...
```

See [`SECURITY.md`](SECURITY.md) for vulnerability reporting and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for validation requirements.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
