"""Benchmark agents by running a self-contained Groovy probe on each one.

The probe is sent to ``/computer/<name>/scriptText``, so it runs inside the
agent's JVM. That needs an account with Overall/Administer, but no plugins,
jobs or agent-side setup.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from importlib import resources
from typing import Any

from .client import JenkinsClient, JenkinsError
from .inventory import Node

RESULT_MARKER = "JFLEET_RESULT "


@dataclass
class BenchmarkResult:
    node: str
    ok: bool
    error: str | None = None
    cores: int | None = None
    os: str | None = None
    arch: str | None = None
    java: str | None = None
    jvm_max_heap_mb: int | None = None
    total_memory_mb: int | None = None
    free_memory_mb: int | None = None
    load_average: float | None = None
    cpu_single_mbps: float | None = None
    cpu_multi_mbps: float | None = None
    cpu_scaling: float | None = None
    disk_path: str | None = None
    disk_free_mb: int | None = None
    disk_write_mbps: float | None = None
    small_files_per_sec: int | None = None
    busy_executors_during_run: int = 0
    elapsed_sec: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def groovy_string(value: str | None) -> str:
    """Render a Python string as a Groovy single-quoted (non-interpolated) literal."""
    if value is None:
        return "null"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
    return f"'{escaped}'"


def build_script(
    cpu_seconds: float = 3.0,
    disk_mb: int = 256,
    small_files: int = 2000,
    disk_path: str | None = None,
) -> str:
    template = resources.files("jenkins_fleet").joinpath("benchmark.groovy").read_text("utf-8")
    return (
        template.replace("@CPU_SECONDS@", repr(float(cpu_seconds)))
        .replace("@DISK_MB@", str(int(disk_mb)))
        .replace("@SMALL_FILES@", str(int(small_files)))
        .replace("@DISK_PATH@", groovy_string(disk_path))
    )


def parse_output(node: str, output: str) -> BenchmarkResult:
    for line in output.splitlines():
        if line.startswith(RESULT_MARKER):
            data = json.loads(line[len(RESULT_MARKER) :])
            warnings = [str(e) for e in data.pop("errors", []) or []]
            result = BenchmarkResult.from_dict({"node": node, "ok": True, **data})
            result.warnings.extend(warnings)
            return result
    snippet = output.strip().splitlines()[-5:] if output.strip() else ["<empty response>"]
    return BenchmarkResult(node=node, ok=False, error="probe failed: " + " | ".join(snippet))


def benchmark_node(client: JenkinsClient, node: Node, script: str) -> BenchmarkResult:
    started = time.monotonic()
    try:
        output = client.run_script(script, node.computer_path)
        result = parse_output(node.name, output)
    except JenkinsError as exc:
        result = BenchmarkResult(node=node.name, ok=False, error=str(exc))
    result.elapsed_sec = round(time.monotonic() - started, 1)
    result.busy_executors_during_run = node.busy_executors
    if node.busy_executors:
        result.warnings.append(
            f"{node.busy_executors} build(s) were running; scores are likely understated"
        )
    return result


def run_benchmarks(
    client: JenkinsClient,
    nodes: list[Node],
    script: str | Callable[[Node], str],
    parallel: int = 1,
    on_result: Callable[[BenchmarkResult], None] | None = None,
) -> list[BenchmarkResult]:
    """Benchmark nodes, ``parallel`` at a time (default: one by one, so
    agents that share a hypervisor don't skew each other's numbers).

    ``script`` is the probe, or a function that builds it for each node."""
    results: list[BenchmarkResult] = []

    def task(node: Node) -> BenchmarkResult:
        res = benchmark_node(client, node, script(node) if callable(script) else script)
        if on_result:
            on_result(res)
        return res

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        results = list(pool.map(task, nodes))
    return results


def load_results(path: str) -> list[BenchmarkResult]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("results", data) if isinstance(data, dict) else data
    return [BenchmarkResult.from_dict(item) for item in items]


def save_results(path: str, results: list[BenchmarkResult], meta: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({**meta, "results": [r.to_dict() for r in results]}, fh, indent=2)
        fh.write("\n")
