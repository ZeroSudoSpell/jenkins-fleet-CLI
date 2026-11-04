"""Utilization from Jenkins' built-in load statistics.

Every node, label and the controller keep exponential-moving-average time
series of busy/total executors and queue length (the data behind the
"Load Statistics" graphs). Reading them gives hours or days of history in
one request, so nothing has to sit and poll.

Windows:
  * ``short`` - the ``min`` series: one sample per minute, up to ~6 hours.
  * ``long``  - the ``hour`` series: one sample per hour, up to ~15 days.
"""

from __future__ import annotations

import math
import urllib.parse
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .client import JenkinsClient, JenkinsError
from .inventory import Node

WINDOWS = {"short": "min", "long": "hour"}
# totalQueueLength only exists on /overallLoad; tree= silently skips it elsewhere.
SERIES = ("busyExecutors", "onlineExecutors", "totalExecutors", "queueLength", "totalQueueLength")


@dataclass
class LoadSummary:
    scope: str
    window: str
    samples: int
    mean_busy: float
    p95_busy: float
    max_busy: float
    mean_online: float
    mean_utilization: float  # busy / online, 0..1
    p95_utilization: float
    mean_queue: float
    p95_queue: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank-with-interpolation percentile; 0.0 for empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo))


def _tree(series_key: str) -> str:
    return ",".join(f"{name}[{series_key}[history]]" for name in SERIES)


def _history(data: dict[str, Any], name: str, key: str) -> list[float]:
    series = (data.get(name) or {}).get(key) or {}
    return [float(v) for v in series.get("history") or [] if v is not None]


def summarize(data: dict[str, Any], scope: str, window: str = "short") -> LoadSummary:
    key = WINDOWS[window]
    busy = _history(data, "busyExecutors", key)
    online = _history(data, "onlineExecutors", key) or _history(data, "totalExecutors", key)
    queue = _history(data, "totalQueueLength", key) or _history(data, "queueLength", key)

    n = min(len(busy), len(online)) if online else len(busy)
    util = [b / o for b, o in zip(busy[:n], online[:n]) if o > 0]
    util = [min(u, 1.0) for u in util]

    def mean(xs: Sequence[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return LoadSummary(
        scope=scope,
        window=window,
        samples=len(busy),
        mean_busy=round(mean(busy), 2),
        p95_busy=round(percentile(busy, 95), 2),
        max_busy=round(max(busy), 2) if busy else 0.0,
        mean_online=round(mean(online), 2),
        mean_utilization=round(mean(util), 3),
        p95_utilization=round(percentile(util, 95), 3),
        mean_queue=round(mean(queue), 2),
        p95_queue=round(percentile(queue, 95), 2),
    )


def fetch_load(client: JenkinsClient, path: str, window: str = "short") -> dict[str, Any]:
    return client.get_json(path, tree=_tree(WINDOWS[window]))


def node_load(client: JenkinsClient, node: Node, window: str = "short") -> LoadSummary:
    if node.is_controller:
        # Jenkins >= 2.307 calls it (built-in); older releases use (master).
        try:
            data = fetch_load(client, "computer/(built-in)/loadStatistics", window)
        except JenkinsError as exc:
            if exc.status != 404:
                raise
            data = fetch_load(client, "computer/(master)/loadStatistics", window)
    else:
        data = fetch_load(client, node.load_statistics_path, window)
    return summarize(data, node.name, window)


def label_load(client: JenkinsClient, label: str, window: str = "short") -> LoadSummary:
    path = "label/" + urllib.parse.quote(label, safe="") + "/loadStatistics"
    return summarize(fetch_load(client, path, window), f"label:{label}", window)


def overall_load(client: JenkinsClient, window: str = "short") -> LoadSummary:
    return summarize(fetch_load(client, "overallLoad", window), "overall", window)
