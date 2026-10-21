# `edgectl` operator guide

`edgectl` is the supported cluster-local CLI. The Helm release creates a
least-privilege CLI Deployment named `<release>-cli`; it can edit only the
runtime ConfigMap and read the status resources needed for inspection.

The examples below assume:

```sh
NAMESPACE=public-edge-system
RELEASE=public-edge-manager
EDGECLI="kubectl -n ${NAMESPACE} exec deploy/${RELEASE}-cli -- edgectl"
```

If your shell does not expand a command stored in a variable as expected, use
the complete `kubectl exec ... -- edgectl` form shown in the README.

## Inspect state

```sh
$EDGECLI status
```

The JSON response contains:

- `runtime.services`: service registrations used by the authority;
- `runtime.model`: runtime scoring overrides;
- `runtime.routes`: service routing metadata;
- `edges`: derived `PublicEdge` specifications and status;
- `gateways`: labelled public-edge Gateways and their current conditions.

Run this before and after every mutation. A successful ConfigMap write does not
by itself prove that a service is externally reachable.

## Register and remove a service

```sh
$EDGECLI service-register app.example.com \
  --service app \
  --class web \
  --probe-path /healthz \
  --accepted-statuses 200,401
```

The domain is normalized to lower case with a trailing dot. `--class` defaults
to `web`, `--probe-path` defaults to `/`, and an omitted
`--accepted-statuses` uses the controller's default success policy.

Remove the runtime registration:

```sh
$EDGECLI service-delete app.example.com
```

Runtime service changes are operational overrides. Put durable registrations
in reviewed Helm/GitOps values.

## Override scoring parameters

```sh
$EDGECLI model-set capacityWeight 12
$EDGECLI model-set latencyPenaltyCap 40
$EDGECLI model-unset latencyPenaltyCap
```

The value is parsed as JSON when possible, so `12`, `true`, arrays, and objects
are stored as their JSON types. The current authority reads these runtime keys:

| Key | Default | Effect |
| --- | ---: | --- |
| `capacityWeight` | 10 | Score added per declared Mbps |
| `localAreaBonus` | 100000 | Hard preference for a candidate in the request Area |
| `priorityWeight` | 1 | Penalty multiplier; a lower candidate priority is better |
| `latencyDivisorMs` | 20 | Milliseconds represented by one latency penalty point |
| `latencyPenaltyCap` | 50 | Maximum latency penalty |
| `localNodeBonus` | 5 | Small bonus when the candidate is the authority's Node |

Other candidate-selection settings, including `failoverGraceSeconds`,
`minReadySeconds`, `minHoldSeconds`, quality mode, and resolver CIDRs, are Helm
configuration and are not runtime `model-set` overrides.

Use GitOps for lasting model changes. Treat `model-set` as a bounded experiment,
record the old value, inspect status, and use `model-unset` to return to the
reviewed default.

## Set routing metadata

Each path argument is parsed as JSON when valid:

```sh
$EDGECLI route-set app \
  '{"via":"canonical-gateway"}' \
  '{"via":"regional-relay","area":"US"}'

$EDGECLI route-delete app
```

Routing metadata describes service paths; it does not configure a router,
Gateway, NAT, or BGP session.

## Developer-local CLI check

The CLI help path does not require cluster credentials:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
edgectl --help
edgectl service-register --help
```

Commands that read or mutate state must run inside the CLI pod because they use
its Kubernetes ServiceAccount token and CA bundle.

## Safe operator walkthrough

1. Run `status` and save the current `runtime` object.
2. Make one service, model, or route change.
3. Run `status` again and confirm the intended runtime delta.
4. Check `PublicEdge` readiness and query authoritative DNS over UDP and TCP.
5. Undo an unsuccessful experiment with the matching `delete` or `unset`
   command.
6. Reconcile successful long-lived changes into GitOps values.
