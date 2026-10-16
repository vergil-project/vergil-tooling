"""Classify each host dependency (version state x aux checks) and render it.

``vrg-host status`` (epic vergil-project/.github#167, spec §8) classifies each
manifest dependency on two axes — its **version state** and its **auxiliary
check results** — and reports the worst severity of the two.

Severity is the 3-level exit-code contract: ``OK`` (0) / ``WARN`` (1) /
``ERROR`` (2). "Tracked debt" (a pinned dep) and "info" (an unmanaged-latest
dep) are display distinctions carrying ``OK`` severity, never separate levels.

Version states (spec §8):

- ``missing`` — not installed (ERROR).
- ``current`` — installed is at or newer than latest (OK).
- ``behind`` — installed is older than latest, unpinned (WARN). Versions are
  ordered by :func:`vergil_tooling.lib.host_versions.compare`; an install
  newer than latest is ``current``, and two versions that differ but cannot
  be ordered raise ``IncomparableVersionError`` (a ``ValueError``), which
  ``vrg-host status`` reports as ``probe-error`` — never guessed at.
- ``pinned`` — an active pin holds the dep: OK (tracked debt) while the
  installed version satisfies the pin constraint (see :func:`pin_satisfied`),
  WARN once it has drifted off it. It is also WARN when the #155
  re-evaluation trigger has fired (the leading edge is past the pin's
  ``inducing_release``) or cannot be evaluated (latest unknown, or versions
  not comparable) — see :func:`pin_review`. An unsupported constraint raises
  ``ValueError``, which ``vrg-host status`` reports as ``probe-error``.
- ``latest-unmanaged`` — the manifest declares no latest feed (OK, info).
- ``latest-unknown`` — a latest probe that should work failed (WARN).

Update levels (issue #3269): a dependency whose latest probe offers several
versions (:class:`~vergil_tooling.lib.host_methods.Offers`, today
``macos`` and ``xcode-clt``) is classified against its **leading edge** — the highest offer, or
the installed version when nothing newer is offered — exactly as a single
latest would be, so ``behind``, ``current`` and the pin rules are unchanged.
The row additionally lists every level that is behind (see
:func:`levels_behind`): ``behind (drift): minor 26.7.1, major 27.0.1``. An offer
that is not newer than the installed version is ignored; several offers in one
level report the highest.

One further state is not a version state but a probe outcome:
``probe-error`` — a probe answered but its output was unreadable, or the
dependency's manifest entry cannot be probed (ERROR). It is classified per
dependency so one broken probe is reported explicitly without taking down the
rest of the report, and it is never treated as ``current`` or ``missing``.
"""

from __future__ import annotations

import datetime
import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib import host_pins, host_versions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.host_deps import HostDependency, HostPin

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


type Latest = str | hm.Offers | hm._Unmanaged | None  # noqa: SLF001 - the sentinel's type
type Edge = str | hm._Unmanaged | None  # noqa: SLF001 - one leading-edge version
type CheckResult = tuple[str, bool]

_ORDER: Final = functools.cmp_to_key(host_versions.compare)


def leading_edge(installed: str | None, latest: Latest) -> Edge:
    """Return the single leading-edge version *latest* stands for.

    For :class:`~host_methods.Offers` it is the highest offer — or *installed*
    when that is higher (nothing newer is offered). Any other *latest* is
    returned unchanged.

    Raises:
        host_versions.IncomparableVersionError: two versions cannot be ordered.
    """
    if not isinstance(latest, hm.Offers):
        return latest
    candidates = [*latest.versions, *([installed] if installed is not None else [])]
    return max(candidates, key=_ORDER)


def levels_behind(installed: str, offers: hm.Offers) -> list[tuple[str, str]]:
    """Return ``(level, version)`` for every level at which *offers* lead *installed*.

    Each offer is classified by :func:`host_versions.update_level`; one that is
    not newer is ignored, and several in one level keep the highest. The result
    is ordered patch, minor, major and is empty when nothing newer is offered.

    Raises:
        host_versions.IncomparableVersionError: two versions cannot be ordered.
    """
    best: dict[str, str] = {}
    for version in offers.versions:
        level = host_versions.update_level(installed, version)
        if level is not None and (level not in best or _ORDER(version) > _ORDER(best[level])):
            best[level] = version
    return [(level, best[level]) for level in host_versions.LEVELS if level in best]


def classify(
    dep: HostDependency,
    installed: str | None,
    latest: Latest,
    check_results: Sequence[CheckResult],
) -> tuple[str, int]:
    """Return ``(version_state, severity)`` for *dep*.

    Severity is the worst of the version-state severity and the auxiliary
    checks (any unsatisfied check is ``ERROR``).

    Raises:
        ValueError: *dep* has an active pin with an unsupported constraint, or
            its installed and latest versions differ and cannot be ordered
            (``host_versions.IncomparableVersionError``).
    """
    check_sev = ERROR if any(not ok for _, ok in check_results) else OK
    if installed is None:
        return "missing", max(ERROR, check_sev)
    edge = leading_edge(installed, latest)  # the highest offer, for a multi-level probe
    if dep.pin is not None and dep.pin.state == "active":
        held = pin_satisfied(installed, dep.pin.constraint)
        review, _ = pin_review(dep.pin, edge)
        review_sev = WARN if review in {"due", "unknown"} else OK
        return "pinned", max(OK if held else WARN, review_sev, check_sev)
    if edge is hm.UNMANAGED:
        return "latest-unmanaged", max(OK, check_sev)
    if edge is None:
        return "latest-unknown", max(WARN, check_sev)
    if _behind(installed, str(edge)):
        return "behind", max(WARN, check_sev)
    return "current", max(OK, check_sev)


def pin_satisfied(installed: str, constraint: str) -> bool:
    """Return whether *installed* lies within the pin *constraint*.

    A constraint is a dotted version prefix whose trailing components may be
    the wildcard ``x`` — ``1.x``, ``2.40.x`` or an exact ``2.40.0`` (spec §5:
    pin the least-specific version that solves the problem). Trailing ``x``
    components are dropped and the remaining components must equal the
    installed version's leading components: ``2.40.0`` satisfies ``2.x``,
    ``2.40.x`` and ``2.40.0``; ``2.41.0`` does not satisfy ``2.40.x``; ``1.9``
    does not satisfy ``1.9.2``. A Homebrew revision suffix on the installed
    version is a rebuild of the same upstream release, so ``2.40.0_1``
    satisfies ``2.40.0``.

    Raises:
        ValueError: *constraint* uses any other syntax (operators such as
            ``<=``/``>=``/``~=``, empty components, a non-trailing ``x``, or
            no concrete component at all). It is refused, never guessed at.
    """
    parts = host_pins.parse_constraint(constraint)
    return host_versions.upstream(installed).split(".")[: len(parts)] == parts


def pin_review(pin: HostPin, latest: Edge) -> tuple[str, str | None]:
    """Evaluate the #155 re-evaluation trigger for *pin* against *latest*.

    Returns ``(review, message)`` where *review* is one of:

    - ``"not-due"`` — the leading edge is still at or before ``inducing_release``
      (no message).
    - ``"due"`` — the leading edge has moved past ``inducing_release``.
    - ``"unknown"`` — the trigger cannot be evaluated: the latest version is
      unknown, or the two versions are not comparable. Never guessed at.
    - ``"no-feed"`` — the dependency declares no latest feed, so the trigger
      can only be checked by hand.
    """
    inducing = pin.inducing_release
    if latest is hm.UNMANAGED:
        return "no-feed", (
            f"re-evaluation: no latest feed; check the leading edge by hand "
            f"against inducing release {inducing}"
        )
    if latest is None:
        return "unknown", (
            f"!! RE-EVALUATION UNKNOWN: latest version unknown, cannot tell whether "
            f"the leading edge is past inducing release {inducing} !!"
        )
    try:
        due = host_pins.is_reevaluation_due(pin, str(latest))
    except host_versions.IncomparableVersionError as exc:
        return "unknown", f"!! RE-EVALUATION UNKNOWN: {exc} !!"
    if due:
        return "due", (
            f"!! RE-EVALUATION DUE: leading edge {latest} is past inducing release "
            f"{inducing} (#155: re-test and free or re-anchor) !!"
        )
    return "not-due", None


def _pin_age(pinned_date: str, today: datetime.date) -> str:
    days = (today - datetime.date.fromisoformat(pinned_date)).days
    if days < 0:
        return f"pinned_date {pinned_date} is in the future"
    return f"{days} day{'' if days == 1 else 's'} (pinned {pinned_date})"


def _pin_lines(pin: HostPin, latest: Edge, today: datetime.date) -> list[str]:
    """Render an active pin as tracked debt (spec §9)."""
    pad = "          "
    lines = [
        f"{pad}pin: {pin.constraint}, holding back from {fmt_latest(latest)} "
        f"(inducing release {pin.inducing_release})",
        f"{pad}  reason: {pin.reason}",
        f"{pad}  tracking issue: {pin.tracking_issue or 'none'}",
        f"{pad}  age: {_pin_age(pin.pinned_date, today)}",
    ]
    _, message = pin_review(pin, latest)
    if message is not None:
        lines.append(f"{pad}  {message}")
    return lines


def _behind(installed: str, latest: str) -> bool:
    """Return whether *installed* is strictly older than *latest*.

    Both are the bare versions the method handlers normalized. Identical
    strings are current without needing to be ordered; otherwise the single
    host comparator decides, so an install *newer* than latest (a pre-release
    or HEAD build) is ``current``, never ``behind``. Versions it cannot order
    raise rather than fall back to "differs" (spec §8: never guessed at).

    Raises:
        host_versions.IncomparableVersionError: the two versions differ and
            cannot be ordered.
    """
    if installed == latest:
        return False
    return host_versions.compare(installed, latest) < 0


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


def fmt_latest(latest: Edge) -> str:
    """Render a leading edge for display: a version, ``unmanaged`` or ``unknown``."""
    if latest is hm.UNMANAGED:
        return "unmanaged"
    if latest is None:
        return "unknown"
    return str(latest)


def render(reports: Sequence[Report], *, today: datetime.date | None = None) -> str:
    """Render *reports* as a human-readable table plus per-dep detail lines.

    *today* anchors pin ages (defaults to the current date).
    """
    today = today or datetime.date.today()
    lines: list[str] = []
    for r in reports:
        tag = SEVERITY_LABELS[r.severity]
        installed = r.installed if r.installed is not None else "-"
        edge = None if r.state == PROBE_ERROR else leading_edge(r.installed, r.latest)
        latest = "-" if r.state == PROBE_ERROR else fmt_latest(edge)
        state = r.state
        if r.state in _DISPLAY:
            state = f"{r.state} ({_DISPLAY[r.state]})"
        if isinstance(r.latest, hm.Offers) and r.installed is not None:
            levels = levels_behind(r.installed, r.latest)
            if levels:
                state += ": " + ", ".join(f"{level} {version}" for level, version in levels)
        lines.append(
            f"[{tag:5}] {r.dep.name:16} {r.dep.method:13} {installed:>12} -> {latest:<12} {state}"
        )
        if r.error is not None:
            lines.append(f"          error: {r.error}")
        if r.dep.pin is not None and r.dep.pin.state == "active":
            lines.extend(_pin_lines(r.dep.pin, edge, today))
        elif r.dep.pin is not None and r.dep.pin.state == "under-evaluation":
            lines.append(
                f"          pin under evaluation (not holding): {r.dep.pin.constraint} "
                f"({r.dep.pin.reason}); tracking issue: {r.dep.pin.tracking_issue or 'none'}"
            )
        for name, ok in r.check_results:
            lines.append(f"          check {name}: {'satisfied' if ok else 'UNSATISFIED'}")
    worst = worst_severity(reports)
    noun = "dependency" if len(reports) == 1 else "dependencies"
    lines.append(f"overall: {SEVERITY_LABELS[worst]} ({len(reports)} {noun})")
    return "\n".join(lines)
