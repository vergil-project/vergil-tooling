"""The held-back report: dependencies newer than their declared bounds allow.

Spec §7 (epic vergil-project/.github#381). Built-in updaters produce entries
directly; a repo hook appends JSON lines to ``$VRG_DEPS_REPORT``. A malformed
line fails the run loud rather than being dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.update_deps.updater import UpdateResult

EMPTY_REPORT = "No dependencies are held back by their declared bounds."

_REQUIRED = ("source", "name", "current", "latest")
_OPTIONAL = ("note",)


@dataclass(frozen=True)
class HeldBack:
    """One dependency whose newest release is outside its declared bounds."""

    source: str
    name: str
    current: str
    latest: str
    note: str | None = None


def to_line(entry: HeldBack) -> str:
    """Serialize *entry* as one JSON line (no trailing newline)."""
    data = {
        "source": entry.source,
        "name": entry.name,
        "current": entry.current,
        "latest": entry.latest,
    }
    if entry.note is not None:
        data["note"] = entry.note
    return json.dumps(data)


def _reject(origin: str, lineno: int, why: str) -> UpdateDepsError:
    return UpdateDepsError(
        phase="update",
        command=origin,
        message=f"Malformed held-back report from {origin}, line {lineno}: {why}.",
    )


def parse_report_lines(text: str, *, origin: str) -> list[HeldBack]:
    """Parse JSON-lines report *text*.

    Blank lines are ignored; every other line must be a valid entry, or the
    whole parse fails loud naming *origin* and the line number.
    """
    entries: list[HeldBack] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _reject(origin, lineno, f"not valid JSON ({exc.msg})") from exc
        if not isinstance(data, dict):
            raise _reject(origin, lineno, "not a JSON object")
        for key in data:
            if key not in _REQUIRED and key not in _OPTIONAL:
                raise _reject(origin, lineno, f"unknown key '{key}'")
        for key in _REQUIRED:
            if key not in data:
                raise _reject(origin, lineno, f"missing required key '{key}'")
        for key in (*_REQUIRED, *_OPTIONAL):
            if key in data and not isinstance(data[key], str):
                raise _reject(origin, lineno, f"'{key}' must be a string")
        entries.append(HeldBack(**data))
    return entries


def collect(results: Sequence[UpdateResult]) -> list[HeldBack]:
    """Every result's held-back entries, in registry order."""
    return [entry for result in results for entry in result.held_back]


def _cell(value: str | None) -> str:
    return (value or "").replace("|", "\\|")


def render_markdown(entries: Sequence[HeldBack], *, max_rows: int = 200) -> str:
    """Render *entries* as a Markdown table, capped at *max_rows* rows."""
    if not entries:
        return EMPTY_REPORT
    lines = [
        "| Source | Dependency | Current | Latest | Note |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {_cell(e.source)} | {_cell(e.name)} | {_cell(e.current)} | "
        f"{_cell(e.latest)} | {_cell(e.note)} |"
        for e in entries[:max_rows]
    )
    if len(entries) > max_rows:
        lines.append("")
        lines.append(f"…and {len(entries) - max_rows} more.")
    return "\n".join(lines)
