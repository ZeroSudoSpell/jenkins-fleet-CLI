import json
from pathlib import Path

import pytest

from jenkins_fleet.benchmark import BenchmarkResult, groovy_string, parse_output
from jenkins_fleet.inventory import parse_computer, select_nodes
from jenkins_fleet.load import percentile, summarize
from jenkins_fleet.output import csv_text, table
from jenkins_fleet.recommend import Profile, compute_capacity, recommend_node

from .conftest import LOADS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def nodes():
    data = json.loads((FIXTURES / "computers.json").read_text())
    return [parse_computer(c) for c in data["computer"]]


# ---------------------------------------------------------------- inventory
def test_parse_computer(nodes):
    ctrl, big, small, win = nodes
    assert ctrl.is_controller and ctrl.computer_path is None
    assert big.name == "linux-big" and not big.is_controller
    assert big.num_executors == 8 and big.busy_executors == 2
    assert big.total_memory_mb == 32768
    assert big.disk_free_mb == 204800 and big.disk_path == "/home/jenkins"
    assert big.labels == ["docker", "linux"]  # self-label dropped
    assert small.arch == "Linux (aarch64)"
    assert win.offline and win.total_memory_mb is None and win.labels == ["windows"]
    assert win.computer_path == "computer/win%2001/"


def test_select_nodes(nodes):
    assert [n.name for n in select_nodes(nodes)] == ["Built-In Node", "linux-big", "linux-small"]
    assert [n.name for n in select_nodes(nodes, label="linux")] == ["linux-big", "linux-small"]
    assert [n.name for n in select_nodes(nodes, names=["*small"])] == ["linux-small"]
    assert len(select_nodes(nodes, include_offline=True)) == 4
    assert "Built-In Node" not in [n.name for n in select_nodes(nodes, include_controller=False)]


# --------------------------------------------------------------------- load
def test_percentile():
    assert percentile([], 95) == 0.0
    assert percentile([5], 95) == 5.0
    assert percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert percentile(list(range(101)), 95) == 95.0


def test_summarize_saturated():
    s = summarize(LOADS["computer/linux-big/loadStatistics"], "linux-big", "short")
    assert s.samples == 100
    assert s.p95_utilization >= 0.97
    assert s.mean_queue == 2.0


def test_summarize_prefers_total_queue_for_overall():
    s = summarize(LOADS["overallLoad"], "overall", "long")
    assert s.mean_queue == 3.0


def test_summarize_empty():
    s = summarize({}, "x")
    assert s.samples == 0 and s.p95_utilization == 0.0


# ---------------------------------------------------------------- benchmark
def test_groovy_string_escaping():
    assert groovy_string(None) == "null"
    assert groovy_string("C:\\agent's $HOME") == "'C:\\\\agent\\'s $HOME'"


def test_parse_output_ok():
    out = 'noise\nJFLEET_RESULT {"cores": 4, "cpu_scaling": 0.9, "unknown": 1, "errors": ["x"]}\n'
    r = parse_output("n1", out)
    assert r.ok and r.cores == 4 and r.cpu_scaling == 0.9 and r.warnings == ["x"]


def test_parse_output_failure():
    r = parse_output("n1", "java.lang.SecurityException: nope\n")
    assert not r.ok and "SecurityException" in r.error


def test_benchmark_result_roundtrip():
    r = BenchmarkResult(node="a", ok=True, cores=2, warnings=["w"])
    assert BenchmarkResult.from_dict(json.loads(json.dumps(r.to_dict()))) == r


# ---------------------------------------------------------------- recommend
def test_capacity_takes_minimum():
    cap, limit, eff, _ = compute_capacity(
        Profile(), cores=16, scaling=1.0, memory_mb=9216, disk_free_mb=None
    )
    assert (cap, limit, eff) == (4, "memory", 16.0)


def test_capacity_uses_effective_cores():
    cap, limit, eff, reasons = compute_capacity(
        Profile(), cores=8, scaling=0.5, memory_mb=65536, disk_free_mb=None
    )
    assert (cap, limit, eff) == (4, "cpu", 4.0)
    assert "50%" in reasons[0]


def test_capacity_disk_cap_and_floor():
    p = Profile(disk_per_executor_mb=10240)
    cap, limit, _, _ = compute_capacity(
        p, cores=8, scaling=None, memory_mb=65536, disk_free_mb=5000
    )
    assert (cap, limit) == (1, "disk")  # never below one executor


def test_capacity_unknown():
    assert compute_capacity(Profile(), None, None, None, None)[0] is None


def _bench(name):
    from .conftest import PROBES

    return parse_output(name, "JFLEET_RESULT " + json.dumps(PROBES[name]))


def test_recommend_scale_up(nodes):
    big = nodes[1]
    load = summarize(LOADS["computer/linux-big/loadStatistics"], big.name)
    r = recommend_node(big, _bench("linux-big"), load, Profile())
    # 16 cores * 0.95 = 15.2 -> 15 by cpu; memory (32768-1024)/2048 = 15
    assert r.verdict == "scale-up"
    assert r.recommended == 15 and r.delta == 7
    assert r.confidence == "high"


def test_recommend_overcommitted(nodes):
    small = nodes[2]
    load = summarize(LOADS["computer/linux-small/loadStatistics"], small.name)
    r = recommend_node(small, _bench("linux-small"), load, Profile())
    # 4 cores * 0.5 scaling = 2 effective
    assert r.verdict == "overcommitted"
    assert r.recommended == 2 and r.limited_by == "cpu"


def test_recommend_at_capacity(nodes):
    big = nodes[1]
    big.num_executors = 15
    load = summarize(LOADS["computer/linux-big/loadStatistics"], big.name)
    r = recommend_node(big, _bench("linux-big"), load, Profile())
    assert r.verdict == "at-capacity" and r.delta == 0


def test_recommend_underused(nodes):
    small = nodes[2]
    small.num_executors = 3
    load = summarize(LOADS["computer/linux-small/loadStatistics"], small.name)
    r = recommend_node(small, None, load, Profile())
    # memory-only capacity: (8192-1024)/2048 = 3
    assert r.verdict == "underused" and r.recommended == 3
    assert r.confidence == "medium"


def test_recommend_without_any_data(nodes):
    win = nodes[3]
    r = recommend_node(win, None, None, Profile())
    assert r.verdict == "unknown" and r.recommended == win.num_executors
    assert r.confidence == "low"


def test_failed_benchmark_is_ignored(nodes):
    big = nodes[1]
    failed = BenchmarkResult(node=big.name, ok=False, error="x", cores=999)
    r = recommend_node(big, failed, None, Profile())
    assert r.cores is None


# ------------------------------------------------------------------- output
def test_table_and_csv():
    rows = [{"a": 1, "b": None, "c": ["x", "y"]}, {"a": 22.5, "b": True, "c": []}]
    cols = [("a", "A"), ("b", "B"), ("c", "C")]
    t = table(rows, cols).splitlines()
    assert t[0].split() == ["A", "B", "C"]
    assert t[2].split() == ["1", "-", "x,y"]
    assert csv_text(rows, cols).splitlines() == ["a,b,c", '1,,"x,y"', "22.5,yes,-"]


def test_capacity_ignores_scaling_noise():
    # Found on a real 1-core agent: 96% scaling must not turn 1 core into 0.
    cap, limit, eff, reasons = compute_capacity(
        Profile(), cores=1, scaling=0.96, memory_mb=None, disk_free_mb=None
    )
    assert (cap, limit, eff) == (1, "cpu", 1.0)
    assert reasons == ["cpu: 1 cores / 1 per executor = 1"]


def test_capacity_explains_minimum_of_one():
    cap, limit, _, reasons = compute_capacity(
        Profile(), cores=None, scaling=None, memory_mb=1536, disk_free_mb=None
    )
    assert (cap, limit) == (1, "memory")
    assert "minimum of 1" in reasons[-1]


def test_label_queue_counts_as_node_demand(nodes):
    # Builds waiting for a shared label never show in a node's own queue.
    small = nodes[2]
    small.num_executors = 2
    own = summarize(LOADS["computer/linux-small/loadStatistics"], small.name)
    assert own.mean_queue == 0
    label = summarize(LOADS["label/linux/loadStatistics"], "label:linux")
    bench = _bench("linux-small")
    bench.cpu_scaling = 1.0  # 4 effective cores; memory allows 3

    without = recommend_node(small, bench, own, Profile())
    assert without.verdict == "underused"

    with_label = recommend_node(small, bench, own, Profile(), [label])
    assert with_label.verdict == "scale-up" and with_label.recommended == 3
    assert with_label.mean_queue == 2.0
    assert any("label:linux has a mean queue of 2" in r for r in with_label.reasons)


def test_few_samples_lower_confidence(nodes):
    big = nodes[1]
    data = LOADS["computer/linux-big/loadStatistics"]
    short = {
        k: {"min": {"history": v["min"]["history"][:8]}} for k, v in data.items() if k != "_class"
    }
    load = summarize(short, big.name)
    assert load.samples == 8
    bench = _bench("linux-big")
    r = recommend_node(big, bench, load, Profile())
    assert r.confidence == "medium"
    assert any("only 8 load samples" in reason for reason in r.reasons)
