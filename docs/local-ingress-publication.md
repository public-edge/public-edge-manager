# Local ingress capability publication

Each cluster runs its own PublicEdge discovery controller. A controller may
project its eligible **local** ingress endpoints into an explicitly configured
existing Service's `peering.re8ch.com/public-ingress` annotation. AmongCluster
still owns the signed ServiceAdvertisement and ImportedService lifecycle.
Remote candidates are never republished as local Nodes or local capabilities.

```yaml
federation:
  clusterRef: example-main
  sourceServices:
    - namespace: apps
      name: backend
      bindings:
        - id: tools
          service: tools
          hostname: tools.example.com
          path: /ready
          port: 443
```

The binding's `service` selects the existing PublicEdge per-service probe result.
The hostname/path/port describe the public capability consumers must verify.
Configure the Service's ordinary Among export policy, class and target peers
through its existing reviewed desired state. This feature does not create a
Service, enable its export, grant a peer access, install a route or change DNS
delegation. Generated Roles grant get/patch only for the named local Services.

The controller exports all eligible local nodes, using stable binding/edge IDs.
Revision hashes include owner cluster, actual Service UID, edge UID, edge
specification and endpoint contract. IP changes and Service recreation update
the revision automatically; ordinary successful probes do not. Candidates with
foreign ownership, old generations, stale/failed backend probes, missing Node
identity or failed transport readiness are omitted. No eligible endpoints means
the annotation is removed, withdrawing public ingress without revoking the
ordinary Service transport.

Every successful projection renews these source observations:

- `networking.re8ch.com/public-ingress-owner`: owner cluster reference;
- `networking.re8ch.com/public-ingress-source-uid`: current Service UID;
- `networking.re8ch.com/public-ingress-valid-until`: RFC3339 source lease.

Validity derives from the existing controller and probe budgets:
`max(90s, 4 * discovery interval + max(30s, connect + response + 2 * probe interval))`.
The Among agent must validate these managed-source annotations before renewing
public ingress. It must not copy the heartbeat into advertisement spec, which
would invalidate consumer generations every control cycle. Deploy that agent
support before enabling sourceServices. Existing manually reviewed, unowned
public-ingress annotations remain a separate compatibility input.

Each projection uses the current Service UID/resourceVersion as write
preconditions. A failed projection never renews its source lease or stops DNS
reconciliation or unrelated Service withdrawals. The agent rejects expired or
mismatched managed-source observations even if it continues publishing ordinary
service heartbeats. Consumers still require current imported endpoint and AF
path evidence; publication alone does not prove business functionality.

Runtime acceptance must change a real source address and Service revision,
observe automatic advertisement/import/AF/DNS convergence, withdraw an endpoint,
and stop source renewals while leaving the agent connected. Verify removal after
source expiry and verify real authenticated application calls separately.
