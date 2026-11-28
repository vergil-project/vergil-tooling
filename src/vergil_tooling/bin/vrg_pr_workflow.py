"""Record PR metadata for the submit step: the oracle CLI.

Run-and-done since #1872. The implementing agent calls ``report-ready`` when its
work is green; that writes ``.vergil/pr-workflow.json`` with the PR metadata,
and ``vrg-submit-pr`` reads it. ``status`` prints the current state.

``report-ready`` publishes the branch itself (#3602): it pushes the reported
commit to ``origin/<branch>`` (setting upstream) *before* recording anything, so
a relay ``vrg-submit-pr <branch>`` always finds the branch on origin and no
caller has to remember a separate push. Already on origin at that commit, the
push is a no-op. A push GitHub refuses for ``.github/workflows/**`` changes is
a failure like any other: every in-scope installation grants the user App
``workflows: write``, so the refusal means this repository's installation has
not accepted it, and report-ready names that and the human's remedy (#5176).
Every push failure fails report-ready with nothing recorded and no relay ref
written. Once the branch is pushed and the state recorded, a relay ref that
cannot be written fails report-ready, naming the ref, with the local state kept
for the re-run that writes it (#5035). A branch with no commits ahead of
``--base`` is refused before anything is pushed or recorded, so it is never
frozen (#3870). A re-run on a frozen branch whose HEAD has drifted past the
reported commit is refused before anything is pushed. A re-run after a
deliberate ``unfreeze`` pushes with a lease on the recorded head, so a rebased branch
replaces the reported commit but an unexpectedly moved origin is refused (#3783).
``unfreeze`` overwrites the relay ref with the reopened state before it
reopens the local one, and fails with nothing changed if it cannot, so no
``vrg-submit-pr`` can still read the branch as ready at its old head (#3815).

Every ``report-ready`` records who ran it as the state's ``recorded_by``, the
current identity mode, in the local file and the relay ref alike (epic
vergil-project/.github#1384, Task 9). It is a hint for the finalize watch,
which can only make a change more agent-involved; ``vrg-submit-pr`` runs its
gate on every submission whatever it says. A human re-run never replaces an
agent's recording.

A batch PR repeats ``--issue``, in commit order (epic vergil-project/.github#1019).
Each issue runs the per-issue refusals; then the batch hard rules and the
one-commit-per-issue check run before anything is pushed, and either failing
records nothing. The ordered list is recorded as the state's ``issues`` (in the
local file and the relay ref alike), and a re-run must name the same list in
the same order. Once the state is recorded, the members' batch markers are
removed. The JSON response lists the issues as this repo's ``#N`` refs, the
form ``batch-start`` and ``batch-show`` print, while the state keeps each one as
given (#4297). A single ``--issue`` behaves exactly as before.

When the single ``--issue`` is an epic's documentation task, the response
previews what the PR does with the epic's planning issue
(:mod:`vergil_tooling.lib.planning_close`, vergil-project/.github#950):
``planning_close`` is ``{"closes": <ref>}`` when the PR will also close it,
``{"planning": <ref>, "skipped": <why>}`` when it will not, and
``{"unknown": <why>}`` when the check could not be read. In that case
``vrg-submit-pr`` makes it again, authoritatively, when it builds the body. The
key is absent for any other issue and for a batch.

``batch-start``, ``batch-extract`` and ``batch-show`` manage the batch marker
(:mod:`vergil_tooling.lib.pr_workflow.batch_marker`, epic
vergil-project/.github#1019) in the main clone. ``batch-start`` runs the batch
hard rules before it writes anything. Each takes ``--issue`` as ``N``, ``#N``
or ``owner/repo#N``, and exits 2 when a qualified ref names another repo than
this clone's (or this clone's repo cannot be told), so a wrong clone is never
read as "no marker". ``batch-show`` exits 0 with the marker, 1 when there is
none, and 2 on any other failure.

``batch-extract --issue <N> [--issue <M> ...] [--check]`` takes a set out of its
batch in one marker rewrite (epic vergil-project/.github#1672, spec §5.5). The
set is refused, naming the group, when it would split a release group: a group
with a member leaving the batch and another staying in it. A group member
outside the batch does not stay in it. ``--check`` makes the same judgment
without writing anything and prints ``{"ok", "split", "remaining"}``, exiting 0
when ok and 1 otherwise; a group that cannot be read fails the judgment, and
``batch-extract`` refuses on it too.

``batch-check --pr <url> [--head <sha>]`` checks a submitted batch PR before
a push to it (epic vergil-project/.github#1218, spec §6.8): its body's
``Closes`` refs against GitHub's closing references, and one commit per issue
from the PR's base to ``--head`` (or the PR's head). It never runs the hard
rules, writes nothing and makes no freeze check. The body is read only through
the trust layer (epic vergil-project/.github#1467): one it does not trust fails
the check, batch or not, since its ``Closes`` lines are what the check goes by.
It exits 0 when the PR passes or is not a batch PR, and 1 otherwise.

``batch-gates [--json]`` judges a batch's merge-review gate before handoff, run
in the batch's worktree (epic vergil-project/.github#1672, plan Task 6a). It
reads the batch's issues from the clone's batch markers, takes the base from a
freshly fetched ``origin/develop`` as ``vrg-submit-pr`` does, and evaluates the
gate's merge-review check commit by commit (:func:`agent_gates.evaluate_commits`).
It writes nothing: no state, no marker, and no git write but the fetch. It
prints one line per commit, or with ``--json`` the object
``{"branch", "head", "commits": [{"issue", "sha", "covered", "reason"}]}``, and
exits 0 when every commit is covered, 1 when one is not or on a read failure,
2 on usage. Report-ready removes the markers, so after it the probe refuses
with exit 1.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    agent_gates,
    epics,
    git,
    github,
    identity_mode,
    local_settings,
    planning_close,
    pr_path,
    trust,
)
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.linkage import (
    find_linkage_keyword,
    freetext_linkage_error,
    normalize_linkage,
)
from vergil_tooling.lib.merge_review import record as merge_record
from vergil_tooling.lib.pr_body import normalize_issue_ref
from vergil_tooling.lib.pr_workflow import batch_marker, batch_rules, engine, freeze
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.github_transport import GitHubTransport
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_groups import record
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

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
    parser = argparse.ArgumentParser(description="Record PR metadata for the submit step.")
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ready = sub.add_parser(
        "report-ready",
        help="Push the branch, record the PR metadata, and freeze the branch",
        description=(
            "Push the branch to origin (setting upstream), record the PR metadata "
            "for this worktree, and freeze the branch. No separate push is needed. "
            "A push failure records nothing. A push GitHub refuses for "
            ".github/workflows/** changes is such a failure: the App lacks "
            "workflows: write in this repository's installation, and the human "
            "must accept that permission on the installation."
        ),
    )
    p_ready.add_argument(
        "--issue",
        action="append",
        required=True,
        help=(
            "The task the PR hands off (N or owner/repo#N). Repeat it, in commit "
            "order, for a batch PR of 2 to 20 same-epic issues: report-ready then runs "
            "the batch hard rules and the one-commit-per-issue check before anything "
            "is pushed, and removes the batch markers once the state is recorded."
        ),
    )
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument(
        "--linkage",
        default="Ref",
        help=(
            "The PR-body linkage keyword to record (default: Ref). vrg-submit-pr "
            "upgrades it to Closes for a managed task (an issue with an epic "
            "parent), so the PR closes the task on merge; any other issue keeps it."
        ),
    )
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
        help="Take issues out of their batch and remove their whole state directories",
        description=(
            "Take the issues out of their batch in one rewrite: every remaining "
            "member's batch marker is rewritten without them (the last one's is "
            "removed when fewer than two remain), then each issue's "
            "issue-implement/<N>/ state directory is removed, its writer-commits "
            "ledger included. Refused while any "
            "security-*.md details file is under one of them. Refused, naming the "
            "group, when the set would split a release group (a member leaving and "
            "another staying); nothing is changed then. --check makes the same "
            'judgment, writes nothing, and prints {"ok", "split", "remaining"} '
            "as JSON, exiting 0 when ok and 1 otherwise."
        ),
    )
    p_bextract.add_argument(
        "--issue",
        action="append",
        required=True,
        help=issue_help + "; repeat for a set",
    )
    p_bextract.add_argument(
        "--check",
        action="store_true",
        help="Judge the group split and print it as JSON; write nothing",
    )
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

    p_bcheck = sub.add_parser(
        "batch-check",
        help="Check a submitted batch PR: one commit per issue and its closing references",
        description=(
            "Read the PR's body and base. A batch PR (more than one Closes line) "
            "must close exactly the issues GitHub lists as its closing references, "
            "and its branch must hold one commit per issue, in the body's order, "
            "since the base. The hard rules are not run. The body is read through "
            "the trust layer, and an untrusted body fails the check, batch or not. "
            "Exits 0 when the PR "
            "passes or is not a batch PR, 1 with the rule's message otherwise. It "
            "only reads, so it runs on a submitted, frozen branch, and it never "
            "moves the branch's remote-tracking ref, so a later leased push still "
            "checks against what it last saw."
        ),
    )
    p_bcheck.add_argument("--pr", required=True, help="The PR's URL (or number, in its clone)")
    p_bcheck.add_argument(
        "--head",
        help=(
            "The commit to check, present in this clone (default: the PR's head on "
            "GitHub). pr-watch passes the head it is about to push."
        ),
    )
    p_bcheck.set_defaults(func=cmd_batch_check)

    p_bgates = sub.add_parser(
        "batch-gates",
        help="Judge a batch's gate commit by commit before handoff (read-only)",
        description=(
            "Run in a batch's worktree before report-ready. The batch's issues are read "
            "from the batch markers of the branch's issues, the base is a freshly fetched "
            "origin/develop, and the merge-review rule of the submit gate is evaluated "
            "commit by commit, exactly as vrg-submit-pr would. Writes nothing. Exits 0 "
            "when every commit is covered, 1 when one is not, or on a read failure, and "
            "2 on usage. Report-ready removes the markers, so after it this exits 1: the "
            "probe runs only before handoff. --json prints the verdicts as one object, "
            '{"branch", "head", "commits": [{"issue", "sha", "covered", "reason"}]}.'
        ),
    )
    p_bgates.add_argument("--json", action="store_true", help="Print the verdicts as JSON")
    p_bgates.set_defaults(func=cmd_batch_gates)

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
    except subprocess.CalledProcessError, OSError:
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
    except subprocess.CalledProcessError, OSError:
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
    except subprocess.CalledProcessError, OSError:
        return
    if is_operational:
        raise WorkflowError(
            f"--issue ({issue_ref}) is an operational task (validation/deployment), which "
            "is not PR-workable; record the Outcome as a comment (issue-validate / "
            "issue-deploy) instead of preparing a PR."
        )


def _planning_close_preview(issue_ref: str) -> dict[str, str] | None:
    """What the PR will do with the planning issue of a documentation task, or None.

    A preview only: ``vrg-submit-pr`` decides it again when it builds the body,
    and a failed read there aborts the submission. Here a failed read is
    reported in the response (``unknown``), never passed over in silence.
    """
    try:
        task = epics.parse_issue_ref(issue_ref, default_repo=github.current_repo())
        result = planning_close.find(task)
    except (
        subprocess.CalledProcessError,
        OSError,
        RuntimeError,
        ValueError,
        TrustedAuthorsNotConfiguredError,
        trust.UntrustedContentError,
        trust.TrustReadError,
    ) as exc:
        return {
            "unknown": (
                f"could not check whether {issue_ref} is an epic's documentation task "
                f"({error_text(exc)}); vrg-submit-pr checks it when it builds the PR body"
            )
        }
    if result is None:
        return None
    if result.closes:
        return {"closes": result.planning.slug}
    return {"planning": result.planning.slug, "skipped": str(result.skipped)}


def _reject_queue_issue(issue_ref: str) -> None:
    """Refuse a queue item or an ops issue at report time — not buildable.

    Mirrors vrg-submit-pr's ``_reject_if_queue_item`` so the error surfaces where
    the value is entered (#2485). The test is the issue's native parent: a child
    of a queue epic (intake, planning or deferred) must be routed out before it
    can take a PR, and a child of the operations epic (an ops issue) never takes
    one.
    Best-effort like :func:`_reject_operational_issue`: if the parent cannot be
    read (no remote, no gh auth), defer to vrg-submit-pr's authoritative check —
    but say so on stderr rather than passing silently.
    """
    try:
        kind = epics.unbuildable_parent_kind(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"warning: could not check whether --issue ({issue_ref}) is a queue item or an "
            f"ops issue ({error_text(exc)}); vrg-submit-pr will check it at submit time.",
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


def _refuse_open_pr_branch(branch: str) -> None:
    """Refuse report-ready's push when *branch* has an open PR into ``develop``.

    Spec §5.4 of epic vergil-project/.github#1664: every push the tooling
    makes to an open PR's branch goes through the one PR path, which judges
    the head before it pushes it. Report-ready's push does not, so it never
    pushes to one: the new head goes through ``vrg-submit-pr --push``. The
    lookup fails closed: when it cannot be told, nothing is pushed.
    """
    try:
        pr_url = pr_path.open_develop_pr_url(branch)
    except pr_path.PRPathError as exc:
        raise WorkflowError(
            f"report-ready: could not tell whether {branch} has an open PR into develop "
            f"({exc}); nothing was pushed or recorded. Fix the cause and re-run report-ready."
        ) from exc
    if pr_url is not None:
        raise WorkflowError(
            f"report-ready: {branch} has an open PR into develop ({pr_url}); report-ready "
            "does not push to an open PR's branch, so nothing was pushed or recorded. Push "
            "a new head with `vrg-submit-pr --push` (add --force-with-lease for a rebased head)."
        )


def _push_branch(branch: str, *, lease_sha: str | None = None) -> None:
    """Publish *branch* at the reported commit to origin, setting its upstream.

    Runs before anything is recorded (#3602), so a failure leaves no ready state
    and no relay ref behind for a branch that is not on origin. The push goes
    through ``lib.git`` — the installation-token credentials and transient-retry
    of every other tooling push — not the ``vrg-git`` wrapper, whose freeze would
    otherwise refuse a re-run's push. Its output is captured, keeping stdout a
    pure JSON document; a branch already on origin at this commit is a no-op.

    GitHub's refusal to push a ``.github/workflows/**`` change is recognized
    from the push error itself and fails like any other push, with the cause
    named: the installation has not accepted ``workflows: write`` (#5176).

    With *lease_sha* (see :func:`_lease_sha`) and the branch on origin, the push
    carries ``--force-with-lease=refs/heads/<branch>:<lease_sha>`` so a rebase
    after an unfreeze replaces the reported commit without a manual force-push
    (#3783). A branch absent from origin (deleted there since the first report)
    has nothing to overwrite, so it gets the plain push.

    The branch of an open PR into ``develop`` is refused before anything is
    pushed (:func:`_refuse_open_pr_branch`): a new head for it goes through
    the one PR path, ``vrg-submit-pr --push``.
    """
    _refuse_open_pr_branch(branch)
    ref = f"refs/heads/{branch}"
    lease = lease_sha is not None and branch in git.remote_branches("origin")
    lease_args = (f"--force-with-lease={ref}:{lease_sha}",) if lease else ()
    try:
        git.read_output("push", "-u", *lease_args, "origin", f"{ref}:{ref}")
    except subprocess.CalledProcessError as exc:
        if git.is_workflow_push_refusal(exc.stderr):
            raise WorkflowError(
                f"report-ready: could not push {branch} to origin "
                f"({error_text(exc, output=False)}); nothing was recorded.\n"
                f"  {git.workflow_push_refusal_text()}\n"
                "  Then re-run report-ready."
            ) from exc
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


def _push_relay_ref(state: WorkflowState, base: str) -> str | None:
    """Mirror the ready state onto the reserved relay ref, after the local write.

    Unconditional by design (Task 3, epic vergil-project/.github#148): every
    ``report-ready`` also force-pushes the state to
    ``refs/vergil/pr-workflow/<branch>`` via ``GitHubTransport`` — no
    off-platform detection — so a cloud VM's report-ready is visible to the Mac
    that later runs ``vrg-submit-pr`` (the two never share a disk). The durable
    local file has already been written and stays put, so a failure never rolls
    the local state back. It is a failed handoff all the same (#5035): the
    returned message, naming the ref that was not written and the re-run that
    writes it, fails report-ready once the rest of its recording is done. A
    success returns None.

    ``report-ready`` speaks a JSON-on-stdout contract; ``GitHubTransport.write``
    is quiet-by-design (#2793), routing the relay push's git chatter to stderr
    itself, so no caller-side ``redirect_stdout`` workaround is needed here to
    keep stdout a pure JSON document.
    """
    relay = GitHubTransport(state.branch, base=base)
    try:
        relay.write(state)
    except (subprocess.CalledProcessError, OSError) as exc:
        return (
            f"report-ready: the relay ref {relay.ref} was NOT written "
            f"({error_text(exc)}). The ready state is recorded only in this "
            "worktree's .vergil/pr-workflow.json, so a vrg-submit-pr "
            f"{state.branch} that reads origin cannot find it and the handoff is "
            "not complete. Re-run report-ready with the same arguments to write "
            "the relay ref."
        )
    return None


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
    after the relay push, whatever that did: a relay failure fails report-ready
    only once the markers are gone (#5035). A failure here is loud, never
    skipped: the state stays recorded, and a re-run of report-ready (a frozen
    re-run, which pushes nothing new) removes them.
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
        recorded_by=identity_mode.current_mode().value,
    )
    _push_branch(branch, lease_sha=lease_sha)
    transport.write(state)
    relay_failure = _push_relay_ref(state, args.base)
    response: dict[str, object] = {"ok": True, "status": state.status}
    if batch:
        try:
            _remove_batch_markers(marker_refs)
        except WorkflowError as exc:
            if relay_failure is None:
                raise
            raise WorkflowError(f"{relay_failure}\n{exc}") from exc
    if relay_failure is not None:
        # The recording is done (markers included), so the re-run it names is a
        # frozen re-run that only writes the relay ref; but the handoff is not
        # complete, so report-ready fails rather than print ok (#5035).
        raise WorkflowError(relay_failure)
    if batch:
        # Printed as this repo's #N refs, the form batch-start and batch-show
        # print, so a reader matches the lists as they are (#4297). The state
        # keeps the values as given: vrg-submit-pr reads them from it.
        response["issues"] = marker_refs
    else:
        preview = _planning_close_preview(issue_refs[0])
        if preview is not None:
            response["planning_close"] = preview
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


@dataclass(frozen=True)
class _Split:
    """A release group that a set would split: its ID and its members on each side."""

    group: str
    leaving: list[str]
    staying: list[str]

    def as_json(self) -> dict[str, object]:
        return {"group": self.group, "leaving": self.leaving, "staying": self.staying}


def _judge_extract(root: Path, issues: list[str]) -> tuple[list[_Split], list[str]]:
    """The group split a set would make in its batch, and the issues that remain.

    Spec §5.5, read-only: a release group splits when it has a member of the
    set and another member of the batch that stays. A group member outside the
    batch does not stay in it. Raises :class:`WorkflowError` when the set is not
    one batch's or a group cannot be read, so nothing is judged from a guess.
    """
    marker = batch_marker.batch_of(root, issues)
    leaving_set = set(issues)
    remaining = [issue for issue in marker.issues if issue not in leaving_set]
    repo = _clone_repo()
    refs = {issue: epics.parse_issue_ref(issue, default_repo=repo) for issue in marker.issues}
    groups: dict[tuple[str, str], record.Group] = {}
    try:
        for issue in marker.issues:
            for group in record.groups_of(refs[issue]):
                groups.setdefault((group.epic.slug, group.group), group)
    except record.GroupRecordError as exc:
        msg = (
            f"batch-extract: cannot tell whether the set {', '.join(issues)} splits a"
            f" release group ({exc}); nothing was changed"
        )
        raise WorkflowError(msg) from exc

    split: list[_Split] = []
    for group in sorted(groups.values(), key=lambda g: (g.epic.slug, int(g.group[1:]))):
        keys = {_ref_key(member) for member in group.members}
        members = [issue for issue in marker.issues if _ref_key(refs[issue]) in keys]
        leaving = [issue for issue in members if issue in leaving_set]
        staying = [issue for issue in members if issue not in leaving_set]
        if leaving and staying:
            split.append(_Split(group.group, leaving, staying))
    return split, remaining


def _ref_key(ref: epics.IssueRef) -> tuple[str, str, int]:
    """An issue's identity, the repo compared without case."""
    return (ref.owner.lower(), ref.repo.lower(), ref.number)


def _split_refusal(split: list[_Split]) -> str:
    """The refusal naming each group the set would split."""
    lines = "; ".join(
        f"release group {entry.group} (leaving {', '.join(entry.leaving)}, staying"
        f" {', '.join(entry.staying)})"
        for entry in split
    )
    return (
        f"batch-extract refused: the set would split {lines}. A group leaves a batch"
        " whole: take all its members out together, or record a group change with the"
        " human first. Nothing was changed."
    )


def cmd_batch_extract(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Take a set of issues out of their batch, refusing a group split first.

    The judgment runs before anything is changed, and ``--check`` stops after it.
    """
    issues = [_batch_ref(str(raw)) for raw in args.issue]
    root = git.main_worktree_root()
    split, judged_remaining = _judge_extract(root, issues)
    if args.check:
        _emit(
            {
                "ok": not split,
                "split": [entry.as_json() for entry in split],
                "remaining": judged_remaining,
            }
        )
        return 0 if not split else 1
    if split:
        raise WorkflowError(_split_refusal(split))
    remaining = batch_marker.extract(root, issues)
    _emit(
        {
            "ok": True,
            "extracted": issues,
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


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _check_pr_fields(pr: str) -> tuple[str, str, str]:
    """The PR's body, base branch and head commit; a failed or malformed read refuses.

    The base and head are read with ``gh pr view``, the body only through the
    trust layer (:func:`_check_body`, epic vergil-project/.github#1467).
    """
    try:
        repo, number = github.pr_repo_and_number(pr, "batch-check")
        info = github.read_json("pr", "view", pr, "--json", "baseRefName,headRefOid")
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise WorkflowError(f"batch-check: cannot read PR {pr}: {error_text(exc)}") from exc
    fields = info if isinstance(info, dict) else {}
    base, head = fields.get("baseRefName"), fields.get("headRefOid")
    if not isinstance(base, str) or not base:
        raise WorkflowError(f"batch-check: cannot read PR {pr}: no base in {info!r}")
    if not isinstance(head, str) or _SHA_RE.fullmatch(head) is None:
        raise WorkflowError(f"batch-check: cannot read PR {pr}: no head commit in {info!r}")
    return _check_body(pr, repo, number), base, head


def _check_body(pr: str, repo: str, number: int) -> str:
    """The PR's body, read through the trust layer; an untrusted body refuses.

    The body's ``Closes`` lines say whether the PR is a batch and set the
    issue order its commits are held to, so a body the layer does not trust
    (its author or any editor holds no role) leaves nothing to check by: the
    check fails, naming the untrusted identity by metadata only, rather than
    pass the PR as "not a batch".
    """
    try:
        content = trust.read_issue(repo, number, require_body=True)
    except trust.UntrustedContentError as exc:
        raise WorkflowError(
            f"batch-check: PR {pr}: its body is untrusted, so its Closes lines cannot be "
            f"checked: {exc.item.describe()} ({exc.item.cause}). The human decides next."
        ) from exc
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError, ValueError) as exc:
        raise WorkflowError(
            f"batch-check: cannot read the body of PR {pr}: {error_text(exc)}"
        ) from exc
    if content.body is None:
        # require_body raises on an untrusted body; a missing one is never read as empty.
        raise WorkflowError(f"batch-check: cannot read the body of PR {pr}: no trusted body")
    return content.body


def _check_closing_refs(pr: str, body: str) -> str:
    """Hold the body's ``Closes`` refs to GitHub's closing references; return the PR's repo.

    *body* is the trusted body :func:`_check_body` read. GitHub's list is
    only compared with it, and any difference refuses: a reference the body
    does not close (a hand link, by anyone) or a ``Closes`` line GitHub does
    not list.
    """
    try:
        refs = github.pr_closing_references(pr)
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, OSError, RuntimeError, ValueError) as exc:
        raise WorkflowError(
            f"batch-check: cannot read the closing issue references of PR {pr}: {error_text(exc)}"
        ) from exc
    try:
        batch_rules.check_closing_refs(body, [issue.ref for issue in refs.issues], repo=refs.repo)
    except batch_rules.BatchRuleError as exc:
        raise WorkflowError(f"batch-check: PR {refs.ref}: {exc}") from exc
    return refs.repo


def _check_commits_at(pr: str, base: str, head: str) -> tuple[str, str]:
    """Fetch the base, and *head* when it is not here; return both as commit SHAs.

    The base is fetched into its own remote-tracking ref. The head is fetched
    by its SHA alone, so the PR branch's remote-tracking ref, the one a
    ``--force-with-lease`` push checks against, never moves.
    """
    try:
        git.run("fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        base_sha = _commit_sha(f"refs/remotes/origin/{base}")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"batch-check: cannot fetch PR {pr}'s base {base}: {error_text(exc)}"
        ) from exc
    try:
        if not git.ref_exists(f"{head}^{{commit}}") and _SHA_RE.fullmatch(head) is not None:
            git.run("fetch", "origin", head)
        return base_sha, _commit_sha(head)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"batch-check: cannot find the commit {head} to check for PR {pr}: {error_text(exc)}"
        ) from exc


def _commit_sha(rev: str) -> str:
    return git.read_output("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")


def cmd_batch_check(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Check a submitted batch PR; exit 0 when it passes or is not a batch PR.

    The checks are :func:`batch_rules.check_closing_refs` against GitHub's
    closing references, then :func:`batch_rules.check_commit_per_issue` from
    the PR's base, as fetched now, to ``--head`` (or the PR's head), the
    issues in the body's ``Closes`` order. The hard rules are not run: they
    read issue state a push cannot change, and submit checked them (epic
    vergil-project/.github#1218, spec §6.8). It writes no state and makes no
    freeze check, so it runs on a submitted, frozen branch.
    """
    pr = str(args.pr)
    if args.head is not None and (not args.head or args.head.startswith("-")):
        raise WorkflowError(f"batch-check: --head is not a git revision: {args.head!r}")
    body, base, pr_head = _check_pr_fields(pr)
    closes = batch_rules.batch_closes(body)
    if not closes:
        print(
            f"PR {pr} is not a batch PR (its body has one Closes line or none); nothing to check."
        )
        return 0
    repo = _check_closing_refs(pr, body)
    try:
        clone = _clone_repo()
    except WrongCloneError as exc:
        raise WorkflowError(f"batch-check: {exc}") from exc
    if clone.lower() != repo.lower():
        raise WorkflowError(
            f"batch-check: PR {pr} is in {repo}, but this is a clone of {clone}; "
            f"run it from a clone of {repo}"
        )
    base_sha, head_sha = _check_commits_at(pr, base, args.head or pr_head)
    issues = [f"#{ref.rpartition('#')[2]}" for ref in closes]
    try:
        batch_rules.check_commit_per_issue(issues, base=base_sha, head=head_sha)
    except batch_rules.BatchRuleError as exc:
        raise WorkflowError(f"batch-check: PR {pr} at {head_sha[:12]}: {exc}") from exc
    print(
        f"Batch check passed for PR {pr} at {head_sha}: it closes {', '.join(issues)}, "
        f"one commit per issue since {base} ({base_sha[:12]})."
    )
    return 0


#: The base the batch probe judges against: the freshly fetched develop.
BATCH_GATES_BASE = "origin/develop"


def _batch_gate_candidates(commits: list[tuple[str, str]]) -> list[int]:
    """The issue numbers the branch's own (non-fixup) commits name, in branch order, once each."""
    numbers: list[int] = []
    for _sha, subject in commits:
        number = merge_record.subject_issue(subject)
        if number is None or subject.startswith(merge_record.FIXUP_PREFIX):
            continue
        if number not in numbers:
            numbers.append(number)
    return numbers


def _batch_gate_line(commit: agent_gates.CommitVerdict) -> str:
    """One commit's verdict as a line for a person to read."""
    short = commit.sha[:12] if commit.sha is not None else "(no single commit)"
    state = "covered" if commit.covered else "UNCOVERED"
    line = f"{commit.issue}  {short}  {state}"
    return line if commit.reason is None else f"{line}  {commit.reason}"


def cmd_batch_gates(args: argparse.Namespace, _transport: LocalFileTransport) -> int:
    """Judge a batch's merge-review gate commit by commit, before handoff; write nothing.

    The batch is the one the branch's issues' markers list: the first issue
    (in branch order) that has a marker gives its issues. The gate's
    merge-review check (:func:`agent_gates.evaluate_commits`) runs on
    ``origin/develop..HEAD`` as ``vrg-submit-pr`` runs it. Exits 0 when every
    commit is covered, 1 otherwise, and 1 with the failure on stderr on any
    read failure or when no marker is found (report-ready removed them).
    The only git write is the fetch of ``origin/develop``, and it needs no
    freeze check.
    """
    try:
        branch = git.current_branch()
        head = git.read_output("rev-parse", "--verify", "HEAD^{commit}")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(f"batch-gates: cannot read HEAD ({error_text(exc)})") from exc
    if branch == "HEAD":
        raise WorkflowError("batch-gates: HEAD is detached; check out the batch's branch first.")
    # Quiet and captured: git's ref report must not reach the verdicts on stdout.
    try:
        git.read_output("fetch", "-q", "origin", "develop")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(
            f"batch-gates: cannot fetch origin develop: {error_text(exc)}; nothing was written"
        ) from exc
    repo = Path(git.repo_root())
    try:
        commits = merge_record.branch_commits(repo, BATCH_GATES_BASE, head)
        root = git.main_worktree_root()
    except (merge_record.RecordError, subprocess.CalledProcessError, OSError) as exc:
        raise WorkflowError(f"batch-gates: {error_text(exc)}; nothing was written") from exc
    marker = None
    for number in _batch_gate_candidates(commits):
        marker = batch_marker.read_marker(root, f"#{number}")
        if marker is not None:
            break
    if marker is None:
        raise WorkflowError(
            "batch-gates: no batch marker: batch-gates runs only before handoff"
            " (report-ready removes the markers); nothing was written"
        )
    try:
        repo_name = _clone_repo()
    except WrongCloneError as exc:
        raise WorkflowError(f"batch-gates: {exc}; nothing was written") from exc
    try:
        authors = load_trusted_authors()
    except (TrustedAuthorsNotConfiguredError, ValueError) as exc:
        raise WorkflowError(f"batch-gates: {exc}; nothing was written") from exc
    issue_refs = [f"{repo_name}{issue}" for issue in marker.issues]
    verdicts = agent_gates.evaluate_commits(
        repo, base=BATCH_GATES_BASE, head=head, issue_refs=issue_refs, authors=authors
    )
    if verdicts.read_failure:
        failure = "; ".join(verdicts.extra_reasons)
        raise WorkflowError(f"batch-gates: {failure}; nothing was written")
    if args.json:
        _emit(
            {
                "branch": branch,
                "head": head,
                "commits": [
                    {"issue": c.issue, "sha": c.sha, "covered": c.covered, "reason": c.reason}
                    for c in verdicts.commits
                ],
            }
        )
    else:
        for commit in verdicts.commits:
            print(_batch_gate_line(commit))
    for reason in verdicts.extra_reasons:
        print(f"batch-gates: {reason}", file=sys.stderr)
    return 0 if verdicts.ok else 1


if __name__ == "__main__":
    sys.exit(main())
