"""vrg-host: observe and manage Vergil-critical macOS host dependencies.

Subcommands (epic vergil-project/.github#167, spec §6):

- ``vrg-host status`` (the default when run bare) — the read-only report: for
  each dependency in the tooling-owned manifest, its install method, installed
  and latest versions, version state, auxiliary-check results, pin and
  severity. The exit code is the worst severity: ``0`` all OK, ``1`` warnings
  (drift, latest-unknown), ``2`` errors (missing, unsatisfied check, probe
  error, or an unreadable manifest or pins file).
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
    sub = parser.add_subparsers(dest="cmd", metavar="{status,pin,unpin}")
    sub.add_parser(
        "status",
        help="report installed-vs-latest gaps and health (default)",
        description=(
            "Report each host dependency's installed and latest version, version "
            "state, auxiliary checks and pin. Exit 0 = all OK, 1 = warnings, "
            "2 = errors."
        ),
    )
    _add_pin_parsers(sub)
    args = parser.parse_args(argv)
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


def _status(_args: argparse.Namespace) -> int:
    try:
        deps = host_pins.load_effective_manifest()
    except host_pins.PinsError as exc:
        print(f"vrg-host: invalid host pins file: {exc}", file=sys.stderr)
        return host_status.ERROR
    except host_deps.ManifestError as exc:
        print(f"vrg-host: invalid host-dependency manifest: {exc}", file=sys.stderr)
        return host_status.ERROR
    reports = [_report(dep) for dep in deps]
    print(host_status.render(reports))
    return host_status.worst_severity(reports)


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _status,
    "pin": _pin,
    "unpin": _unpin,
}


if __name__ == "__main__":
    sys.exit(main())
