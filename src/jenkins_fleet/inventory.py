"""Read the agent inventory from ``/computer/api/json``."""

from __future__ import annotations

import fnmatch
import urllib.parse
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from .client import JenkinsClient

COMPUTER_TREE = (
    "computer[_class,displayName,numExecutors,offline,temporarilyOffline,"
    "offlineCauseReason,idle,assignedLabels[name],executors[idle],monitorData[*]]"
)

_SWAP = "hudson.node_monitors.SwapSpaceMonitor"
_ARCH = "hudson.node_monitors.ArchitectureMonitor"
_DISK = "hudson.node_monitors.DiskSpaceMonitor"
_TMP = "hudson.node_monitors.TemporarySpaceMonitor"
_RESPONSE = "hudson.node_monitors.ResponseTimeMonitor"
_CLOCK = "hudson.node_monitors.ClockMonitor"

MIB = 1024 * 1024


@dataclass
class Node:
    name: str
    is_controller: bool
    num_executors: int
    busy_executors: int
    offline: bool
    temporarily_offline: bool = False
    offline_reason: str = ""
    labels: list[str] = field(default_factory=list)
    arch: str | None = None
    total_memory_mb: int | None = None
    free_memory_mb: int | None = None
    disk_free_mb: int | None = None
    disk_path: str | None = None
    tmp_free_mb: int | None = None
    response_ms: int | None = None
    clock_diff_ms: int | None = None

    @property
    def computer_path(self) -> str | None:
        """URL fragment for this node, or None for the controller."""
        if self.is_controller:
            return None
        return "computer/" + urllib.parse.quote(self.name, safe="") + "/"

    @property
    def load_statistics_path(self) -> str:
        if self.is_controller:
            return "computer/(built-in)/loadStatistics"
        return self.computer_path + "loadStatistics"  # type: ignore[operator]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mb(value: Any) -> int | None:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value // MIB)
    return None


def parse_computer(raw: dict[str, Any]) -> Node:
    cls = raw.get("_class", "")
    is_controller = cls.endswith("MasterComputer")
    name = raw.get("displayName") or ("built-in" if is_controller else "?")
    monitors = raw.get("monitorData") or {}

    swap = monitors.get(_SWAP) or {}
    disk = monitors.get(_DISK) or {}
    tmp = monitors.get(_TMP) or {}
    response = monitors.get(_RESPONSE) or {}
    clock = monitors.get(_CLOCK) or {}

    executors = raw.get("executors") or []
    busy = sum(1 for e in executors if e and not e.get("idle", True))

    own_label = name if not is_controller else None
    labels = sorted(
        {
            lbl["name"]
            for lbl in raw.get("assignedLabels") or []
            if lbl.get("name") and lbl.get("name") != own_label
        }
    )

    return Node(
        name=name,
        is_controller=is_controller,
        num_executors=int(raw.get("numExecutors") or 0),
        busy_executors=busy,
        offline=bool(raw.get("offline")),
        temporarily_offline=bool(raw.get("temporarilyOffline")),
        offline_reason=raw.get("offlineCauseReason") or "",
        labels=labels,
        arch=monitors.get(_ARCH) if isinstance(monitors.get(_ARCH), str) else None,
        total_memory_mb=_mb(swap.get("totalPhysicalMemory")),
        free_memory_mb=_mb(swap.get("availablePhysicalMemory")),
        disk_free_mb=_mb(disk.get("size")),
        disk_path=disk.get("path"),
        tmp_free_mb=_mb(tmp.get("size")),
        response_ms=response.get("average"),
        clock_diff_ms=clock.get("diff"),
    )


def fetch_nodes(client: JenkinsClient) -> list[Node]:
    data = client.get_json("computer/api/json", tree=COMPUTER_TREE)
    return [parse_computer(c) for c in data.get("computer", [])]


def select_nodes(
    nodes: Iterable[Node],
    names: list[str] | None = None,
    label: str | None = None,
    include_offline: bool = False,
    include_controller: bool = True,
) -> list[Node]:
    """Filter nodes by name glob(s), label, and online state."""
    out = []
    for node in nodes:
        if names and not any(fnmatch.fnmatchcase(node.name, pat) for pat in names):
            continue
        if label and label not in node.labels and label != node.name:
            continue
        if node.offline and not include_offline:
            continue
        if node.is_controller and not include_controller:
            continue
        out.append(node)
    return out
