"""Post the ``vergil-audit/approved`` check-run that gates merges (§10).

The AUDIT agent runs this when it approves the PR's current head. It posts a
GitHub App check-run named ``vergil-audit/approved``; branch protection marks
that context **required** and pins it to the audit App's id, so a successful
post is what unblocks the merge. The check is bound to the head SHA, so a new
push invalidates a prior approval until the audit re-runs.

Check-runs are GitHub-App-only and the audit App alone holds ``checks:write``.
This tool additionally refuses to run under the USER identity: posting this
check as ``vergil-user`` would be an attempt to forge the gate. That guard is
defense-in-depth — the authoritative control is the credential and the
``integration_id`` pin in branch protection (§10).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main

CHECK_NAME = "vergil-audit/approved"

_TITLES = {
    "success": "Vergil audit approved",
    "failure": "Vergil audit requested changes",
    "action_required": "Vergil audit requires human action",
    "neutral": "Vergil audit (no verdict)",
}


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if identity_mode.current_mode() == identity_mode.IdentityMode.USER:
        print(
            "vrg-audit-approve: the USER identity must not post the audit check — "
            "that would forge the merge gate. Run as the AUDIT identity.",
            file=sys.stderr,
        )
        return 1

    # The check run belongs on the PR's own repo, read from the PR itself, not
    # the current directory's remote: run from a checkout of another repo, it
    # would otherwise land on the wrong repo and the PR would never get its
    # approval (#4033). A PR whose repo cannot be read raises GitHubAPIError.
    repo, _ = github.pr_repo_and_number(args.pr, "the audit check run")
    sha = github.head_sha(args.pr)
    title = _TITLES.get(args.conclusion, _TITLES["neutral"])
    summary = args.summary or f"{title} for {sha[:8]}."

    github.post_check_run(
        repo,
        name=CHECK_NAME,
        head_sha=sha,
        conclusion=args.conclusion,
        title=title,
        summary=summary,
    )
    print(f"Posted {CHECK_NAME} ({args.conclusion}) for {repo}@{sha[:8]}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Post the vergil-audit/approved check-run for a PR's head commit.",
    )
    parser.add_argument("pr", help="PR URL or number")
    parser.add_argument(
        "--conclusion",
        default="success",
        choices=("success", "failure", "action_required", "neutral"),
        help="Check-run conclusion (default: success — posted only on approval)",
    )
    parser.add_argument(
        "--summary",
        default="",
        help="Check-run summary (defaults to a conclusion-appropriate line)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
