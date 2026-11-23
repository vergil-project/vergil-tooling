"""Report an unattended job's result on its ops issue (epic vergil-project/.github#1494).

Run once at the end of every unattended job's run, as the org's automation
App (``VRG_AUTOMATION_APP`` set and the token verified as that App's): any
other identity is refused before anything is read. ``--results`` takes the
GitHub job results of every job that makes up the named job, combined into
one (:func:`~vergil_tooling.lib.ops_report.combine_results`). The rules live
in :mod:`vergil_tooling.lib.ops_report`; nothing here closes an issue.

Exit 0 on every outcome (``created``, ``commented``, ``recovered``,
``nothing``), printed on stdout; 1 on any read, write or identity error, named
on stderr; 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime

from vergil_tooling.lib import epics, github, identity_mode, ops_report, trust
from vergil_tooling.lib.automation_identity import AUTOMATION_ENV, AutomationIdentityError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

_PROG = "vrg-ops-report"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        mode = identity_mode.current_mode()
    except AutomationIdentityError as exc:
        return _fail(f"refusing to report: {exc}")
    if mode is not IdentityMode.AUTOMATION:
        return _fail(
            f"refusing to report as identity mode {mode.value!r}: ops issues are written"
            f" only by the org's automation App (set ${AUTOMATION_ENV} and its token)"
        )
    try:
        result = ops_report.combine_results(args.results.split(","))
    except ValueError as exc:
        return _fail(f"--results: {exc}")
    owner = args.repo.split("/", 1)[0]
    try:
        with github.target_org(owner):
            outcome = ops_report.report(
                args.repo,
                args.job,
                result,
                run_url=args.run_url,
                step=args.step,
                today=datetime.now(UTC).date(),
            )
    except github.MissingGitHubTokenError:
        raise
    except (
        ops_report.OpsReportError,
        trust.TrustReadError,
        trust.UntrustedContentError,
        TrustedAuthorsNotConfiguredError,
        epics.EpicHomeError,
        epics.StandingEpicNotVisibleError,
        subprocess.CalledProcessError,
        ValueError,
    ) as exc:
        return _fail(f"{args.repo} / {args.job}: {' '.join(error_text(exc).split())}")
    shown = "no report (every job skipped)" if result is None else result
    print(f"{_PROG}: {args.repo} / {args.job}: {shown}: {outcome}")
    return 0


def _fail(message: str) -> int:
    print(f"{_PROG}: {message}", file=sys.stderr)
    return 1


def _repo_arg(value: str) -> str:
    if not _REPO_RE.match(value):
        msg = f"expected OWNER/NAME, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Report an unattended job's result on its ops issue under the org's "
            "operations epic: open it on a failure or cancel, comment on it, or "
            "record the first green run after one. Never closes an issue."
        ),
    )
    parser.add_argument(
        "--repo", required=True, type=_repo_arg, help="The repo the job ran in, as OWNER/NAME"
    )
    parser.add_argument(
        "--job", required=True, help="The job's stable name (the report key, never the file)"
    )
    parser.add_argument(
        "--results",
        required=True,
        help="Comma list of the GitHub job results that make up the job: success, "
        "failure, cancelled or skipped (any failure wins, then cancelled, then success; "
        "all skipped reports nothing)",
    )
    parser.add_argument("--run-url", required=True, help="The https:// URL of the run")
    parser.add_argument("--step", help="The failing step's name, if known")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
