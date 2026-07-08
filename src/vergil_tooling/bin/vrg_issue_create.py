"""Create a GitHub issue and link it under an epic in one atomic step.

Every issue must be born linked to an epic (a native sub-issue), so this is the
only sanctioned issue-creation path: ``vrg-gh`` denies raw ``gh issue create``
and redirects here. ``--epic`` is required — pass ``--epic adhoc`` (or the
deprecated alias ``standing``) to target the repo's ad-hoc epic in ``.github``,
or an explicit ``owner/repo#N`` / ``#N`` epic ref.

If the issue is created but the link fails, the created issue's URL is reported
so it is never a silent orphan; recover with ``vrg-epic-move``.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Create an issue linked under an epic (native sub-issue)."
    )
    parser.add_argument("--epic", required=True, help="Epic ref: 'adhoc', #N, or owner/repo#N")
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument("--label", action="append", default=[], help="Label (repeatable)")
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    parser.add_argument(
        "--kind",
        choices=("task", "validation", "deployment"),
        default="task",
        help="Issue kind; an operational kind (validation/deployment) stamps its "
        "label and executable scaffold",
    )
    parser.add_argument(
        "--blocked-by",
        action="append",
        default=[],
        metavar="REF",
        help="Dependency ref this operational task is blocked by "
        "(repeatable; validation/deployment only)",
    )
    return parser.parse_args(argv)


def _issue_number_from_url(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


_OPERATIONAL_INTRO = {
    "validation": "Post-merge validation task.",
    "deployment": "Deployment task.",
}


def _render_operational_body(*, kind: str, intro: str, deps: list[epics.IssueRef]) -> str:
    """Build an operational-task body from the *kind*'s scaffold template.

    Each operational kind (validation, deployment) has its own
    ``<kind>_task_body.md`` scaffold — a precondition self-check, the procedure,
    acceptance criteria, and a SUCCESS/FAILURE results template. *deps* are
    rendered as machine-parseable ``Blocked-by:`` reflinks under a Dependencies
    heading (or omitted entirely when there are none).
    """
    template = (
        resources.files("vergil_tooling.data")
        .joinpath(f"{kind}_task_body.md")
        .read_text(encoding="utf-8")
    )
    blocked_by = ""
    if deps:
        blocked_by = "## Dependencies (merge-first)\n\n" + epics.render_blocked_by(deps) + "\n"
    return template.format(intro=intro or _OPERATIONAL_INTRO[kind], blocked_by=blocked_by)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo or github.current_repo()
    owner, name = repo.split("/", 1)
    # Pre-network cross-org guard: the issue and its epic must share an owner so
    # one App-installation token reaches both (#2070). The 'adhoc' sentinel (and
    # its deprecated 'standing' alias) resolves within *repo*'s org (.github), so
    # only an explicit epic ref can diverge.
    if args.epic not in ("standing", "adhoc"):
        try:
            epic_owner = epics.parse_issue_ref(args.epic, default_repo=repo).owner
        except ValueError as exc:
            print(f"vrg-issue-create: {exc}", file=sys.stderr)
            return 1
        if epic_owner != owner:
            print(
                "vrg-issue-create: cross-org operation is out of scope: issue owner "
                f"{owner!r} != epic owner {epic_owner!r}",
                file=sys.stderr,
            )
            return 1

    # Build the issue body/labels. An operational kind (validation/deployment)
    # stamps its executable scaffold + its label and renders its Blocked-by
    # reflinks; a plain task passes the caller's body/labels through unchanged.
    labels = list(args.label)
    body = args.body
    body_file = args.body_file
    if args.kind in ("validation", "deployment"):
        if args.body_file:
            print(
                f"vrg-issue-create: --body-file is not compatible with --kind {args.kind} "
                f"(the {args.kind} scaffold defines the body)",
                file=sys.stderr,
            )
            return 1
        try:
            deps = [epics.parse_issue_ref(ref, default_repo=repo) for ref in args.blocked_by]
        except ValueError as exc:
            print(f"vrg-issue-create: invalid --blocked-by ref: {exc}", file=sys.stderr)
            return 1
        labels.append(args.kind)
        body = _render_operational_body(kind=args.kind, intro=args.body, deps=deps)
        body_file = None

    # Scope every App-token call below to the issue's owner, not the cwd org.
    try:
        with github.target_org(owner):
            try:
                epic = epics.resolve_epic_ref(args.epic, repo=repo)
            except ValueError as exc:
                print(f"vrg-issue-create: {exc}", file=sys.stderr)
                return 1

            url = github.create_issue(
                repo=repo,
                title=args.title,
                body=body,
                body_file=body_file,
                labels=labels,
                assignees=args.assignee,
            )
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
    except github.NoInstallationError as exc:
        print(f"vrg-issue-create: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1

    print(f"Created {url}, linked under epic {epic.slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
