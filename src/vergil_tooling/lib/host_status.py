"""Classify each host dependency (version state x aux checks) and render it.

``vrg-host status`` (epic vergil-project/.github#167, spec §8) classifies each
manifest dependency on two axes — its **version state** and its **auxiliary
check results** — and reports the worst severity of the two.

Severity is the 3-level exit-code contract: ``OK`` (0) / ``WARN`` (1) /
``ERROR`` (2). "Tracked debt" (a pinned dep) and "info" (an unmanaged-latest
dep) are display distinctions carrying ``OK`` severity, never separate levels.

Version states (spec §8):

- ``missing`` — not installed (ERROR).
- ``current`` — installed == latest (OK).
- ``behind`` — installed differs from latest, unpinned (WARN).
- ``pinned`` — an active pin holds the dep (OK; WARN while it drifts off the
  constraint under the interim "differs" comparator).
- ``latest-unmanaged`` — the manifest declares no latest feed (OK, info).
- ``latest-unknown`` — a latest probe that should work failed (WARN).

One further state is not a version state but a probe outcome:
``probe-error`` — a probe answered but its output was unreadable, or the
dependency's manifest entry cannot be probed (ERROR). It is classified per
dependency so one broken probe is reported explicitly without taking down the
rest of the report, and it is never treated as ``current`` or ``missing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from vergil_tooling.lib import host_methods as hm

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.host_deps import HostDependency

OK: Final = 0
WARN: Final = 1
ERROR: Final = 2

SEVERITY_LABELS: Final = {OK: "OK", WARN: "WARN", ERROR: "ERROR"}

VERSION_STATES: Final = frozenset(
    {"missing", "current", "behind", "pinned", "latest-unmanaged", "latest-unknown"}
)
PROBE_ERROR: Final = "probe-error"

_DISPLAY: Final = {
    "behind": "drift",
    "pinned": "tracked debt",
    "latest-unmanaged": "info: no latest feed",
}

type Latest = str | hm._Unmanaged | None  # noqa: SLF001 - the sentinel's type
type CheckResult = tuple[str, bool]


def classify(
    dep: HostDependency,
    installed: str | None,
    latest: Latest,
    check_results: Sequence[CheckResult],
) -> tuple[str, int]:
    """Return ``(version_state, severity)`` for *dep*.

    Severity is the worst of the version-state severity and the auxiliary
    checks (any unsatisfied check is ``ERROR``).
    """
    check_sev = ERROR if any(not ok for _, ok in check_results) else OK
    if installed is None:
        return "missing", max(ERROR, check_sev)
    if dep.pin is not None and dep.pin.state == "active":
        return "pinned", max(WARN if _behind(installed, dep.pin.constraint) else OK, check_sev)
    if latest is hm.UNMANAGED:
        return "latest-unmanaged", max(OK, check_sev)
    if latest is None:
        return "latest-unknown", max(WARN, check_sev)
    if _behind(installed, str(latest)):
        return "behind", max(WARN, check_sev)
    return "current", max(OK, check_sev)


def _behind(installed: str, latest: str) -> bool:
    # Heterogeneous version schemes: treat "differs" as behind unless a
    # tool-specific comparator is registered (refined in plan Task 7).
    return installed != latest


@dataclass(frozen=True)
class Report:
    """The status of one dependency, ready to render."""

    dep: HostDependency
    installed: str | None
    latest: Latest
    state: str
    severity: int
    check_results: list[CheckResult] = field(default_factory=list)
    error: str | None = None


def probe_error_report(dep: HostDependency, message: str) -> Report:
    """Return the ``probe-error`` (ERROR) report for a dependency whose probe failed."""
    return Report(
        dep=dep,
        installed=None,
        latest=None,
        state=PROBE_ERROR,
        severity=ERROR,
        check_results=[],
        error=message,
    )


def worst_severity(reports: Sequence[Report]) -> int:
    """Return the worst severity across *reports* (``OK`` when empty)."""
    return max((r.severity for r in reports), default=OK)


def _fmt_latest(latest: Latest) -> str:
    if latest is hm.UNMANAGED:
        return "unmanaged"
    if latest is None:
        return "unknown"
    return str(latest)


def render(reports: Sequence[Report]) -> str:
    """Render *reports* as a human-readable table plus per-dep detail lines."""
    lines: list[str] = []
    for r in reports:
        tag = SEVERITY_LABELS[r.severity]
        installed = r.installed if r.installed is not None else "-"
        latest = "-" if r.state == PROBE_ERROR else _fmt_latest(r.latest)
        state = r.state
        if r.state in _DISPLAY:
            state = f"{r.state} ({_DISPLAY[r.state]})"
        lines.append(
            f"[{tag:5}] {r.dep.name:16} {r.dep.method:13} {installed:>12} -> {latest:<12} {state}"
        )
        if r.error is not None:
            lines.append(f"          error: {r.error}")
        if r.dep.pin is not None and r.dep.pin.state == "active":
            lines.append(f"          pin: {r.dep.pin.constraint} ({r.dep.pin.reason})")
        for name, ok in r.check_results:
            lines.append(f"          check {name}: {'satisfied' if ok else 'UNSATISFIED'}")
    worst = worst_severity(reports)
    noun = "dependency" if len(reports) == 1 else "dependencies"
    lines.append(f"overall: {SEVERITY_LABELS[worst]} ({len(reports)} {noun})")
    return "\n".join(lines)
