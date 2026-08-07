"""Discover and select canonical ``.worktrees/`` worktrees.

Single home for worktree-convention logic: enumeration of worktrees
under the canonical ``.worktrees/`` container, branch lookup, and
interactive selection. Worktrees elsewhere (developer-managed,
outside the convention) are deliberately ignored — auto-acting on
them would surprise the user. Issue #315.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from vergil_tooling.lib import git, github
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.repo_init import prompt_choice, prompt_multi_choice


@dataclass(frozen=True)
class Worktree:
    """A canonical worktree and the branch it has checked out."""

    path: Path
    branch: str


@dataclass(frozen=True)
class RawWorktree:
    """A worktree under the canonical container exactly as git reports it.

    Unlike ``Worktree``, this keeps **detached-HEAD** worktrees — ``branch`` is
    ``None`` when no branch is checked out, the normal state during a paused
    rebase. Discovery retains them so a status view can surface an in-flight
    worktree rather than dropping it and reporting "none" (issue #2634).
    """

    path: Path
    branch: str | None


class WorktreeState(StrEnum):
    """Lifecycle state of a canonical worktree, derived from PR + local signals."""

    OPEN_PR = "open-pr"
    REBASING = "rebasing"
    DETACHED = "detached"
    NO_PR = "no-pr"
    DRAFT = "draft"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorktreeStatus:
    """A worktree's derived lifecycle state and the signals behind it."""

    worktree: Worktree
    state: WorktreeState
    pr_number: int | None
    ahead: int
    dirty: bool
    detail: str | None = None
    # True when the PR is merged/closed but the worktree is *not* removable —
    # a finished-but-stuck worktree (dirty tree, or a reused branch name that
    # withheld the merged verdict, issue #1719). These are neither live work
    # nor removable cruft: they need a human's attention. Set by
    # classify_worktree; defaulted so callers that build a WorktreeStatus by
    # hand are unaffected. See _summary in vrg_worktree_status.
    needs_attention: bool = False
    # Local pr-workflow handoff signals (.vergil/pr-workflow.json). Defaulted so
    # classify_worktree and the finalize sweep, which do not gather them, are
    # unaffected; gather_worktree_status attaches them. See _probe_pr_workflow.
    workflow_status: str | None = None
    workflow_error: str | None = None
    pr_prepared: bool = False
    # Freshness signals (epoch seconds), attached by gather_worktree_status
    # only when called with with_freshness=True (vrg-worktree-status). Left
    # None for the finalize sweep, which neither needs nor pays for them.
    last_commit_ts: float | None = None
    last_modified_ts: float | None = None

    @property
    def removable(self) -> bool:
        """True when the worktree is finished cruft safe to delete.

        Merged or closed PRs are removable — unless the tree is dirty,
        in which case there is uncommitted work to rescue first.
        """
        return self.state in (WorktreeState.MERGED, WorktreeState.CLOSED) and not self.dirty


def classify_worktree(
    worktree: Worktree,
    *,
    pr_number: int | None,
    pr_state: str | None,
    pr_lookup_failed: bool,
    ahead: int,
    dirty: bool,
    detail: str | None = None,
    merged_head_matches_tip: bool = True,
) -> WorktreeStatus:
    """Map already-gathered signals to a WorktreeStatus. Pure: no I/O.

    A failed PR lookup yields UNKNOWN with *detail* — never a silent
    downgrade to NO_PR, which would mislabel real work as stalled.

    ``merged_head_matches_tip`` guards against a reused branch name
    (issue #1719). A closed/merged PR is matched to a branch by *name*,
    but a name reused after that PR merged points at an entirely new tip
    whose commits were never merged. When the merged PR's head no longer
    equals the branch tip, the MERGED/CLOSED verdict is dropped and the
    branch is classified by its local commits (NO_PR / DRAFT) — never
    removable — so the straggler sweep cannot delete unmerged work.

    A merged/closed PR whose worktree is *not* removable — the tree is
    dirty, or the branch name was reused — is flagged ``needs_attention``.
    Such a worktree is finished-but-stuck: not live work, but not safe to
    delete either. The flag keeps ``_summary`` from miscounting it as
    ``active`` and hiding it behind a "0 cruft" message (issue #2347).
    """
    if pr_lookup_failed:
        state = WorktreeState.UNKNOWN
    elif pr_state == "OPEN":
        state = WorktreeState.OPEN_PR
    elif pr_state in ("MERGED", "CLOSED") and not merged_head_matches_tip:
        # Name-matched a merged/closed PR, but the branch tip has moved
        # past it — the name was reused. Treat the current tip as unmerged
        # work: stalled if it has commits, an empty draft otherwise.
        state = WorktreeState.NO_PR if ahead > 0 else WorktreeState.DRAFT
    elif pr_state == "MERGED":
        state = WorktreeState.MERGED
    elif pr_state == "CLOSED":
        state = WorktreeState.CLOSED
    elif ahead > 0:
        state = WorktreeState.NO_PR
    else:
        state = WorktreeState.DRAFT
    # Finished (PR merged/closed) but not removable → needs attention: either
    # the tree is dirty, or the merged verdict was withheld for a reused
    # branch name. Removable cruft (merged/closed + clean + matching tip) and
    # live work never qualify.
    needs_attention = pr_state in ("MERGED", "CLOSED") and (dirty or not merged_head_matches_tip)
    return WorktreeStatus(
        worktree=worktree,
        state=state,
        pr_number=pr_number,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        needs_attention=needs_attention,
    )


def _resolve_pr_state(branch: str) -> tuple[int | None, str | None, str | None]:
    """Resolve ``(pr_number, pr_state, pr_head_sha)`` for *branch*.

    An open PR wins; otherwise the most recent closed/merged PR (whose
    ``MERGED`` vs ``CLOSED`` state is read explicitly); otherwise
    ``(None, None, None)`` for a branch with no PR.

    ``pr_head_sha`` is the closed/merged PR's head commit, used to confirm
    the PR actually corresponds to the branch's current tip rather than a
    same-named PR that merged before the name was reused (issue #1719). It
    is ``None`` for an open PR, where the name match is authoritative.
    """
    open_pr = github.pr_for_branch(branch)
    if open_pr is not None:
        return int(open_pr["number"]), "OPEN", None
    closed = github.closed_pr_for_branch(branch)
    if closed is not None:
        return (
            int(closed["number"]),
            github.pr_state(closed["number"]),
            closed.get("headRefOid") or None,
        )
    return None, None, None


def _newest_mtime(path: Path) -> float | None:
    """Return the newest mtime across *path*'s tracked + untracked files.

    The file set is ``git ls-files`` (tracked) ∪ ``ls-files --others
    --exclude-standard`` (untracked, not gitignored), so ``.gitignore`` is
    honored and ``.venv`` / ``node_modules`` / build artifacts are skipped.
    A file listed but gone by the time it is stat'd (a benign race) is
    skipped, not an error. Returns ``None`` when no eligible files exist.
    """
    tracked = git.read_output("-C", str(path), "ls-files")
    untracked = git.read_output("-C", str(path), "ls-files", "--others", "--exclude-standard")
    names = [n for n in (*tracked.splitlines(), *untracked.splitlines()) if n]
    newest: float | None = None
    for name in names:
        try:
            mtime = (path / name).stat().st_mtime
        except FileNotFoundError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _probe_pr_workflow(worktree: Worktree) -> tuple[str | None, str | None, bool]:
    """Read the worktree's local ``.vergil/pr-workflow.json`` prep signals.

    Returns ``(workflow_status, workflow_error, pr_prepared)``:

    - **Absent file** (the normal pre-report-ready case) →
      ``(None, None, False)``.
    - **Loaded** → ``(state.status, None, prepared)`` where ``prepared`` is
      ``True`` only when PR metadata is present *and* the worktree is not yet
      marked submitted — exactly the ``vrg-submit-pr`` ready gate (which skips
      already-submitted worktrees).
    - **Unreadable/malformed** → ``(None, <reason>, False)``. The error is
      captured and surfaced by the caller, never swallowed; a real read error
      is not collapsed into the "no file" case.
    """
    try:
        state = LocalFileTransport(worktree.path).read()
    except (WorkflowError, OSError) as exc:
        return None, str(exc), False
    if state is None:
        return None, None, False
    prepared = state.pr_metadata is not None and state.submitted is None
    return state.status, None, prepared


def _describe_dirt(porcelain: str, *, limit: int = 5) -> str:
    """Render ``git status --porcelain`` output as a human-readable summary.

    Shows the count of uncommitted paths and up to *limit* of them (stripped
    of the two-char porcelain status prefix) so the human can see at a glance
    that the dirt is, e.g., just build output. Overflow is reported as a
    ``(+N more)`` tail rather than a wall of paths.
    """
    paths = [line[3:].strip() for line in porcelain.splitlines() if line.strip()]
    count = len(paths)
    shown = paths[:limit]
    preview = ", ".join(shown)
    if count > limit:
        preview += f" (+{count - limit} more)"
    return f"{count} uncommitted path(s): {preview}"


def gather_worktree_status(
    worktree: Worktree, *, target: str, with_freshness: bool = False
) -> WorktreeStatus:
    """Gather local + remote signals for *worktree* and classify it.

    The single source of truth shared by ``vrg-worktree-status`` and the
    ``vrg-finalize-pr`` straggler sweep. A failed ``gh`` PR lookup is
    surfaced as ``UNKNOWN`` with the captured reason — never a silent
    failure that would misclassify the worktree.

    For a closed/merged PR, the PR's head SHA is compared against the
    branch's current tip. When they differ — the branch name was reused
    after a same-named PR merged (issue #1719) — the merged verdict is
    withheld so the branch is never classified removable; the mismatch is
    recorded in ``detail`` rather than swallowed.

    Freshness timestamps (last_commit_ts / last_modified_ts) are gathered
    only when ``with_freshness`` is set — the display-only concern of
    ``vrg-worktree-status``; the finalize sweep leaves them unset.
    """
    ahead = git.commits_ahead(target, worktree.branch)
    porcelain = git.read_output("-C", str(worktree.path), "status", "--porcelain")
    dirty = bool(porcelain)
    workflow_status, workflow_error, pr_prepared = _probe_pr_workflow(worktree)
    # Freshness is opt-in: only vrg-worktree-status needs it. The finalize
    # straggler sweep also drives this function and must not pay for (or be
    # broken by) the extra git log + filesystem walk.
    last_commit_ts = git.committer_timestamp(worktree.path) if with_freshness else None
    last_modified_ts = _newest_mtime(worktree.path) if with_freshness else None

    def _with_workflow(status: WorktreeStatus) -> WorktreeStatus:
        return replace(
            status,
            workflow_status=workflow_status,
            workflow_error=workflow_error,
            pr_prepared=pr_prepared,
            last_commit_ts=last_commit_ts,
            last_modified_ts=last_modified_ts,
        )

    detail: str | None = None
    try:
        pr_number, pr_state, pr_head_sha = _resolve_pr_state(worktree.branch)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc)).strip()
        return _with_workflow(
            classify_worktree(
                worktree,
                pr_number=None,
                pr_state=None,
                pr_lookup_failed=True,
                ahead=ahead,
                dirty=dirty,
                detail=detail,
            )
        )

    merged_head_matches_tip = True
    if pr_state in ("MERGED", "CLOSED"):
        tip = git.commit_sha(worktree.branch)
        # A missing head SHA is treated as a mismatch: without positive
        # proof the PR covers this tip, withholding removal is the safe
        # default (never delete unproven-merged work — issue #1719).
        merged_head_matches_tip = pr_head_sha is not None and pr_head_sha == tip
        if not merged_head_matches_tip:
            shown = (pr_head_sha or "unknown")[:8]
            detail = (
                f"closed PR #{pr_number} head {shown} does not match branch "
                f"tip {tip[:8]} — branch name reused after that PR merged "
                f"(issue #1719); current commits are unmerged"
            )
        elif dirty:
            # Merged/closed and the tip matches, but the tree is dirty, so the
            # worktree is stuck: not removable cruft, not live work. Surface an
            # actionable note showing *what* the dirt is (often just build
            # output) so the human can clear it and run vrg-finalize-pr (#2347).
            verb = "merged" if pr_state == "MERGED" else "closed"
            detail = (
                f"{verb} PR #{pr_number} but worktree is dirty — "
                f"{_describe_dirt(porcelain)}. "
                f"Commit, stash, or discard the changes, then run vrg-finalize-pr."
            )

    return _with_workflow(
        classify_worktree(
            worktree,
            pr_number=pr_number,
            pr_state=pr_state,
            pr_lookup_failed=False,
            ahead=ahead,
            dirty=dirty,
            detail=detail,
            merged_head_matches_tip=merged_head_matches_tip,
        )
    )


def discover_worktrees(repo_root: Path) -> list[RawWorktree]:
    """Return every worktree under ``repo_root/.worktrees/``, detached included.

    Parses ``git worktree list --porcelain`` and keeps one ``RawWorktree`` per
    worktree inside the canonical container. ``branch`` is the checked-out
    branch, or ``None`` when HEAD is detached (a worktree paused mid-rebase).
    Worktrees outside the canonical container are ignored, as with
    ``list_worktrees``.

    This is the full-fidelity discovery a status view needs: a detached-HEAD
    worktree still physically exists and must never be silently dropped
    (issue #2634). ``list_worktrees`` layers the branch-only filter on top for
    the callers (finalize/submit sweeps) that must not act on a mid-operation
    worktree.
    """
    output = git.read_output("worktree", "list", "--porcelain")
    canonical_root = (repo_root / ".worktrees").resolve()

    records: list[RawWorktree] = []
    current_path: Path | None = None
    current_branch: str | None = None

    def flush() -> None:
        nonlocal current_path, current_branch
        if current_path is not None:
            resolved = current_path.resolve()
            try:
                resolved.relative_to(canonical_root)
            except ValueError:
                pass
            else:
                records.append(RawWorktree(path=resolved, branch=current_branch))
        current_path = None
        current_branch = None

    # Porcelain output is one blank-line-separated block per worktree, each
    # opening with a `worktree <path>` line; `branch` is present only when a
    # branch is checked out (absent for detached HEAD). Flush the previous
    # block when a new `worktree` line opens the next, and once more at the end.
    for line in output.splitlines():
        if line.startswith("worktree "):
            flush()
            current_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch ") and current_path is not None:
            ref = line.removeprefix("branch ").strip()
            current_branch = ref.removeprefix("refs/heads/")
    flush()
    return records


def list_worktrees(repo_root: Path) -> list[Worktree]:
    """Return worktrees under ``repo_root/.worktrees/`` with their branches.

    Detached worktrees (no ``branch`` line in the porcelain output) and
    worktrees outside the canonical container are excluded — a mid-operation
    worktree is deliberately not a finalize/submit candidate. Use
    ``discover_worktrees`` when detached worktrees must be surfaced too.
    """
    return [
        Worktree(path=raw.path, branch=raw.branch)
        for raw in discover_worktrees(repo_root)
        if raw.branch is not None
    ]


def _branch_from_worktree_name(name: str) -> str | None:
    """Map a canonical worktree directory name to its feature branch.

    ``issue-<N>-<slug>`` -> ``feature/<N>-<slug>``. Returns ``None`` for a name
    that does not follow the convention, so a caller can fall back rather than
    inventing a branch. Used to recover the intended branch of a detached
    worktree from its directory name (issue #2634).
    """
    remainder = name.removeprefix("issue-")
    if remainder == name or not remainder:
        return None
    return f"feature/{remainder}"


def _rebase_head_name(git_dir: Path) -> str | None:
    """Return the branch a paused rebase is rebuilding, or ``None``.

    Git records the original branch as ``refs/heads/<branch>`` in the
    ``head-name`` file of the in-progress rebase state directory —
    ``rebase-merge/`` (merge backend, the modern default) or ``rebase-apply/``
    (apply backend). Returns the short branch name, or ``None`` when no rebase
    is in progress or the file is absent/empty.
    """
    for backend in ("rebase-merge", "rebase-apply"):
        try:
            ref = (git_dir / backend / "head-name").read_text().strip()
        except OSError:
            continue
        if ref:
            return ref.removeprefix("refs/heads/")
    return None


def _rebase_in_progress(git_dir: Path) -> bool:
    """Return True when *git_dir* holds an in-progress rebase's state directory."""
    return (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir()


def gather_detached_status(
    raw: RawWorktree, *, target: str, with_freshness: bool = False
) -> WorktreeStatus:
    """Classify a detached-HEAD worktree — the state a paused rebase leaves.

    A detached worktree has no checked-out branch, so it never resolves to a
    lifecycle state via ``gather_worktree_status``; without this it would be
    dropped from discovery entirely and the worktree would look like it
    vanished (issue #2634). The intended branch is recovered from the paused
    rebase's ``head-name`` metadata, falling back to the ``issue-<N>-<slug>``
    directory name, and the state is ``REBASING`` when a rebase is in progress
    or ``DETACHED`` otherwise. A PR lookup is deliberately skipped: the point is
    to *surface* the in-flight worktree with an actionable note, not to derive a
    lifecycle it does not yet have.

    Freshness timestamps mirror ``gather_worktree_status`` — gathered only when
    ``with_freshness`` is set (``vrg-worktree-status``).
    """
    git_dir = git.worktree_git_dir(raw.path)
    rebasing = _rebase_in_progress(git_dir)
    branch = _rebase_head_name(git_dir) or _branch_from_worktree_name(raw.path.name)
    worktree = Worktree(path=raw.path, branch=branch or "(detached)")

    porcelain = git.read_output("-C", str(raw.path), "status", "--porcelain")
    dirty = bool(porcelain)
    # The branch ref still points at its pre-rebase tip during a rebase, so
    # commits-ahead is meaningful — but only when the resolved branch actually
    # exists; a name recovered solely from the directory may not.
    ahead = git.commits_ahead(target, branch) if branch and git.ref_exists(branch) else 0
    workflow_status, workflow_error, pr_prepared = _probe_pr_workflow(worktree)
    last_commit_ts = git.committer_timestamp(raw.path) if with_freshness else None
    last_modified_ts = _newest_mtime(raw.path) if with_freshness else None

    if rebasing:
        detail = (
            "rebase in progress (detached HEAD) — resolve conflicts and run "
            "'vrg-git rebase --continue', or 'vrg-git rebase --abort' to undo"
        )
    else:
        detail = "detached HEAD — no branch checked out; check out a branch to resume work"

    return WorktreeStatus(
        worktree=worktree,
        state=WorktreeState.REBASING if rebasing else WorktreeState.DETACHED,
        pr_number=None,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        workflow_status=workflow_status,
        workflow_error=workflow_error,
        pr_prepared=pr_prepared,
        last_commit_ts=last_commit_ts,
        last_modified_ts=last_modified_ts,
    )


def worktree_for_branch(branch: str, repo_root: Path) -> Path | None:
    """Return the canonical worktree path that has *branch* checked out, or None."""
    for wt in list_worktrees(repo_root):
        if wt.branch == branch:
            return wt.path
    return None


def require_tty(context: str) -> None:
    """Fail fast when an interactive prompt cannot reach the human.

    These tools are human touch points by design: a human is assumed to
    be present, and EOF-as-default would be a silent failure. Scripted
    use is served by explicit arguments, not by piping into prompts.

    Both stdin and stdout must be terminals: a non-TTY stdin means the
    answer cannot be typed; a non-TTY stdout means the prompt text is
    written into a pipe the human never sees — the prompt blocks
    invisibly instead of failing fast (issue #1448).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        msg = (
            f"{context} requires an interactive terminal.\n"
            "  Pass the target explicitly to run non-interactively."
        )
        raise SystemExit(msg)


def select_worktree(
    candidates: list[Worktree],
    *,
    purpose: str,
    labels: list[str],
) -> Worktree:
    """Choose among candidate worktrees; prompt only when there are several.

    ``labels`` must parallel ``candidates`` one-to-one and is what the
    menu displays.
    """
    if not candidates:
        msg = "select_worktree requires at least one candidate"
        raise ValueError(msg)
    if len(candidates) == 1:
        return candidates[0]
    require_tty(purpose)
    chosen = prompt_choice(purpose, labels)
    return candidates[labels.index(chosen)]


def select_worktrees(
    candidates: list[Worktree],
    *,
    purpose: str,
    labels: list[str],
) -> list[Worktree]:
    """Choose one or more candidate worktrees via a checkbox-style menu.

    A single candidate is returned without prompting. With several, a TTY is
    required and a multi-select menu (numbers or 'all') is shown. ``labels``
    parallels ``candidates`` one-to-one.
    """
    if not candidates:
        msg = "select_worktrees requires at least one candidate"
        raise ValueError(msg)
    if len(candidates) == 1:
        return [candidates[0]]
    require_tty(purpose)
    return [candidates[i] for i in prompt_multi_choice(purpose, labels)]


def match_worktrees(candidates: list[Worktree], tokens: list[str]) -> list[Worktree]:
    """Resolve *tokens* (issue numbers or worktree dir names) to worktrees.

    Each token matches a candidate by directory name (``wt.path.name``) or by
    the issue number in a canonical ``issue-<N>-<slug>`` name. Result order
    follows *tokens*. Unmatched or ambiguous tokens raise ``ValueError``
    naming them — never a silent skip.
    """
    by_name = {wt.path.name: wt for wt in candidates}
    by_issue: dict[str, list[Worktree]] = {}
    for wt in candidates:
        name = wt.path.name
        if name.startswith("issue-") and len(name.split("-", 2)) >= 2:
            by_issue.setdefault(name.split("-", 2)[1], []).append(wt)

    selected: list[Worktree] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for raw in tokens:
        tok = raw.strip()
        if tok in by_name:
            selected.append(by_name[tok])
        elif tok in by_issue and len(by_issue[tok]) == 1:
            selected.append(by_issue[tok][0])
        elif tok in by_issue:
            ambiguous.append(tok)
        else:
            unmatched.append(tok)

    if unmatched or ambiguous:
        parts = []
        if unmatched:
            parts.append(f"no ready worktree matches: {', '.join(unmatched)}")
        if ambiguous:
            parts.append(f"ambiguous (multiple worktrees): {', '.join(ambiguous)}")
        raise ValueError("; ".join(parts))
    return selected


def rebase_onto(worktree: Worktree, base: str) -> None:
    """Fetch *base* from origin and rebase *worktree*'s branch onto it.

    Run via ``git -C`` so the batch orchestrator can process each worktree
    without changing the process CWD. This is the step that makes a batch's
    CI gate run exactly once: rebasing onto the current ``develop`` before
    the PR opens means the gate runs against the final state and the later
    merge is not ``BEHIND``. A rebase conflict raises
    ``subprocess.CalledProcessError`` for the caller to convert to a
    ``BatchAbortError``.
    """
    git.run("-C", str(worktree.path), "fetch", "origin", base)
    git.run("-C", str(worktree.path), "rebase", f"origin/{base}")
