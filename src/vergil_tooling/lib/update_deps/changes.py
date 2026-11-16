"""What each dependency update changed: the packages it moved, and from what to what.

Each built-in updater itemizes its changes from a machine-readable source (the
parsed ``uv.lock`` before and after the upgrade, the image refs it rewrote, the
``vergil.toml`` values it bumped). A repo hook's edits are opaque, so its
result is not itemized. The PR body renders a per-updater count and the list
(vergil-tooling#4295).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vergil_tooling.lib.update_deps.updater import UpdateResult

HEADING = "## Package changes"
NEW = "(new)"
REMOVED = "(removed)"
NOT_ITEMIZED = "not itemized"
NONE_MOVED = "no package versions changed"


@dataclass(frozen=True)
class PackageChange:
    """One package an updater moved. ``old`` is None when added, ``new`` when removed."""

    name: str
    old: str | None
    new: str | None


def diff_versions(
    before: Mapping[str, Sequence[str]], after: Mapping[str, Sequence[str]]
) -> list[PackageChange]:
    """The packages whose versions differ between two name-to-versions maps, by name.

    A name may carry several versions (a forked resolution locks more than one);
    they are compared as a set and shown comma-joined.
    """
    found: list[PackageChange] = []
    for name in sorted(set(before) | set(after)):
        old = sorted(set(before.get(name, ())))
        new = sorted(set(after.get(name, ())))
        if old == new:
            continue
        found.append(
            PackageChange(
                name=name,
                old=", ".join(old) if old else None,
                new=", ".join(new) if new else None,
            )
        )
    return found


def _count(result: UpdateResult) -> str:
    if result.packages is None:
        return NOT_ITEMIZED
    if not result.packages:
        return NONE_MOVED
    n = len(result.packages)
    return f"{n} package{'' if n == 1 else 's'}"


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_markdown(results: Sequence[UpdateResult], *, max_rows: int = 200) -> str:
    """The ``## Package changes`` section for the changed *results*, rows capped."""
    changed = [r for r in results if r.changed]
    lines = [HEADING, ""]
    lines.extend(f"- **{r.updater}** — {_count(r)}" for r in changed)
    rows = [(r.updater, p) for r in changed for p in (r.packages or [])]
    if rows:
        lines.extend(["", "| Updater | Package | From | To |", "|---|---|---|---|"])
        lines.extend(
            f"| {_cell(updater)} | {_cell(p.name)} | {_cell(p.old or NEW)} | "
            f"{_cell(p.new or REMOVED)} |"
            for updater, p in rows[:max_rows]
        )
        if len(rows) > max_rows:
            lines.extend(["", f"…and {len(rows) - max_rows} more."])
    return "\n".join(lines)
