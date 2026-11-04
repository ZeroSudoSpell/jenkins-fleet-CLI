"""Turn hardware facts and observed load into executor-count recommendations.

Two questions, answered separately and then combined:

1. **Capacity**: how many concurrent builds can this box sustain?
   The minimum of a CPU cap, a memory cap and (optionally) a disk cap,
   using a per-executor resource profile. The CPU cap uses *effective*
   cores: cores x multi-thread scaling efficiency, so an oversubscribed VM
   that reports 8 vCPUs but only scales like 4 is treated as 4.

2. **Demand**: is the node actually short of executors?
   From Jenkins' load statistics: p95 utilization and queue length.

| situation                                    | verdict      | recommendation |
|----------------------------------------------|--------------|----------------|
| current executors > capacity                 | overcommitted| capacity       |
| saturated or queued, and capacity > current  | scale-up     | capacity       |
| saturated or queued, already at capacity     | at-capacity  | current (add agents) |
| p95 utilization <= idle threshold            | underused    | current        |
| otherwise                                    | ok           | current        |
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .benchmark import BenchmarkResult
from .inventory import Node
from .load import LoadSummary


@dataclass
class Profile:
    """Resources one concurrent build is expected to need."""

    cpus_per_executor: float = 1.0
    mem_per_executor_mb: int = 2048
    reserve_mem_mb: int = 1024  # OS + agent JVM
    disk_per_executor_mb: int = 0  # 0 disables the disk cap
    saturation: float = 0.85  # p95 utilization at/above this = saturated
    idle: float = 0.25  # p95 utilization at/below this = underused
    queue_threshold: float = 0.5  # mean queue length that signals unmet demand
    headroom: float = 1.25
    scaling_noise: float = 0.9  # multi-thread scaling at/above this counts as full cores
    min_samples: int = 30  # fewer load samples than this lowers confidence


@dataclass
class Recommendation:
    node: str
    current: int
    recommended: int
    capacity: int | None
    verdict: str
    limited_by: str | None
    confidence: str
    cores: int | None = None
    effective_cores: float | None = None
    memory_mb: int | None = None
    p95_utilization: float | None = None
    mean_queue: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def delta(self) -> int:
        return self.recommended - self.current

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "delta": self.delta}


def compute_capacity(
    profile: Profile,
    cores: int | None,
    scaling: float | None,
    memory_mb: int | None,
    disk_free_mb: int | None,
) -> tuple[int | None, str | None, float | None, list[str]]:
    caps: dict[str, int] = {}
    reasons: list[str] = []
    effective: float | None = None

    if cores:
        # A few percent below perfect scaling is measurement noise (or builds
        # running during the probe); only discount cores when it's clearly low.
        degraded = scaling is not None and scaling < profile.scaling_noise
        eff_scale = max(0.25, scaling) if degraded else 1.0  # type: ignore[type-var]
        effective = round(cores * eff_scale, 2)
        caps["cpu"] = math.floor(effective / profile.cpus_per_executor + 1e-9)
        msg = f"cpu: {cores} cores"
        if degraded:
            msg += f", but multi-thread scaling is only {scaling:.0%} -> ~{effective:g} effective"
        reasons.append(f"{msg} / {profile.cpus_per_executor:g} per executor = {caps['cpu']}")

    if memory_mb:
        usable = memory_mb - profile.reserve_mem_mb
        caps["memory"] = max(0, math.floor(usable / profile.mem_per_executor_mb))
        reasons.append(
            f"memory: ({memory_mb} - {profile.reserve_mem_mb} reserved) MiB"
            f" / {profile.mem_per_executor_mb} per executor = {caps['memory']}"
        )

    if profile.disk_per_executor_mb > 0 and disk_free_mb is not None:
        caps["disk"] = math.floor(disk_free_mb / profile.disk_per_executor_mb)
        reasons.append(
            f"disk: {disk_free_mb} MiB free / {profile.disk_per_executor_mb} per executor"
            f" = {caps['disk']}"
        )

    if not caps:
        return None, None, effective, reasons
    limiting = min(caps, key=lambda k: caps[k])
    if caps[limiting] < 1:
        reasons.append(
            f"{limiting} allows less than one build at this profile; using the minimum of 1"
            " (the profile may be too large for this agent)"
        )
    return max(1, caps[limiting]), limiting, effective, reasons


def recommend_node(
    node: Node,
    bench: BenchmarkResult | None,
    load: LoadSummary | None,
    profile: Profile,
    label_loads: list[LoadSummary] | None = None,
) -> Recommendation:
    """``label_loads`` are the load summaries of the labels this node carries.

    Jenkins only counts a queued build against a node's own queue when it can
    run nowhere else, so builds waiting for a shared label (``linux``) show up
    only in that label's queue. Those queues count as demand on the node too.
    """
    bench = bench if bench and bench.ok else None
    cores = bench.cores if bench else None
    scaling = bench.cpu_scaling if bench else None
    memory = (bench.total_memory_mb if bench else None) or node.total_memory_mb
    disk = (bench.disk_free_mb if bench else None) or node.disk_free_mb

    capacity, limiting, effective, reasons = compute_capacity(profile, cores, scaling, memory, disk)
    current = node.num_executors

    if bench is None:
        reasons.append("no benchmark data: core count unknown, capacity is memory-only")

    saturated = underused = False
    queue = load.mean_queue if load and load.samples else 0.0
    busiest_label = max(
        (ll for ll in label_loads or [] if ll.samples), key=lambda ll: ll.mean_queue, default=None
    )
    if busiest_label and busiest_label.mean_queue > queue:
        queue = busiest_label.mean_queue
    if load and load.samples:
        saturated = load.p95_utilization >= profile.saturation or queue >= profile.queue_threshold
        underused = load.p95_utilization <= profile.idle and queue < profile.queue_threshold
        reasons.append(
            f"load ({load.window} window, {load.samples} samples): p95 utilization"
            f" {load.p95_utilization:.0%}, p95 busy {load.p95_busy:g},"
            f" mean queue {load.mean_queue:g}"
        )
        if busiest_label and busiest_label.mean_queue >= profile.queue_threshold:
            reasons.append(
                f"{busiest_label.scope} has a mean queue of {busiest_label.mean_queue:g}:"
                " builds are waiting for nodes like this one"
            )
        if load.samples < profile.min_samples:
            reasons.append(
                f"only {load.samples} load samples: Jenkins' moving averages start at zero and"
                " need time to converge, so utilization is understated (try again later)"
            )
    else:
        reasons.append("no load statistics available")

    if capacity is None:
        verdict, recommended = "unknown", current
    elif current > capacity:
        verdict, recommended = "overcommitted", capacity
        reasons.append(
            f"{current} executors exceeds {limiting} capacity of {capacity}: builds will contend"
        )
    elif saturated and capacity > current:
        verdict, recommended = "scale-up", capacity
        reasons.append(f"demand is unmet and {limiting} capacity allows {capacity}")
    elif saturated:
        verdict, recommended = "at-capacity", current
        reasons.append("demand is unmet but the node is at capacity: add agents instead")
    elif underused and current > 1:
        verdict, recommended = "underused", current
        need = max(1, math.ceil(load.p95_busy * profile.headroom)) if load else 1
        reasons.append(f"p95 demand fits in ~{need} executor(s); consider consolidating agents")
    else:
        verdict, recommended = "ok", current

    has_load = bool(load and load.samples)
    confidence = "high" if bench and has_load else "medium" if bench or has_load else "low"
    few_samples = has_load and load.samples < profile.min_samples  # type: ignore[union-attr]
    if confidence == "high" and (few_samples or (bench and bench.busy_executors_during_run)):
        confidence = "medium"

    return Recommendation(
        node=node.name,
        current=current,
        recommended=recommended,
        capacity=capacity,
        verdict=verdict,
        limited_by=limiting,
        confidence=confidence,
        cores=cores,
        effective_cores=effective,
        memory_mb=memory,
        p95_utilization=load.p95_utilization if has_load else None,
        mean_queue=round(queue, 2) if has_load else None,
        reasons=reasons,
    )


@dataclass
class FleetSummary:
    nodes: int
    current_executors: int
    recommended_executors: int
    capacity_executors: int
    p95_busy: float | None
    demand_executors: int | None
    mean_queue: float | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_fleet(
    recs: list[Recommendation], overall: LoadSummary | None, profile: Profile
) -> FleetSummary:
    current = sum(r.current for r in recs)
    recommended = sum(r.recommended for r in recs)
    capacity = sum(r.capacity if r.capacity is not None else r.current for r in recs)
    notes: list[str] = []
    demand = None
    if overall and overall.samples:
        demand = math.ceil(overall.p95_busy * profile.headroom)
        if overall.p95_queue > 0:
            notes.append(
                f"builds queued at p95 (queue {overall.p95_queue:g}); real demand exceeds"
                " what busy executors show"
            )
        if demand > capacity:
            notes.append(
                f"p95 demand x{profile.headroom:g} headroom = {demand} executors exceeds"
                f" fleet capacity of {capacity}: add agents"
            )
        elif demand < current * profile.idle:
            notes.append(
                f"p95 demand x{profile.headroom:g} headroom = {demand} of {current}"
                " executors: the fleet is over-provisioned"
            )
    over = [r.node for r in recs if r.verdict == "overcommitted"]
    if over:
        notes.append("overcommitted: " + ", ".join(over))
    return FleetSummary(
        nodes=len(recs),
        current_executors=current,
        recommended_executors=recommended,
        capacity_executors=capacity,
        p95_busy=overall.p95_busy if overall and overall.samples else None,
        demand_executors=demand,
        mean_queue=overall.mean_queue if overall and overall.samples else None,
        notes=notes,
    )
