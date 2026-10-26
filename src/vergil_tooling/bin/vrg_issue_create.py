"""Create a GitHub issue and link it under an epic in one atomic step.

Every issue must be born linked to an epic (a native sub-issue), so this is the
only sanctioned issue-creation path: ``vrg-gh`` denies raw ``gh issue create``
and redirects here. ``--epic`` is required — pass ``--epic adhoc`` to target the
target repo's ad-hoc epic, ``--epic intake`` / ``--epic planning`` for its intake
or planning queue epic, or an explicit ``owner/repo#N`` / ``#N`` epic ref.

The target repo is ``--repo`` (default: the current repo). The sentinels and a
bare ``#N`` are all read against it, never the cwd repo: ``#N`` is issue N in the
target repo's epic home (``<org>/.github`` for a public repo, the repo itself
for a self-homed private one). A fully qualified ``owner/repo#N`` is taken as
written (#3409, #3411).

If the issue is created but the link fails, the created issue's URL is reported
so it is never a silent orphan; recover with ``vrg-epic-move``.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.labels import ensure_labels_or_report
from vergil_tooling.lib.process_errors import error_text


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo or github.current_repo()
    owner, name = repo.split("/", 1)
    # The deferred queue is entered only through vrg-intake-route, which records
    # the deferral (#3673). Refused before resolving: resolving the sentinel
    # would create the epic.
    if args.epic == epics.DEFERRED.sentinel:
        print(
            f"vrg-issue-create: refusing --epic {args.epic}: {epics.DEFERRED_ROUTE_ONLY}",
            file=sys.stderr,
        )
        return 1
    # Pre-network cross-org guard: the issue and its epic must share an owner so
    # one App-installation token reaches both (#2070). The standing sentinels
    # (adhoc/intake/planning) and a bare '#N' all resolve within *repo*'s epic
    # or queue home (in *repo*'s org), so only a qualified epic ref can diverge
    # (#3514).
    if args.epic not in epics.EPIC_SENTINELS:
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

    # Build the issue body/labels. A scaffolded kind stamps its label + template
    # body; an operational kind (validation/deployment) also renders its
    # Blocked-by reflinks, while retrospective is a plain PR-workable task that
    # merely carries a scaffold. A plain task passes the caller's body/labels
    # through unchanged.
    labels = list(args.label)
    body = args.body
    body_file = args.body_file
    deps: list[epics.IssueRef] = []
    if args.kind == "task" and args.blocked_by:
        # A plain task carries its dependencies as the same portable Blocked-by:
        # reflinks the operational scaffolds use, appended to the caller's body
        # (#2269). The dependency machinery (epics.blockers_of) is already
        # generic; this is purely unlocking the creation edge for plain tasks.
        try:
            deps = [epics.parse_issue_ref(ref, default_repo=repo) for ref in args.blocked_by]
        except ValueError as exc:
            print(f"vrg-issue-create: invalid --blocked-by ref: {exc}", file=sys.stderr)
            return 1
        if body_file is not None:
            try:
                base = Path(body_file).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"vrg-issue-create: cannot read --body-file: {exc}", file=sys.stderr)
                return 1
        else:
            base = body
        body = _append_blocked_by(base, deps)
        body_file = None
    elif args.kind != "task":
        if args.body_file:
            print(
                f"vrg-issue-create: --body-file is not compatible with --kind {args.kind} "
                f"(the {args.kind} scaffold defines the body)",
                file=sys.stderr,
            )
            return 1
        if args.kind in _OPERATIONAL_KINDS:
            try:
                deps = [epics.parse_issue_ref(ref, default_repo=repo) for ref in args.blocked_by]
            except ValueError as exc:
                print(f"vrg-issue-create: invalid --blocked-by ref: {exc}", file=sys.stderr)
                return 1
        elif args.blocked_by:
            # --blocked-by encodes an operational merge-first dependency; a
            # retrospective is an ordinary PR-workable task and takes none.
            print(
                f"vrg-issue-create: --blocked-by is only valid for an operational kind "
                f"(validation/deployment), not --kind {args.kind}",
                file=sys.stderr,
            )
            return 1
        labels.append(args.kind)
        body = _render_scaffold_body(kind=args.kind, intro=args.body, deps=deps)
        body_file = None

    # Scope every App-token call below to the issue's owner, not the cwd org.
    try:
        with github.target_org(owner):
            try:
                epic = epics.resolve_epic_ref(args.epic, repo=repo)
            except ValueError as exc:
                print(f"vrg-issue-create: {exc}", file=sys.stderr)
                return 1
            if epics.queue_kind(epic) == epics.DEFERRED.key:
                print(
                    f"vrg-issue-create: refusing to create an issue under deferred epic "
                    f"{epic.slug}: {epics.DEFERRED_ROUTE_ONLY}",
                    file=sys.stderr,
                )
                return 1

            # Dependency guard (#2842): a Blocked-by ref names the task this one
            # waits on, never an epic or a standing epic's archive. Checked
            # before the issue is created; a failed label read propagates.
            for dep in deps:
                kind = epics.non_task_kind(dep)
                if kind is not None:
                    message = epics.non_task_refusal_message("--blocked-by", dep.slug, kind)
                    print(f"vrg-issue-create: {message}", file=sys.stderr)
                    return 1

            # Ensure every applied label exists in the target repo first: gh
            # refuses a label the repo lacks ('validation' not found) (#3373).
            if not ensure_labels_or_report("vrg-issue-create", repo, labels):
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
                    f"{epic.slug}: {error_text(exc)}. Link it with: "
                    f"vrg-epic-move --task #{task.number} --epic {epic.slug}",
                    file=sys.stderr,
                )
                return 1
    except github.NoInstallationError as exc:
        print(f"vrg-issue-create: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1

    print(f"Created {url}, linked under epic {epic.slug}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Create an issue linked under an epic (native sub-issue)."
    )
    parser.add_argument(
        "--epic",
        required=True,
        help="Epic ref: 'adhoc' (the target repo's ad-hoc epic), 'intake' or "
        "'planning' (the target repo's intake or planning queue epic), #N (issue N in the "
        "target repo's epic home: <org>/.github for a public repo, the repo itself "
        "for a self-homed private one), or owner/repo#N (taken as written)",
    )
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument("--label", action="append", default=[], help="Label (repeatable)")
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    parser.add_argument(
        "--kind",
        choices=("task", "validation", "deployment", "retrospective"),
        default="task",
        help="Issue kind; validation/deployment stamp an operational "
        "(not-PR-workable) label + scaffold, retrospective stamps a PR-workable "
        "epic-retrospective label + scaffold",
    )
    parser.add_argument(
        "--blocked-by",
        action="append",
        default=[],
        metavar="REF",
        help="Dependency ref this task is blocked by; stamps a portable "
        "Blocked-by: reflink (repeatable; not valid for --kind retrospective)",
    )
    return parser.parse_args(argv)


def _issue_number_from_url(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


# The operational kinds — a not-PR-workable task run to completion and recorded
# as a comment. Kept in sync with ``epics._OPERATIONAL_LABELS``. ``retrospective``
# is deliberately NOT here: it is an ordinary PR-workable task whose docs PR
# publishes ``retrospective.md``, so PR tooling must accept it.
_OPERATIONAL_KINDS = ("validation", "deployment")

_SCAFFOLD_INTRO = {
    "validation": "Post-merge validation task.",
    "deployment": "Deployment task.",
    "retrospective": "Epic retrospective task.",
}


def _render_scaffold_body(*, kind: str, intro: str, deps: list[epics.IssueRef]) -> str:
    """Build a scaffolded issue body from the *kind*'s template.

    Each scaffolded kind has its own ``<kind>_task_body.md`` template. The
    operational kinds (validation, deployment) render a precondition self-check,
    the procedure, acceptance criteria, and a SUCCESS/FAILURE results template;
    ``retrospective`` renders the backward-looking epic-retrospective outline.
    *deps* (operational only) are rendered as machine-parseable ``Blocked-by:``
    reflinks under a Dependencies heading (or omitted entirely when there are
    none); a template without a ``{blocked_by}`` field simply ignores them.
    """
    template = (
        resources.files("vergil_tooling.data")
        .joinpath(f"{kind}_task_body.md")
        .read_text(encoding="utf-8")
    )
    blocked_by = ""
    if deps:
        blocked_by = "## Dependencies (merge-first)\n\n" + epics.render_blocked_by(deps) + "\n"
    return template.format(intro=intro or _SCAFFOLD_INTRO[kind], blocked_by=blocked_by)


def _append_blocked_by(base: str, deps: list[epics.IssueRef]) -> str:
    """Append a machine-parseable ``Blocked-by:`` reflink section to *base*.

    A plain task has no scaffold template, so its dependencies are stamped by
    appending the same portable ``Blocked-by: owner/repo#N`` reflinks the
    operational scaffolds use (``epics.render_blocked_by``) under a Dependencies
    heading. The reflink is what ``epics.blockers_of`` parses, so a plain task's
    dependency becomes as machine-readable as an operational one (#2269).
    """
    section = "## Dependencies\n\n" + epics.render_blocked_by(deps)
    return f"{base.rstrip()}\n\n{section}" if base.strip() else section


if __name__ == "__main__":
    sys.exit(main())
