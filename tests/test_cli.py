"""End-to-end tests of the CLI against the fake Jenkins in conftest.py."""

import json

from jenkins_fleet.cli import EXIT_CHANGES, main

from .conftest import FakeJenkins


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_nodes_table(cli_env, capsys):
    code, out, err = run(capsys, "nodes")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split()[:3] == ["NODE", "STATUS", "EXEC"]
    assert any(line.startswith("win 01") and "offline" in line for line in lines)
    assert "4 nodes, 18 executors, 2 busy now" in err


def test_nodes_json_filtered(cli_env, capsys):
    code, out, _ = run(capsys, "nodes", "-l", "docker", "-o", "json")
    assert code == 0
    assert [n["name"] for n in json.loads(out)] == ["linux-big"]


def test_bad_credentials(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("JENKINS_API_TOKEN", "wrong")
    code, _, err = run(capsys, "nodes")
    assert code == 1 and "HTTP 401" in err


def test_missing_url(capsys, monkeypatch):
    monkeypatch.delenv("JENKINS_URL", raising=False)
    code, _, err = run(capsys, "nodes")
    assert code == 1 and "URL is required" in err


def test_load_by_node(cli_env, capsys):
    code, out, _ = run(capsys, "load", "-o", "json")
    assert code == 0
    rows = {r["scope"]: r for r in json.loads(out)}
    assert set(rows) == {"Built-In Node", "linux-big", "linux-small", "overall"}
    assert rows["linux-big"]["p95_utilization"] >= 0.97
    # the built-in node is found via computer/(built-in)
    assert any(
        p == "computer/(built-in)/loadStatistics/api/json" for _, p, _ in FakeJenkins.requests
    )


def test_load_by_label(cli_env, capsys):
    code, out, err = run(capsys, "load", "--by", "label", "-o", "csv")
    assert code == 0
    scopes = [line.split(",")[0] for line in out.splitlines()[1:]]
    assert scopes == ["label:docker", "label:linux", "overall"]
    # the fake has no stats for the controller's label: warn, don't fail
    assert "no load statistics for label built-in" in err


def test_benchmark_saves_results(cli_env, capsys, tmp_path):
    path = tmp_path / "bench.json"
    code, out, err = run(
        capsys, "benchmark", "-l", "linux", "--cpu-seconds", "0.5", "--save", str(path)
    )
    assert code == 0
    assert "linux-big: ok" in err
    assert "2 build(s) were running" in err  # linux-big has busy executors
    saved = json.loads(path.read_text())
    assert [r["node"] for r in saved["results"]] == ["linux-big", "linux-small"]
    assert saved["results"][0]["cores"] == 16
    # script was POSTed with crumb + the right parameters
    posts = [r for r in FakeJenkins.requests if r[0] == "POST"]
    assert posts[0][1] == "computer/linux-big/scriptText"
    assert "def cpuSeconds = 0.5" in posts[0][2]["script"][0]


def test_benchmark_failure_exit_code(cli_env, capsys):
    code, out, err = run(
        capsys, "benchmark", "--include-controller", "-n", "Built-In*", "-o", "json"
    )
    # The controller probe is posted to /scriptText, which the fake doesn't serve.
    assert code == 1
    assert json.loads(out)[0]["ok"] is False


def test_benchmark_skip_busy(cli_env, capsys):
    code, out, err = run(capsys, "benchmark", "-l", "linux", "--skip-busy", "-o", "json")
    assert code == 0
    assert [r["node"] for r in json.loads(out)] == ["linux-small"]


def test_recommend_end_to_end(cli_env, capsys, tmp_path):
    groovy = tmp_path / "apply.groovy"
    code, out, err = run(
        capsys,
        "recommend",
        "--run-benchmark",
        "--cpu-seconds",
        "0.1",
        "-o",
        "json",
        "--emit-groovy",
        str(groovy),
        "--check",
    )
    assert code == EXIT_CHANGES
    data = json.loads(out)
    recs = {r["node"]: r for r in data["nodes"]}
    assert recs["linux-big"]["verdict"] == "scale-up"
    assert recs["linux-big"]["recommended"] == 15
    assert recs["linux-small"]["verdict"] == "overcommitted"
    assert recs["linux-small"]["recommended"] == 2
    assert data["fleet"]["current_executors"] == 14
    assert data["fleet"]["recommended_executors"] == 17

    script = groovy.read_text()
    assert "j.getNode('linux-big')" in script and "setNumExecutors(15)" in script
    assert "setNumExecutors(2)" in script


def test_recommend_from_saved_file_with_explain(cli_env, capsys, tmp_path):
    path = tmp_path / "bench.json"
    assert run(capsys, "benchmark", "--save", str(path))[0] == 0
    code, out, _ = run(
        capsys, "recommend", "--benchmark", str(path), "--explain", "--include-controller"
    )
    assert code == 0
    assert "linux-big:" in out and "effective" in out
    assert "Jenkins recommends 0" in out
    assert "fleet: 16 executors now" in out


def test_recommend_without_benchmark(cli_env, capsys):
    code, out, err = run(capsys, "recommend", "-o", "csv")
    assert code == 0
    assert "no benchmark data" in err
    rows = {line.split(",")[0]: line.split(",") for line in out.splitlines()[1:]}
    # memory-only: (8192 - 1024) / 2048 = 3 < 6 executors
    assert rows["linux-small"][6] == "overcommitted"
