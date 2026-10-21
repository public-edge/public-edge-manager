"""Pure, deterministic candidate scoring shared by runtime and research tools."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScoreModel:
    capacity_weight: int = 10
    local_area_bonus: int = 100_000
    priority_weight: int = 1
    latency_divisor_ms: int = 20
    latency_penalty_cap: int = 50
    local_node_bonus: int = 5


@dataclass(frozen=True)
class ScoreBreakdown:
    capacity: int
    locality: int
    priority: int
    latency: int
    local_node: int
    total: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def score_candidate(
    *,
    capacity_mbps: int,
    candidate_area: str,
    request_area: str,
    priority: int,
    latency_ms: int,
    candidate_id: str,
    local_node_id: str,
    model: ScoreModel = ScoreModel(),
) -> ScoreBreakdown:
    """Return every additive score component without reading runtime state."""

    capacity = int(capacity_mbps) * max(0, int(model.capacity_weight))
    locality = max(0, int(model.local_area_bonus)) if candidate_area == request_area else 0
    priority_component = -int(priority) * max(0, int(model.priority_weight))
    divisor = max(1, int(model.latency_divisor_ms))
    latency_component = -min(max(0, int(latency_ms)) // divisor,
                             max(0, int(model.latency_penalty_cap)))
    local_node = max(0, int(model.local_node_bonus)) if candidate_id == local_node_id else 0
    total = capacity + locality + priority_component + latency_component + local_node
    return ScoreBreakdown(capacity, locality, priority_component,
                          latency_component, local_node, total)


def stable_rank(items: list[dict]) -> list[dict]:
    """Rank eligible candidates by descending score, then stable node ID."""

    return sorted(items, key=lambda item: (-int(item["score"]), str(item["id"])))
