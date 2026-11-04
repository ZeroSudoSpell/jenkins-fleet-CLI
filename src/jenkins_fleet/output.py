"""Table / JSON / CSV rendering."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any

Column = tuple[str, str]  # (key, header)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or "-"
    return str(value)


def table(rows: Sequence[dict[str, Any]], columns: Sequence[Column]) -> str:
    headers = [h for _, h in columns]
    body = [[_cell(row.get(key)) for key, _ in columns] for row in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in body)) if body else len(h) for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for r in body:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def csv_text(rows: Sequence[dict[str, Any]], columns: Sequence[Column]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([key for key, _ in columns])
    for row in rows:
        writer.writerow(["" if row.get(k) is None else _cell(row.get(k)) for k, _ in columns])
    return buf.getvalue().rstrip("\n")


def render(
    fmt: str,
    rows: Sequence[dict[str, Any]],
    columns: Sequence[Column],
    json_payload: Any | None = None,
) -> str:
    if fmt == "json":
        return json.dumps(rows if json_payload is None else json_payload, indent=2)
    if fmt == "csv":
        return csv_text(rows, columns)
    return table(rows, columns)
