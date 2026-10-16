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
  The exit code is ``0`` when everything requested was upgraded (or, under
  ``--all``, deliberately skipped or held), ``1`` when an explicitly named
  dependency was refused (``manual`` or pinned), and ``2`` on any failure (an
  unknown dependency name, a failed or privilege-refused upgrade, an
  unsupported pin constraint, or an unreadable manifest or pins file).
- ``vrg-host pin <dep> --constraint <c> --inducing-release <v> --reason <text>
  [--tracking-issue <url>] [--deterministic]`` — hold a declared dependency
  back (epic #155 doctrine). ``--reason`` is required — the justification
  gate — and the constraint must be a dotted prefix with optional trailing
  ``x`` (``1.x``, ``2.40.x``, ``2.40.0``); anything else is refused up front.
- ``vrg-host unpin <dep>`` — free the pin (state → ``freed``, kept in the pins
  file as history).

Pins live in the host-local ``~/.config/vergil/host-pins.toml`` (see
:mod:`vergil_tooling.lib.host_pins`), never in the packaged manifest. ``pin``
and ``unpin`` exit ``0`` on success and ``1`` when refused.

``vrg-host`` runs unprivileged and never prompts for a password.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import host_deps, host_methods, host_pins, host_status
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
    sub = parser.add_subparsers(dest="cmd", metavar="{status,upgrade,pin,unpin}")
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


def _upgrade_one(dep: HostDependency, *, force: bool) -> tuple[str, int]:
    """Upgrade one ``auto`` dependency; return ``(outcome line, severity)``.

    An active pin holds the dependency (WARN) unless *force*; an unsupported
    pin constraint is an ERROR even under *force*. Every handler failure — a
    failed command, a privilege-escalation refusal, an unupgradeable manifest
    entry — is reported for this dependency (ERROR) and never retried.
    """
    pin = dep.pin
    if pin is not None and pin.state == "active":
        try:
            host_pins.parse_constraint(pin.constraint)
        except ValueError as exc:
            return f"ERROR    {dep.name}: {exc}", host_status.ERROR
        if not force:
            return (
                f"held     {dep.name}: pinned to {pin.constraint} ({pin.reason}); "
                f"run 'vrg-host unpin {dep.name}' or pass --force to upgrade past the pin",
                host_status.WARN,
            )
    try:
        host_methods.upgrade(dep)
    except ValueError as exc:
        return (
            f"MANUAL   {dep.name}: upgrade refused, needs manual action ({exc})",
            host_status.ERROR,
        )
    except (host_methods.UpgradeFailedError, host_deps.ManifestError) as exc:
        return (
            f"FAILED   {dep.name}: {exc} (if it needs elevated privileges, upgrade it manually)",
            host_status.ERROR,
        )
    return f"upgraded {dep.name}", host_status.OK


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
        line, severity = _upgrade_one(dep, force=args.force)
        print(line)
        # Under --all a pin hold is the intended outcome, not a refused request.
        if not (args.all and severity == host_status.WARN):
            worst = max(worst, severity)
    if skipped_manual:
        print(f"skipped (upgrade = manual, upgrade by hand): {', '.join(skipped_manual)}")
    return worst


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _status,
    "upgrade": _upgrade,
    "pin": _pin,
    "unpin": _unpin,
}


if __name__ == "__main__":
    sys.exit(main())
