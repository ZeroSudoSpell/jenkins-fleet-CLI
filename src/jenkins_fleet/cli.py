"""jenkins-fleet command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .benchmark import (
    BenchmarkResult,
    build_script,
    load_results,
    run_benchmarks,
    save_results,
)
from .client import JenkinsClient, JenkinsError
from .inventory import Node, fetch_nodes, select_nodes
from .load import LoadSummary, label_load, node_load, overall_load
from .output import render
from .recommend import Profile, Recommendation, recommend_node, summarize_fleet

EXIT_CHANGES = 3


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


# --------------------------------------------------------------- arguments
def _connection_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("connection")
    g.add_argument(
        "--url", default=os.environ.get("JENKINS_URL"), help="Jenkins base URL (env: JENKINS_URL)"
    )
    g.add_argument(
        "--user", default=os.environ.get("JENKINS_USER"), help="user name (env: JENKINS_USER)"
    )
    g.add_argument(
        "--token",
        default=os.environ.get("JENKINS_API_TOKEN") or os.environ.get("JENKINS_TOKEN"),
        help="API token (env: JENKINS_API_TOKEN)",
    )
    g.add_argument(
        "--ca-bundle",
        default=os.environ.get("JENKINS_CA_BUNDLE"),
        help="PEM file to verify the Jenkins TLS certificate (env: JENKINS_CA_BUNDLE)",
    )
    g.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)"
    )
    g.add_argument(
        "-o",
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="output format (default table)",
    )
    return p


def _selection_parent(include_controller_default: bool) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("node selection")
    g.add_argument(
        "-n", "--node", action="append", metavar="GLOB", help="node name or glob; repeatable"
    )
    g.add_argument("-l", "--label", help="only nodes carrying this label")
    g.add_argument("--include-offline", action="store_true", help="include offline nodes")
    if include_controller_default:
        g.add_argument(
            "--no-controller",
            dest="include_controller",
            action="store_false",
            help="leave out the built-in node",
        )
    else:
        g.add_argument(
            "--include-controller",
            action="store_true",
            help="also target the built-in node (off by default)",
        )
    return p


def _benchmark_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("benchmark")
    g.add_argument(
        "--cpu-seconds",
        type=float,
        default=3.0,
        help="duration of each CPU phase (single + multi thread), default 3",
    )
    g.add_argument(
        "--disk-mb",
        type=int,
        default=256,
        help="MiB written for the sequential write test (0 to skip), default 256",
    )
    g.add_argument(
        "--small-files",
        type=int,
        default=2000,
        help="files for the small-file test (0 to skip), default 2000",
    )
    g.add_argument("--disk-path", help="directory to test on the agent (default: agent root dir)")
    g.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="agents to benchmark at once (default 1: sequential)",
    )
    g.add_argument(
        "--skip-busy", action="store_true", help="skip agents that are currently running builds"
    )


def _profile_options(p: argparse.ArgumentParser) -> None:
    d = Profile()
    g = p.add_argument_group("sizing profile (what one build needs)")
    g.add_argument(
        "--cpus-per-executor",
        type=float,
        default=d.cpus_per_executor,
        help=f"effective cores per concurrent build (default {d.cpus_per_executor:g})",
    )
    g.add_argument(
        "--mem-per-executor",
        type=int,
        default=d.mem_per_executor_mb,
        metavar="MIB",
        help=f"memory per concurrent build in MiB (default {d.mem_per_executor_mb})",
    )
    g.add_argument(
        "--reserve-mem",
        type=int,
        default=d.reserve_mem_mb,
        metavar="MIB",
        help=f"memory kept for OS + agent in MiB (default {d.reserve_mem_mb})",
    )
    g.add_argument(
        "--disk-per-executor",
        type=int,
        default=d.disk_per_executor_mb,
        metavar="MIB",
        help="free disk per concurrent build in MiB (default 0 = ignore disk)",
    )
    g.add_argument(
        "--headroom",
        type=float,
        default=d.headroom,
        help=f"multiplier on p95 demand (default {d.headroom:g})",
    )
    g.add_argument(
        "--saturation",
        type=float,
        default=d.saturation,
        help=f"p95 utilization treated as saturated (default {d.saturation:g})",
    )


def build_parser() -> argparse.ArgumentParser:
    conn = _connection_parent()
    parser = argparse.ArgumentParser(
        prog="jenkins-fleet",
        description="Inventory, benchmark and right-size the agents of any Jenkins instance.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser(
        "nodes",
        parents=[conn, _selection_parent(True)],
        help="list agents with executors, labels, memory and disk",
    )
    p.set_defaults(func=cmd_nodes, include_offline_default=True)

    p = sub.add_parser(
        "load",
        parents=[conn, _selection_parent(True)],
        help="utilization and queue pressure from Jenkins load statistics",
    )
    p.add_argument(
        "--window",
        choices=("short", "long"),
        default="short",
        help="short = last ~6h at 1-min resolution; long = ~15 days hourly",
    )
    p.add_argument(
        "--by",
        choices=("node", "label"),
        default="node",
        help="report per node (default) or per label",
    )
    p.set_defaults(func=cmd_load)

    p = sub.add_parser(
        "benchmark",
        parents=[conn, _selection_parent(False)],
        help="run a CPU/memory/disk probe on agents (needs Overall/Administer)",
    )
    _benchmark_options(p)
    p.add_argument(
        "--save", metavar="FILE", help="write results as JSON for `recommend --benchmark`"
    )
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser(
        "recommend",
        parents=[conn, _selection_parent(False)],
        help="recommend executor counts per agent",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--benchmark", metavar="FILE", help="use results saved by `benchmark --save`")
    src.add_argument("--run-benchmark", action="store_true", help="benchmark the agents first")
    p.add_argument(
        "--window",
        choices=("short", "long"),
        default="long",
        help="load window to size against (default long: ~15 days)",
    )
    p.add_argument("--explain", action="store_true", help="print the reasoning for every node")
    p.add_argument(
        "--emit-groovy",
        metavar="FILE",
        help="write a script-console script that applies the changes (review before running)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=f"exit {EXIT_CHANGES} if any node should change (for scheduled jobs)",
    )
    _profile_options(p)
    _benchmark_options(p)
    p.set_defaults(func=cmd_recommend)
    return parser


# ----------------------------------------------------------------- helpers
def make_client(args: argparse.Namespace, min_timeout: float = 0.0) -> JenkinsClient:
    return JenkinsClient(
        args.url,
        args.user,
        args.token,
        timeout=max(args.timeout, min_timeout),
        ca_bundle=args.ca_bundle,
    )


def selected_nodes(client: JenkinsClient, args: argparse.Namespace) -> list[Node]:
    nodes = select_nodes(
        fetch_nodes(client),
        names=args.node,
        label=args.label,
        include_offline=args.include_offline or getattr(args, "include_offline_default", False),
        include_controller=getattr(args, "include_controller", False),
    )
    if not nodes:
        raise JenkinsError("no nodes matched the selection")
    return nodes


def parallel_map(fn: Callable[[Any], Any], items: Sequence[Any], workers: int = 8) -> list[Any]:
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1))) as pool:
        return list(pool.map(fn, items))


def safe_node_load(client: JenkinsClient, window: str) -> Callable[[Node], LoadSummary | None]:
    def fn(node: Node) -> LoadSummary | None:
        try:
            return node_load(client, node, window)
        except JenkinsError as exc:
            eprint(f"warning: no load statistics for {node.name}: {exc}")
            return None

    return fn


def safe_label_load(client: JenkinsClient, window: str) -> Callable[[str], LoadSummary | None]:
    def fn(label: str) -> LoadSummary | None:
        try:
            return label_load(client, label, window)
        except JenkinsError as exc:
            eprint(f"warning: no load statistics for label {label}: {exc}")
            return None

    return fn


def emit(text: str) -> None:
    print(text)


# ---------------------------------------------------------------- commands
NODE_COLUMNS = [
    ("name", "NODE"),
    ("status", "STATUS"),
    ("num_executors", "EXEC"),
    ("busy_executors", "BUSY"),
    ("arch", "ARCH"),
    ("total_memory_mb", "MEM_MiB"),
    ("disk_free_mb", "DISK_FREE_MiB"),
    ("response_ms", "PING_ms"),
    ("labels", "LABELS"),
]


def _status(n: Node) -> str:
    if n.temporarily_offline:
        return "disabled"
    return "offline" if n.offline else "online"


def cmd_nodes(args: argparse.Namespace) -> int:
    client = make_client(args)
    nodes = selected_nodes(client, args)
    rows = [{**n.to_dict(), "status": _status(n)} for n in nodes]
    emit(render(args.format, rows, NODE_COLUMNS))
    if args.format == "table":
        total = sum(n.num_executors for n in nodes)
        busy = sum(n.busy_executors for n in nodes)
        eprint(f"\n{len(nodes)} nodes, {total} executors, {busy} busy now")
    return 0


LOAD_COLUMNS = [
    ("scope", "SCOPE"),
    ("samples", "SAMPLES"),
    ("mean_online", "ONLINE"),
    ("mean_busy", "BUSY_AVG"),
    ("p95_busy", "BUSY_P95"),
    ("max_busy", "BUSY_MAX"),
    ("mean_utilization", "UTIL_AVG"),
    ("p95_utilization", "UTIL_P95"),
    ("mean_queue", "QUEUE_AVG"),
    ("p95_queue", "QUEUE_P95"),
]


def cmd_load(args: argparse.Namespace) -> int:
    client = make_client(args)
    nodes = selected_nodes(client, args)
    if args.by == "label":
        labels = sorted({lbl for n in nodes for lbl in n.labels})
        found = parallel_map(safe_label_load(client, args.window), labels)
    else:
        found = parallel_map(safe_node_load(client, args.window), nodes)
    summaries = [s for s in found if s]
    try:
        summaries.append(overall_load(client, args.window))
    except JenkinsError as exc:
        eprint(f"warning: no overall load statistics: {exc}")
    emit(render(args.format, [s.to_dict() for s in summaries], LOAD_COLUMNS))
    return 0


BENCH_COLUMNS = [
    ("node", "NODE"),
    ("ok", "OK"),
    ("cores", "CORES"),
    ("total_memory_mb", "MEM_MiB"),
    ("cpu_single_mbps", "CPU_1T"),
    ("cpu_multi_mbps", "CPU_NT"),
    ("cpu_scaling", "SCALING"),
    ("disk_write_mbps", "DISK_MBps"),
    ("small_files_per_sec", "FILES/s"),
    ("load_average", "LOADAVG"),
    ("elapsed_sec", "SECS"),
    ("error", "ERROR"),
]


def _benchmark(
    client: JenkinsClient, nodes: list[Node], args: argparse.Namespace
) -> list[BenchmarkResult]:
    if args.skip_busy:
        busy = [n.name for n in nodes if n.busy_executors]
        if busy:
            eprint("skipping busy agents: " + ", ".join(busy))
        nodes = [n for n in nodes if not n.busy_executors]
    script = build_script(args.cpu_seconds, args.disk_mb, args.small_files, args.disk_path)
    eprint(f"benchmarking {len(nodes)} node(s), {args.parallel} at a time ...")

    def progress(r: BenchmarkResult) -> None:
        status = "ok" if r.ok else f"FAILED: {r.error}"
        eprint(f"  {r.node}: {status} ({r.elapsed_sec}s)")
        for w in r.warnings:
            eprint(f"    warning: {w}")

    return run_benchmarks(client, nodes, script, args.parallel, progress)


def _bench_timeout(args: argparse.Namespace) -> float:
    # warm-up + two CPU phases + generous allowance for slow disks
    return 2 * args.cpu_seconds + 120


def cmd_benchmark(args: argparse.Namespace) -> int:
    client = make_client(args, min_timeout=_bench_timeout(args))
    nodes = selected_nodes(client, args)
    results = _benchmark(client, nodes, args)
    if args.save:
        save_results(
            args.save,
            results,
            {
                "jenkins_url": client.base_url,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool_version": __version__,
            },
        )
        eprint(f"saved {len(results)} result(s) to {args.save}")
    emit(render(args.format, [r.to_dict() for r in results], BENCH_COLUMNS))
    return 0 if all(r.ok for r in results) else 1


REC_COLUMNS = [
    ("node", "NODE"),
    ("current", "CURRENT"),
    ("recommended", "RECOMMENDED"),
    ("delta", "DELTA"),
    ("capacity", "CAPACITY"),
    ("limited_by", "LIMIT"),
    ("verdict", "VERDICT"),
    ("p95_utilization", "UTIL_P95"),
    ("mean_queue", "QUEUE_AVG"),
    ("confidence", "CONFIDENCE"),
]


def groovy_apply_script(recs: list[Recommendation], controller_names: set[str]) -> str:
    lines = [
        "// Generated by jenkins-fleet. Review, then run in Manage Jenkins > Script Console.",
        "import jenkins.model.Jenkins",
        "def j = Jenkins.get()",
    ]
    for r in recs:
        if r.delta == 0:
            continue
        name = r.node.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"// {r.node}: {r.current} -> {r.recommended} ({r.verdict})")
        if r.node in controller_names:
            lines.append(f"j.setNumExecutors({r.recommended})")
        else:
            lines += [
                f"j.getNode('{name}')?.with {{ n ->",
                f"  n.setNumExecutors({r.recommended}); j.updateNode(n)",
                f"  println '{name}: {r.current} -> {r.recommended}'",
                "}",
            ]
    lines.append("return null")
    return "\n".join(lines) + "\n"


def cmd_recommend(args: argparse.Namespace) -> int:
    min_timeout = _bench_timeout(args) if args.run_benchmark else 0.0
    client = make_client(args, min_timeout=min_timeout)
    nodes = selected_nodes(client, args)

    bench: dict[str, BenchmarkResult] = {}
    if args.benchmark:
        bench = {r.node: r for r in load_results(args.benchmark)}
    elif args.run_benchmark:
        bench = {r.node: r for r in _benchmark(client, nodes, args)}
    else:
        eprint(
            "note: no benchmark data (use --run-benchmark or --benchmark FILE);"
            " sizing on memory and load only"
        )

    profile = Profile(
        cpus_per_executor=args.cpus_per_executor,
        mem_per_executor_mb=args.mem_per_executor,
        reserve_mem_mb=args.reserve_mem,
        disk_per_executor_mb=args.disk_per_executor,
        headroom=args.headroom,
        saturation=args.saturation,
    )
    loads = parallel_map(safe_node_load(client, args.window), nodes)
    recs = [recommend_node(n, bench.get(n.name), ld, profile) for n, ld in zip(nodes, loads)]
    try:
        overall: LoadSummary | None = overall_load(client, args.window)
    except JenkinsError:
        overall = None
    fleet = summarize_fleet(recs, overall, profile)
    controllers = {n.name for n in nodes if n.is_controller}
    for n in nodes:
        if n.is_controller and n.num_executors > 0:
            fleet.notes.append(
                f"the built-in node has {n.num_executors} executor(s); Jenkins recommends 0"
                " so builds can't affect the controller"
            )

    if args.format == "json":
        payload = {
            "profile": profile.__dict__,
            "fleet": fleet.to_dict(),
            "nodes": [r.to_dict() for r in recs],
        }
        emit(json.dumps(payload, indent=2))
    else:
        emit(render(args.format, [r.to_dict() for r in recs], REC_COLUMNS))
        if args.format == "table":
            if args.explain:
                for r in recs:
                    print(f"\n{r.node}:")
                    for reason in r.reasons:
                        print(f"  - {reason}")
            print(
                f"\nfleet: {fleet.current_executors} executors now, "
                f"{fleet.recommended_executors} recommended, "
                f"{fleet.capacity_executors} hardware capacity",
                end="",
            )
            if fleet.demand_executors is not None:
                print(
                    f", p95 demand {fleet.p95_busy:g} busy"
                    f" (~{fleet.demand_executors} with headroom)",
                    end="",
                )
            print()
            for note in fleet.notes:
                print(f"  * {note}")

    if args.emit_groovy:
        with open(args.emit_groovy, "w", encoding="utf-8") as fh:
            fh.write(groovy_apply_script(recs, controllers))
        eprint(f"wrote {args.emit_groovy}")

    if args.check and any(r.delta for r in recs):
        return EXIT_CHANGES
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (JenkinsError, ValueError, OSError) as exc:
        eprint(f"jenkins-fleet: error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
