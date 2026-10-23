"""Capture an intake issue and link it under its home's intake epic.

An intake issue is an uncurated capture — a problem, an idea, or an
investigation — that intake-review later routes into the epic/task model. It
carries no kind label (the retired ``triage`` / ``idea`` / ``research`` labels
are gone); its membership in the intake queue *is* its kind. This is the
sanctioned path for creating one — ``vrg-gh`` denies raw ``gh issue create`` —
used by the intake-capture skill (epic vergil-project/.github#176).

Where it lands: ``--repo`` when given; otherwise the current repo's queue home
(:func:`epics.queue_home`) — ``<org>/.github`` for a public repo, the repo
itself for a self-homed private one, so a private repo's captures never leave
it. The issue is linked, as it is created, under the intake epic of the repo it
was filed in (:func:`epics.ensure_intake_epic`). The epic is resolved *before*
the issue is created, so a failure there leaves nothing behind; a failure to
link the created issue exits 1 naming its URL (the orphan sweep would attach it
later, but the operator must know now).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.labels import ensure_labels_or_report
from vergil_tooling.lib.process_errors import error_text

_PROG = "vrg-intake-create"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repo:
        if "/" not in args.repo:
            print(f"{_PROG}: --repo must be owner/name, got {args.repo!r}", file=sys.stderr)
            return 1
        owner = args.repo.split("/", 1)[0]
    else:
        detected = github.detect_org()
        if detected is None:
            print(
                f"{_PROG}: could not determine the GitHub org from this repo's "
                "'origin' remote; run it from inside a repo in the target org, "
                "or pass --repo.",
                file=sys.stderr,
            )
            return 1
        owner = detected
    labels = list(dict.fromkeys(args.label))
    try:
        # Scope the App token to the target org, not whatever the cwd resolves
        # to (mirrors vrg-issue-create, #2070/#2722).
        with github.target_org(owner):
            repo = args.repo or epics.queue_home(github.current_repo())[0]
            intake = epics.ensure_intake_epic(repo)
            # Ensure every extra label exists in the target repo first: gh
            # refuses a label the repo lacks (#3373).
            if labels and not ensure_labels_or_report(_PROG, repo, labels):
                return 1
            url = github.create_issue(
                repo=repo,
                title=args.title,
                body=args.body,
                body_file=args.body_file,
                labels=labels,
                assignees=args.assignee,
            )
            repo_owner, repo_name = repo.split("/", 1)
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            created = epics.IssueRef(owner=repo_owner, repo=repo_name, number=number)
            try:
                epics.add_child(intake, created)
            except Exception as exc:  # noqa: BLE001 - never lose the created issue
                print(
                    f"{_PROG}: created {url} but it is not linked under intake epic "
                    f"{intake.slug}: {error_text(exc)}. Link it with: "
                    f"vrg-epic-move --task {created.slug} --epic {intake.slug}",
                    file=sys.stderr,
                )
                return 1
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    print(f"Created {url} (intake, under {intake.slug}).")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Capture an intake issue, linked under its home's intake epic. Filed "
            "in the current repo's queue home by default (<org>/.github for a "
            "public repo, the repo itself for a private one); use --repo to "
            "override."
        ),
        epilog="intake-review routes the capture into the epic/task model later.",
    )
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument("--label", action="append", default=[], help="Extra label (repeatable)")
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument(
        "--repo", help="Target repo owner/name (defaults to the current repo's queue home)"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
