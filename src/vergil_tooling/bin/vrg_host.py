"""vrg-host: observe and manage Vergil-critical macOS host dependencies.

Subcommands (epic vergil-project/.github#167, spec §6):

- ``vrg-host status`` (the default when run bare) — the read-only report: for
  each dependency in the tooling-owned manifest, its install method, installed
  and latest versions, version state, auxiliary-check results, pin and
  severity. The exit code is the worst severity: ``0`` all OK, ``1`` warnings
  (drift, latest-unknown), ``2`` errors (missing, unsatisfied check, probe
  error, or an unreadable manifest or pins file). ``status --fix`` first runs
  the remediation of each installed dependency's unsatisfied auxiliary checks
  (spec §7.4); a failed remediation is reported and forces exit ``2``.
- ``vrg-host upgrade [<dep>... | --all]`` — remediate. Upgrades only
  ``upgrade = "auto"`` dependencies via their method handler. A ``manual``
  dependency is refused with an actionable message (``--all`` lists every
  ``manual`` dependency it skipped). An actively pinned dependency — pins
  come from the host-local pins file, so ``vrg-host pin`` holds it — is held:
  the package managers cannot upgrade *to a constraint*, so a pin is a real
  hold, unless ``--force`` overrides it. An upgrade that would need privilege
  escalation is reported as needing manual action, never retried elevated.
  Each ``auto`` dependency is probed and classified first, as ``status``
  would (issue #3301): a ``current`` one is skipped without running its
  upgrade; a ``behind`` one is upgraded and its installed version re-probed,
  reporting the real ``before -> after`` (an unchanged version is a WARN,
  never ``upgraded``); one with no latest to compare against is upgraded and
  reported ``before -> after``; a missing one or a probe error is an ERROR
  and is not attempted.
  The exit code is ``0`` when everything requested was upgraded or already
  current (or, under ``--all``, deliberately skipped or held), ``1`` when an
  explicitly named dependency was refused (``manual`` or pinned) or an upgrade
  left a dependency behind or its latest unknown, and ``2`` on any failure (an
  unknown dependency name, a missing dependency, a probe error, a failed or
  privilege-refused upgrade, an unsupported pin constraint, or an unreadable
  manifest or pins file).
- ``vrg-host pin <dep> --constraint <c> --inducing-release <v> --reason <text>
  [--tracking-issue <url>] [--deterministic]`` — hold a declared dependency
  back (epic #155 doctrine). ``--reason`` is required — the justification
  gate — and the constraint must be a dotted prefix with optional trailing
  ``x`` (``1.x``, ``2.40.x``, ``2.40.0``); anything else is refused up front.
- ``vrg-host unpin <dep>`` — free the pin (state → ``freed``, kept in the pins
  file as history).
- ``vrg-host plugins`` — refresh the host's Claude Code plugins from
  ``~/.claude/settings.json`` and verify them (epic
  vergil-project/.github#760, spec §6): re-add each declared marketplace at its
  declared ref, update the marketplaces and every plugin install, then check
  each declared marketplace is registered at its declared repo and ref. It is
  a human command: an agent identity is refused (exit ``1``) before any
  ``claude`` call. Exit ``0`` when everything refreshed and verified, ``2``
  when the settings cannot be read or any step or the verification failed,
  naming each failure.

Pins live in the host-local ``~/.config/vergil/host-pins.toml`` (see
:mod:`vergil_tooling.lib.host_pins`), never in the packaged manifest. ``pin``
and ``unpin`` exit ``0`` on success and ``1`` when refused.

``vrg-host`` runs unprivileged and never prompts for a password.
"""

from __future__ import annotations

import argparse
import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from vergil_tooling.lib import (
    claude_marketplace,
    host_deps,
    host_methods,
    host_pins,
    host_status,
    host_versions,
    identity_mode,
)
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.host_status import Report

if TYPE_CHECKING:
    from collections.abc import Callable

    from vergil_tooling.lib.host_deps import AuxCheck, HostDependency


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return _COMMANDS[args.cmd](args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-host",
        description="Observe and manage Vergil-critical macOS host dependencies.",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="{status,upgrade,pin,unpin,plugins}")
    status = sub.add_parser(
        "status",
        help="report installed-vs-latest gaps and health (default)",
        description=(
            "Report each host dependency's installed and latest version, version "
            "state, auxiliary checks and pin. Exit 0 = all OK, 1 = warnings, "
            "2 = errors."
        ),
    )
    status.add_argument(
        "--fix",
        action="store_true",
        help=(
            "first run the remediation of each installed dependency's unsatisfied "
            "auxiliary checks (unprivileged; never prompts for a password)"
        ),
    )
    up = sub.add_parser(
        "upgrade",
        help="upgrade auto-managed dependencies (pin-respecting)",
        description=(
            "Upgrade the named dependencies, or every dependency with --all. Only "
            "'upgrade = auto' dependencies are upgraded; 'manual' ones are refused "
            "and actively pinned ones are held unless --force. Exit 0 = done, "
            "1 = a named dependency was refused, 2 = a failure."
        ),
    )
    up.add_argument("deps", nargs="*", metavar="dep", help="dependency names to upgrade")
    up.add_argument("--all", action="store_true", help="upgrade every auto dependency")
    up.add_argument(
        "--force", action="store_true", help="upgrade past an active pin (overrides the hold)"
    )
    _add_pin_parsers(sub)
    sub.add_parser(
        "plugins",
        help="refresh and verify the host's Claude Code plugins (human only)",
        description=(
            "Re-add each marketplace ~/.claude/settings.json declares at its declared "
            "ref, update the marketplaces and every plugin install, then verify each "
            "declared marketplace is registered at its declared repo and ref. Refuses "
            "an agent identity. Exit 0 = refreshed and verified, 1 = refused, "
            "2 = a failure."
        ),
    )
    args = parser.parse_args(argv)
    if args.cmd == "upgrade" and bool(args.deps) == args.all:
        up.error("name one or more dependencies, or pass --all (not both)")
    if args.cmd is None:
        args.cmd = "status"
    return args


def _add_pin_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    pin = sub.add_parser(
        "pin",
        help="hold a dependency back at a constraint (requires --reason)",
        description=(
            "Record an active pin (epic #155) in ~/.config/vergil/host-pins.toml. "
            "The constraint is a dotted version prefix with optional trailing 'x' "
            "(e.g. 1.x, 2.40.x, 2.40.0)."
        ),
    )
    pin.add_argument("dep", help="a dependency declared in the host manifest")
    pin.add_argument("--constraint", required=True, help="e.g. 1.x, 2.40.x, 2.40.0")
    pin.add_argument(
        "--inducing-release", required=True, help="the release that broke us (#155 anchor)"
    )
    pin.add_argument("--reason", required=True, help="why the pin exists (required)")
    pin.add_argument("--tracking-issue", default=None, help="URL of the tracking issue")
    pin.add_argument(
        "--deterministic",
        action="store_true",
        help="the breakage reproduces deterministically (#155 re-evaluation path)",
    )
    unpin = sub.add_parser(
        "unpin",
        help="free a dependency's pin (state -> freed)",
        description="Free a pin: its state becomes 'freed' and it is kept as history.",
    )
    unpin.add_argument("dep", help="the pinned dependency")


def _pin(args: argparse.Namespace) -> int:
    try:
        pin = host_pins.make_pin(
            constraint=args.constraint,
            inducing_release=args.inducing_release,
            deterministic=args.deterministic,
            reason=args.reason,
            tracking_issue=args.tracking_issue,
        )
        path = host_pins.default_pins_path()
        host_pins.write_pin(args.dep, pin, path)
    except (ValueError, host_deps.ManifestError) as exc:
        print(f"vrg-host pin: refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"pinned {args.dep} at {pin.constraint} (inducing release {pin.inducing_release}) in {path}"
    )
    return 0


def _unpin(args: argparse.Namespace) -> int:
    path = host_pins.default_pins_path()
    try:
        host_pins.clear_pin(args.dep, path)
    except host_deps.ManifestError as exc:
        print(f"vrg-host unpin: refused: {exc}", file=sys.stderr)
        return 1
    print(f"freed pin on {args.dep} (kept as history in {path})")
    return 0


def _run_check(check: AuxCheck) -> tuple[str, bool]:
    """Run one auxiliary check's probe; return ``(name, satisfied)``."""
    ok = host_methods._run(check.probe) is not None  # noqa: SLF001 - the single shell-out seam
    return check.name, ok


class RemediationFailedError(Exception):
    """A check's remediation failed, or the check is still unsatisfied after it ran."""


def remediate_checks(dep: HostDependency) -> list[str]:
    """Run the remediation of each of *dep*'s unsatisfied auxiliary checks.

    A satisfied check is never touched. An unsatisfied check that declares no
    remediation is left for ``status`` to report as ERROR. Every remediation
    that runs is verified by re-probing its check. Remediations go through the
    same unprivileged :func:`host_methods._run` seam as every probe, so a
    privilege-escalating command is refused (``ValueError``).

    Returns:
        The names of the checks whose remediation ran and now pass.

    Raises:
        RemediationFailedError: a remediation command failed, or its check is
            still unsatisfied afterwards.
        ValueError: a probe or remediation would escalate privilege.
    """
    remediated: list[str] = []
    for check in dep.checks:
        _, ok = _run_check(check)
        if ok or check.remediation is None:
            continue
        if host_methods._run(check.remediation) is None:  # noqa: SLF001 - the shell-out seam
            raise RemediationFailedError(
                f"{dep.name}: remediation for check {check.name!r} failed: "
                f"{' '.join(check.remediation)}"
            )
        if not _run_check(check)[1]:
            raise RemediationFailedError(
                f"{dep.name}: check {check.name!r} is still unsatisfied after its remediation ran"
            )
        remediated.append(check.name)
    return remediated


def _fix_checks(deps: tuple[HostDependency, ...]) -> bool:
    """Remediate the auxiliary checks of every installed dependency.

    A missing dependency is skipped (``status`` reports it as ``missing``).
    Each fixed check is printed; each failure is printed to stderr.

    Returns:
        ``True`` when every attempted remediation succeeded.
    """
    all_ok = True
    for dep in deps:
        if not dep.checks:
            continue
        try:
            if host_methods.installed_version(dep) is None:
                continue
            fixed = remediate_checks(dep)
        except (
            RemediationFailedError,
            host_methods.ProbeError,
            host_deps.ManifestError,
            ValueError,
        ) as exc:
            print(f"vrg-host: fix {dep.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            all_ok = False
            continue
        for name in fixed:
            print(f"fixed: {dep.name} check {name}")
    return all_ok


def _report(dep: HostDependency) -> Report:
    """Probe and classify one dependency.

    A probe that answers unreadably (:class:`~host_methods.ProbeError`), an
    entry that cannot be probed (:class:`~host_deps.ManifestError`), a probe
    refused by the privilege guard or an unsupported pin constraint (both
    ``ValueError``) becomes an explicit
    ``probe-error`` (ERROR) report for this dependency alone — never ``OK``,
    and never fatal to the rest of the report.
    """
    try:
        installed = host_methods.installed_version(dep)
        latest = host_methods.latest_version(dep)
        checks = [_run_check(c) for c in dep.checks]
        state, severity = host_status.classify(dep, installed, latest, checks)
    except (host_methods.ProbeError, host_deps.ManifestError, ValueError) as exc:
        return host_status.probe_error_report(dep, f"{type(exc).__name__}: {exc}")
    return Report(dep, installed, latest, state, severity, checks)


def _status(args: argparse.Namespace) -> int:
    try:
        deps = host_pins.load_effective_manifest()
    except host_pins.PinsError as exc:
        print(f"vrg-host: invalid host pins file: {exc}", file=sys.stderr)
        return host_status.ERROR
    except host_deps.ManifestError as exc:
        print(f"vrg-host: invalid host-dependency manifest: {exc}", file=sys.stderr)
        return host_status.ERROR
    # A bare `vrg-host` never parses the status subparser, so `fix` may be absent.
    fixes_ok = _fix_checks(deps) if getattr(args, "fix", False) else True
    reports = [_report(dep) for dep in deps]
    print(host_status.render(reports))
    worst = host_status.worst_severity(reports)
    return worst if fixes_ok else host_status.ERROR


class _Outcome(NamedTuple):
    """One dependency's upgrade outcome: its report line and severity."""

    line: str
    severity: int
    held: bool = False


_PROBE_ERRORS: Final = (host_methods.ProbeError, host_deps.ManifestError, ValueError)


def _probe_versions(dep: HostDependency) -> tuple[str | None, host_status.Latest, str]:
    """Probe *dep*'s installed and latest versions and classify them as ``status`` would.

    The version state is classified *without* the pin (the hold is decided by
    the caller) and without auxiliary checks (``upgrade`` does not run them),
    so it is one of ``missing``, ``current``, ``behind``, ``latest-unmanaged``
    or ``latest-unknown``.

    Raises:
        host_methods.ProbeError: a probe answered unreadably.
        host_deps.ManifestError: the entry cannot be probed.
        ValueError: a probe would escalate privilege, or the versions cannot
            be ordered.
    """
    installed = host_methods.installed_version(dep)
    latest = host_methods.latest_version(dep)
    state, _ = host_status.classify(dataclasses.replace(dep, pin=None), installed, latest, [])
    return installed, latest, state


def _run_upgrade(dep: HostDependency) -> _Outcome | None:
    """Run *dep*'s upgrade command; return a failure outcome, or ``None`` on success."""
    try:
        host_methods.upgrade(dep)
    except ValueError as exc:
        return _Outcome(
            f"MANUAL   {dep.name}: upgrade refused, needs manual action ({exc})",
            host_status.ERROR,
        )
    except (host_methods.UpgradeFailedError, host_deps.ManifestError) as exc:
        return _Outcome(
            f"FAILED   {dep.name}: {exc} (if it needs elevated privileges, upgrade it manually)",
            host_status.ERROR,
        )
    return None


def _reprobe(dep: HostDependency) -> str | _Outcome:
    """Re-probe *dep*'s installed version after its upgrade ran.

    Returns the new installed version, or an ERROR outcome when it cannot be
    read or the dependency is no longer detected — never the expected latest.
    """
    try:
        after = host_methods.installed_version(dep)
    except _PROBE_ERRORS as exc:
        return _Outcome(
            f"ERROR    {dep.name}: upgrade ran but the installed version could not be "
            f"re-probed ({type(exc).__name__}: {exc})",
            host_status.ERROR,
        )
    if after is None:
        return _Outcome(
            f"ERROR    {dep.name}: upgrade ran but {dep.name} is no longer detected as installed",
            host_status.ERROR,
        )
    return after


def _upgrade_behind(dep: HostDependency, before: str, edge: str) -> _Outcome:
    """Upgrade a dependency that is behind *edge* and report the real transition."""
    outcome = _run_upgrade(dep)
    if outcome is not None:
        return outcome
    after = _reprobe(dep)
    if isinstance(after, _Outcome):
        return after
    if after == before:
        return _Outcome(
            f"unchanged {dep.name}: still {before}, latest {edge} "
            "(the upgrade command succeeded but did not change the installed version)",
            host_status.WARN,
        )
    try:
        still_behind = host_versions.compare(after, edge) < 0
    except ValueError as exc:
        return _Outcome(
            f"upgraded {dep.name}: {before} -> {after} (cannot order against latest {edge}: {exc})",
            host_status.WARN,
        )
    if still_behind:
        return _Outcome(
            f"upgraded {dep.name}: {before} -> {after} (still behind latest {edge})",
            host_status.WARN,
        )
    return _Outcome(f"upgraded {dep.name}: {before} -> {after}", host_status.OK)


def _upgrade_no_latest(dep: HostDependency, before: str, state: str) -> _Outcome:
    """Upgrade a dependency with no latest to compare against; report before -> after.

    An ``unmanaged`` latest is by design, so the outcome is informational (OK).
    A ``latest-unknown`` probe failed, so the outcome keeps ``status``'s WARN.
    """
    outcome = _run_upgrade(dep)
    if outcome is not None:
        return outcome
    after = _reprobe(dep)
    if isinstance(after, _Outcome):
        return after
    unmanaged = state == "latest-unmanaged"
    why = "no latest feed" if unmanaged else "latest unknown: its latest probe did not answer"
    note = f" (unchanged; {why})" if after == before else f" ({why})"
    severity = host_status.OK if unmanaged else host_status.WARN
    return _Outcome(f"ran      {dep.name}: {before} -> {after}{note}", severity)


def _upgrade_one(dep: HostDependency, *, force: bool) -> _Outcome:
    """Probe, then upgrade one ``auto`` dependency only if it needs it.

    An unsupported pin constraint is an ERROR even under *force*. The
    dependency is then probed and classified as ``status`` would: a probe
    error or a missing dependency is an ERROR and is never upgraded. An active
    pin holds the dependency (WARN) unless *force*. A ``current`` dependency
    is skipped (OK) — ``--force`` crosses a pin, it never forces a no-op. A
    ``behind`` one is upgraded and its installed version re-probed: an
    unchanged version is WARN, never ``upgraded``. With no latest to compare
    against, the upgrade runs and before -> after is reported. Every handler
    failure — a failed command, a privilege-escalation refusal, an
    unupgradeable manifest entry — is an ERROR and never retried.
    """
    pin = dep.pin
    active = pin is not None and pin.state == "active"
    if pin is not None and active:
        try:
            host_pins.parse_constraint(pin.constraint)
        except ValueError as exc:
            return _Outcome(f"ERROR    {dep.name}: {exc}", host_status.ERROR)
    try:
        before, latest, state = _probe_versions(dep)
        edge = host_status.leading_edge(before, latest)
    except _PROBE_ERRORS as exc:
        return _Outcome(
            f"ERROR    {dep.name}: probe-error ({type(exc).__name__}: {exc}); not upgraded",
            host_status.ERROR,
        )
    if before is None:
        return _Outcome(
            f"ERROR    {dep.name}: missing (not installed by {dep.method}); "
            "vrg-host upgrades, it does not install",
            host_status.ERROR,
        )
    if pin is not None and active and not force:
        return _Outcome(
            f"held     {dep.name}: pinned to {pin.constraint} ({pin.reason}) at {before}, "
            f"holding back from {host_status.fmt_latest(edge)}; "
            f"run 'vrg-host unpin {dep.name}' or pass --force to upgrade past the pin",
            host_status.WARN,
            held=True,
        )
    if state == "current":
        return _Outcome(f"skipped  {dep.name}: current ({before})", host_status.OK)
    if state == "behind":
        return _upgrade_behind(dep, before, str(edge))
    return _upgrade_no_latest(dep, before, state)


def _upgrade(args: argparse.Namespace) -> int:
    try:
        deps = host_pins.load_effective_manifest()
    except host_pins.PinsError as exc:
        print(f"vrg-host: invalid host pins file: {exc}", file=sys.stderr)
        return host_status.ERROR
    except host_deps.ManifestError as exc:
        print(f"vrg-host: invalid host-dependency manifest: {exc}", file=sys.stderr)
        return host_status.ERROR
    by_name = {dep.name: dep for dep in deps}
    unknown = [name for name in args.deps if name not in by_name]
    if unknown:
        print(
            f"vrg-host: unknown dependency: {', '.join(unknown)} (known: {', '.join(by_name)})",
            file=sys.stderr,
        )
        return host_status.ERROR
    targets = list(deps) if args.all else [by_name[name] for name in dict.fromkeys(args.deps)]
    worst = host_status.OK
    skipped_manual: list[str] = []
    for dep in targets:
        if dep.upgrade == "manual":
            if args.all:
                skipped_manual.append(dep.name)
                continue
            print(
                f"refused  {dep.name}: upgrade = manual (vrg-host never upgrades it); "
                "upgrade it by hand"
            )
            worst = max(worst, host_status.WARN)
            continue
        outcome = _upgrade_one(dep, force=args.force)
        print(outcome.line)
        # Under --all a pin hold is the intended outcome, not a refused request.
        if not (args.all and outcome.held):
            worst = max(worst, outcome.severity)
    if skipped_manual:
        print(f"skipped (upgrade = manual, upgrade by hand): {', '.join(skipped_manual)}")
    return worst


def _host_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _plugins(_args: argparse.Namespace) -> int:
    """Refresh and verify the host's Claude Code plugins (spec §6 of epic #760)."""
    if identity_mode.is_agent() or identity_mode.any_agent_signal():
        print(
            "vrg-host plugins: refreshing the host's plugins is a human action. "
            "Agents cannot run vrg-host plugins.",
            file=sys.stderr,
        )
        return 1
    try:
        settings = claude_marketplace.read_settings_file(_host_settings_path())
    except claude_marketplace.MarketplaceError as exc:
        print(f"vrg-host plugins: {exc}", file=sys.stderr)
        return host_status.ERROR
    print("Refreshing Claude plugins...")
    try:
        failures = claude_marketplace.refresh(claude_marketplace.local_run, settings, "")
    except (claude_marketplace.MarketplaceError, subprocess.CalledProcessError, OSError) as exc:
        print(f"vrg-host plugins: {type(exc).__name__}: {exc}", file=sys.stderr)
        return host_status.ERROR
    if failures:
        print(f"vrg-host plugins: failed: {', '.join(failures)}", file=sys.stderr)
        return host_status.ERROR
    print("Plugins refreshed; every declared marketplace is at its declared repo and ref.")
    return host_status.OK


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _status,
    "upgrade": _upgrade,
    "pin": _pin,
    "unpin": _unpin,
    "plugins": _plugins,
}


if __name__ == "__main__":
    sys.exit(main())
