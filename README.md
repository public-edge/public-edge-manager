# Public Edge Manager

Public Edge Manager is an open-source Kubernetes controller and authoritative
DNS service for selecting healthy public ingress edges by locality, capacity,
priority, and observed application latency. It is application- and DNS-provider
agnostic: operators supply their own zones, services, nodes, endpoints, Gateway
VIPs, and publication integration.

The repository is the complete build context. It contains the controller source,
tests, container definition, Helm chart, license, and CI/release workflows. It
does not require code or ConfigMaps from a separate private repository.

## How it works

1. Disabled, draining, and unhealthy edges are excluded.
2. A healthy edge in the authority replica's configured area beats a remote edge.
3. Capacity, priority, application latency, and a small node-local preference
   provide deterministic ordering inside an area.
4. Optional provider-neutral `NetworkPathAssessment/v1alpha2` evidence excludes
   candidates whose node, freshness, executed-path or reachability evidence is
   unusable. It is an eligibility gate and never contributes a ranking score.
5. DNS A or CNAME answers publish the best equally scored candidates.
6. Optional named publication adapters update provider-scoped ExternalDNS-only
   Ingresses with the selected edge.

Public Edge Manager does not configure routers, NAT, BGP, certificates, or
application Gateways. Those remain explicit operator-owned infrastructure.

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

## Install

Start from [`examples/values-example.yaml`](examples/values-example.yaml), replace
all documentation addresses and names, then install the OCI chart:

```sh
helm install public-edge-manager \
  oci://ghcr.io/public-edge/charts/public-edge-manager \
  --version 0.4.6 \
  --namespace public-edge-system --create-namespace \
  --values values-production.yaml
```

The chart defaults to `enabled: false`; enabling it requires at least one
nameserver, authority node, service, and edge. `api.group` is configurable for
organizations that own a Kubernetes API group. Existing installations can keep
`networking.re8ch.com` for API compatibility without using any RE8CH service
domain or infrastructure.

## Exposure and security model

The chart creates two Services:

- `public-edge-manager-dns` carries only authoritative UDP/TCP 53 and may be
  configured as `LoadBalancer`.
- `public-edge-manager` is always `ClusterIP` and carries the health/discovery
  HTTP API on port 8080.

Ingress mutation is disabled by default. Enable `publication.enabled` and
`rbac.mutateIngresses` together only for provider publication. Normal delegated
authoritative DNS requires read-only Ingress access.

### Multiple DNS publication adapters

Public Edge Manager can fan one selected healthy edge out to multiple named,
provider-scoped publication objects. Each adapter owns separate
ExternalDNS-only Ingresses, so credentials, zone filters, ownership registries,
write policies and failure domains remain isolated in the corresponding
ExternalDNS release:

```yaml
publication:
  enabled: true
  publisherNode: edge-controller-1
  adapters:
    alidns:
      provider: alibabacloud
      refs:
        app: {namespace: dns-publication, name: app-alidns}
    dnspod:
      provider: tencent-dnspod
      refs:
        app: {namespace: dns-publication, name: app-dnspod}
    esa:
      enabled: false
      provider: alibaba-esa
      refs:
        app: {namespace: dns-publication, name: app-esa}
rbac:
  mutateIngresses: true
```

The chart deliberately does not mount cloud credentials or call provider APIs.
Alibaba Cloud DNS uses ExternalDNS's `alibabacloud` provider, Tencent DNSPod
uses an ExternalDNS webhook, and ESA requires an ESA-capable ExternalDNS webhook
or controller. Keep an adapter disabled until its executor, credentials and
ownership policy have been validated. The deprecated `publication.refs` map is
still accepted as the single `legacy` adapter.

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
