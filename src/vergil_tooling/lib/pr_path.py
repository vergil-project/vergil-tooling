"""The one PR path: how every tool gets a PR into ``develop`` (epic vergil-project/.github#1664).

Spec §5.1 and §5.4 of the release-lock epic. Every PR into ``develop`` that
the tooling opens or arms is armed here, by :func:`arm`, and nothing else
turns auto-merge on or merges a PR into ``develop``. ``vrg-submit-pr``, the
mechanized automation (:func:`vergil_tooling.lib.mechanized_pr.open_sync_pr`),
update-deps (:func:`vergil_tooling.lib.update_deps.pr.arm_and_wait`) and the
release's back-merge (:func:`vergil_tooling.lib.release.bump.back_merge_and_bump`)
are its callers; the re-arm sweeps converge on it in a later task. The release
PR into ``main`` is the one direct merge outside it
(:func:`vergil_tooling.lib.release.merge.wait_and_merge`, which refuses any
other base), and ``tests/vergil_tooling/test_one_pr_path_guard.py`` holds every
other module to that.

:func:`accepts` is the path's admission rule: an open, non-draft PR into
``develop`` whose head is a same-repo branch with a trusted author, as the
trust layer confirms it (:func:`finalize_watch.confirmed_head`). Any other PR
is reported and never armed.

:func:`merge_method` works the merge method out from the PR itself, never
from who is calling, so every re-arm gets the right one:

- ``MERGE`` for a ``release/post-*`` head (a release's back-merge) and for a
  PR carrying the dependency-update label, each from a trusted author on a
  same-repo branch. Either from anyone else is refused, naming why.
- ``REBASE`` for a batch PR, told by its trusted body's linkage (more than
  one ``Closes`` line, :func:`batch_rules.batch_closes`).
- ``SQUASH`` otherwise.

:func:`arm` reads the PR at its current head, the first read behind
:func:`retry.retry_known_resource`, since every caller arms a PR GitHub has
just created and a brief 404 then means "not readable yet". At that head it
runs :func:`accepts`, the provenance check
(:func:`pr_provenance.check_pr`) and, for an agent-involved PR only
(:func:`finalize_watch.agent_involvement`), the agent gates
(:func:`agent_gates.evaluate`, through :func:`finalize_watch.gates_at_head`).
The gates judge local git objects, so they run in a clone of the PR's own
repository: the caller's *clone*, checked as
:func:`finalize_watch.require_clone_of` checks it, or else a fresh one the
path makes itself, so an arm run from a clone of another repo still judges.
A PR with no agent involvement (the back-merge, a dependency-update PR, a
mechanized sync PR) is armed on :func:`accepts` and the provenance check
alone.

The path then turns auto-merge on with that method, pinned to that head
(``expectedHeadOid``). GitHub refuses that for a PR that can already merge
(clean status): the path then merges it directly at that head with that
method. A PR the path does not accept, or that fails a check, is
``refused`` and left unarmed. A step that cannot be completed (a read, the
provenance check, the enable) raises :class:`PRPathError`, naming the PR.

**The arming gate** (spec §5.4) closes the race between a release taking the
release lock and the path arming a PR. Every judgment above completes first;
only then does the path write the PR's arming ref
(:func:`vergil_tooling.lib.release_lock.arming.create`, a create-only push),
and then read the lock:

- Lock free: renew the arming ref by compare-and-swap, turn auto-merge on (or
  merge) under :func:`retry.bounded`'s :data:`ARM_CALL_BOUND_SECS`, retries
  included, then delete the ref. A renewal that finds the ref gone is
  ``refused`` with ``arming ref expired``. An arm call stopped at the bound
  raises and leaves the ref for the release to expire, since the stopped
  call may still land.
- Lock held, or unreadable: delete the ref and report ``held`` (*detail*
  ``held for release <version>``, or the read error), with the PR unarmed.
- The create finds a ref already there (another caller is arming the PR):
  the path does not arm, and waits up to :data:`ARM_CALL_BOUND_SECS` for the
  PR to be armed or merged; still unarmed, it raises
  :class:`ArmingCollisionError`.

The release writes the lock and then waits until every arming ref is gone,
so each side writes before it reads and at least one sees the other.
**The holder's pass** is the lock's token as the calling release holds it:
when it is the token of the lock as it stands, the path arms with no
handshake. A heartbeat rewrites the lock but keeps the token; a re-take,
adopt or takeover writes a new one, so a lost holder's pass is an ordinary
call, held like any other.

:func:`push_to_open_pr` is the path's push mode (``vrg-submit-pr --push``):
every push the tooling makes to the branch of an open PR into ``develop``
goes through it. It runs the same admission rule, the provenance check and
the gates against the new head **before** pushing it
(:func:`finalize_watch.gates_before_push`), and never pushes a head that
fails them. It never touches auto-merge, so the PR stays armed throughout.
:func:`open_develop_pr_url` is the lookup the tooling's other pushes make
first: ``vrg-reword`` routes a push to an open PR's branch through
:func:`push_to_open_pr`, and report-ready refuses one.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from vergil_tooling.lib import (
    auto_merge,
    finalize_watch,
    git,
    github,
    identity_mode,
    pr_provenance,
    release_lock,
    retry,
    trust,
)
from vergil_tooling.lib.develop_clone import clone_url
from vergil_tooling.lib.pr_workflow import batch_rules
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import arming
from vergil_tooling.lib.release_lock import refs as lock_refs
from vergil_tooling.lib.release_lock.model import ARM_CALL_BOUND_SECS
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.update_deps.context import DEPS_LABEL

if TYPE_CHECKING:
    from vergil_tooling.lib.auto_merge import MergeMethod

#: What :func:`arm` did. ``held``: a release holds the lock (or it cannot be
#: read), and the PR is left unarmed for the release to re-arm.
ArmOutcome = Literal["armed", "merged", "held", "refused"]

_DEVELOP = "develop"
_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)$")
_OPEN = "OPEN"
#: The head-branch prefix of a release's back-merge PR into ``develop``.
POST_RELEASE_PREFIX = "release/post-"

#: The errors a step of the path raises when it cannot be completed: each is
#: re-raised as :class:`PRPathError`, never read as a pass.
_STEP_ERRORS = (
    subprocess.CalledProcessError,
    finalize_watch.FinalizeWatchError,
    trust.TrustReadError,
    trust.UntrustedContentError,
    TrustedAuthorsNotConfiguredError,
    auto_merge.AutoMergeError,
    ValueError,
)


class PRPathError(RuntimeError):
    """A step of the one PR path could not be completed; the message names the PR."""


class ArmingCollisionError(PRPathError):
    """Another caller held the PR's arming ref, and the PR was still unarmed after the wait."""


class _ArmCallStoppedError(PRPathError):
    """The arm call ran past :data:`ARM_CALL_BOUND_SECS` and was stopped, not retried."""


@dataclass(frozen=True)
class ArmResult:
    """What :func:`arm` did with a PR.

    *head* is the head it judged (and pinned, for ``armed`` and ``merged``).
    *method* is the merge method it worked out, ``None`` when it refused
    before working one out. *detail* is the reason for ``refused``, and
    ``held for release <v>`` for ``held``; ``""`` otherwise.
    """

    outcome: ArmOutcome
    pr_url: str
    head: str
    method: MergeMethod | None
    detail: str = ""


@dataclass(frozen=True)
class _PR:
    """One read of a PR: its snapshot, whether it is a draft, and its confirmed branch.

    *branch* is the head branch :func:`finalize_watch.confirmed_head` gave,
    ``None`` when the PR is not a same-repo PR with a trusted author on its
    branch.
    """

    snap: finalize_watch.Snapshot
    draft: bool
    repo: str
    number: int
    branch: str | None


def _repo_and_number(url: str) -> tuple[str, int]:
    """The ``OWNER/REPO`` and number in a PR's own *url*; a URL naming neither raises."""
    match = _PR_URL_RE.search(url)
    if match is None:
        msg = f"{url}: cannot read the repository and number from the PR's URL"
        raise ValueError(msg)
    return match.group(1), int(match.group(2))


def _read(pr_url: str) -> _PR:
    """Read *pr_url*, its first read behind the known-resource retry; any failure raises."""

    def view() -> object:
        return github.read_json("pr", "view", pr_url, "--json", "isDraft")

    try:
        data = retry.retry_known_resource(view, resource=f"PR {pr_url}")
    except subprocess.CalledProcessError as exc:
        msg = f"{pr_url}: reading the PR failed: {error_text(exc)}"
        raise PRPathError(msg) from exc
    if not isinstance(data, dict) or not isinstance(data.get("isDraft"), bool):
        msg = f"{pr_url}: gh pr view returned no isDraft: {data!r}"
        raise PRPathError(msg)
    try:
        snap = finalize_watch.snapshot(pr_url)
        repo, number = _repo_and_number(snap.url)
        branch = finalize_watch.confirmed_head(snap)
    except _STEP_ERRORS as exc:
        msg = f"{pr_url}: reading the PR failed: {error_text(exc)}"
        raise PRPathError(msg) from exc
    return _PR(snap=snap, draft=bool(data["isDraft"]), repo=repo, number=number, branch=branch)


def _refusal(pr: _PR) -> str | None:
    """Why the path does not accept *pr*, or ``None`` when it does."""
    snap = pr.snap
    if snap.state != _OPEN:
        return f"it is {snap.state.lower()}, not open"
    if snap.base != _DEVELOP:
        return f"its base is {snap.base}, not {_DEVELOP}"
    if pr.draft:
        return "it is a draft"
    if pr.branch is None:
        return "it is not a same-repo PR with a trusted author on its branch"
    return None


def accepts(pr_url: str) -> str | None:
    """``None`` when the one PR path accepts *pr_url*, else why it does not.

    It accepts an open, non-draft PR into ``develop`` whose head is a
    same-repo branch with a trusted author. A read that fails raises
    :class:`PRPathError`.
    """
    return _refusal(_read(pr_url))


def _method(pr: _PR) -> MergeMethod:
    """*pr*'s merge method, or :class:`PRPathError` naming why it cannot be told."""
    url = pr.snap.url
    head_ref = pr.branch or pr.snap.head_ref
    special = None
    if head_ref.startswith(POST_RELEASE_PREFIX):
        special = f"its head {head_ref} is a release's back-merge branch"
    elif DEPS_LABEL in pr.snap.labels:
        special = f"it carries the {DEPS_LABEL} label"
    if special is not None:
        if pr.branch is None:
            msg = (
                f"{url}: {special}, which merges with a merge commit only from a trusted "
                "author on a same-repo branch, and this PR is not one; its merge method "
                "cannot be worked out"
            )
            raise PRPathError(msg)
        return "MERGE"
    try:
        body = trust.read_issue(pr.repo, pr.number, require_body=True).body or ""
    except _STEP_ERRORS as exc:
        msg = (
            f"{url}: its merge method cannot be worked out: reading its body for the "
            f"batch linkage failed: {error_text(exc)}"
        )
        raise PRPathError(msg) from exc
    return "REBASE" if batch_rules.batch_closes(body) else "SQUASH"


def merge_method(pr_url: str) -> MergeMethod:
    """The merge method of *pr_url*, worked out from the PR (see the module docstring).

    Raises :class:`PRPathError` when it cannot be told, saying why.
    """
    return _method(_read(pr_url))


def _violations(url: str) -> str:
    """*url*'s provenance violations as text, ``""`` for none; a check that cannot run raises."""
    try:
        result = pr_provenance.check_pr(url)
    except _STEP_ERRORS as exc:
        msg = f"{url}: the provenance check could not run: {error_text(exc)}"
        raise PRPathError(msg) from exc
    return ", ".join(f"{v.login} ({v.role.value}): {v.action}" for v in result.violations)


def _require_clone(clone: Path, pr: _PR) -> None:
    """Refuse *clone* unless it is a clone of *pr*'s repository."""
    try:
        finalize_watch.require_clone_of(clone, pr.repo, pr.snap.url)
    except finalize_watch.FinalizeWatchError as exc:
        raise PRPathError(str(exc)) from exc


def _fresh_clone(where: Path, repo: str) -> Path:
    """An empty repository at *where* whose ``origin`` is *repo*, for the gates to fetch into."""
    try:
        git.read_output("init", "--quiet", str(where))
        git.read_output("-C", str(where), "remote", "add", "origin", clone_url(repo))
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"could not make a fresh clone of {repo} for the gates: {error_text(exc)}"
        raise PRPathError(msg) from exc
    return where


def _gates(pr: _PR, branch: str, clone: Path | None) -> str:
    """Why *pr*'s head does not pass the gates, ``""`` when it does or no agent took part."""
    try:
        if clone is not None:
            return finalize_watch.gates_at_head(pr.snap, branch, clone)
        with tempfile.TemporaryDirectory(prefix="vrg-pr-path-") as tmp:
            fresh = _fresh_clone(Path(tmp), pr.repo)
            return finalize_watch.gates_at_head(pr.snap, branch, fresh)
    except finalize_watch.FinalizeWatchError as exc:
        raise PRPathError(str(exc)) from exc


def arm(
    pr_url: str,
    *,
    clone: Path | None = None,
    head: str | None = None,
    holder_pass: str | None = None,
) -> ArmResult:
    """Arm *pr_url* through the one PR path (see the module docstring).

    *clone*, when given, must be a clone of the PR's repository
    (:class:`PRPathError` otherwise, before any check runs); without it the
    gates judge in a fresh fetch the path makes. *head*, when given, is the
    head the caller expects: a PR whose current head is another one is
    refused. *holder_pass* is the release lock's token as the calling
    release holds it: when it is the token of the lock as it stands, the
    arm skips the arming gate's handshake.

    Every judgment completes before the handshake starts, so no fetch or
    gate run happens while the PR's arming ref exists. Returns ``armed``,
    ``merged`` (GitHub refused auto-merge at clean status and the path
    merged it directly), ``held`` (a release holds the lock, or the lock
    cannot be read; nothing turned on, and *detail* says which) or
    ``refused`` (nothing turned on). A step that cannot be completed raises
    :class:`PRPathError`: :class:`ArmingCollisionError` when another
    caller's arming ref left the PR unarmed.
    """
    pr = _read(pr_url)
    url, current = pr.snap.url, pr.snap.head
    if clone is not None:
        _require_clone(clone, pr)

    def refused(detail: str, method: MergeMethod | None = None) -> ArmResult:
        return ArmResult(outcome="refused", pr_url=url, head=current, method=method, detail=detail)

    reason = _refusal(pr)
    if reason is not None:
        return refused(f"the one PR path does not accept it: {reason}")
    if head is not None and current != head:
        return refused(f"its head is {current[:12]}, not the expected {head[:12]}")
    found = _violations(url)
    if found:
        return refused(f"provenance violations found ({found})")
    try:
        method = _method(pr)
    except PRPathError as exc:
        return refused(str(exc))
    # _refusal refuses a PR with no confirmed branch, so it has one here.
    gates = _gates(pr, cast("str", pr.branch), clone)
    if gates:
        return refused(f"the gates refused its head {current[:12]}: {gates}", method)
    # Every judgment is made: only the lock read and the enable are left.
    if holder_pass is not None and _holds_the_lock(pr, holder_pass):
        return _enable_bounded(pr, current, method)
    return _through_the_gate(pr, current, method)


def held_detail(state: lock_refs.LockState) -> str:
    """What a PR the held lock *state* leaves unarmed reports: ``held for release <v>``.

    An unreadable lock is held too, and the detail names its read error.
    """
    if state.record is not None:
        return f"held for release {state.record.version}"
    return f"held: the release lock is held but cannot be read ({state.error})"


def _read_lock_of(repo: str, url: str) -> lock_refs.LockState:
    try:
        return lock_refs.read_lock(repo)
    except release_lock.ReleaseLockError as exc:
        msg = f"{url}: reading the release lock failed: {exc}"
        raise PRPathError(msg) from exc


def _read_lock(pr: _PR) -> lock_refs.LockState:
    return _read_lock_of(pr.repo, pr.snap.url)


def held_unarmed(pr_url: str) -> str:
    """The held detail when a release holds the lock and *pr_url* is unarmed, else ``""``.

    Spec §5.5: a wait on a PR asks this on each poll. A PR that is still
    armed while the lock is held is not yet held: the release's disarm sweep
    turns it off, or it merges before the cut. A lock that cannot be read is
    held (:func:`held_detail`). A failed read raises :class:`PRPathError`.
    """
    repo, _number = _repo_and_number(pr_url)
    state = _read_lock_of(repo, pr_url)
    if not state.held:
        return ""
    try:
        armed = auto_merge.auto_merge_enabled(pr_url)
    except auto_merge.AutoMergeError as exc:
        raise PRPathError(str(exc)) from exc
    return "" if armed else held_detail(state)


def _holds_the_lock(pr: _PR, holder_pass: str) -> bool:
    """Whether *holder_pass* is the token of the lock as it stands: the caller holds it."""
    record = _read_lock(pr).record
    return record is not None and record.token == holder_pass


def _enable_bounded(pr: _PR, head: str, method: MergeMethod) -> ArmResult:
    """Turn auto-merge on (or merge directly), bounded by :data:`ARM_CALL_BOUND_SECS`.

    The bound covers the call's retries, and a call past it is stopped and
    not retried: :class:`PRPathError` names it.
    """
    url = pr.snap.url
    try:
        with retry.bounded(ARM_CALL_BOUND_SECS, what=f"{url}: the arm call"):
            # The path is the one caller of the enable (spec §5.4).
            result = auto_merge._enable(url, head=head, method=method)  # noqa: SLF001
    except retry.DeadlineExceededError as exc:
        msg = f"{exc}; it was stopped and is not retried"
        raise _ArmCallStoppedError(msg) from exc
    except auto_merge.AutoMergeError as exc:
        raise PRPathError(str(exc)) from exc
    outcome: ArmOutcome = "merged" if result.merged_now else "armed"
    return ArmResult(outcome=outcome, pr_url=url, head=head, method=method)


def _delete_arming_ref(pr: _PR, sha: str) -> None:
    """Delete *pr*'s arming ref at *sha*; a value written by another caller is left alone."""
    try:
        arming.delete(pr.repo, pr.number, expected=sha)
    except release_lock.ReleaseLockError as exc:
        msg = f"{pr.snap.url}: deleting its arming ref failed: {exc}"
        raise PRPathError(msg) from exc


def _through_the_gate(pr: _PR, head: str, method: MergeMethod) -> ArmResult:
    """Arm *pr* through the arming gate's handshake (spec §5.4, see :func:`arm`)."""
    url = pr.snap.url
    try:
        sha = arming.create(pr.repo, pr.number)
    except release_lock.ReleaseLockError as exc:
        msg = f"{url}: writing its arming ref failed: {exc}"
        raise PRPathError(msg) from exc
    if sha is None:
        return _await_other_caller(pr, head, method)
    try:
        state = _read_lock(pr)
    except PRPathError as exc:
        _delete_after_failure(pr, sha, exc)
        raise
    if state.held:
        _delete_arming_ref(pr, sha)
        return ArmResult(
            outcome="held", pr_url=url, head=head, method=method, detail=held_detail(state)
        )
    try:
        renewed = arming.renew(pr.repo, pr.number, expected=sha)
    except release_lock.ReleaseLockError as exc:
        msg = f"{url}: renewing its arming ref failed: {exc}"
        raise PRPathError(msg) from exc
    if renewed is None:
        # Gone (a release saw it unchanged for the expiry and deleted it) or
        # rewritten by another caller: either way it is no longer ours.
        return ArmResult(
            outcome="refused", pr_url=url, head=head, method=method, detail="arming ref expired"
        )
    try:
        result = _enable_bounded(pr, head, method)
    except PRPathError as exc:
        # A stopped call may still land, so its arming ref stays for the
        # release to expire (spec §5.4), which never disarms before then.
        if not isinstance(exc, _ArmCallStoppedError):
            _delete_after_failure(pr, renewed, exc)
        raise
    _delete_arming_ref(pr, renewed)
    return result


def _delete_after_failure(pr: _PR, sha: str, failure: PRPathError) -> None:
    """Delete *pr*'s arming ref after *failure*; a delete that fails too raises both."""
    try:
        _delete_arming_ref(pr, sha)
    except PRPathError as exc:
        msg = f"{failure}; then {exc}"
        raise PRPathError(msg) from failure


def _await_other_caller(pr: _PR, head: str, method: MergeMethod) -> ArmResult:
    """Another caller holds *pr*'s arming ref: wait, bounded, for it to arm or merge the PR."""
    url = pr.snap.url

    def probe() -> dict[str, object]:
        try:
            data = github.read_json("pr", "view", url, "--json", "state,autoMergeRequest")
        except subprocess.CalledProcessError as exc:
            msg = f"{url}: reading its auto-merge state failed: {error_text(exc)}"
            raise PRPathError(msg) from exc
        if not isinstance(data, dict):
            msg = f"{url}: gh pr view returned {data!r} for its auto-merge state"
            raise PRPathError(msg)
        return data

    def settled(data: dict[str, object]) -> bool:
        return data.get("state") != _OPEN or data.get("autoMergeRequest") is not None

    def on_wait(_data: object, delay: float) -> None:
        print(f"{url}: another caller is arming it; checking again in {delay:.0f}s")

    _done, data = retry.poll_until(
        probe,
        settled,
        budget=ARM_CALL_BOUND_SECS,
        sleep=time.sleep,
        clock=time.monotonic,
        on_wait=on_wait,
    )
    if data.get("state") == "MERGED":
        return ArmResult(outcome="merged", pr_url=url, head=head, method=method)
    if data.get("state") == _OPEN and data.get("autoMergeRequest") is not None:
        return ArmResult(outcome="armed", pr_url=url, head=head, method=method)
    state = str(data.get("state", "")).lower() or "unknown"
    msg = (
        f"{url}: another caller is arming it (its arming ref exists), and it is still "
        f"unarmed after {ARM_CALL_BOUND_SECS}s (state {state})"
    )
    raise ArmingCollisionError(msg)


@dataclass(frozen=True)
class PushResult:
    """What :func:`push_to_open_pr` did.

    *pr_url* is the branch's open PR into ``develop``, ``""`` when it has
    none. *detail* says what was pushed, or why nothing was.
    """

    pushed: bool
    pr_url: str
    detail: str


def _here() -> tuple[Path, str]:
    """The clone the push runs from (the current worktree) and its ``origin`` repository."""
    try:
        clone = Path(git.read_output("rev-parse", "--show-toplevel"))
        origin = github.github_remote_repo(git.remote_url("origin", repo=clone))
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"push mode: cannot read the current clone and its origin: {error_text(exc)}"
        raise PRPathError(msg) from exc
    if origin is None:
        msg = f"push mode: the origin of {clone} is not a GitHub repository"
        raise PRPathError(msg)
    return clone, origin


def _open_develop_pr(repo: str, branch: str) -> trust.PRRef | None | str:
    """*branch*'s one open PR into ``develop``, ``None`` for none, or why it cannot be told."""
    try:
        prs, skipped = trust.prs_for_branch(repo, branch, state="open")
    except _STEP_ERRORS as exc:
        msg = f"push mode: reading the open PRs on {branch} failed: {error_text(exc)}"
        raise PRPathError(msg) from exc
    trust.report_skipped(skipped, stream=sys.stderr)
    found = [pr for pr in prs if pr.state == _OPEN and pr.base == _DEVELOP]
    if len(found) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in found)
        return f"{branch} has {len(found)} open PRs into {_DEVELOP} ({numbers})"
    return found[0] if found else None


def open_develop_pr_url(branch: str) -> str | None:
    """The URL of *branch*'s open PR into ``develop``, or ``None`` when it has none.

    Spec §5.4: the tooling's own pushes (``vrg-reword``, report-ready) ask
    this before they push, so a push to an open PR's branch goes through
    :func:`push_to_open_pr` or is refused. The branch is looked up in the
    current clone's ``origin``, as :func:`push_to_open_pr` looks it up. It
    fails closed: when it cannot be told (the clone or its origin cannot be
    read, the lookup fails, or the branch has several open PRs into
    ``develop``) it raises :class:`PRPathError`, naming why, and the caller
    refuses the push.
    """
    _clone, repo = _here()
    found = _open_develop_pr(repo, branch)
    if isinstance(found, str):
        msg = f"cannot tell the one open PR into {_DEVELOP} on {branch}: {found}"
        raise PRPathError(msg)
    return None if found is None else found.url


def push_to_open_pr(branch: str, *, head: str, lease: str | None) -> PushResult:
    """Push *head* to *branch*, the branch of an open PR into ``develop``, through the path.

    Spec §5.4: every push the tooling makes to an open PR's branch is judged
    before it is pushed. Run from a clone of the PR's repository (the
    branch's worktree), which holds *head*. It finds the branch's open PR
    into ``develop``, reads it (the first read behind the known-resource
    retry, as :func:`arm` does), and at *head* runs :func:`accepts`, the
    provenance check and the agent gates
    (:func:`finalize_watch.gates_before_push`). A push by an agent identity
    makes the PR agent-involved, so its head is gated even on a PR no agent
    has acted on yet. Only then does it push: with
    ``--force-with-lease=<branch>:<lease>`` when *lease* is given (a rebased
    head; *lease* is the head the caller expects origin to have), a plain
    push otherwise. A head that fails a check is not pushed. Auto-merge is
    never touched, so it stays on throughout.

    A branch with no open PR into ``develop``, a PR the path does not
    accept, and a head that fails a check give ``pushed=False`` with the
    reason. A step that cannot be completed (a read, the provenance check,
    the push itself) raises :class:`PRPathError`.
    """
    clone, repo = _here()
    found = _open_develop_pr(repo, branch)
    if isinstance(found, str):
        return PushResult(pushed=False, pr_url="", detail=found)
    if found is None:
        detail = (
            f"{branch} has no open PR into {_DEVELOP}; push mode pushes only to an open PR's branch"
        )
        return PushResult(pushed=False, pr_url="", detail=detail)
    pr = _read(found.url)
    url = pr.snap.url

    def refused(detail: str) -> PushResult:
        return PushResult(pushed=False, pr_url=url, detail=detail)

    reason = _refusal(pr)
    if reason is not None:
        return refused(f"the one PR path does not accept it: {reason}")
    if pr.branch != branch:
        return refused(f"its head branch is {pr.branch}, not {branch}")
    found_violations = _violations(url)
    if found_violations:
        return refused(f"provenance violations found ({found_violations})")
    try:
        gates = finalize_watch.gates_before_push(
            pr.snap, branch, clone, head=head, pusher_is_agent=identity_mode.any_agent_signal()
        )
    except finalize_watch.FinalizeWatchError as exc:
        raise PRPathError(str(exc)) from exc
    if gates:
        return refused(f"the gates refused the head {head[:12]}: {gates}; it was not pushed")
    lease_args = () if lease is None else (f"--force-with-lease={branch}:{lease}",)
    try:
        git.run("push", *lease_args, "origin", f"{head}:refs/heads/{branch}")
    except subprocess.CalledProcessError as exc:
        msg = f"{url}: pushing {head[:12]} to {branch} failed: {error_text(exc)}"
        raise PRPathError(msg) from exc
    return PushResult(pushed=True, pr_url=url, detail=f"pushed {head[:12]} to {branch}")
