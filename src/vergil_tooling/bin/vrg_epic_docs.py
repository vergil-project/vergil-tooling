"""Write an epic's Documents section: the links to its spec, plan and retrospective.

``backfill`` writes the section on every epic of an epic home: the org's
``.github`` (``--org``) or a private repo that keeps its own epics
(``--repo``). It lists every ``epic``-labelled issue, open and closed, skips
container and ``[archived]`` epics, finds each epic's documents, and decides
every entry that is still a note by the entry rules; a link already in the
section is kept. An epic whose documents are ambiguous is named as needing a
decision and left unchanged. It is a dry run unless ``--apply`` is given, and
exits 1 when any epic needs a decision or failed.

``set`` adds the marked section at the top of the epic's body, or rewrites the
existing one in place. An entry not named on the command line keeps its
current value; a new section starts with all three ``pending``. Hand-written
document lines elsewhere in the body (a ``Documents:`` list, a standalone
``Spec:`` line) are removed. The command does not check that a named file
exists: at handoff it exists only on the PR branch, so the link resolves once
that PR merges. Re-running with the same arguments makes no edit. An issue
without the ``epic`` label is refused.

A ``PATH`` is repo-relative, with no leading ``/``, whitespace or backtick
(the link text is a backticked path); a ``<repo>`` name is letters, digits,
``.``, ``-`` and ``_``, as GitHub allows.

The whole run, reads and writes, happens under the epic owner's App token
(``github.target_org``). See :mod:`vergil_tooling.lib.epic_docs` for the
section's format.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from vergil_tooling.lib import epic_docs, epics, github
from vergil_tooling.lib.cli import cli_main

_PROG = "vrg-epic-docs"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "backfill":
        return _run_backfill(args.repo or f"{args.org}/.github", apply=args.apply)
    try:
        epic = epics.parse_issue_ref(args.epic, default_repo="")
        paths = {doc: _parse_path(getattr(args, doc), epic.owner) for doc in epic_docs.DOCS}
    except ValueError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    try:
        with github.target_org(epic.owner):
            return _set(epic, paths)
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, ValueError) as exc:
        # Every failure that reaches here comes before the write (_set
        # reports a failed write itself): a malformed section, an unreadable
        # API answer, or a failed read or branch lookup.
        print(f"{_PROG}: {epic.slug}: {exc}; nothing was edited", file=sys.stderr)
        return 1


_REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def _parse_path(value: str | None, owner: str) -> tuple[str | None, str] | None:
    """Split a ``--spec``/``--plan``/``--retrospective`` value into (repo, path).

    ``<path>`` is in the epic's home repo (repo ``None``); ``<repo>:<path>`` is
    in another repo of the same org. A value naming the owner,
    ``<owner>/<repo>:<path>``, is refused rather than read as a home path.
    Returns None when the option was not given. Raises ValueError, before any
    API call, for a value it cannot use.
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
        if not _REPO_NAME_RE.fullmatch(repo) or repo in {".", ".."}:
            raise ValueError(
                f"bad repo name {repo!r} in {value!r}: use letters, digits, '.', '-' or '_'"
            )
    elif sep and head.count("/") == 1 and head.split("/")[0].lower() == owner.lower():
        raise ValueError(
            f"{value!r} names the owner: give <repo>:<path> for another repo of the same org"
        )
    if not path or path.startswith("/") or any(c.isspace() or c == "`" for c in path):
        raise ValueError(
            f"bad document path {value!r}: give a repo-relative path without spaces or backticks"
        )
    return repo, path


def _set(epic: epics.IssueRef, paths: dict[str, tuple[str | None, str] | None]) -> int:
    """Rewrite *epic*'s Documents section with the named *paths*.

    *epic* must carry the ``epic`` label, so a mistyped number cannot rewrite
    a task's or a pull request's body. The default branches are looked up
    before the body is read, so the read and the write are as close together
    as they can be.
    """
    if not epics.is_epic(epic):
        print(f"{_PROG}: {epic.slug} is not an epic (missing the 'epic' label)", file=sys.stderr)
        return 1
    branches: dict[str, str] = {}
    for named in paths.values():
        repo = (named[0] or epic.repo) if named is not None else None
        if repo is not None and repo not in branches:
            branches[repo] = epic_docs.default_branch(epic.owner, repo)
    body = epic_docs.read_body(epic.owner, epic.repo, epic.number)
    current = epic_docs.parse(body, epic.repo)
    entries: dict[str, epic_docs.Entry] = (
        dict(current) if current is not None else dict.fromkeys(epic_docs.DOCS, epic_docs.PENDING)
    )
    for doc, named in paths.items():
        if named is not None:
            repo = named[0] or epic.repo
            entries[doc] = epic_docs.DocLink(epic.owner, repo, named[1], branches[repo])
    block = epic_docs.render(entries, epic.repo)
    new_body, removed = epic_docs.upsert(body, block)
    print(block)
    for line in removed:
        print(f"removed legacy line: {line}")
    if new_body == body:
        print(f"{epic.slug}: no change")
        return 0
    try:
        epic_docs.write_body(epic.owner, epic.repo, epic.number, new_body)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        _report_failed_write(epic.slug, exc)
        return 1
    print(f"{epic.slug}: {'changed' if current is not None else 'added'} the Documents section")
    return 0


def _report_failed_write(slug: str, exc: github.GitHubAPIError) -> None:
    """Name a failed body write. The client retries, so the edit may have landed."""
    print(
        f"{_PROG}: {slug}: the edit may not have been applied ({exc}); re-run to confirm",
        file=sys.stderr,
    )


# --- backfill -----------------------------------------------------------------


def _run_backfill(home: str, *, apply: bool) -> int:
    """Backfill every epic of *home* (``owner/repo``) under the owner's App token."""
    owner = home.split("/", 1)[0]
    try:
        with github.target_org(owner):
            return _backfill(home, apply=apply)
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, ValueError) as exc:
        # Per-epic failures are reported inside the loop; what reaches here is
        # the listing or the home's branch lookup, before any epic is edited.
        print(f"{_PROG}: {home}: {exc}; nothing was edited", file=sys.stderr)
        return 1


@dataclass
class _Tally:
    """The backfill's per-outcome counts."""

    add: int = 0
    change: int = 0
    no_change: int = 0
    decision: int = 0
    failed: int = 0

    def summary(self, *, apply: bool) -> str:
        total = self.add + self.change + self.no_change + self.decision + self.failed
        verbs = ("added", "changed") if apply else ("to add", "to change")
        return (
            f"{total} epics: {self.add} {verbs[0]}, {self.change} {verbs[1]}, "
            f"{self.no_change} no change, {self.decision} need a decision, "
            f"{self.failed} failed"
        )


def _backfill(home: str, *, apply: bool) -> int:
    owner, repo = home.split("/", 1)
    records = epic_docs.list_epics(home)
    branch = epic_docs.default_branch(owner, repo)
    dirs = epic_docs.epic_directories(owner, repo, branch)
    branches = {repo: branch}
    tally = _Tally()
    for record in records:
        slug = f"{home}#{record['number']}"
        try:
            outcome = _backfill_one(record, owner, repo, branch, dirs, branches, apply=apply)
        except epic_docs.Ambiguous as exc:
            print(f"{slug}: needs a decision: {exc}; left unchanged (settle it with set)")
            tally.decision += 1
            continue
        except github.MissingGitHubTokenError:
            raise
        except (github.GitHubAPIError, ValueError) as exc:
            print(f"{_PROG}: {slug}: {exc}; left unchanged", file=sys.stderr)
            tally.failed += 1
            continue
        setattr(tally, outcome, getattr(tally, outcome) + 1)
    print(tally.summary(apply=apply))
    return 1 if tally.decision or tally.failed else 0


def _backfill_one(  # noqa: PLR0913 - the run's shared lookups, passed rather than re-read
    record: epic_docs.EpicRecord,
    owner: str,
    repo: str,
    branch: str,
    dirs: frozenset[str],
    branches: dict[str, str],
    *,
    apply: bool,
) -> str:
    """Decide, print and (with *apply*) write one epic's section; return its tally field.

    Under *apply* the body is read again just before the write, so an edit
    made since the listing is not lost. Discovery runs on the body as it is,
    before legacy lines are removed. Raises :class:`epic_docs.Ambiguous`, or
    an API or format error, before anything is written; a failed write is
    reported here and counted as ``failed``.
    """
    number = record["number"]
    slug = f"{owner}/{repo}#{number}"
    body = epic_docs.read_body(owner, repo, number) if apply else (record["body"] or "")
    current = epic_docs.parse(body, repo)
    links = {
        doc: entry for doc, entry in (current or {}).items() if isinstance(entry, epic_docs.DocLink)
    }
    wanted = [doc for doc in epic_docs.DOCS if doc not in links]
    found = epic_docs.discover(
        owner, repo, number, body, branch, epic_dirs=dirs, branches=branches, wanted=wanted
    )
    entries: dict[str, epic_docs.Entry] = {
        doc: links.get(doc)
        or epic_docs.entry_for(
            doc,
            found[doc],
            state=record["state"],
            state_reason=record["stateReason"],
            created_at=record["createdAt"],
        )
        for doc in epic_docs.DOCS
    }
    block = epic_docs.render(entries, repo)
    new_body, removed = epic_docs.upsert(body, block)
    outcome = "no_change" if new_body == body else ("add" if current is None else "change")
    print(f"{slug}: {outcome.replace('_', ' ')}")
    print(block)
    for line in removed:
        print(f"removed legacy line: {line}")
    if not apply or outcome == "no_change":
        return outcome
    try:
        epic_docs.write_body(owner, repo, number, new_body)
    except github.GitHubAPIError as exc:
        if isinstance(exc, github.MissingGitHubTokenError):
            raise
        _report_failed_write(slug, exc)
        return "failed"
    print(f"{slug}: {'added' if outcome == 'add' else 'changed'} the Documents section")
    return outcome


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
    backfill = sub.add_parser(
        "backfill",
        help="write the Documents section on every epic of an epic home",
        description="List every epic of the home, open and closed, skipping container "
        "and [archived] epics; find each epic's documents and decide every entry that "
        "is still a note; keep existing links; remove hand-written document lines. An "
        "ambiguous epic is named and left unchanged. A dry run unless --apply is given. "
        "Exits 1 when any epic needs a decision or failed.",
    )
    home = backfill.add_mutually_exclusive_group(required=True)
    home.add_argument("--org", type=_org_name, help="the org whose .github holds the epics")
    home.add_argument(
        "--repo", type=_home_repo, help="OWNER/REPO: a private repo that keeps its own epics"
    )
    backfill.add_argument("--apply", action="store_true", help="make the edits")
    return parser.parse_args(argv)


_ORG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")


def _org_name(value: str) -> str:
    if not _ORG_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"bad org name {value!r}")
    return value


def _home_repo(value: str) -> str:
    owner, sep, repo = value.partition("/")
    if not sep or not _ORG_RE.fullmatch(owner) or not _REPO_NAME_RE.fullmatch(repo):
        raise argparse.ArgumentTypeError(f"bad repo {value!r}: give OWNER/REPO")
    if repo in {".", ".."}:
        raise argparse.ArgumentTypeError(f"bad repo {value!r}: give OWNER/REPO")
    return value


if __name__ == "__main__":
    sys.exit(main())
