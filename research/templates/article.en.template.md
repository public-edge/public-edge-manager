# PublicEdge multi-node selection under nine network/resource conditions

## Abstract

This study evaluates PublicEdge's production candidate-scoring function in a deterministic nine-grid experiment: healthy, degraded, and failed external networks crossed with scarce, adequate, and abundant edge resources. The generator executed {{TOTAL_RUNS}} reproducible boundary runs and emitted {{TOTAL_ROWS}} candidate observations. It tests explainability and fail-closed behavior; it is not a benchmark of the Internet or a claim about production traffic.

## Problem definition

An edge publisher must exclude candidates without valid readiness evidence, rank the remainder consistently, and return no new answer when every candidate is ineligible. The useful question is not “which node is globally fastest?” but “given the evidence and resources currently held, which eligible node does the declared policy select, and why?”

## PublicEdge scoring model

The simulator imports the same pure function as the DNS runtime. For eligible candidate *i*:

```text
score(i) = capacityMbps(i) × 10
         + 100000 if area(i) = requestArea
         - priority(i)
         - min(floor(latencyMs(i) / 20), 50)
         + 5 if id(i) = localNode
```

Readiness is a gate, not a score component: an ineligible node never wins, regardless of capacity. Ties use ascending node ID. Production stickiness and failover timers operate after ranking and are intentionally outside this static boundary study.

## Nine-grid experiment design

The resource axis contains one local 100 Mbps node (scarce), two 100/300 Mbps candidates (adequate), and six candidates across three areas covering 100/300/1000 Mbps (abundant). Healthy evidence is valid at 20/50/80 ms. Degraded evidence combines 150/300 ms with stale or timed-out candidates. Failed conditions explicitly fail the local application path; an eligible alternative may be selected, otherwise the model fails closed.

Each cell runs a baseline plus latency 0–1000 ms in 20 ms increments, capacities 50/100/300/1000 Mbps, priorities 0/10/100, and every failure mask permitted by its candidate set. No random number, cluster call, production record, Globalping measurement, or Monte Carlo sample is used.

## Data table

{{SUMMARY_TABLE}}

The full candidate-level table is [`data/raw-results.csv`](data/raw-results.csv); [`data/nine-grid-summary.csv`](data/nine-grid-summary.csv) is derived from it.

## Figures

![Nine-grid availability](charts/nine-grid-availability.svg)

![Eligible nodes and selected capacity](charts/eligibility-capacity.svg)

![Candidate score components](charts/score-components.svg)

![Latency and capacity boundary](charts/latency-capacity-boundary.svg)

![Score margin and switches](charts/margin-switches.svg)

## Interpretation of the nine cells

1. **Healthy / scarce:** the sole node is selected until its failure mask removes eligibility; this isolates fail-closed behavior.
2. **Healthy / adequate:** the same-area bonus normally keeps the local candidate ahead of a larger remote node, making the locality boundary visible.
3. **Healthy / abundant:** multiple eligible candidates improve availability, while deterministic ID ordering removes tie ambiguity.
4. **Degraded / scarce:** the local candidate can still serve when its evidence remains valid, but no stale fallback is invented.
5. **Degraded / adequate:** partial evidence loss reduces the eligible set; capacity never overrides the readiness gate.
6. **Degraded / abundant:** redundant areas preserve choices, and the margin shows how much policy separates first and second place.
7. **Failed / scarce:** explicit local failure leaves no alternative and therefore fails closed.
8. **Failed / adequate:** the remote candidate is explainably selected only because it is eligible; this is policy fallback, not a locality-weight change.
9. **Failed / abundant:** several alternatives remain, so capacity, priority, latency and stable tie-breaking determine the winner.

## Boundary conditions and counterexamples

- A 1000 Mbps ineligible candidate remains excluded.
- Latency penalties saturate at 50 points after 1000 ms; they cannot silently erase the 100000-point area boundary.
- Local-node preference is only five points and cannot compensate for a material capacity difference.
- Equal scores select the lexicographically smaller node ID.
- With zero eligible candidates, selection is empty rather than an unverified “best effort” answer.

These counterexamples are executable assertions in `tests/test_simulation.py`.

## Limitations

The experiment deliberately freezes the current weights, models evidence as ready/ineligible, and does not estimate end-user throughput, congestion, DNS cache behavior, BGP convergence, or real traffic. The enumerated observations are policy-model outputs, not independent Internet measurements. Any model change discovered from this work should be proposed separately rather than hidden in the research branch.

## Reproduction

```sh
python -m pip install --require-hashes -r requirements-research.lock
PYTHONPATH=src python research/generate.py
git diff --exit-code -- research
```

The generator fixes scenario order, CSV formatting, SVG hash salt, metadata, and tie-breaking. A clean rerun must be byte-for-byte identical.

## Conclusion

The study shows that PublicEdge's current policy is deterministic and explainable across the nine declared conditions: evidence controls eligibility, area is the dominant placement boundary, capacity ranks candidates inside that boundary, and lack of a qualified alternative fails closed. It does not establish that these weights are optimal for every deployment; it establishes a reproducible baseline for reviewing them.
