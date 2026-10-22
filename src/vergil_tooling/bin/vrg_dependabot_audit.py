"""``vrg-dependabot-audit`` — fail on any open Dependabot alert.

The fleet runs Dependabot report-only (epic vergil-project/.github#420): alerts
are on, Dependabot never opens a PR, and this command is the nightly gate that
turns an open alert into a red ops run. Exit codes are a contract for the ops
job:

- ``0`` — no open alerts.
- ``1`` — at least one open alert, at any severity.
- ``2`` — the alerts could not be read (alerts disabled, the token lacks
  "Dependabot alerts: Read", no App installation, unknown repo). "Could not
  check" is never a pass, and never the "has alerts" exit 1 either.

A private repo gets the itemized report. A public repo, or one whose
visibility cannot be resolved, gets counts by severity plus the Security-tab
link only, so a world-readable log never names a vulnerable package.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.dependabot_alerts import AuditUnavailable, fetch_open_alerts, render_report

_PROG = "vrg-dependabot-audit"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo: str = args.repo
    # Mint the App token for the audited repo's owner, not the cwd org (#3220).
    # cli_main would map an auth failure to exit 1, which here means "has
    # alerts", so every auth failure is caught and made exit 2 (#1691).
    try:
        with github.target_org(repo.split("/", 1)[0]):
            return _audit(repo)
    except AuditUnavailable as exc:
        _fail(str(exc))
    except github.MissingGitHubTokenError:
        print(github.missing_token_message(_PROG), file=sys.stderr)
    except github.NoInstallationError as exc:
        _fail(github.no_installation_message(exc))
    except github.AppAuthError as exc:
        _fail(github.app_auth_message(exc))
    return 2


def _fail(message: str) -> None:
    print(f"{_PROG}: {message}", file=sys.stderr)


def _audit(repo: str) -> int:
    alerts = fetch_open_alerts(repo)
    if not alerts:
        print(render_report(repo, alerts, public=False))
        return 0
    print(render_report(repo, alerts, public=_counts_only(repo)))
    return 1


def _counts_only(repo: str) -> bool:
    """True unless *repo* is positively known to be private."""
    try:
        return github.repo_visibility(repo) != "PRIVATE"
    except github.GitHubAPIError:
        _fail(
            f"warning: could not resolve visibility for {repo}; "
            "using the public (counts-only) format"
        )
        return True


def _repo_arg(value: str) -> str:
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name or "/" in name:
        msg = f"expected OWNER/REPO, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Fail when a repository has any open Dependabot alert, at any "
            "severity. Dismissing an alert on GitHub is the only way to clear "
            "it. Public repositories get counts and a Security-tab link only; "
            "private repositories get an itemized report."
        ),
        epilog="Exit codes: 0 no open alerts, 1 open alerts, 2 could not check.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        type=_repo_arg,
        metavar="OWNER/REPO",
        help="Repository to audit.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
