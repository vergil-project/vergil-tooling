"""Mechanics behind ``vrg-upstream-refresh`` (epic vergil-project/.github#472, spec §5.6).

The repo supplies the logic in its ``[upstream-refresh].hook``; this module
supplies the mechanics: run the hook on a clean checkout of the base branch,
then keep exactly one refresh PR in step with what the hook produced.

- **No change** (per git): close any open refresh PR and delete its branch, or
  do nothing at all when there is none.
- **A change:** commit it on top of the base (never moving ``HEAD``), push it to
  the single ``upstream-refresh`` branch with ``--force-with-lease`` only when
  the tree differs from what is already there, and open or refresh one PR.
  Superseded refresh PRs are closed.

It never merges, and never runs dependency updaters, validation or a release:
the PR's own CI does the checking and a human merges. It works from a shallow
(depth-1) checkout, as the reusable ops workflow provides: every ref it needs is
fetched explicitly, at depth 1 when the repository is shallow.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git, github

REFRESH_BRANCH = "upstream-refresh"
REPORT_ENV = "VRG_UPSTREAM_REPORT"
COMMIT_SUBJECT = "chore(upstream): refresh upstream inputs"

_BODY_INTRO = "Automated upstream refresh. A human reviews and merges; nothing auto-merges."


class UpstreamRefreshError(Exception):
    """A refusal or failure of the upstream refresh; the message says why."""


@dataclass(frozen=True)
class RefreshPr:
    """An open refresh PR: its number, URL and head branch."""

    number: int
    url: str
    head: str


# -- git helpers ---------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    return git.read_output("-C", str(repo_root), *args)


def _depth_args(repo_root: Path) -> list[str]:
    """``--depth=1`` for a shallow repository, so a fetch never deepens it.

    A plain fetch into a depth-1 clone can pull the fetched ref's whole history;
    a full clone is left full (``--depth`` would make it shallow).
    """
    shallow = _git(repo_root, "rev-parse", "--is-shallow-repository") == "true"
    return ["--depth=1"] if shallow else []


def _fetch_branch(repo_root: Path, branch: str) -> None:
    _git(
        repo_root,
        "fetch",
        "--quiet",
        *_depth_args(repo_root),
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )


def _remote_sha(repo_root: Path, branch: str) -> str | None:
    """The tip of *branch* on origin, or None when the branch does not exist."""
    out = _git(repo_root, "ls-remote", "origin", f"refs/heads/{branch}")
    return out.split()[0] if out else None


def _tree(repo_root: Path, rev: str) -> str:
    return _git(repo_root, "rev-parse", f"{rev}^{{tree}}")


def ensure_clean_on_base(repo_root: Path, base: str) -> str:
    """Refuse unless the checkout is clean and ``HEAD`` is ``origin/<base>``; return its SHA."""
    _fetch_branch(repo_root, base)
    if _git(repo_root, "status", "--porcelain", "--untracked-files=all"):
        msg = "the working tree is not clean; run vrg-upstream-refresh on a clean checkout"
        raise UpstreamRefreshError(msg)
    head = _git(repo_root, "rev-parse", "HEAD")
    base_sha = _git(repo_root, "rev-parse", f"refs/remotes/origin/{base}")
    if head != base_sha:
        msg = (
            f"HEAD ({head[:12]}) is not origin/{base} ({base_sha[:12]}); run "
            f"vrg-upstream-refresh on a clean checkout of {base}"
        )
        raise UpstreamRefreshError(msg)
    return base_sha


# -- hook ----------------------------------------------------------------------


def run_hook(repo_root: Path, hook: str, report: Path) -> list[str]:
    """Run *hook* with ``bash -c`` from *repo_root*; return the report's notes.

    The report path is passed in ``$VRG_UPSTREAM_REPORT``. The notes are its
    non-empty lines (stripped); a hook that writes no report has none.
    """
    env = {**os.environ, REPORT_ENV: str(report)}
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", hook],  # noqa: S607
        cwd=repo_root,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        msg = f"[upstream-refresh].hook failed (exit {result.returncode}): {hook}"
        raise UpstreamRefreshError(msg)
    if not report.exists():
        return []
    return [line.strip() for line in report.read_text().splitlines() if line.strip()]


def changed_paths(repo_root: Path) -> list[str]:
    """The tracked paths the hook changed, sorted; untracked files are refused.

    The hook contract is "edit tracked files only": a file it leaves untracked
    is never committed silently. Ignored files are not reported.
    """
    untracked = _nul_list(_git(repo_root, "ls-files", "-z", "--others", "--exclude-standard"))
    if untracked:
        msg = f"hook left untracked files: {', '.join(sorted(untracked))}"
        raise UpstreamRefreshError(msg)
    # Against HEAD, so staged and unstaged edits both count; --no-renames lists
    # both sides of a rename (the old path is a deletion to commit).
    return sorted(
        set(_nul_list(_git(repo_root, "diff", "--name-only", "--no-renames", "-z", "HEAD")))
    )


def _nul_list(raw: str) -> list[str]:
    return [item for item in raw.split("\0") if item]


# -- PR body -------------------------------------------------------------------


def render_body(notes: list[str], paths: list[str]) -> str:
    """The refresh PR body: intro, changed files, and the hook's notes (if any)."""
    lines = [_BODY_INTRO, "", "## Changed files", ""]
    lines.extend(f"- `{path}`" for path in paths)
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


# -- GitHub --------------------------------------------------------------------


def _is_refresh_head(head: str) -> bool:
    """True for the refresh branch, or a manual re-create of it (``upstream-refresh-*``)."""
    return head == REFRESH_BRANCH or head.startswith(f"{REFRESH_BRANCH}-")


def open_refresh_prs(repo: str, base: str) -> list[RefreshPr]:
    """The open refresh PRs against *base*, oldest first."""
    result = github.read_json(
        "pr",
        "list",
        "--repo",
        repo,
        "--base",
        base,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,url,headRefName",
    )
    if not isinstance(result, list):
        msg = f"gh pr list returned unexpected JSON: {result!r}"
        raise UpstreamRefreshError(msg)
    prs: list[RefreshPr] = []
    for item in result:
        if not isinstance(item, dict):
            msg = f"gh pr list returned unexpected JSON: {item!r}"
            raise UpstreamRefreshError(msg)
        head = str(item.get("headRefName") or "")
        if _is_refresh_head(head):
            prs.append(RefreshPr(int(str(item["number"])), str(item["url"]), head))
    return sorted(prs, key=lambda pr: pr.number)


def _close(repo: str, pr: RefreshPr, comment: str, *, delete_branch: bool) -> None:
    # Not `gh pr close --delete-branch`: gh also deletes the *local* branch and
    # may switch off it first. The remote branch is deleted through the API
    # instead, and only when no surviving PR still uses it (#3332).
    github.run("pr", "close", str(pr.number), "--repo", repo, "--comment", comment)
    print(f"Closed {pr.url}.")
    if delete_branch:
        github.delete_if_exists(f"repos/{repo}/git/refs/heads/{pr.head}")
        print(f"Deleted branch {pr.head}.")


def _open_pr(repo: str, base: str, branch: str, body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        return github.read_output(
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            COMMIT_SUBJECT,
            "--body-file",
            str(body_path),
        )
    finally:
        body_path.unlink(missing_ok=True)


# -- commit and push -------------------------------------------------------------


def _commit(repo_root: Path, paths: list[str]) -> str:
    """Commit *paths* on top of ``HEAD`` (the base) without moving ``HEAD``; return the SHA.

    Plumbing (``write-tree`` + ``commit-tree``) builds a commit detached from
    any branch, so no local branch moves and no commit hook runs. The index is
    reset afterwards, leaving the hook's edits in the working tree.
    """
    _git(repo_root, "add", "-A", "--", *paths)
    try:
        tree = _git(repo_root, "write-tree")
        return _git(repo_root, "commit-tree", tree, "-p", "HEAD", "-m", COMMIT_SUBJECT)
    finally:
        _git(repo_root, "reset", "--quiet")


def _push(repo_root: Path, sha: str, branch: str, expected: str | None) -> None:
    _git(
        repo_root,
        "push",
        "--quiet",
        f"--force-with-lease=refs/heads/{branch}:{expected or ''}",
        "origin",
        f"{sha}:refs/heads/{branch}",
    )
    print(f"Pushed {sha[:12]} to {branch}.")


# -- the decision ----------------------------------------------------------------


def sync(repo: str, base: str, repo_root: Path, notes: list[str], paths: list[str]) -> str:
    """Bring the refresh PR in line with the hook's result; return the outcome.

    One of ``"nothing"`` (no change, no PR), ``"closed"`` (no change, stale PRs
    closed), ``"opened"``, ``"refreshed"`` (new content force-pushed) or
    ``"unchanged"`` (the open PR already carries this exact tree).
    """
    prs = open_refresh_prs(repo, base)
    if not paths:
        for pr in prs:
            _close(repo, pr, f"Nothing to refresh on {base}; closing.", delete_branch=True)
        return "closed" if prs else "nothing"

    survivor = prs[0] if prs else None
    branch = survivor.head if survivor is not None else REFRESH_BRANCH
    remote = _remote_sha(repo_root, branch)
    sha = _commit(repo_root, paths)
    body = render_body(notes, paths)

    if survivor is None:
        _push(repo_root, sha, branch, remote)
        url = _open_pr(repo, base, branch, body)
        print(f"Opened {url}.")
        outcome = "opened"
    else:
        same = False
        if remote is not None:
            _fetch_branch(repo_root, branch)
            same = _tree(repo_root, remote) == _tree(repo_root, sha)
        if same:
            print(f"{survivor.url} already carries this refresh; nothing pushed.")
            outcome = "unchanged"
        else:
            _push(repo_root, sha, branch, remote)
            github.edit_pr_body(survivor.url, body=body)
            github.run(
                "pr",
                "comment",
                survivor.url,
                "--body",
                f"Refreshed: re-ran the upstream refresh on top of `{base}`.",
            )
            print(f"Refreshed {survivor.url}.")
            outcome = "refreshed"

    for pr in prs[1:]:
        _close(
            repo,
            pr,
            f"Superseded by #{prs[0].number}, which carries the current upstream refresh. "
            "Only one refresh PR is kept open.",
            delete_branch=pr.head != branch,
        )
    return outcome
