#!/usr/bin/env python3
"""Generate the deterministic PublicEdge nine-grid dataset, tables and SVGs."""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from statistics import fmean

from public_edge_manager.selection import ScoreModel, score_candidate, stable_rank

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"
DATA = RESEARCH / "data"
CHARTS = RESEARCH / "charts"
NETWORKS = ("healthy", "degraded", "failed")
RESOURCES = ("scarce", "adequate", "abundant")
MODEL = ScoreModel()
RAW_FIELDS = [
    "scenario", "network", "resources", "timeStep", "scan", "scanValue",
    "failureMask", "candidateId", "area", "capacityMbps", "priority",
    "latencyMs", "readiness", "failureReason", "capacityScore",
    "localityScore", "priorityScore", "latencyScore", "localNodeScore",
    "totalScore", "rank", "selected", "qualifiedCount", "sameAreaHit",
    "scoreMargin", "failClosed", "selectionSwitched",
]


def baseline_state(network: str, candidates: list[dict], latencies: list[int]) -> list[dict]:
    result = deepcopy(candidates)
    for index, candidate in enumerate(result):
        candidate["latencyMs"] = latencies[index % len(latencies)]
        candidate["ready"] = True
        candidate["failureReason"] = ""
        if network == "degraded" and index % 2 == 1:
            candidate["ready"] = False
            candidate["failureReason"] = "stale-or-timeout"
        if network == "failed" and index == 0:
            candidate["ready"] = False
            candidate["failureReason"] = "explicit-application-failure"
    return result


def scan_states(config: dict, network: str, resource: str):
    candidates = config["resources"][resource]
    base = baseline_state(network, candidates, config["network"][network]["latenciesMs"])
    yield "baseline", "baseline", 0, base
    latency = config["scan"]["latencyMs"]
    for value in range(latency["start"], latency["stop"] + 1, latency["step"]):
        state = deepcopy(base)
        state[0]["latencyMs"] = value
        yield "latency", value, 0, state
    for value in config["scan"]["capacityMbps"]:
        state = deepcopy(base)
        state[0]["capacityMbps"] = value
        yield "capacity", value, 0, state
    for value in config["scan"]["priority"]:
        state = deepcopy(base)
        state[0]["priority"] = value
        yield "priority", value, 0, state
    for mask in range(1 << len(base)):
        state = deepcopy(base)
        for index, candidate in enumerate(state):
            if mask & (1 << index):
                candidate["ready"] = False
                candidate["failureReason"] = "failure-mask"
        yield "failure-mask", mask, mask, state


def evaluate(config: dict) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    runs: list[dict] = []
    step = 0
    previous: dict[str, str] = {}
    for network in NETWORKS:
        for resource in RESOURCES:
            scenario = f"{network}-{resource}"
            for scan, value, mask, candidates in scan_states(config, network, resource):
                step += 1
                scored = []
                for candidate in candidates:
                    if not candidate["ready"]:
                        continue
                    breakdown = score_candidate(
                        capacity_mbps=candidate["capacityMbps"],
                        candidate_area=candidate["area"],
                        request_area=config["requestArea"],
                        priority=candidate["priority"],
                        latency_ms=candidate["latencyMs"],
                        candidate_id=candidate["id"],
                        local_node_id=config["localNode"],
                        model=MODEL,
                    )
                    scored.append({**candidate, "breakdown": breakdown, "score": breakdown.total})
                ranked = stable_rank(scored)
                selected = ranked[0]["id"] if ranked else ""
                margin = ranked[0]["score"] - ranked[1]["score"] if len(ranked) > 1 else None
                switched = bool(previous.get(scenario) and selected != previous[scenario])
                previous[scenario] = selected
                ranks = {item["id"]: index + 1 for index, item in enumerate(ranked)}
                by_id = {item["id"]: item for item in ranked}
                same_area = bool(selected and by_id[selected]["area"] == config["requestArea"])
                runs.append({
                    "scenario": scenario, "network": network, "resources": resource,
                    "timeStep": step, "scan": scan, "scanValue": value,
                    "failureMask": mask, "selected": selected,
                    "selectedCapacityMbps": by_id[selected]["capacityMbps"] if selected else 0,
                    "qualifiedCount": len(ranked), "sameAreaHit": same_area,
                    "scoreMargin": margin, "failClosed": not bool(selected),
                    "selectionSwitched": switched,
                })
                for candidate in candidates:
                    eligible = by_id.get(candidate["id"])
                    breakdown = eligible["breakdown"] if eligible else None
                    rows.append({
                        "scenario": scenario, "network": network, "resources": resource,
                        "timeStep": step, "scan": scan, "scanValue": value,
                        "failureMask": mask, "candidateId": candidate["id"],
                        "area": candidate["area"], "capacityMbps": candidate["capacityMbps"],
                        "priority": candidate["priority"], "latencyMs": candidate["latencyMs"],
                        "readiness": "ready" if candidate["ready"] else "ineligible",
                        "failureReason": candidate["failureReason"],
                        "capacityScore": breakdown.capacity if breakdown else 0,
                        "localityScore": breakdown.locality if breakdown else 0,
                        "priorityScore": breakdown.priority if breakdown else 0,
                        "latencyScore": breakdown.latency if breakdown else 0,
                        "localNodeScore": breakdown.local_node if breakdown else 0,
                        "totalScore": breakdown.total if breakdown else 0,
                        "rank": ranks.get(candidate["id"], ""), "selected": candidate["id"] == selected,
                        "qualifiedCount": len(ranked), "sameAreaHit": same_area,
                        "scoreMargin": "" if margin is None else margin,
                        "failClosed": not bool(selected), "selectionSwitched": switched,
                    })
    return rows, runs


def summarize(runs: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        groups[run["scenario"]].append(run)
    summary = []
    for network in NETWORKS:
        for resource in RESOURCES:
            scenario = f"{network}-{resource}"
            items = groups[scenario]
            selected = [item for item in items if item["selected"]]
            margins = [item["scoreMargin"] for item in items if item["scoreMargin"] is not None]
            summary.append({
                "scenario": scenario, "network": network, "resources": resource,
                "runs": len(items),
                "availabilityRate": f"{len(selected) / len(items):.4f}",
                "failClosedRuns": len(items) - len(selected),
                "meanQualifiedNodes": f"{fmean(item['qualifiedCount'] for item in items):.3f}",
                "meanSelectedCapacityMbps": f"{fmean(item['selectedCapacityMbps'] for item in items):.3f}",
                "sameAreaHitRate": f"{sum(item['sameAreaHit'] for item in selected) / max(1, len(selected)):.4f}",
                "meanScoreMargin": f"{fmean(margins) if margins else 0:.3f}",
                "selectionSwitches": sum(item["selectionSwitched"] for item in items),
            })
    return summary


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(summary: list[dict]) -> str:
    header = "| Network | Resources | Runs | Available | Fail closed | Mean eligible | Mean selected Mbps | Same-area | Mean margin | Switches |\n"
    rule = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    body = "".join(
        f"| {row['network']} | {row['resources']} | {row['runs']} | {float(row['availabilityRate']):.1%} | "
        f"{row['failClosedRuns']} | {row['meanQualifiedNodes']} | {row['meanSelectedCapacityMbps']} | "
        f"{float(row['sameAreaHitRate']):.1%} | {row['meanScoreMargin']} | {row['selectionSwitches']} |\n"
        for row in summary
    )
    return header + rule + body


def charts(summary: list[dict], rows: list[dict], runs: list[dict]) -> None:
    import matplotlib
    matplotlib.use("svg")
    matplotlib.rcParams.update({"svg.hashsalt": "public-edge-v1", "font.family": "DejaVu Sans"})
    import matplotlib.pyplot as plt
    import numpy as np

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(CHARTS / name, format="svg", metadata={"Date": None, "Creator": "PublicEdge deterministic simulator"})
        plt.close(fig)

    by_scenario = {row["scenario"]: row for row in summary}
    availability = np.array([[float(by_scenario[f"{n}-{r}"]["availabilityRate"]) for r in RESOURCES] for n in NETWORKS])
    fig, ax = plt.subplots(figsize=(8, 4.8)); image = ax.imshow(availability, vmin=0, vmax=1, cmap="RdYlGn")
    for y, network in enumerate(NETWORKS):
        for x, resource in enumerate(RESOURCES):
            row = by_scenario[f"{network}-{resource}"]
            ax.text(x, y, f"{availability[y, x]:.1%}\nFC {row['failClosedRuns']}", ha="center", va="center")
    ax.set_xticks(range(3), RESOURCES); ax.set_yticks(range(3), NETWORKS)
    ax.set_title("Nine-grid availability and fail-closed runs"); fig.colorbar(image, ax=ax, label="selection availability")
    save(fig, "nine-grid-availability.svg")

    labels = [row["scenario"] for row in summary]; x = np.arange(len(labels))
    fig, left = plt.subplots(figsize=(11, 5)); right = left.twinx()
    left.bar(x - .2, [float(row["meanQualifiedNodes"]) for row in summary], .4, label="eligible nodes")
    right.bar(x + .2, [float(row["meanSelectedCapacityMbps"]) for row in summary], .4, color="#d97706", label="selected Mbps")
    left.set_xticks(x, labels, rotation=35, ha="right"); left.set_ylabel("mean eligible nodes"); right.set_ylabel("mean selected capacity (Mbps)")
    left.set_title("Eligibility and selected capacity"); left.legend(loc="upper left"); right.legend(loc="upper right")
    save(fig, "eligibility-capacity.svg")

    baseline = [row for row in rows if row["scan"] == "baseline" and row["readiness"] == "ready"]
    keys = ["capacityScore", "localityScore", "priorityScore", "latencyScore", "localNodeScore"]
    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#0891b2"]
    labels2 = [f"{row['scenario']}:{row['candidateId']}" for row in baseline]
    fig, ax = plt.subplots(figsize=(12, 6)); pos = np.zeros(len(baseline)); neg = np.zeros(len(baseline))
    for key, color in zip(keys, colors):
        vals = np.array([int(row[key]) for row in baseline]); bottoms = np.where(vals >= 0, pos, neg)
        ax.bar(np.arange(len(vals)), vals, bottom=bottoms, label=key, color=color)
        pos += np.where(vals >= 0, vals, 0); neg += np.where(vals < 0, vals, 0)
    ax.set_xticks(range(len(labels2)), labels2, rotation=55, ha="right", fontsize=7); ax.set_ylabel("score points")
    ax.set_title("Baseline score components for every eligible candidate"); ax.legend(ncol=3)
    save(fig, "score-components.svg")

    boundary = [run for run in runs if run["scenario"] == "healthy-adequate" and run["scan"] in {"latency", "capacity"}]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for scan, marker in (("latency", "o"), ("capacity", "s")):
        items = [run for run in boundary if run["scan"] == scan]
        ax.scatter([int(run["scanValue"]) for run in items], [run["selectedCapacityMbps"] for run in items], label=scan, marker=marker, alpha=.75)
    ax.set_xscale("symlog", linthresh=20); ax.set_xlabel("scanned latency (ms) or capacity (Mbps)"); ax.set_ylabel("selected capacity (Mbps)")
    ax.set_title("Healthy/adequate latency–capacity selection boundary"); ax.legend()
    save(fig, "latency-capacity-boundary.svg")

    fig, left = plt.subplots(figsize=(11, 5)); right = left.twinx()
    left.plot(x, [float(row["meanScoreMargin"]) for row in summary], "o-", label="mean margin")
    right.bar(x, [row["selectionSwitches"] for row in summary], alpha=.35, color="#dc2626", label="switches")
    left.set_xticks(x, labels, rotation=35, ha="right"); left.set_ylabel("mean score margin"); right.set_ylabel("selection switches")
    left.set_title("Selection confidence and switching"); left.legend(loc="upper left"); right.legend(loc="upper right")
    save(fig, "margin-switches.svg")


def render_articles(config: dict, table: str, summary: list[dict]) -> None:
    facts = {
        "TOTAL_RUNS": str(sum(int(row["runs"]) for row in summary)),
        "TOTAL_ROWS": str(sum(1 for _ in (DATA / "raw-results.csv").open(encoding="utf-8")) - 1),
        "SUMMARY_TABLE": table.rstrip(),
    }
    for template_name, output_name in (("article.en.template.md", "MULTI_NODE_SIMULATION.md"),
                                       ("article.zh-CN.template.md", "MULTI_NODE_SIMULATION.zh-CN.md")):
        text = (RESEARCH / "templates" / template_name).read_text(encoding="utf-8")
        for key, value in facts.items():
            text = text.replace("{{" + key + "}}", value)
        (RESEARCH / output_name).write_text(text.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True); CHARTS.mkdir(parents=True, exist_ok=True)
    config = json.loads((RESEARCH / "scenarios.json").read_text(encoding="utf-8"))
    rows, runs = evaluate(config); summary = summarize(runs)
    write_csv(DATA / "raw-results.csv", RAW_FIELDS, rows)
    summary_fields = list(summary[0]); write_csv(DATA / "nine-grid-summary.csv", summary_fields, summary)
    table = markdown_table(summary); (DATA / "nine-grid-summary.md").write_text(table, encoding="utf-8")
    charts(summary, rows, runs); render_articles(config, table, summary)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
