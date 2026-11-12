"""Record PR metadata for the human's submit step: the oracle CLI.

Run-and-done since #1872. The implementing agent calls ``report-ready`` when its
work is green; that writes ``.vergil/pr-workflow.json`` with the PR metadata,
and ``vrg-submit-pr`` (human-run) reads it. ``status`` prints the current state.

``report-ready`` publishes the branch itself (#3602): it pushes the reported
commit to ``origin/<branch>`` (setting upstream) *before* recording anything, so
a relay ``vrg-submit-pr <branch>`` always finds the branch on origin and no
caller has to remember a separate push. Already on origin at that commit, the
push is a no-op. When the delta touches ``.github/workflows/**`` GitHub refuses
an agent identity's push; that is expected — report-ready says so and records
the state anyway, and the human's ``vrg-submit-pr`` pushes the branch instead.
A stale branch, whose own commits change no workflow file but whose tree carries
an outdated one, is refused the same way; report-ready tells it apart as
``vrg-git push`` does and fails with the rebase steps, recording nothing (#4077).
Any other push failure fails report-ready with nothing recorded and no relay ref
written. A branch with no commits ahead of ``--base`` is refused before
anything is pushed or recorded, so it is never frozen (#3870). A re-run on a
frozen branch whose HEAD has drifted past the reported commit is refused before
anything is pushed. A re-run after a deliberate
``unfreeze`` pushes with a lease on the recorded head, so a rebased branch
replaces the reported commit but an unexpectedly moved origin is refused (#3783).
``unfreeze`` overwrites the relay ref with the reopened state before it
reopens the local one, and fails with nothing changed if it cannot, so no
``vrg-submit-pr`` can still read the branch as ready at its old head (#3815).

A batch PR repeats ``--issue``, in commit order (epic vergil-project/.github#1019).
Each issue runs the per-issue refusals; then the batch hard rules and the
one-commit-per-issue check run before anything is pushed, and either failing
records nothing. The ordered list is recorded as the state's ``issues`` (in the
local file and the relay ref alike), and a re-run must name the same list in
the same order. Once the state is recorded, the members' batch markers are
removed. A single ``--issue`` behaves exactly as before.

``batch-start``, ``batch-extract`` and ``batch-show`` manage the batch marker
(:mod:`vergil_tooling.lib.pr_workflow.batch_marker`, epic
vergil-project/.github#1019) in the main clone. ``batch-start`` runs the batch
hard rules before it writes anything. Each takes ``--issue`` as ``N``, ``#N``
or ``owner/repo#N``, and exits 2 when a qualified ref names another repo than
this clone's (or this clone's repo cannot be told), so a wrong clone is never
read as "no marker". ``batch-show`` exits 0 with the marker, 1 when there is
none, and 2 on any other failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, git, github, local_settings
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.linkage import (
    find_linkage_keyword,
    freetext_linkage_error,
    normalize_linkage,
)
from vergil_tooling.lib.pr_body import normalize_issue_ref
from vergil_tooling.lib.pr_workflow import batch_marker, batch_rules, engine, freeze
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.github_transport import GitHubTransport
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # batch-show keeps exit 1 for "no marker" alone: every failure exits 2.
    failure_exit = int(getattr(args, "failure_exit", 1))
    try:
        transport = LocalFileTransport(git.repo_root(), base=args.base)
        return int(args.func(args, transport))
    except WrongCloneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return failure_exit
    except (subprocess.CalledProcessError, OSError) as exc:
        if failure_exit == 1:
            raise
        print(f"error: {error_text(exc)}", file=sys.stderr)
        return failure_exit


class WrongCloneError(WorkflowError):
    """A qualified ``--issue`` names another repo than this clone's (exit 2)."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record PR metadata for the human submit step.")
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ready = sub.add_parser(
        "report-ready",
        help="Push the branch, record the PR metadata, and freeze the branch",
        description=(
            "Push the branch to origin (setting upstream), record the PR metadata "
            "for this worktree, and freeze the branch. No separate push is needed. "
            "If the delta touches .github/workflows/**, GitHub refuses the agent "
            "push; report-ready records the state anyway and the human's "
            "vrg-submit-pr pushes the branch instead. A stale branch (its own "
            "commits change no workflow file) is refused with the rebase steps, "
            "and that and any other push failure record nothing."
        ),
    )
    p_ready.add_argument(
        "--issue",
        action="append",
        required=True,
        help=(
            "The task the PR hands off (N or owner/repo#N). Repeat it, in commit "
            "order, for a batch PR of 2 to 8 same-epic issues: report-ready then runs "
            "the batch hard rules and the one-commit-per-issue check before anything "
            "is pushed, and removes the batch markers once the state is recorded."
        ),
    )
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument("--linkage", default="Ref")
    p_ready.set_defaults(func=cmd_report_ready)

    p_unfreeze = sub.add_parser(
        "unfreeze",
        help="Deliberately reopen a frozen (reported-ready) branch for more commits",
    )
    p_unfreeze.set_defaults(func=cmd_unfreeze)

    p_status = sub.add_parser("status", help="Print the current workflow state")
    p_status.set_defaults(func=cmd_status)

    issue_help = "N, #N or owner/repo#N (this clone's repo)"
    p_bstart = sub.add_parser(
        "batch-start",
        help="Check the batch hard rules and write the batch marker for every member",
        description=(
            "Run the batch hard rules on the issues, in order, then write the batch "
            "marker (the list and the --base commit) for every member in the main "
            "clone. Nothing is written when a rule fails. When the members already "
            "hold a marker for exactly this list, it is a resume and nothing changes; "
            "any other existing marker refuses."
        ),
    )
    p_bstart.add_argument("--issue", action="append", required=True, help=issue_help)
    p_bstart.set_defaults(func=cmd_batch_start)

    p_bextract = sub.add_parser(
        "batch-extract",
        help="Take an issue out of its batch and remove its whole state directory",
        description=(
            "Rewrite every other member's batch marker without the issue (removing "
            "the last one's when fewer than two remain) and remove the issue's "
            "issue-implement/<N>/ state directory. Refused while any security-*.md "
            "details file is under it."
        ),
    )
    p_bextract.add_argument("--issue", required=True, help=issue_help)
    p_bextract.set_defaults(func=cmd_batch_extract)

    p_bshow = sub.add_parser(
        "batch-show",
        help="Print an issue's batch marker (exit 1 when it has none)",
        description=(
            "Print the issue's batch on stdout as one JSON object, "
            '{"issues": ["#10", "#11"], "base_sha": "<full commit SHA>"}, the issues '
            "as this repo's #N refs in batch order, and exit 0. Exits 1, printing "
            "nothing on stdout, when the issue has no marker (it is not in a batch), "
            "and 2 on any other failure: a qualified --issue for another repo's "
            "clone, a clone whose repo cannot be told, or a marker that cannot be read."
        ),
    )
    p_bshow.add_argument("--issue", required=True, help=issue_help)
    p_bshow.set_defaults(func=cmd_batch_show, failure_exit=2)

    return parser.parse_args(argv)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _reject_cross_repo_issue(issue_ref: str) -> None:
    """Refuse a cross-repo --issue at report time — a PR closes only same-repo issues.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is entered
    (report time) rather than later at submit. A PR can only ``Closes`` an issue
    in its own repo; because issue numbers are not unique across repos, a
    cross-repo close would shut an unrelated same-numbered issue — a genuine
    mis-close hazard. Best-effort: if the current repo cannot be resolved (no
    remote, no gh auth — an offline run), defer silently to vrg-submit-pr's
    authoritative check. *issue_ref* is the canonical ref (``#N`` or
    ``owner/repo#N``); a same-repo ``#N`` resolves to the current repo and always
    passes; the compare is case-insensitive so a differently cased spelling of the
    current repo is not a false refusal.
    """
    try:
        current = github.current_repo()
    except (subprocess.CalledProcessError, OSError):
        return
    if not current:
        return
    # *issue_ref* is already the canonical form (normalize_issue_ref ran upstream)
    # and *current* is non-empty, so parse_issue_ref cannot raise here — a bare
    # number was the only unparseable shape, and it is now '#N' before this guard.
    ref = epics.parse_issue_ref(issue_ref, default_repo=current)
    if f"{ref.owner}/{ref.repo}".lower() != current.lower():
        raise WorkflowError(
            f"--issue ({ref.slug}) is in a different repo than this PR ({current}); "
            "a PR can only close an issue in its own repo. File the task in this repo "
            "and reference the other issue via a comment or a Ref line."
        )


def _reject_non_task_issue(issue_ref: str) -> None:
    """Refuse an epic or archive linkage at report time — a PR links a task.

    Mirrors vrg-submit-pr's ``_reject_if_non_task`` so the error surfaces where
    the value is entered (report time) rather than later at submit time (#2842).
    *issue_ref* is the canonical ref (``#N`` or ``owner/repo#N``) so a bare
    number no longer slips past ``parse_issue_ref`` (#2213). Best-effort: if the
    labels cannot be read (no remote, no gh auth — e.g. an offline run), it
    defers silently to vrg-submit-pr's authoritative check rather than blocking
    report-ready.
    """
    try:
        kind = epics.non_task_ref_kind(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if kind is not None:
        raise WorkflowError(epics.non_task_refusal_message("--issue", issue_ref, kind))


def _reject_operational_issue(issue_ref: str) -> None:
    """Refuse an operational-task linkage at report time — it is not PR-workable.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is
    entered. *issue_ref* is the canonical ref (``#N`` or ``owner/repo#N``) so a
    bare number no longer slips past ``parse_issue_ref`` (#2213). Best-effort: if
    operational-ness cannot be determined (no remote, no gh auth — e.g. an offline
    run), defer silently to vrg-submit-pr's authoritative check rather than
    blocking report-ready.
    """
    try:
        is_operational = epics.is_operational_task(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if is_operational:
        raise WorkflowError(
            f"--issue ({issue_ref}) is an operational task (validation/deployment), which "
            "is not PR-workable; record the Outcome as a comment (issue-validate / "
            "issue-deploy) instead of preparing a PR."
        )


def _reject_queue_issue(issue_ref: str) -> None:
    """Refuse a queue (intake/planning/deferred) item at report time — not buildable.

    Mirrors vrg-submit-pr's ``_reject_if_queue_item`` so the error surfaces where
    the value is entered (#2485). The test is the issue's native parent: a child
    of a queue epic must be routed out before it can take a PR.
    Best-effort like :func:`_reject_operational_issue`: if the parent cannot be
    read (no remote, no gh auth), defer to vrg-submit-pr's authoritative check —
    but say so on stderr rather than passing silently.
    """
    try:
        kind = epics.queue_item_kind(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"warning: could not check whether --issue ({issue_ref}) is in the intake or "
            f"planning queue ({error_text(exc)}); vrg-submit-pr will check it at submit time.",
            file=sys.stderr,
        )
        return
    if kind is not None:
        raise WorkflowError(epics.queue_refusal_message(issue_ref, kind))


def _lease_sha(state: WorkflowState) -> str | None:
    """Return the recorded head to lease the push against, or None for a plain push.

    Only the deliberate unfreeze -> rebase -> re-report path leases (#3783): a
    state that is neither frozen nor submitted but already records a reported
    ``head_sha``. Origin should still be at that commit, so a rebased branch is
    pushed over it with ``--force-with-lease`` — accepted only while origin is
    exactly there, refused if it moved unexpectedly. A frozen re-run keeps the
    plain push: its HEAD is the recorded commit (drift is refused before this),
    so a lease could only turn a legitimate publish into a refusal.
    """
    if freeze.is_frozen(state) or state.submitted is not None:
        return None
    recorded = state.git.get("head_sha")
    return str(recorded) if recorded else None


def _push_branch(branch: str, *, lease_sha: str | None = None) -> None:
    """Publish *branch* at the reported commit to origin, setting its upstream.

    Runs before anything is recorded (#3602), so a failure leaves no ready state
    and no relay ref behind for a branch that is not on origin. The push goes
    through ``lib.git`` — the installation-token credentials and transient-retry
    of every other tooling push — not the ``vrg-git`` wrapper, whose freeze would
    otherwise refuse a re-run's push. Its output is captured, keeping stdout a
    pure JSON document; a branch already on origin at this commit is a no-op.

    GitHub's refusal to let an agent identity push a ``.github/workflows/**``
    change is recognized from the push error itself — the authoritative signal,
    where a diff of the delta would only predict it (a human identity's push of
    the same delta succeeds). That case is expected: it is reported and the state
    is still recorded, since the human's ``vrg-submit-pr`` pushes the branch.
    A stale branch gets the same refusal and is refused here instead (see
    :func:`_handle_workflow_refusal`).

    With *lease_sha* (see :func:`_lease_sha`) and the branch on origin, the push
    carries ``--force-with-lease=refs/heads/<branch>:<lease_sha>`` so a rebase
    after an unfreeze replaces the reported commit without a manual force-push
    (#3783). A branch absent from origin (its first push was the expected
    workflow-file refusal) has nothing to overwrite, so it gets the plain push.
    """
    ref = f"refs/heads/{branch}"
    lease = lease_sha is not None and branch in git.remote_branches("origin")
    lease_args = (f"--force-with-lease={ref}:{lease_sha}",) if lease else ()
    try:
        git.read_output("push", "-u", *lease_args, "origin", f"{ref}:{ref}")
    except subprocess.CalledProcessError as exc:
        if git.is_workflow_push_refusal(exc.stderr):
            _handle_workflow_refusal(branch)
            return
        if lease:
            raise WorkflowError(
                f"report-ready: could not push {branch} to origin "
                f"({error_text(exc, output=False)}); nothing was recorded. "
                f"origin/{branch} is not at the recorded head {lease_sha} that "
                "report-ready may overwrite — it moved unexpectedly. Inspect it "
                "before re-running report-ready."
            ) from exc
        raise WorkflowError(
            f"report-ready: could not push {branch} to origin "
            f"({error_text(exc, output=False)}); nothing was recorded. Fix the push "
            "(e.g. after a rebase, vrg-git push --force-with-lease) and re-run report-ready."
        ) from exc
    print(f"report-ready: pushed {branch} to origin", file=sys.stderr)


def _handle_workflow_refusal(branch: str) -> None:
    """Report GitHub's workflow-permission refusal of *branch*, or refuse a stale one.

    The refusal comes both from a branch whose own commits change
    ``.github/workflows/**`` and from a stale branch whose tree only carries an
    outdated workflow file; ``vrg-git push`` tells them apart with the same
    :func:`lib.git.diagnose_workflow_refusal` (#3814, #4077). An own change is
    the expected hand-off: report it and return, so the state is recorded and
    the human's ``vrg-submit-pr`` pushes the branch. A stale branch raises, so
    nothing is recorded and the agent rebases instead. When the cause cannot be
    told, the hand-off stands as before, with how to check by hand.
    """
    diagnosis = git.diagnose_workflow_refusal("origin")
    if diagnosis.kind == "stale":
        raise WorkflowError(
            f"report-ready: {git.stale_workflow_refusal_text(diagnosis)}"
            "  then re-run `vrg-container-run -- vrg-validate` and re-run report-ready.\n"
            f"  Nothing was recorded: {branch} is not handed off as ready."
        )
    if diagnosis.kind == "own":
        cause = (
            f"This branch's own commits change {git.WORKFLOWS_PATH}** "
            f"({', '.join(diagnosis.files)}), which"
        )
    else:
        cause = f"GitHub refused it for {git.WORKFLOWS_PATH}** changes, which"
    print(
        f"report-ready: push of {branch} refused for workflow-file changes — "
        f"EXPECTED, not an error. {cause} an agent identity may not push; the "
        "human's vrg-submit-pr pushes it at submit time. Recording the ready state anyway.",
        file=sys.stderr,
    )
    if diagnosis.kind == "unknown":
        print(
            f"  report-ready {git.unknown_workflow_refusal_note(diagnosis)}",
            file=sys.stderr,
        )


def _refuse_drifted_rerun(state: WorkflowState, head_sha: str) -> None:
    """Refuse re-reporting a frozen branch whose HEAD moved past the reported commit.

    Re-running report-ready on a frozen branch is a prose correction; it must not
    push (or re-record) commits that landed after the freeze — that is the
    reused-branch straggler (#1719). Reopening is the deliberate ``unfreeze``.
    """
    if freeze.is_frozen(state) and freeze.has_drifted(state, head_sha):
        raise WorkflowError(
            freeze.build_refusal(
                state, action="push and re-report a drifted HEAD", current_head=head_sha
            )
        )


def _refuse_empty_branch(branch: str, base: str) -> None:
    """Refuse reporting a branch with no commits ahead of *base* (#3870).

    Such a branch has nothing to merge — typically a ``vrg-commit`` that failed
    unnoticed — and recording it ready would freeze it. The count is taken
    against the same base ref the recorded ``base_sha`` comes from, and runs
    before anything is pushed or recorded.
    """
    if git.commits_ahead(base, "HEAD") == 0:
        raise WorkflowError(
            f"report-ready: branch {branch} has no commits ahead of {base}; nothing "
            "was recorded and the branch is not frozen. Commit the work (check that "
            "vrg-commit succeeded) and re-run report-ready."
        )


def _push_relay_ref(state: WorkflowState, base: str) -> None:
    """Mirror the ready state onto the reserved relay ref, after the local write.

    Unconditional by design (Task 3, epic vergil-project/.github#148): every
    ``report-ready`` also force-pushes the state to
    ``refs/vergil/pr-workflow/<branch>`` via ``GitHubTransport`` — no
    off-platform detection — so a cloud VM's report-ready is visible to the Mac
    that later runs ``vrg-submit-pr`` (the two never share a disk). The durable
    local file has already been written and stays put; a push failure is
    therefore surfaced loudly on stderr but never rolls the local state back.

    ``report-ready`` speaks a JSON-on-stdout contract; ``GitHubTransport.write``
    is quiet-by-design (#2793), routing the relay push's git chatter to stderr
    itself, so no caller-side ``redirect_stdout`` workaround is needed here to
    keep stdout a pure JSON document.
    """
    relay = GitHubTransport(git.current_branch(), base=base)
    try:
        relay.write(state)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"warning: could not push pr-workflow relay ref {relay.ref}: {error_text(exc)}",
            file=sys.stderr,
        )


def _listed(issues: list[str]) -> str:
    """*issues* as ``#10, #11`` for a message (an ``owner/repo#N`` ref as given)."""
    return ", ".join(f"#{issue}" if issue.isdigit() else issue for issue in issues)


def _stale_issues_message(state: WorkflowState, issues: list[str]) -> str:
    """Refuse a re-run whose ordered ``--issue`` list is not the recorded one.

    A single issue recorded and a single other issue passed keeps the message it
    always had; any batch on either side names both whole lists.
    """
    if state.issues is None and len(issues) == 1:
        return (
            f"stale workflow file for issue #{state.issue}; you passed #{issues[0]}. "
            "Delete .vergil/pr-workflow.json to start fresh."
        )
    return (
        f"stale workflow file for issues {_listed(state.all_issues)}; you passed "
        f"{_listed(issues)}. A re-run must name the recorded issues in the recorded "
        "order; nothing was recorded. Delete .vergil/pr-workflow.json to start fresh."
    )


def _check_batch(issue_refs: list[str], *, base: str, head_sha: str) -> list[str]:
    """Run the batch checks before anything is pushed; return the marker refs.

    The hard rules (:func:`batch_rules.check_hard_rules`) run against this
    clone's repo, read from its ``origin`` remote as ``batch-start`` reads it,
    and the commit-per-issue invariant (:func:`batch_rules.check_commit_per_issue`)
    runs over ``base..head_sha``. Either failing refuses with nothing pushed or
    recorded. The returned refs are each issue's ``#N`` in this repo, the form
    the batch markers are keyed by.
    """
    try:
        repo = _clone_repo()
    except WrongCloneError as exc:
        raise WorkflowError(f"report-ready: {exc}; nothing was pushed or recorded") from exc
    try:
        batch_rules.check_hard_rules(issue_refs, repo=repo)
        batch_rules.check_commit_per_issue(issue_refs, base=base, head=head_sha)
    except batch_rules.BatchRuleError as exc:
        raise WorkflowError(f"report-ready: {exc}; nothing was pushed or recorded") from exc
    return [f"#{epics.parse_issue_ref(ref, default_repo=repo).number}" for ref in issue_refs]


def _remove_batch_markers(marker_refs: list[str]) -> None:
    """Remove the batch markers once the ready state is recorded.

    Runs after the local state write, report-ready's point of no return, and
    after the relay push, whatever that did (it only warns). A failure is
    loud, never skipped: the state stays recorded, and a re-run of report-ready
    (a frozen re-run, which pushes nothing new) removes them.
    """
    try:
        batch_marker.remove_markers(git.main_worktree_root(), marker_refs)
    except (WorkflowError, subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"report-ready: the ready state is recorded, but the batch markers of "
            f"{', '.join(marker_refs)} could not be removed ({error_text(exc)}); "
            "re-run report-ready to remove them."
        ) from exc


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    # Local-settings guard (epic vergil-project/.github#558), before anything is
    # pushed or recorded: refuse an agent, warn a human.
    if local_settings.enforce(Path.cwd()) != 0:
        return 1
    # Resolve --issue to a canonical ref (bare number -> '#N') before the guards,
    # mirroring vrg-submit-pr. The guards call epics.parse_issue_ref, which rejects
    # a bare number (no '#') and would silently return False — letting a
    # bare-number epic/operational issue bypass the report-time guard (#2213). The
    # raw args.issue is still what gets *stored* (vrg-submit-pr's resolve_issue_ref
    # re-normalizes it and rejects a leading '#'), so only the guards see the
    # canonical form.
    #
    # --issue repeats for a batch (epic vergil-project/.github#1019): every issue
    # runs the same per-issue guards, and the raw values are recorded in the order
    # given (state.issues, first one also as state.issue), the commit order the
    # batch checks hold the branch to.
    issues = [str(raw) for raw in args.issue]
    issue_refs: list[str] = []
    for raw in issues:
        try:
            issue_refs.append(normalize_issue_ref(raw))
        except ValueError as exc:
            raise WorkflowError(f"report-ready: {exc}") from exc
    for issue_ref in issue_refs:
        _reject_cross_repo_issue(issue_ref)
        _reject_non_task_issue(issue_ref)
        _reject_operational_issue(issue_ref)
        _reject_queue_issue(issue_ref)
    batch = batch_rules.is_batch(issue_refs)
    try:
        linkage, linkage_warning = normalize_linkage(args.linkage)
    except ValueError as exc:
        raise WorkflowError(f"report-ready: {exc}") from exc
    for value in (args.notes, args.summary):
        found = find_linkage_keyword(value)
        if found:
            raise WorkflowError(freetext_linkage_error(found, issues[0]))
    branch = git.current_branch()
    if branch == "HEAD":
        raise WorkflowError("report-ready: HEAD is detached; check out the feature branch first.")
    _refuse_empty_branch(branch, transport.base)
    head_sha = transport.head_sha()
    state = transport.read()
    lease_sha: str | None = None
    if state is None:
        state = engine.init_state(
            issues=issues,
            branch=branch,
            base=transport.base,
            head_sha=head_sha,
            base_sha=transport.merge_base(),
            now=_now(),
        )
    elif issues != state.all_issues:
        raise WorkflowError(_stale_issues_message(state, issues))
    elif state.branch != branch:
        raise WorkflowError(
            f"workflow file records branch {state.branch}, but {branch} is checked out; "
            f"check out {state.branch} to re-report it."
        )
    else:
        _refuse_drifted_rerun(state, head_sha)
        # Read before apply_report_ready overwrites the recorded head_sha.
        lease_sha = _lease_sha(state)
    marker_refs = _check_batch(issue_refs, base=state.base, head_sha=head_sha) if batch else []
    engine.apply_report_ready(
        state,
        title=args.title,
        summary=args.summary,
        notes=args.notes,
        linkage=linkage,
        head_sha=head_sha,
        now=_now(),
    )
    _push_branch(branch, lease_sha=lease_sha)
    transport.write(state)
    _push_relay_ref(state, args.base)
    response: dict[str, object] = {"ok": True, "status": state.status}
    if batch:
        _remove_batch_markers(marker_refs)
        response["issues"] = state.all_issues
    if linkage_warning:
        response["warning"] = linkage_warning
    _emit(response)
    return 0


def _retract_relay_ref(state: WorkflowState, base: str) -> None:
    """Publish the unfrozen state to the relay ref, before the local write.

    The relay ref still carries the ``ready`` state the last ``report-ready``
    pushed. Left in place, a named-branch ``vrg-submit-pr`` on another machine
    would read it as ready and open the PR at the head reported before the
    unfreeze, dropping every commit made since (issue #3815). Overwriting it
    with the ``implementing`` state makes that submit refuse. This runs before
    the local file is written, and a failure fails the unfreeze with nothing
    changed: the branch stays frozen rather than reopened with a stale ready
    relay ref still on origin.
    """
    relay = GitHubTransport(state.branch, base=base)
    try:
        relay.write(state)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"unfreeze: could not update the pr-workflow relay ref {relay.ref} "
            f"({error_text(exc)}); nothing changed and the branch stays frozen. "
            "The relay ref still says ready at the reported head, so reopening "
            "without it would let vrg-submit-pr open the PR at that stale head. "
            "Fix the push and re-run unfreeze."
        ) from exc


def cmd_unfreeze(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    """Deliberately reopen a frozen (reported-ready) branch for more commits.

    The sanctioned escape hatch from the post-report-ready freeze: it is a
    distinct, explicit subcommand precisely so reopening a branch is never a
    silent side effect of another action. The relay ref is updated to the
    reopened state first (:func:`_retract_relay_ref`), so no submit anywhere
    can still see the branch as ready at its old head.
    """
    state = transport.read()
    if state is None:
        raise WorkflowError(
            "no workflow file to unfreeze; run report-ready first (there is nothing frozen here)."
        )
    engine.apply_unfreeze(state, now=_now())
    _retract_relay_ref(state, args.base)
    transport.write(state)
    _emit({"ok": True, "status": state.status})
    return 0


def cmd_status(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        _emit({"exists": False})
        return 0
    print(state.to_json())
    return 0


_BATCH_PLAIN_RE = re.compile(r"#?([1-9][0-9]*)")
_BATCH_QUALIFIED_RE = re.compile(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)")


def _clone_repo() -> str:
    """This clone's ``owner/repo``, read locally from its ``origin`` remote.

    Raises :class:`WrongCloneError` when it cannot be told, so a caller never
    takes an unknown clone for the right one.
    """
    try:
        url = git.remote_url("origin")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WrongCloneError(
            f"cannot tell which repo this clone is: its origin remote cannot be read "
            f"({error_text(exc)})"
        ) from exc
    repo = github.github_remote_repo(url)
    if repo is None:
        raise WrongCloneError(
            "cannot tell which repo this clone is: its origin remote is not a github.com repo"
        )
    return repo


def _batch_ref(raw: str) -> str:
    """Normalize one ``--issue`` of a batch command to this clone's ``#N``.

    ``N`` and ``#N`` are this clone's. A qualified ``owner/repo#N`` must name
    this clone's repo (compared without case), or :class:`WrongCloneError`.
    """
    value = raw.strip()
    plain = _BATCH_PLAIN_RE.fullmatch(value)
    if plain is not None:
        return f"#{plain.group(1)}"
    qualified = _BATCH_QUALIFIED_RE.fullmatch(value)
    if qualified is None:
        raise WorkflowError(f"--issue must be N, #N or owner/repo#N, got {raw!r}")
    repo = _clone_repo()
    if qualified.group(1).lower() != repo.lower():
        raise WrongCloneError(
            f"--issue {value} is in {qualified.group(1)}, but this is a clone of {repo}; "
            f"run the command from a clone of {qualified.group(1)}"
        )
    return f"#{qualified.group(2)}"


def _base_commit(base: str) -> str:
    """The full SHA of the commit *base* names now: the batch's fixed base."""
    try:
        return git.read_output("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"batch-start: cannot resolve the base {base} to a commit ({error_text(exc)}); "
            "nothing was written"
        ) from exc


def cmd_batch_start(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Check the hard rules, then write every member's marker (or resume)."""
    issues = [_batch_ref(str(raw)) for raw in args.issue]
    repo = _clone_repo()
    batch_rules.check_hard_rules(issues, repo=repo)
    root = git.main_worktree_root()
    existing = batch_marker.existing_batch(root, issues)
    if existing is not None:
        _emit(
            {
                "ok": True,
                "resumed": True,
                "issues": existing.issues,
                "base_sha": existing.base_sha,
            }
        )
        return 0
    base_sha = _base_commit(args.base)
    batch_marker.write_markers(root, issues, base_sha=base_sha)
    _emit({"ok": True, "resumed": False, "issues": issues, "base_sha": base_sha})
    return 0


def cmd_batch_extract(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Take one issue out of its batch."""
    issue = _batch_ref(str(args.issue))
    remaining = batch_marker.extract(git.main_worktree_root(), issue)
    _emit(
        {
            "ok": True,
            "extracted": issue,
            "remaining": remaining,
            "batch": batch_rules.is_batch(remaining),
        }
    )
    return 0


def cmd_batch_show(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Print an issue's marker as JSON; exit 1 when it has none.

    Every failure exits 2 (its ``failure_exit``, see :func:`main`).
    """
    issue = _batch_ref(str(args.issue))
    marker = batch_marker.read_marker(git.main_worktree_root(), issue)
    if marker is None:
        print(f"{issue} has no batch marker", file=sys.stderr)
        return 1
    _emit({"issues": marker.issues, "base_sha": marker.base_sha})
    return 0


if __name__ == "__main__":
    sys.exit(main())
