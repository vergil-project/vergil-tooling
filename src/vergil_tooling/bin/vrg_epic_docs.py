"""Write an epic's Documents section: the links to its spec, plan and retrospective.

``set`` adds the marked section at the top of the epic's body, or rewrites the
existing one in place. An entry not named on the command line keeps its
current value; a new section starts with all three ``pending``. Hand-written
document lines elsewhere in the body (a ``Documents:`` list, a standalone
``Spec:`` line) are removed. The command does not check that a named file
exists: at handoff it exists only on the PR branch, so the link resolves once
that PR merges. Re-running with the same arguments makes no edit.

The whole run, reads and writes, happens under the epic owner's App token
(``github.target_org``). See :mod:`vergil_tooling.lib.epic_docs` for the
section's format.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epic_docs, epics, github
from vergil_tooling.lib.cli import cli_main

_PROG = "vrg-epic-docs"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        epic = epics.parse_issue_ref(args.epic, default_repo="")
        paths = {doc: _parse_path(getattr(args, doc)) for doc in epic_docs.DOCS}
    except ValueError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    try:
        with github.target_org(epic.owner):
            return _set(epic, paths)
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    except epic_docs.EpicDocsFormatError as exc:
        print(f"{_PROG}: {epic.slug}: {exc}; nothing was edited", file=sys.stderr)
        return 1


def _parse_path(value: str | None) -> tuple[str | None, str] | None:
    """Split a ``--spec``/``--plan``/``--retrospective`` value into (repo, path).

    ``<path>`` is in the epic's home repo (repo ``None``); ``<repo>:<path>`` is
    in another repo of the same org. Returns None when the option was not given.
    """
    if value is None:
        return None
    repo: str | None = None
    path = value
    head, sep, tail = value.partition(":")
    if sep and "/" not in head:
        repo, path = head, tail
        if not repo:
            raise ValueError(f"empty repo name in {value!r}; use <repo>:<path> or <path>")
    if not path or path.startswith("/") or any(c.isspace() or c == "`" for c in path):
        raise ValueError(
            f"bad document path {value!r}: give a repo-relative path without spaces or backticks"
        )
    return repo, path


def _set(epic: epics.IssueRef, paths: dict[str, tuple[str | None, str] | None]) -> int:
    """Rewrite *epic*'s Documents section with the named *paths*."""
    body = epic_docs.read_body(epic.owner, epic.repo, epic.number)
    current = epic_docs.parse(body)
    entries: dict[str, epic_docs.Entry] = (
        dict(current) if current is not None else dict.fromkeys(epic_docs.DOCS, epic_docs.PENDING)
    )
    branches: dict[str, str] = {}
    for doc, named in paths.items():
        if named is None:
            continue
        repo = named[0] or epic.repo
        if repo not in branches:
            branches[repo] = epic_docs.default_branch(epic.owner, repo)
        entries[doc] = epic_docs.DocLink(epic.owner, repo, named[1], branches[repo])
    block = epic_docs.render(entries, epic.repo)
    new_body, removed = epic_docs.upsert(body, block)
    print(block)
    for line in removed:
        print(f"removed legacy line: {line}")
    if new_body == body:
        print(f"{epic.slug}: no change")
        return 0
    epic_docs.write_body(epic.owner, epic.repo, epic.number, new_body)
    print(f"{epic.slug}: {'changed' if current is not None else 'added'} the Documents section")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Write an epic's Documents section (links to its spec, plan and "
        "retrospective).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser(
        "set",
        help="add or update one epic's Documents section",
        description="Add the Documents section at the top of the epic's body, or "
        "update it in place. Entries not named keep their current value; a new "
        "section starts all 'pending'. The named files are not checked for "
        "existence. No edit is made when the body is already correct.",
    )
    set_parser.add_argument("--epic", required=True, help="Epic ref, fully qualified: owner/repo#N")
    for doc in epic_docs.DOCS:
        set_parser.add_argument(
            f"--{doc}",
            metavar="PATH",
            help=f"the {doc} document: a path in the epic's home repo, or "
            "<repo>:<path> for another repo of the same org",
        )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
