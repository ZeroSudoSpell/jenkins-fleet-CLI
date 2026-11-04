"""A small fake Jenkins that serves the endpoints jenkins-fleet uses."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
USER, TOKEN = "admin", "11aa22bb33cc"
CRUMB = "c0ffee"


def series(values: list[float]) -> dict:
    # Jenkins stores history newest-first.
    return {"history": list(reversed(values)), "latest": values[-1]}


def load_stats(busy: list[float], online: list[float], queue: list[float]) -> dict:
    def mst(values: list[float]) -> dict:
        s = series(values)
        return {"sec10": s, "min": s, "hour": s}

    return {
        "_class": "hudson.model.LoadStatistics",
        "busyExecutors": mst(busy),
        "onlineExecutors": mst(online),
        "totalExecutors": mst(online),
        "queueLength": mst(queue),
    }


N = 100
LOADS = {
    # Saturated: nearly all 8 executors busy, builds queueing.
    "computer/linux-big/loadStatistics": load_stats(
        [7.5 + (i % 3) * 0.25 for i in range(N)], [8.0] * N, [2.0] * N
    ),
    # Mostly idle.
    "computer/linux-small/loadStatistics": load_stats(
        [0.5 if i % 10 else 1.0 for i in range(N)], [6.0] * N, [0.0] * N
    ),
    "computer/(built-in)/loadStatistics": load_stats([0.0] * N, [2.0] * N, [0.0] * N),
    "overallLoad": {
        **load_stats([8.0 + (i % 2) for i in range(N)], [16.0] * N, [1.0] * N),
        "totalQueueLength": {"min": series([3.0] * N), "hour": series([3.0] * N)},
    },
    "label/linux/loadStatistics": load_stats([8.0] * N, [14.0] * N, [2.0] * N),
    "label/docker/loadStatistics": load_stats([7.75] * N, [8.0] * N, [2.0] * N),
    "label/linux-big/loadStatistics": load_stats([7.75] * N, [8.0] * N, [2.0] * N),
    "label/linux-small/loadStatistics": load_stats([0.5] * N, [6.0] * N, [0.0] * N),
}

PROBES = {
    "linux-big": {
        "cores": 16,
        "os": "Linux 6.8",
        "arch": "amd64",
        "java": "21.0.5",
        "jvm_max_heap_mb": 512,
        "total_memory_mb": 32768,
        "free_memory_mb": 16000,
        "load_average": 7.9,
        "cpu_single_mbps": 1000.0,
        "cpu_multi_mbps": 15200.0,
        "cpu_scaling": 0.95,
        "disk_path": "/home/jenkins",
        "disk_free_mb": 204800,
        "disk_write_mbps": 900.0,
        "small_files_per_sec": 20000,
        "errors": [],
    },
    "linux-small": {
        "cores": 4,
        "os": "Linux 6.8",
        "arch": "aarch64",
        "java": "17.0.9",
        "jvm_max_heap_mb": 256,
        "total_memory_mb": 8192,
        "free_memory_mb": 2048,
        "load_average": 0.3,
        "cpu_single_mbps": 800.0,
        "cpu_multi_mbps": 1600.0,
        "cpu_scaling": 0.5,
        "disk_path": "/srv/agent",
        "disk_free_mb": 20480,
        "disk_write_mbps": 120.0,
        "small_files_per_sec": 3000,
        "errors": ["disk: something odd"],
    },
}


class FakeJenkins(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: bytes | str, ctype: str = "application/json", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def _path(self) -> tuple[str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        # Served under a /jenkins context path, like many real installs.
        path = urllib.parse.unquote(parsed.path).strip("/").removeprefix("jenkins/")
        return path, urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        path, query = self._path()
        FakeJenkins.requests.append(("GET", path, query))
        if not self._authorized():
            return self._send(401, "{}")
        if path == "computer/api/json":
            return self._send(200, (FIXTURES / "computers.json").read_text())
        if path == "crumbIssuer/api/json":
            return self._send(
                200,
                json.dumps({"crumb": CRUMB, "crumbRequestField": "Jenkins-Crumb"}),
                headers={"Set-Cookie": "JSESSIONID.x=abc; Path=/"},
            )
        if path.endswith("/api/json"):
            key = path[: -len("/api/json")]
            if key in LOADS:
                return self._send(200, json.dumps(LOADS[key]))
        return self._send(404, "{}")

    def do_POST(self):
        path, _ = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        FakeJenkins.requests.append(("POST", path, form))
        if not self._authorized():
            return self._send(401, "{}")
        if self.headers.get("Jenkins-Crumb") != CRUMB or "JSESSIONID" not in (
            self.headers.get("Cookie") or ""
        ):
            return self._send(403, "No valid crumb was included in the request", "text/plain")
        script = form.get("script", [""])[0]
        if "JFLEET_RESULT" not in script:
            return self._send(400, "unexpected script", "text/plain")
        if path.startswith("computer/") and path.endswith("/scriptText"):
            node = path[len("computer/") : -len("/scriptText")]
            if node in PROBES:
                out = "JFLEET_RESULT " + json.dumps(PROBES[node]) + "\n"
                return self._send(200, out, "text/plain")
            return self._send(200, "groovy.lang.MissingPropertyException: boom\n", "text/plain")
        return self._send(404, "")


@pytest.fixture()
def jenkins_url():
    FakeJenkins.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJenkins)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/jenkins/"
    finally:
        server.shutdown()


@pytest.fixture()
def cli_env(jenkins_url, monkeypatch):
    monkeypatch.setenv("JENKINS_URL", jenkins_url)
    monkeypatch.setenv("JENKINS_USER", USER)
    monkeypatch.setenv("JENKINS_API_TOKEN", TOKEN)
    return jenkins_url
