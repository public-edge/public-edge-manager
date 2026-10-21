# Network and resource nine-grid

The model can be explained with two independent axes:

- **External network condition**: healthy, degraded, or failed.
- **Available edge resources**: scarce, adequate, or abundant.

The grid is an operator reasoning aid, not a replacement for live readiness
evidence.

| External network \ Resources | Scarce: one small usable edge | Adequate: two qualified edges | Abundant: several regions and spare capacity |
| --- | --- | --- | --- |
| **Healthy** | Publish the only qualified edge. Alert on the lack of redundancy; do not invent a second candidate. | Prefer the same-Area candidate, then capacity, priority, and latency. Keep the healthy selection sticky. | Keep traffic local to each Area. Select the strongest local edge and retain the others as qualified spares. |
| **Degraded** | Preserve the last verified address only for the bounded grace window when the failure is a transport timeout. Explicit application failure produces no answer if no replacement exists. | Keep the current healthy edge. If it becomes unavailable, require the replacement to satisfy readiness and timing gates; never let hold time extend a failed address beyond grace. | Withdraw only affected candidates. Other local candidates absorb traffic; remote regions remain safety choices rather than winning on raw capacity. |
| **Failed** | Fail closed after grace. Preserve diagnostic status, but do not publish a permanently unreachable edge. | Switch to the healthy replacement when qualified. If all candidates fail, return no service address instead of random or stale routing. | Isolate the failed Area, select healthy edges for other Areas, and use only an explicitly declared global safety entry when policy permits it. |

## What changes the result

Candidate eligibility is evaluated before score:

```text
eligible = edgeReady
        && acceptable network-path evidence
        && acceptable external-quality evidence when mode=Required
```

For an eligible candidate:

```text
score = capacityMbps × capacityWeight
      + localAreaBonus when candidate.area == request.area
      - priority × priorityWeight
      - min(latencyMs / latencyDivisorMs, latencyPenaltyCap)
      + localNodeBonus when candidate is local
```

The large default `localAreaBonus` makes Area a locality boundary. Capacity is
then the dominant signal among healthy candidates in the same Area.

## Reproduce the nine cells as a developer

The repository's unit tests are the smallest deterministic simulator. Run them
without a Kubernetes cluster:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-test.lock
python -m pip install --no-deps --no-build-isolation .
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m pytest -q
```

The current suite covers the important transitions represented by the grid:

- high-capacity selection inside one Area;
- local relay preference over a higher-capacity remote origin;
- no migration when only ranking changes;
- qualified failover after grace and readiness windows;
- bounded retention for probe transport failure;
- immediate withdrawal or replacement on explicit application failure;
- recovery after all candidates temporarily disappear.

To explore one cell, copy an existing `test_authority.py` selection test and
change only these inputs:

```python
candidate = {
    "id": "edge-a",
    "area": "CN",
    "region": "cn-east",
    "capacityMbps": 100,
    "priority": 0,
    "edgeReady": True,
    "serviceBackendReady": True,
    "latencyMs": 25,
}
```

Model external conditions with observable facts rather than a subjective
`networkQuality` number:

| Condition | Simulation facts |
| --- | --- |
| Healthy | Fresh probe, accepted HTTP status, fresh required evidence |
| Degraded | Timeout, stale evidence, elevated latency, or one transport unavailable |
| Failed | Explicit rejected HTTP status, Gateway not Programmed, listener failure, or required evidence rejection |

Model resources with candidate count, `capacityMbps`, Area distribution, and
whether a qualified spare exists. Assertions should check the selected edge,
DNS answer retention or withdrawal, and the exact time at which a replacement
may take over.

## Production-safe evaluation

Keep Globalping in `Shadow` while evaluating external measurements. Compare its
`wouldReject` result with the active selection; change to `Required` only after
representative observations show that the evidence is timely and stable.

Do not run destructive network fault injection against production. Use unit
fixtures or an isolated Kind cluster, and keep public addresses, credentials,
and raw production telemetry out of Git.
