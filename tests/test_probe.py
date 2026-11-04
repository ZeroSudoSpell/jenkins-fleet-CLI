"""Run the real Groovy probe on a local JVM.

Jenkins executes script-console Groovy with its bundled Groovy 2.4. Point
JFLEET_GROOVY_JAR at a groovy-all-2.4.x jar to run this test (CI does);
it is skipped otherwise.
"""

import os
import shutil
import subprocess

import pytest

from jenkins_fleet.benchmark import build_script, parse_output

GROOVY_JAR = os.environ.get("JFLEET_GROOVY_JAR")

pytestmark = pytest.mark.skipif(
    not GROOVY_JAR or not shutil.which("java"),
    reason="set JFLEET_GROOVY_JAR to a groovy-all jar and install java",
)


def test_probe_runs_on_a_real_jvm(tmp_path):
    script = tmp_path / "probe.groovy"
    script.write_text(
        build_script(cpu_seconds=0.3, disk_mb=8, small_files=50, disk_path=str(tmp_path))
    )
    proc = subprocess.run(
        ["java", "-cp", GROOVY_JAR, "groovy.ui.GroovyMain", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    result = parse_output("local", proc.stdout)
    assert result.ok, result.error
    assert result.warnings == []
    assert result.cores and result.cores >= 1
    assert result.total_memory_mb and result.total_memory_mb > 0
    assert result.cpu_single_mbps > 0 and result.cpu_multi_mbps > 0
    assert 0 < result.cpu_scaling <= 1.5
    assert result.disk_write_mbps > 0 and result.small_files_per_sec > 0
    # the probe cleans up after itself
    assert sorted(p.name for p in tmp_path.iterdir()) == ["probe.groovy"]


def test_probe_falls_back_when_no_path_given(tmp_path):
    script = tmp_path / "probe.groovy"
    script.write_text(build_script(cpu_seconds=0.1, disk_mb=1, small_files=5, disk_path=None))
    work = tmp_path / "cwd"
    work.mkdir()
    proc = subprocess.run(
        ["java", "-cp", GROOVY_JAR, "groovy.ui.GroovyMain", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=work,
    )
    result = parse_output("local", proc.stdout)
    assert result.ok and result.warnings == [], result.warnings
    assert result.disk_path == str(work)
    assert list(work.iterdir()) == []
