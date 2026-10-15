"""vrg-host: observe and manage Vergil-critical macOS host dependencies.

Subcommands (epic vergil-project/.github#167, spec §6):

- ``vrg-host status`` (the default when run bare) — the read-only report: for
  each dependency in the tooling-owned manifest, its install method, installed
  and latest versions, version state, auxiliary-check results, pin and
  severity. The exit code is the worst severity: ``0`` all OK, ``1`` warnings
  (drift, latest-unknown), ``2`` errors (missing, unsatisfied check, probe
  error, or an unreadable manifest).

``vrg-host`` runs unprivileged and never prompts for a password.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import host_deps, host_methods, host_status
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
    sub = parser.add_subparsers(dest="cmd", metavar="{status}")
    sub.add_parser(
        "status",
        help="report installed-vs-latest gaps and health (default)",
        description=(
            "Report each host dependency's installed and latest version, version "
            "state, auxiliary checks and pin. Exit 0 = all OK, 1 = warnings, "
            "2 = errors."
        ),
    )
    args = parser.parse_args(argv)
    if args.cmd is None:
        args.cmd = "status"
    return args


def _run_check(check: AuxCheck) -> tuple[str, bool]:
    """Run one auxiliary check's probe; return ``(name, satisfied)``."""
    ok = host_methods._run(check.probe) is not None  # noqa: SLF001 - the single shell-out seam
    return check.name, ok


def _report(dep: HostDependency) -> Report:
    """Probe and classify one dependency.

    A probe that answers unreadably (:class:`~host_methods.ProbeError`), an
    entry that cannot be probed (:class:`~host_deps.ManifestError`) or a probe
    refused by the privilege guard (``ValueError``) becomes an explicit
    ``probe-error`` (ERROR) report for this dependency alone — never ``OK``,
    and never fatal to the rest of the report.
    """
    try:
        installed = host_methods.installed_version(dep)
        latest = host_methods.latest_version(dep)
        checks = [_run_check(c) for c in dep.checks]
    except (host_methods.ProbeError, host_deps.ManifestError, ValueError) as exc:
        return host_status.probe_error_report(dep, f"{type(exc).__name__}: {exc}")
    state, severity = host_status.classify(dep, installed, latest, checks)
    return Report(dep, installed, latest, state, severity, checks)


def _status(_args: argparse.Namespace) -> int:
    try:
        deps = host_deps.load_manifest()
    except host_deps.ManifestError as exc:
        print(f"vrg-host: invalid host-dependency manifest: {exc}", file=sys.stderr)
        return host_status.ERROR
    reports = [_report(dep) for dep in deps]
    print(host_status.render(reports))
    return host_status.worst_severity(reports)


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {"status": _status}


if __name__ == "__main__":
    sys.exit(main())
