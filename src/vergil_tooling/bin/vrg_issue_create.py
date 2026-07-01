"""Create a GitHub issue and link it under an epic in one atomic step.

Every issue must be born linked to an epic (a native sub-issue), so this is the
only sanctioned issue-creation path: ``vrg-gh`` denies raw ``gh issue create``
and redirects here. ``--epic`` is required — pass ``--epic standing`` to target
the repo's standing epic, or an explicit ``owner/repo#N`` / ``#N`` epic ref.

If the issue is created but the link fails, the created issue's URL is reported
so it is never a silent orphan; recover with ``vrg-epic-move``.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Create an issue linked under an epic (native sub-issue)."
    )
    parser.add_argument("--epic", required=True, help="Epic ref: 'standing', #N, or owner/repo#N")
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument("--label", action="append", default=[], help="Label (repeatable)")
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    return parser.parse_args(argv)


def _issue_number_from_url(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo or github.current_repo()
    try:
        epic = epics.resolve_epic_ref(args.epic, repo=repo)
    except ValueError as exc:
        print(f"vrg-issue-create: {exc}", file=sys.stderr)
        return 1

    url = github.create_issue(
        repo=repo,
        title=args.title,
        body=args.body,
        body_file=args.body_file,
        labels=args.label,
        assignees=args.assignee,
    )
    owner, name = repo.split("/", 1)
    task = epics.IssueRef(owner=owner, repo=name, number=_issue_number_from_url(url))

    try:
        epics.add_child(epic, task)
    except Exception as exc:  # noqa: BLE001 - orphan-safe: never lose the created issue
        print(
            f"vrg-issue-create: created {url} but failed to link it under epic "
            f"{epic.slug}: {exc}. Link it with: vrg-epic-move --task #{task.number} "
            f"--epic {epic.slug}",
            file=sys.stderr,
        )
        return 1

    print(f"Created {url}, linked under epic {epic.slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
