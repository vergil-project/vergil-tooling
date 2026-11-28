"""PR submission wrapper that constructs standards-compliant PR bodies.

Supports three modes:
- **Template mode** (no args): reads the PR workflow state file
  (``.vergil/pr-workflow.json``); shows the selection table, pushes the
  branch, and creates the PR. From the repo root,
  ``--all``/``--select`` submit several ready worktrees at once (#1673).
- **CLI argument mode** (``--issue``/``--summary``/``--title``): existing
  direct invocation for human emergency use.
- **Named branch mode** (positional ``branches``): opens PRs for one or
  more named branches, worktree-free by default (issue #2368). Each
  branch's ready-state is resolved from a local worktree's
  ``pr-workflow.json`` when one exists, else the relay ref
  (``GitHubTransport``). The local-vs-relay decision is then made from
  **actual state**, not from the mere fact that a branch was named
  (issue #2794): when the branch has a local worktree ready-state **and**
  origin lacks the branch, it is pushed and opened locally
  (``push=True``) — the branch was developed on this machine and never
  reached origin. Otherwise it takes the relay path — origin's tip is verified
  against the recorded ``head_sha`` and the PR is opened **without**
  pushing (``--head`` names the source branch) — the Mac-side of the
  cloud→Mac relay handoff, where the branch and its metadata already rode
  GitHub.

It also has a **push mode** (``--push [--force-with-lease]``, epic
vergil-project/.github#1664, spec §5.4), which opens nothing: run in the
worktree of an open PR's branch, it pushes the checked-out ``HEAD`` to that
branch through the one PR path (:func:`vergil_tooling.lib.pr_path.push_to_open_pr`),
which runs the provenance check and the agent gates against the new head
before it pushes, and never pushes a head that fails them. Auto-merge stays
on throughout. ``--force-with-lease`` is for a rebased head: the lease is
the branch's remote-tracking ref, the head this clone last saw on origin. A
branch frozen by ``report-ready`` is refused, as ``vrg-git`` refuses its
push. It exits 0 once the head is pushed, 1 when it is refused, naming why.

**One path** (epic vergil-project/.github#1218, spec §6.2). Every mode gates
its PRs, prints one compact table of the selection (issues and linkage,
branch, title), then hands the whole selection to :func:`_submit_all`. There
is no confirmation prompt and no PR-body preview (vergil-tooling#4744): the
arguments already chose the selection, and the body is on the opened PR.
For each PR it
re-checks a batch PR's rules, pushes the branch when the mode pushes, holds
origin's tip to the head the PR must open at, opens the PR and records the
submission, runs the provenance check, and arms it through the one PR path
(:func:`vergil_tooling.lib.pr_path.arm`, epic vergil-project/.github#1664),
pinned to that head, with the method the path works out from the PR: rebase
for a batch PR, squash otherwise. A PR the path refuses fails its submit
with auto-merge off. A PR a release holds (the release lock is held, spec
§5.5) is left unarmed and reported as ``held for release <version>; the
release re-arms it``, and goes on to the finalize watch. A PR whose
submit fails is named in the final summary and the other PRs continue; a
provenance violation always fails that PR's submit and leaves its auto-merge
off (vergil-tooling#4695). Submit never rebases a branch:
rebasing before handoff stays the agent's job.

The selection table, refusals and ``--dry-run`` print plainly (a dry run
prints the table and what would follow). Then :func:`_submit_all` runs as a
progress rollup
(:mod:`vergil_tooling.lib.progress`, as ``vrg-release`` does) with one stage
per PR, named after its branch (vergil-tooling#4709): each step's output
goes to ``.vergil/vrg-submit-pr-<stamp>.log`` in the main worktree, each
stage ends on one ✓ / ✗ line with its duration and outcome, and the summary
points at the log. ``--output-format`` and ``--output-window`` choose the
renderer, as for the other progress commands. The chained finalize then
prints its own rollup.

PRs are opened against ``develop`` only: a ``--base``, or a recorded base,
naming any other branch is refused, since releases go through
``vrg-release``.

Every mode driven by a ``report-ready`` state (template, batch and named
branch) opens the PR only at the ``head_sha`` the latest ``report-ready``
recorded, and refuses loudly rather than pick another head (issue #3815):
the checked-out HEAD of a local worktree must be that commit, a named
branch reopened by ``vrg-pr-workflow unfreeze`` and not re-reported is
refused, a local state and relay ref that disagree are refused, and
origin's tip is held to the recorded head immediately before ``gh pr
create``. Auto-merge is pinned to that same head (``expectedHeadOid``).

A ``report-ready`` state may hand off a batch: 2 to 20 issues on one branch,
one commit per issue (epic vergil-project/.github#1019). Every mode driven by
such a state runs the per-issue refusals on every issue, writes one
``Closes`` line per issue, and re-checks the batch's hard rules and
commit-per-issue invariant on the head the PR opens at, against a freshly
fetched ``origin/develop``, before any push or ``gh pr create``. The CLI
emergency mode stays single-issue.

The template and CLI modes push the branch using the human's host
credentials before creating the PR. Because the human is the superset
of any agent's rights, this carries workflow-touching pushes that the
agent's own credentials would be rejected for. The named-branch mode
pushes only when it falls back to the local path for a branch that never
reached origin; the relay path itself never pushes — the branch is
already on origin.

**Who may submit** (epic vergil-project/.github#1384, spec §4.6). The human
may use every mode. The user agent may submit only in the modes
``report-ready`` drives, the template mode and its ``--all``/``--select``
batch: it is refused with ``--release``, in the CLI emergency mode
(``--issue``/``--summary``/``--title``) and on the named-branch relay path.
The audit agent is refused outright.

**The gate** (:mod:`vergil_tooling.lib.agent_gates`). For every PR, a
single-issue one included, the submit rollup runs one more step after the
trust-layer preflight and before any push or ``gh pr create``: it fetches
``origin/develop`` (and the branch on the relay path, which never pushes) and
applies the merge-review rule to ``origin/develop...<head>``. A failure
refuses that PR with every reason and leaves nothing pushed and no PR open,
so a re-run once the cause is fixed (a merge review closed, or a
merge-review exemption approved) submits normally. The gate runs on every
submission, whoever submits it and whoever recorded the ready-state, and
nothing skips it.

**Finalize always** (spec §6.2). Once every PR in the selection has been
tried, submit runs **one** ``vrg-finalize-pr <pr>,<pr>,…`` over the PRs whose
submit succeeded: it watches them merge, then settles develop once. A PR
whose submit failed is left out of that list and named in a summary printed
after finalize's report. When no PR opened, finalize is not run at all.
Finalize runs as a subprocess from the main worktree (not ``exec``), so
control returns here for that summary. ``$VRG_FINALIZE_DEADLINE`` is checked
before any PR opens, so a malformed value refuses the run with nothing done.

- ``--release`` is passed to finalize, which chains into ``vrg-release`` only
  on full success. It is dropped when any PR failed to submit, and the
  summary says the release was skipped. Install is part of every release.

Submit always finishes: it returns only once each PR has merged or cannot
merge (spec §4.8 of epic vergil-project/.github#1440).

**Exit status.** Submit returns finalize's exit status (0 full success, 3
not every PR merged, 4 develop did not validate, 5 the release failed, 6
every PR that did not merge is held by release: run ``vrg-finalize-pr``
again), with two adjustments: when any PR failed to submit it returns at
least 3 (and 3 in place of a 6), and a tool error from finalize (1, or any
code finalize does not define) always wins. When no PR opened, a failed
submit exits 3. A usage error, or a refusal before anything opened, exits 1.

**Removed flags** are refused by name before any side effect: ``--finalize``
(submit always finalizes), ``--no-finalize`` (submit returns once each PR
has merged or cannot merge), ``--install`` and ``--no-install`` (install is
part of every release), ``--allow-provenance-violation`` (a provenance
violation always fails; fix its cause or the classifier) and ``--yes``/``-y``
(there is no confirmation left to answer).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    agent_gates,
    epics,
    finalize_watch,
    git,
    github,
    identity_mode,
    planning_close,
    pr_create,
    pr_path,
    pr_provenance,
    progress,
    trust,
    worktrees,
)
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.linkage import (
    ALLOWED_LINKAGES,
    normalize_linkage,
)
from vergil_tooling.lib.pr_body import build_pr_body, resolve_issue_ref
from vergil_tooling.lib.pr_workflow import batch_rules, freeze, submission
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError
from vergil_tooling.lib.pr_workflow.github_transport import GitHubTransport
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release import gate
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


_AUDIT_REFUSAL = (
    "vrg-submit-pr: the audit agent cannot submit PRs; only the user agent, through "
    "'vrg-pr-workflow report-ready' and then vrg-submit-pr, and the human may."
)
_AGENT_RELEASE_REFUSAL = (
    "vrg-submit-pr: --release is human-only: releases and promotion need a human "
    "maintainer. Submit without --release; the human releases."
)
_AGENT_CLI_REFUSAL = (
    "vrg-submit-pr: the --issue/--summary/--title mode is the human maintainer's "
    "emergency path. An agent records the PR metadata with 'vrg-pr-workflow "
    "report-ready' and then runs vrg-submit-pr from the worktree (or --all/--select "
    "from the repo root)."
)
_AGENT_RELAY_REFUSAL = (
    "vrg-submit-pr: the named-branch relay path is for the human maintainer. An agent "
    "records the PR metadata with 'vrg-pr-workflow report-ready' and then runs "
    "vrg-submit-pr from the worktree (or --all/--select from the repo root)."
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # The identity gate runs after parsing, so --help prints the full usage to
    # any identity (issue #4178): an agent reads it to see which modes it may
    # use, and the help itself says which are human-only.
    refusal = _identity_refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    # The installed version heads every run, so its output (and the run log,
    # whose first line repeats it) ties the run to a release (issue #4994).
    print(progress.tooling_version_line(_SUBMIT_COMMAND))

    refusal = _flag_refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    if args.push or args.force_with_lease:
        return _run_push_mode(args)

    # The finalize run's batch deadline is checked before any PR opens, so a
    # malformed $VRG_FINALIZE_DEADLINE refuses the run with nothing done.
    try:
        finalize_watch.deadline_secs()
    except ValueError as exc:
        print(f"vrg-submit-pr: {exc}; no PR was opened.", file=sys.stderr)
        return 1

    # A release cascade on a repo that is not released is refused before any
    # push, PR or merge, so nothing changes (issue #4175). The check reads the
    # main worktree's vergil.toml, the checkout vrg-release would run from.
    if args.release:
        refusal = gate.unreleased_refusal()
        if refusal is not None:
            print(f"vrg-submit-pr --release: {refusal}", file=sys.stderr)
            return 1

    # A trust-layer failure before any PR opens (an untrusted relay payload or
    # body, a trust read that cannot complete, no trusted-authors configuration)
    # escapes to cli_main, which ends the run with its one-line message.
    if args.branches:
        # Relay path: branches identify the submit. It opens PRs worktree-free
        # (from the branch's relayed ready-state), then finalizes them locally
        # like every other mode. It still rejects the worktree-selection and
        # single-PR CLI flags, which name a different path.
        if args.all_worktrees or args.select is not None:
            print(
                "vrg-submit-pr: branch arguments select the submit directly and "
                "cannot be combined with --all/--select.",
                file=sys.stderr,
            )
            return 1
        if args.issue is not None or args.summary is not None or args.title is not None:
            print(
                "vrg-submit-pr: branch arguments cannot be combined with "
                "--issue/--summary/--title (those drive the single-PR CLI path).",
                file=sys.stderr,
            )
            return 1
        return _run_relay(args.branches, args)

    cli_fields = [args.issue, args.summary, args.title]
    has_any = any(f is not None for f in cli_fields)
    has_all = all(f is not None for f in cli_fields)

    if has_any and not has_all:
        missing = []
        if args.issue is None:
            missing.append("--issue")
        if args.summary is None:
            missing.append("--summary")
        if args.title is None:
            missing.append("--title")
        print(
            f"vrg-submit-pr: The following required arguments are missing: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    if has_all:
        return _run_cli_mode(args)
    return _run_template_mode(args)


def _identity_refusal(args: argparse.Namespace) -> str | None:
    """The refusal for the current identity and these *args*, or ``None`` (spec §4.6).

    The audit agent is refused outright. The user agent is refused with
    ``--release``, the named-branch relay path and the CLI emergency mode,
    and allowed in the
    template and batch modes, which ``report-ready`` drives. Any other use by
    the human or the automation App is not refused here.
    """
    mode = identity_mode.current_mode()
    if mode is IdentityMode.AUDIT:
        return _AUDIT_REFUSAL
    if mode is not IdentityMode.USER:
        return None
    if args.release:
        return _AGENT_RELEASE_REFUSAL
    if args.branches:
        return _AGENT_RELAY_REFUSAL
    if any(f is not None for f in (args.issue, args.summary, args.title)):
        return _AGENT_CLI_REFUSAL
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Create a standards-compliant pull request against develop, then finalize "
            "it. The user agent may submit only what report-ready recorded: run "
            "'vrg-pr-workflow report-ready --issue <N> --title <title> --summary "
            "<summary> --notes <notes>', then vrg-submit-pr from the worktree (or "
            "--all/--select from the repo root). The branch arguments, the "
            "--issue/--summary/--title mode and --release are human-only, and the "
            "audit agent is refused. Every PR must pass the merge-review gate before "
            "anything is pushed or opened."
        ),
    )
    parser.add_argument(
        "branches",
        nargs="*",
        help="One or more branches to open PRs for, worktree-free. Each branch's "
        "ready-state is resolved from a local worktree's pr-workflow.json when "
        "present, else the relay ref; a branch that is on origin is opened "
        "without pushing (relay), while a branch with a local ready worktree that "
        "never reached origin is pushed then opened. With no branch arguments, "
        "the local-worktree flow runs unchanged.",
    )
    parser.add_argument(
        "--issue", default=None, help="Issue reference: number or owner/repo#number"
    )
    parser.add_argument("--summary", default=None, help="One-line PR summary")
    parser.add_argument(
        "--linkage", default="Ref", choices=ALLOWED_LINKAGES, help="Issue linkage keyword"
    )
    parser.add_argument("--notes", default="", help="Additional notes")
    parser.add_argument("--title", default=None, help="PR title")
    parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    parser.add_argument(
        "--base",
        default=None,
        help="Target branch. PRs are opened against develop only (the default); "
        "any other branch is refused, since releases go through vrg-release.",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Human-only. Pass --release to vrg-finalize-pr, which chains into "
        "vrg-release (install is part of every release) only on full success. "
        "Dropped when any PR failed to submit.",
    )
    # Removed flags. Kept hidden so main can refuse each by name, naming the
    # reason, rather than argparse rejecting it as unknown (spec §6.10).
    parser.add_argument("--finalize", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-finalize", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-provenance-violation", action="store_true", help=argparse.SUPPRESS)
    # The two removed human skips of the merge-review gate took a reason; they
    # still do, so the reason is never read as a branch argument.
    parser.add_argument("--override-merge-review", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--human-only", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--all",
        dest="all_worktrees",
        action="store_true",
        help="Select every ready worktree for a batch submission (issue #1673).",
    )
    parser.add_argument(
        "--select",
        default=None,
        help="Comma-separated list of ready worktrees to batch-submit, by issue "
        "number or worktree directory name (issue #1673).",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push mode: from the worktree of an open PR's branch into develop, push "
        "HEAD to that branch through the one PR path, which runs the provenance check "
        "and the agent gates on the new head before pushing it. Opens no PR and leaves "
        "auto-merge on. Takes no other mode's arguments.",
    )
    parser.add_argument(
        "--force-with-lease",
        dest="force_with_lease",
        action="store_true",
        help="With --push, for a rebased head: push with a lease on the branch's "
        "remote-tracking ref, so a head pushed elsewhere since the last fetch is "
        "never overwritten.",
    )
    parser.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)
    progress.add_progress_args(parser, ())
    return parser.parse_args(argv)


FINALIZE_REMOVED = "vrg-submit-pr: --finalize was removed; vrg-submit-pr always finalizes"
NO_FINALIZE_REMOVED = (
    "vrg-submit-pr: --no-finalize was removed; "
    "vrg-submit-pr returns once each PR has merged or cannot merge"
)
INSTALL_REMOVED = "vrg-submit-pr: --install was removed; install is part of every release"
NO_INSTALL_REMOVED = "vrg-submit-pr: --no-install was removed; install is part of every release"
ALLOW_PROVENANCE_VIOLATION_REMOVED = (
    "vrg-submit-pr: --allow-provenance-violation was removed; a provenance "
    "violation always fails. Fix its cause, or fix the classifier if the "
    "violation is a false one"
)
_GATE_ONLY = (
    "the merge-review gate runs on every submission and is met only by a merge-review "
    "record or a merge-review exemption the human approved "
    "(vrg-ruling request --kind merge-review-exemption)"
)
OVERRIDE_MERGE_REVIEW_REMOVED = f"vrg-submit-pr: --override-merge-review was removed; {_GATE_ONLY}"
HUMAN_ONLY_REMOVED = f"vrg-submit-pr: --human-only was removed; {_GATE_ONLY}"
YES_REMOVED = (
    "vrg-submit-pr: --yes was removed; submit no longer asks for confirmation, so drop the flag"
)


def _flag_refusal(args: argparse.Namespace) -> str | None:
    """The refusal for a removed flag, or ``None``.

    Checked before any side effect, so a caller still passing a removed flag
    is told what replaced it rather than having it silently accepted (spec
    §6.10).
    """
    if args.finalize:
        return FINALIZE_REMOVED
    if args.no_finalize:
        return NO_FINALIZE_REMOVED
    if args.install:
        return INSTALL_REMOVED
    if args.no_install:
        return NO_INSTALL_REMOVED
    if args.allow_provenance_violation:
        return ALLOW_PROVENANCE_VIOLATION_REMOVED
    if args.override_merge_review is not None:
        return OVERRIDE_MERGE_REVIEW_REMOVED
    if args.human_only is not None:
        return HUMAN_ONLY_REMOVED
    if args.yes:
        return YES_REMOVED
    return None


_PUSH_PREFIX = "vrg-submit-pr --push"


def _push_mode_refusal(args: argparse.Namespace) -> str | None:
    """Why *args* are not a push-mode run, or ``None`` when they are."""
    if not args.push:
        return "vrg-submit-pr: --force-with-lease is a push-mode flag; pass it with --push"
    others = {
        "branch arguments": bool(args.branches),
        "--issue": args.issue is not None,
        "--summary": args.summary is not None,
        "--title": args.title is not None,
        "--all": args.all_worktrees,
        "--select": args.select is not None,
        "--release": args.release,
        "--dry-run": args.dry_run,
        "--base": args.base is not None,
    }
    named = [name for name, given in others.items() if given]
    if named:
        return f"{_PUSH_PREFIX}: takes no other mode's arguments, got {', '.join(named)}"
    return None


def _run_push_mode(args: argparse.Namespace) -> int:
    """Push the worktree's ``HEAD`` to its open PR's branch through the one PR path.

    Exit 0 once the head is pushed, 1 when the push is refused or a step
    cannot be completed, naming why. Nothing is pushed on a refusal.
    """
    refusal = _push_mode_refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    try:
        root = Path(git.read_output("rev-parse", "--show-toplevel"))
        branch = git.current_branch()
        head = git.read_output("rev-parse", "--verify", "HEAD^{commit}")
    except subprocess.CalledProcessError as exc:
        msg = f"{_PUSH_PREFIX}: cannot read the worktree's branch: {error_text(exc)}"
        print(msg, file=sys.stderr)
        return 1
    if branch == "HEAD":
        print(f"{_PUSH_PREFIX}: HEAD is detached; run it on the PR's branch", file=sys.stderr)
        return 1
    check = freeze.check_worktree(root, action="push")
    if check.read_error is not None:
        print(f"{_PUSH_PREFIX}: warning: {check.read_error}", file=sys.stderr)
    if check.frozen:
        print(check.message, file=sys.stderr)
        return 1
    lease = None
    if args.force_with_lease:
        tracking = f"refs/remotes/origin/{branch}"
        try:
            lease = git.read_output("rev-parse", "--verify", f"{tracking}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            print(
                f"{_PUSH_PREFIX}: --force-with-lease needs {tracking}, the head this clone "
                f"last saw on origin, and it cannot be read ({error_text(exc)}); run "
                "`vrg-git fetch origin` first",
                file=sys.stderr,
            )
            return 1
    try:
        result = pr_path.push_to_open_pr(branch, head=head, lease=lease)
    except pr_path.PRPathError as exc:
        print(f"{_PUSH_PREFIX}: {exc}; nothing was pushed", file=sys.stderr)
        return 1
    if not result.pushed:
        where = f" ({result.pr_url})" if result.pr_url else ""
        print(f"{_PUSH_PREFIX}: refused{where}: {result.detail}", file=sys.stderr)
        return 1
    print(f"{_PUSH_PREFIX}: {result.detail} ({result.pr_url}); auto-merge stays on.")
    return 0


_DEVELOP = "develop"

_BASE_REFUSAL = (
    "vrg-submit-pr: PRs are opened against develop only; releases go through vrg-release"
)


def _target_branch(base_override: str | None, oracle_base: str | None = None) -> str:
    """Resolve the PR's target branch, which is always ``develop``.

    An explicit ``--base`` (*base_override*) or the base the oracle recorded
    (*oracle_base*, ``origin/`` stripped) naming any other branch is refused
    (exit 1) before anything is pushed or opened (spec §6.2), each checked on
    its own, so ``--base develop`` cannot open a PR whose ready-state
    recorded another base. Release PRs are opened by ``vrg-release``, never
    this path.

    There is deliberately no branch-name inference here. The legacy
    ``release/`` → ``main`` special-case silently retargeted PRs (issue #1609).
    """
    for named in (base_override, oracle_base):
        if named and named.removeprefix("origin/") != _DEVELOP:
            raise SystemExit(_BASE_REFUSAL)
    return _DEVELOP


def _push_branch(branch: str) -> None:
    """Push *branch* to origin, tolerating a rebased (diverged) remote.

    Submitting a PR routinely follows a rebase onto the current base
    branch to clear stale drift; that leaves any previously-pushed remote
    branch diverged, and a plain push is rejected non-fast-forward.
    ``--force-with-lease`` is the safe overwrite: it updates the remote
    only while it still matches our remote-tracking ref, so it refuses to
    clobber commits pushed elsewhere since our last fetch. Bare
    ``--force`` is never used. (Issue #1557.)
    """
    try:
        git.run("push", "--force-with-lease", "-u", "origin", branch)
    except subprocess.CalledProcessError as exc:
        merged = _merged_pr_of_gone_branch(branch)
        if merged is not None:
            # The lease is refused as stale when origin deleted the branch
            # after its PR merged: say that, not "the remote moved" (#4993).
            msg = (
                f"vrg-submit-pr: pushing '{branch}' to origin failed: the branch "
                f"is gone from origin and its PR #{merged.number} has merged "
                f"({merged.url}).\n"
                "  This worktree is cleanup, not a submit: run "
                "`vrg-finalize-pr --cleanup-only` to remove it."
            )
            raise SystemExit(msg) from exc
        msg = (
            f"vrg-submit-pr: pushing '{branch}' to origin failed.\n"
            "  --force-with-lease was refused, which means the remote branch "
            "moved since your last fetch.\n"
            "  Run `vrg-git fetch origin` and review the remote commits before "
            "retrying, so you don't overwrite someone else's work."
        )
        raise SystemExit(msg) from exc


# A failed read of a branch's PRs: gh itself, the trust layer's read, or no
# trusted-authors configuration.
_PR_LOOKUP_ERRORS = (
    subprocess.CalledProcessError,
    trust.TrustReadError,
    TrustedAuthorsNotConfiguredError,
)


def _merged_pr_of_gone_branch(branch: str) -> trust.PRRef | None:
    """The merged PR of *branch* when origin no longer has the branch, else ``None``.

    Called only to explain a failed push. A failed read is raised as the
    push's failure, naming what could not be read, never hidden.
    """
    try:
        if _origin_has_branch(branch):
            return None
        prs = worktrees.branch_prs(branch, state="all")
    except _PR_LOOKUP_ERRORS as exc:
        msg = (
            f"vrg-submit-pr: pushing '{branch}' to origin failed, and checking "
            f"whether its PR has merged failed too: {error_text(exc)}"
        )
        raise SystemExit(msg) from exc
    merged = [pr for pr in prs if pr.state == "MERGED"]
    return max(merged, key=lambda pr: pr.number, default=None)


def _published_pr(branch: str, head_sha: str) -> trust.PRRef | None:
    """The PR that already publishes a ready worktree's branch, or ``None``.

    The local ``submitted`` stamp is set only by ``vrg-submit-pr``, so a PR
    opened by hand leaves the worktree looking ready (issue #4993). GitHub
    is asked instead: an open PR on *branch* means the work is in flight,
    and a merged PR whose head is the reported *head_sha* means it has
    landed and the worktree is cleanup. A PR closed without merging at the
    reported head is cleanup too, as ``vrg-repo-status`` and the finalize
    sweep count it, and is never opened again silently (issue #5038): a
    merged one at that head wins over it. A merged or closed PR at another
    head is a reused branch name (issue #1719), whose new tip is still to
    submit. Only same-repo PRs with a trusted author count
    (:func:`worktrees.branch_prs`). A failed read refuses the selection.
    """
    try:
        prs = worktrees.branch_prs(branch, state="all")
    except _PR_LOOKUP_ERRORS as exc:
        msg = (
            f"vrg-submit-pr: reading the PRs on '{branch}' failed: {error_text(exc)}; "
            "nothing was submitted."
        )
        raise SystemExit(msg) from exc
    opened = [pr for pr in prs if pr.state == "OPEN"]
    if opened:
        return max(opened, key=lambda pr: pr.number)
    for state in ("MERGED", "CLOSED"):
        at_head = [pr for pr in prs if pr.state == state and pr.head_oid == head_sha]
        if at_head:
            return max(at_head, key=lambda pr: pr.number)
    return None


def _closed_unmerged_ways_on(pr: trust.PRRef) -> str:
    """The two ways on from a PR closed without merging at the reported head (#5038)."""
    return (
        f"reopen PR #{pr.number} on GitHub to go on with it, or run "
        "`vrg-finalize-pr --cleanup-only` to remove the worktree"
    )


def _create_pr(*, target_branch: str, title: str, pr_body: str, head: str | None = None) -> str:
    """Run ``gh pr create`` once, never retried: :func:`_create_or_adopt` retries it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        tmp_path = f.name
    try:
        pr_url = github.create_pr(
            base=target_branch, title=title, body_file=tmp_path, head=head, retry=False
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return pr_url


def _create_or_adopt(sub: _Submission) -> str:
    """Open *sub*'s PR and return its URL, adopting the one a failed attempt opened.

    ``gh pr create`` is not idempotent, so it is never retried blindly:
    :func:`pr_create.create_or_adopt` looks for the open PR after each failed
    attempt and adopts it only when it matches *sub*'s branch, base, title and
    head commit (#4696). Any other open PR fails the submit, naming it.
    """
    spec = pr_create.PRSpec(
        head=sub.branch, base=sub.base, title=sub.title, head_oid=sub.expected_head
    )
    try:
        created = pr_create.create_or_adopt(
            lambda: _create_pr(
                target_branch=sub.base, title=sub.title, pr_body=sub.pr_body, head=sub.branch
            ),
            spec,
            opener="this submit",
            rerun="submit again",
        )
    except pr_create.PRCreateError as exc:
        raise _SubmitFailedError(str(exc)) from exc
    return created.url


# vrg-finalize-pr's exit codes (spec §6.3), the ones submit adjusts. Any other
# code from finalize (1, a tool or usage error, included) is passed through
# unchanged: a tool error always wins.
EXIT_OK = 0
EXIT_NOT_ALL_MERGED = 3
EXIT_HELD_BY_RELEASE = finalize_watch.EXIT_HELD_BY_RELEASE
_FINALIZE_OUTCOMES = frozenset({EXIT_OK, EXIT_NOT_ALL_MERGED, 4, 5, EXIT_HELD_BY_RELEASE})

# What progress.run_pipeline returns when the rollup is interrupted.
_INTERRUPTED = 130


def _finalize_cmd(opened: list[str], args: argparse.Namespace, *, release: bool) -> list[str]:
    """The one ``vrg-finalize-pr`` command over every opened PR.

    *release* is whether ``--release`` is passed: the caller drops it when
    any PR failed to submit. The progress
    flags ``--output-format`` and ``--output-window`` are passed on when
    given, so both halves of the run render alike (issue #4711); left out,
    finalize auto-detects its renderer as the submit rollup does.
    """
    cmd = ["vrg-finalize-pr", ",".join(opened)]
    if release:
        cmd.append("--release")
    if args.output_format is not None:
        cmd += ["--output-format", args.output_format]
    if args.output_window is not None:
        cmd += ["--output-window", str(args.output_window)]
    return cmd


def _chain_finalize(cmd: list[str]) -> int:
    """Run *cmd*, the one ``vrg-finalize-pr`` over the opened PRs, and return its exit code.

    Runs as a subprocess from the main worktree root, because vrg-finalize-pr
    refuses to run from a secondary worktree (it removes worktrees during
    cleanup) and template mode chdir'd into one. The child inherits the TTY,
    so its live progress display behaves exactly as a manual run.
    """
    print()
    print(f"Finalizing: {shlex.join(cmd)}")
    result = subprocess.run(cmd, cwd=git.main_worktree_root(), check=False)  # noqa: S603
    return result.returncode


def _submit_exit(finalize_rc: int, *, any_failed: bool) -> int:
    """Submit's exit code from finalize's and whether any PR failed to submit (spec §6.2).

    A code finalize does not define as an outcome (1, a tool error, above
    all) always wins. Otherwise a failed submit makes it at least 3 ("not
    every PR merged"), and a higher code from finalize (4 or 5) wins. Held
    by release (6) means every PR finalize watched that did not merge is
    held, which a failed submit makes untrue, so it becomes 3: ``max`` alone
    would keep 6.
    """
    if finalize_rc not in _FINALIZE_OUTCOMES:
        return finalize_rc
    if any_failed:
        if finalize_rc == EXIT_HELD_BY_RELEASE:
            return EXIT_NOT_ALL_MERGED
        return max(finalize_rc, EXIT_NOT_ALL_MERGED)
    return finalize_rc


def _dry_run_plan(args: argparse.Namespace) -> str:
    """The ``[dry-run]`` line naming what would follow the submit."""
    cmd = " ".join(_finalize_cmd(["<pr>,…"], args, release=args.release))
    return f"[dry-run] would finalize every opened PR in one run: {cmd}"


@dataclass(frozen=True)
class SubmitSummary:
    """What one submit run did (epic vergil-project/.github#1218, spec §6.2).

    *opened* holds the URL of every PR opened with auto-merge on, or merged
    at once because it could already merge. *failed* holds a ``(branch or
    PR, reason)`` pair for every PR whose submit failed: the branch when it
    failed before the PR opened, the PR's URL once it had, its reason then
    naming the head it opened at and the step that failed. *left_open* holds
    the URL of every PR that opened but failed a later step, so it is open
    without auto-merge (vergil-tooling#5198).
    """

    opened: list[str]
    failed: list[tuple[str, str]]
    left_open: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Submission:
    """One gated PR for :func:`_submit_all` to open.

    *expected_head* is the commit the PR must open at; auto-merge is pinned to
    it too. *push* says whether the branch is pushed first (the relay path's
    branch is already on origin). *record_in* is the worktree whose
    ``pr-workflow.json`` records the submission, ``None`` when there is none.
    *links* are the body's ``(linkage, ref)`` lines, shown in the selection
    table.
    """

    branch: str
    base: str
    title: str
    pr_body: str
    issue_refs: list[str]
    links: list[tuple[str, str]]
    expected_head: str
    push: bool
    record_in: Path | None


class _SubmitFailedError(Exception):
    """One PR's submit failed in a way :func:`_submit_all` reports and moves past."""


# The failures that end one PR's submit without stopping the others: a refusal
# from this module's checks (SystemExit with its message), a failed git or gh
# call, a malformed GitHub payload, a trust-layer failure (an untrusted or
# unreadable item, or no trusted-authors configuration to classify by), and
# auto-merge that could not be turned on.
_PER_PR_ERRORS = (
    SystemExit,
    subprocess.CalledProcessError,
    ValueError,
    TrustedAuthorsNotConfiguredError,
    trust.UntrustedContentError,
    trust.TrustReadError,
    pr_path.PRPathError,
    _SubmitFailedError,
)


def _failure_reason(exc: BaseException) -> str:
    """The reason a per-PR failure is reported with."""
    if isinstance(exc, SystemExit):
        return str(exc.code).removeprefix("vrg-submit-pr: ")
    return error_text(exc)


def _qualified(issue_ref: str) -> str:
    """*issue_ref* as ``owner/repo#N``; a bare ``#N`` is scoped to this clone's repo.

    A ref that cannot be qualified is returned as given, and the gates refuse it.
    """
    try:
        return epics.parse_issue_ref(issue_ref, default_repo=github.current_repo()).slug
    except ValueError:
        return issue_ref


def _run_gates(sub: _Submission) -> None:
    """Apply the merge-review gate to *sub* (spec §4.6).

    Runs for every submission, before any push or ``gh pr create``.
    ``origin/<base>`` is fetched, and the branch too when *sub.push* is false
    (a pushing submit must not move the ``--force-with-lease`` ref, and its
    head is the local commit); then :func:`agent_gates.evaluate` judges
    ``origin/<base>...<head>``. A failure raises :class:`_SubmitFailedError`
    naming every reason, with nothing pushed or opened.
    """
    authors = load_trusted_authors()
    refs = (sub.base,) if sub.push else (sub.branch, sub.base)
    try:
        git.run("fetch", "origin", *refs)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"could not fetch {', '.join(refs)} from origin for the gates: "
            f"{error_text(exc)}. No PR was opened."
        )
        raise _SubmitFailedError(msg) from exc
    repo = Path(git.repo_root())
    base = f"origin/{sub.base}"
    issue_refs = [_qualified(ref) for ref in sub.issue_refs]
    print(f"Checking the gates for {', '.join(issue_refs)} at {sub.expected_head[:12]}...")
    result = agent_gates.evaluate(
        repo, base=base, head=sub.expected_head, issue_refs=issue_refs, authors=authors
    )
    if not result.ok:
        msg = (
            "the gates refused the change: "
            + "; ".join(result.reasons)
            + ". Nothing was pushed and no PR was opened; submit again once fixed."
        )
        raise _SubmitFailedError(msg)


def _open_one(sub: _Submission) -> str:
    """Open *sub*'s PR at its expected head and return its URL.

    The gate runs first (:func:`_run_gates`), then a batch is re-checked at
    the head, both before anything is pushed; then the branch is pushed (when
    *sub.push*), origin's tip is held to the head, and the PR is opened. A
    failure here means no PR was opened; the steps after the PR exists run in
    :meth:`_SubmitRun._submit`, which reports a failure among them with the
    PR it opened.
    """
    _run_gates(sub)
    _recheck_batch(
        sub.issue_refs, branch=sub.branch, target=sub.base, head=sub.expected_head, push=sub.push
    )
    if sub.push:
        print(f"Ensuring branch '{sub.branch}' is pushed to origin...")
        _push_branch(sub.branch)
    _require_origin_head(sub.branch, sub.expected_head)
    print(f"Creating PR for {sub.branch}...")
    pr_url = _create_or_adopt(sub)
    print(f"PR created: {pr_url}")
    return pr_url


def _check_provenance(pr_url: str) -> None:
    """Run the provenance check on *pr_url* before auto-merge is turned on.

    A violation raises :class:`_SubmitFailedError`, so the PR's submit fails
    and auto-merge stays off: a violation always fails (vergil-tooling#4695).
    A check that cannot run raises too, as :func:`pr_provenance.check_pr`
    does, and auto-merge stays off.
    """
    result = pr_provenance.check_pr(pr_url)
    if not result.violations:
        return
    found = ", ".join(f"{v.login} ({v.role.value}): {v.action}" for v in result.violations)
    msg = (
        f"provenance violations found ({found}); auto-merge was left off. "
        "Investigate the cause, or fix the classifier if the violation is a false one."
    )
    raise _SubmitFailedError(msg)


def _arm(pr_url: str, head: str) -> pr_path.ArmResult:
    """Arm *pr_url* through the one PR path, pinned to *head*; a refusal fails.

    :func:`pr_path.arm` judges the PR again at its head (what the path
    accepts, the provenance check, and the gates for an agent-involved PR,
    in the main worktree, a clone of this repo), works out the merge method
    from the PR (rebase for a batch PR, squash otherwise) and turns
    auto-merge on, or merges the PR directly when GitHub refuses that at
    clean status. A ``held`` result (a release holds the lock, spec §5.5)
    is returned too: the PR stays open and unarmed, the release re-arms it,
    and the finalize watch follows it as any other. A ``refused`` result
    raises :class:`_SubmitFailedError` with its reason, so the PR's submit
    fails with auto-merge left off.
    """
    result = pr_path.arm(pr_url, clone=git.main_worktree_root(), head=head)
    if result.outcome in ("armed", "merged", "held"):
        return result
    raise _SubmitFailedError(result.detail or f"the one PR path left it {result.outcome}")


_SUBMIT_COMMAND = "vrg-submit-pr"

_NOT_ATTEMPTED = "not attempted: an earlier PR's submit stopped on an unexpected error"


def _pr_label(pr_url: str) -> str:
    """``PR #N`` for a pull-request URL ending in its number, else the URL itself."""
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return f"PR #{tail}" if tail.isdigit() else pr_url


@dataclass
class _SubmitRun:
    """The state one :func:`_submit_all` rollup builds up, one stage per PR.

    *opened*, *failed* and *left_open* become the run's :class:`SubmitSummary`. *fatal* is
    an error outside :data:`_PER_PR_ERRORS` (a bug, not a failed PR): the PRs
    after it are not attempted, and :func:`_submit_all` raises it once the
    rollup's summary is printed, as the loop before the rollup did.
    """

    opened: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    left_open: list[str] = field(default_factory=list)
    fatal: Exception | None = None

    def stage(self, sub: _Submission) -> progress.Stage:
        """The rollup stage for *sub*, named after its branch."""
        return progress.Stage(sub.branch, lambda _ctx: self.submit(sub), mode="fail_defer")

    def submit(self, sub: _Submission) -> None:
        """Run *sub*'s stage: open its PR and turn auto-merge on, or fail the stage."""
        if self.fatal is not None:
            raise progress.StageFailedError(_NOT_ATTEMPTED)
        try:
            self._submit(sub)
        except progress.StageFailedError:
            raise
        except Exception as exc:
            self.fatal = exc
            raise

    def _submit(self, sub: _Submission) -> None:
        try:
            pr_url = _open_one(sub)
        except _PER_PR_ERRORS as exc:
            reason = _failure_reason(exc)
            self.failed.append((sub.branch, reason))
            raise progress.StageFailedError(f"no PR opened: {reason}") from exc
        # The submission is recorded as soon as the PR exists, so a later step
        # that fails leaves the worktree marked with the PR it opened rather
        # than ready for a second one. A failure from here on reports the PR,
        # its head and the step that failed (vergil-tooling#5198).
        step = "recording the submission"
        try:
            if sub.record_in is not None:
                submission.record_submission(sub.record_in, pr_url=pr_url)
            step = "the provenance check"
            _check_provenance(pr_url)
            step = "turning auto-merge on"
            result = _arm(pr_url, sub.expected_head)
        except _PER_PR_ERRORS as exc:
            reason = f"opened at {sub.expected_head[:12]}; {step} failed: {_failure_reason(exc)}"
            self.failed.append((pr_url, reason))
            self.left_open.append(pr_url)
            raise progress.StageFailedError(f"{_pr_label(pr_url)} {reason}") from exc
        except Exception as exc:
            exc.add_note(
                f"{pr_url} was opened at {sub.expected_head[:12]}; {step} failed, "
                "so its auto-merge is not on."
            )
            raise
        self.opened.append(pr_url)
        method = str(result.method).lower()
        if result.outcome == "merged":
            outcome = f"merged directly ({method})"
            print(f"{pr_url}: {outcome}; it could already merge.")
        elif result.outcome == "held":
            outcome = f"{result.detail}; the release re-arms it"
            print(f"{pr_url}: {outcome}.")
        else:
            outcome = f"auto-merge on ({method}, pinned to {sub.expected_head[:12]})"
            print(f"{pr_url}: {outcome}.")
        progress.note(f"{_pr_label(pr_url)} {outcome}")


def _submit_all(selection: list[_Submission], args: argparse.Namespace) -> SubmitSummary:
    """Open every PR in *selection* and turn its auto-merge on: the one submit path.

    Every mode reaches this rollup with its PRs already gated (ready state,
    freeze, recorded head) and listed. Each PR is one stage of a
    :mod:`~vergil_tooling.lib.progress` rollup, named after its branch: open
    it (:func:`_open_one`), run the provenance check, then arm it through the
    one PR path (:func:`_arm`, :func:`pr_path.arm`), pinned to the expected
    head, with the method the path works out from the PR: rebase for a batch
    PR, squash otherwise. GitHub refuses auto-merge on a PR that can already
    merge; the path then merges it directly. The
    steps' output goes to the run log (``.vergil/vrg-submit-pr-<stamp>.log``
    in the main worktree); each stage ends on one line with its outcome,
    and the summary points at the log.

    A PR whose submit fails ends its stage ✗ with its reason, is named in
    the summary, and the rollup moves on to the next PR: nothing here is
    fail-fast, since one PR no longer waits on another. An error outside
    :data:`_PER_PR_ERRORS`, or an interrupt, stops the rollup and is raised
    once its summary is printed.
    """
    run = _SubmitRun()
    log = progress.RunLog(
        _SUBMIT_COMMAND,
        git.main_worktree_root(),
        header=progress.tooling_version_line(_SUBMIT_COMMAND),
    )
    try:
        rc = progress.run_pipeline(
            run,
            [run.stage(sub) for sub in selection],
            command=_SUBMIT_COMMAND,
            label=_SUBMIT_COMMAND,
            args=args,
            repo_root=git.main_worktree_root(),
            log=log,
            failure_exit=None,
        )
    finally:
        log.close()
    if run.fatal is not None:
        raise run.fatal
    if rc == _INTERRUPTED:
        raise KeyboardInterrupt
    if rc != EXIT_OK and not run.failed:
        msg = f"internal error: the submit rollup exited {rc} with no PR recorded as failed"
        raise RuntimeError(msg)
    return SubmitSummary(opened=run.opened, failed=run.failed, left_open=run.left_open)


def _print_failed(summary: SubmitSummary) -> None:
    """Name every PR whose submit failed, with its reason."""
    if not summary.failed:
        return
    print()
    print("Failed to submit:", file=sys.stderr)
    for label, reason in summary.failed:
        print(f"  {label} — {reason}", file=sys.stderr)


def _issues_cell(links: list[tuple[str, str]]) -> str:
    """``Closes #1, #2`` for a selection-table row: each linkage keyword once, in order."""
    groups: dict[str, list[str]] = {}
    for linkage, ref in links:
        groups.setdefault(linkage, []).append(ref)
    return "; ".join(f"{linkage} {', '.join(refs)}" for linkage, refs in groups.items())


def _print_selection(selection: list[_Submission]) -> None:
    """Print one compact table of the PRs about to be submitted (vergil-tooling#4744).

    One row per PR, with its issues and linkage, branch and title, under a
    header naming the count and the base. It replaces the PR-body previews
    and the confirmation prompt: the arguments already chose the selection.
    """
    if not selection:
        return
    bases = sorted({sub.base for sub in selection})
    noun = "PR" if len(selection) == 1 else "PRs"
    print(f"Submitting {len(selection)} {noun} to {', '.join(bases)}:")
    header = ("ISSUES", "BRANCH", "TITLE")
    rows = [(_issues_cell(sub.links), sub.branch, sub.title) for sub in selection]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(2)]
    for row in (header, *rows):
        print(f"  {row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]}")
    print()


def _finish(summary: SubmitSummary, args: argparse.Namespace) -> int:
    """Finalize the opened PRs in one run, report the failed submits; return the exit code.

    One ``vrg-finalize-pr`` runs over every opened PR, with ``--release``
    dropped when any PR failed to submit; when none opened, it is not run at
    all. The failed submits are named after finalize's report, each PR that
    opened with its head and the step that failed.
    """
    if not summary.opened:
        _print_failed(summary)
        if summary.left_open:
            print(
                f"\nNothing was finalized: {len(summary.left_open)} PR(s) opened but a later "
                "step failed (named above), so they are open without auto-merge.",
                file=sys.stderr,
            )
        else:
            print("\nNo PR opened, so nothing was finalized.", file=sys.stderr)
        return EXIT_NOT_ALL_MERGED
    release = args.release and not summary.failed
    cmd = _finalize_cmd(summary.opened, args, release=release)
    rc = _chain_finalize(cmd)
    _print_failed(summary)
    if args.release and summary.failed:
        print(f"Release skipped: {len(summary.failed)} PR(s) failed to submit.", file=sys.stderr)
    if rc not in _FINALIZE_OUTCOMES:
        print(
            f"vrg-submit-pr: vrg-finalize-pr exited {rc}; the PRs are open with "
            "auto-merge on and are unaffected.\n"
            f"  Re-run the finalize alone with: {shlex.join(cmd)}",
            file=sys.stderr,
        )
    elif rc == EXIT_OK and not summary.failed:
        done = "submitted, finalized and released" if release else "submitted and finalized"
        print(f"\nDone: {len(summary.opened)} PR(s) {done}.")
    return _submit_exit(rc, any_failed=bool(summary.failed))


def _reject_if_cross_repo(issue_ref: str) -> None:
    """Abort if --issue names an issue in a different repo than this PR's own.

    A PR can only ``Closes`` an issue in its own repository: GitHub scopes the
    close keyword to same-repo linkage, and issue numbers are not unique across
    repos, so a cross-repo close is a genuine mis-close hazard (an unrelated
    same-numbered issue would be closed), not a cosmetic slip. Refuse here and
    tell the caller to file the task in this repo and reference the other issue
    via a comment or a ``Ref`` line. This runs before the epic/operational
    guards because it is a cheap, local structural check — no authenticated
    ``gh`` call — and short-circuiting a cross-repo ref avoids an epic-ness
    lookup against a repo the token may not even cover. A bare ``#N`` resolves to
    the current repo and always passes; an unparseable ref defers to the
    downstream validators. The compare is case-insensitive so a differently
    cased spelling of the current repo is not a false refusal.
    """
    current = github.current_repo()
    try:
        ref = epics.parse_issue_ref(issue_ref, default_repo=current)
    except ValueError:
        return
    if current and f"{ref.owner}/{ref.repo}".lower() != current.lower():
        raise SystemExit(
            f"vrg-submit-pr: --issue ({ref.slug}) is in a different repo than this "
            f"PR ({current}); a PR can only close an issue in its own repo. File the "
            "task in this repo and reference the other issue via a comment or a Ref line."
        )


def _reject_if_non_task(issue_ref: str) -> None:
    """Abort if the linkage is an epic or archive — PRs link a task, never an umbrella.

    This lives here, at PR construction time, because deciding epic-ness needs
    an authenticated, cross-repo ``gh`` call (e.g. an epic in ``.github``) —
    which vrg-submit-pr has via the App installation token. An ``archive`` (a
    standing epic's per-quarter archive) is refused the same way (#2842).
    Self-scoping: legacy issues are never epics or archives, so they pass.
    """
    kind = epics.non_task_ref_kind(issue_ref, default_repo=github.current_repo())
    if kind is not None:
        raise SystemExit(
            f"vrg-submit-pr: {epics.non_task_refusal_message('--issue', issue_ref, kind)}"
        )


def _reject_if_operational_task(issue_ref: str) -> None:
    """Abort if the linkage is an operational task — it is not PR-workable.

    An operational task (validation, deployment, …) is proven by *running* it and
    recording an ``Outcome:`` comment; it has no code PR. Refuse PR construction
    here so the guard matches the skill boundary (issue-implement redirects to the
    task's run skill). Deciding operational-ness needs the same authenticated
    ``gh`` call as the epic guard. Self-scoping: plain/legacy tasks are never
    operational, so they pass.
    """
    if epics.is_operational_task(issue_ref, default_repo=github.current_repo()):
        raise SystemExit(
            "vrg-submit-pr: --issue is an operational task (validation/deployment), "
            "which is not PR-workable; run it with its run skill (issue-validate / "
            "issue-deploy) and record the Outcome as a comment instead of opening a PR."
        )


def _reject_if_queue_item(issue_ref: str) -> None:
    """Abort if the linkage is a queue item or an ops issue — it is not buildable.

    An item whose native parent is a queue epic (intake, planning or deferred)
    has not been routed (intake), expanded into an epic (planning) or revived
    (deferred), so it must not take a PR and close as though it were groomed work (#2485, closing
    vergil-project/.github#175). An ops issue, under the operations epic, is
    closed by hand and never by a PR; its fix is a follow-up task of its own
    (epic vergil-project/.github#1494). The authoritative check: unlike report-ready's
    best-effort mirror, a failed parent lookup propagates and aborts submission
    rather than reading as "buildable". Self-scoping: an unparseable ref is
    never a queue item, so it passes.
    """
    kind = epics.unbuildable_parent_kind(issue_ref, default_repo=github.current_repo())
    if kind is not None:
        raise SystemExit(f"vrg-submit-pr: {epics.queue_refusal_message(issue_ref, kind)}")


def _task_linkage(issue_ref: str, requested: str) -> str:
    """Choose the PR-body linkage keyword for *issue_ref*.

    A managed task — an issue with an ``epic``-labeled parent — is closed by its
    single PR, so it links with ``Closes`` to auto-close on merge to the default
    branch (epic vergil-project/.github#75). Legacy issues (no epic parent) keep
    *requested* (default ``Ref``) and stay open for manual close. Assumes the
    epic-target case was already rejected by :func:`_reject_if_non_task`.

    Deciding task-ness needs the same authenticated cross-repo ``gh`` call as the
    epic rejection — a parent epic may live in ``.github``. The chosen keyword
    is shown in the selection table, so the upgrade is not announced separately.
    """
    try:
        task = epics.parse_issue_ref(issue_ref, default_repo=github.current_repo())
    except ValueError:
        return requested
    parent = epics.parent_of(task)
    if parent is not None and epics.is_epic(parent):
        return "Closes"
    return requested


def _checked_links(issue_refs: list[str], requested: str) -> list[tuple[str, str]]:
    """Run the per-issue refusals on every issue and return the body's linkage lines.

    Each ref goes through the same four checks a single-issue submit runs
    (cross-repo, non-task, operational, queue) and its linkage is resolved on
    its own, so a batch PR gets one ``(linkage, ref)`` per issue, in order
    (epic vergil-project/.github#1019). Every batch issue is a task under the
    batch's epic, so each resolves to ``Closes``. The first refusal aborts.
    """
    links: list[tuple[str, str]] = []
    for issue_ref in issue_refs:
        _reject_if_cross_repo(issue_ref)
        _reject_if_non_task(issue_ref)
        _reject_if_operational_task(issue_ref)
        _reject_if_queue_item(issue_ref)
        links.append((_task_linkage(issue_ref, requested), issue_ref))
    return links


def _planning_close(issue_refs: list[str]) -> str | None:
    """The planning issue this PR also closes, or None (vergil-project/.github#950).

    Only a single-issue PR whose issue is an epic's documentation task closes
    its planning issue (:mod:`vergil_tooling.lib.planning_close`); a batch PR
    never does. When the docs task's planning issue is not closed, the reason
    is printed. A failed read propagates and aborts the submission, as the
    other per-issue checks do.
    """
    if len(issue_refs) != 1:
        return None
    try:
        task = epics.parse_issue_ref(issue_refs[0], default_repo=github.current_repo())
    except ValueError:
        return None
    result = planning_close.find(task)
    if result is None:
        return None
    print(f"vrg-submit-pr: {planning_close.describe(result)}")
    return result.planning.slug if result.closes else None


def _ready_issue_refs(fields: dict[str, str]) -> list[str]:
    """The canonical refs of every issue a ready-state hands off, in order.

    Reads ``issues`` (the comma-joined ``WorkflowState.all_issues`` that
    ``submission.read_pr_fields`` and :func:`_metadata_from_state` both
    return), never ``issue`` alone: a batch record's ``issue`` is only its
    first issue, and reading it would drop the rest. Fields with no
    ``issues`` are refused rather than read as one issue.
    """
    raw = fields.get("issues")
    if not raw:
        raise SystemExit(
            "vrg-submit-pr: internal error: the ready-state fields carry no 'issues' "
            "list; nothing was pushed or opened."
        )
    return [resolve_issue_ref(item.strip()) for item in raw.split(",")]


def _recheck_batch(
    issue_refs: list[str], *, branch: str, target: str, head: str, push: bool
) -> None:
    """Re-check a batch's hard rules and commits on the head the PR opens at.

    Runs only for a batch, in the submit rollup, before any push or ``gh pr
    create``. It fetches the target, then holds ``origin/<target>..<head>`` to
    one commit per issue and re-reads every issue against the hard rules
    (spec §5.1, §5.6). *head* is the commit the PR must open at;
    :func:`_require_origin_head` later holds origin's tip to that same commit,
    so the head checked here is the head the PR opens at. Any failure refuses
    with nothing opened.

    Only a relay submit (*push* false) also fetches the branch, so the head is
    present in a clone that never had it. A submit that pushes never does:
    fetching the branch would move ``refs/remotes/origin/<branch>``, the lease
    :func:`_push_branch`'s ``--force-with-lease`` holds, so the push could then
    overwrite commits pushed elsewhere. Its head is the local commit, already
    present.
    """
    if not batch_rules.is_batch(issue_refs):
        return
    print(f"Re-checking the batch rules for {', '.join(issue_refs)} at {head[:12]}...")
    refs = (target,) if push else (branch, target)
    try:
        git.run("fetch", "origin", *refs)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"vrg-submit-pr: could not fetch {', '.join(refs)} from origin to re-check "
            f"the batch: {error_text(exc)}. No PR was opened."
        ) from exc
    repo = github.current_repo()
    try:
        batch_rules.check_hard_rules(issue_refs, repo=repo)
        batch_rules.check_commit_per_issue(issue_refs, base=f"origin/{target}", head=head)
    except batch_rules.BatchRuleError as exc:
        raise SystemExit(f"vrg-submit-pr: {exc}. No PR was opened.") from exc


def _run_cli_mode(args: argparse.Namespace) -> int:
    # main() only routes here when all three are present; narrow for the
    # type checker without relying on an assert (ruff S101).
    if args.issue is None or args.summary is None or args.title is None:  # pragma: no cover
        msg = "internal error: CLI mode requires --issue, --summary, and --title"
        raise SystemExit(msg)
    # The CLI emergency mode stays single-issue: --issue is one ref, and a
    # batch is handed off only through report-ready (epic .github#1019).
    issue_ref = resolve_issue_ref(args.issue)
    links = _checked_links([issue_ref], args.linkage)
    # No ready-state records who prepared the change, so it counts as recorded
    # by its submitter: always the human, since agents are refused this mode.
    branch = git.current_branch()
    target = _target_branch(args.base)
    pr_body = build_pr_body(
        summary=args.summary, links=links, notes=args.notes, planning=_planning_close([issue_ref])
    )

    # No ready-state records a head here, so the PR (and its auto-merge) is
    # pinned to the commit the push publishes: the local HEAD.
    selection = [
        _Submission(
            branch=branch,
            base=target,
            title=args.title,
            pr_body=pr_body,
            issue_refs=[issue_ref],
            links=links,
            expected_head=git.commit_sha("HEAD"),
            push=True,
            record_in=None,
        )
    ]
    _print_selection(selection)
    if args.dry_run:
        print(_dry_run_plan(args))
        return 0
    summary = _submit_all(selection, args)
    return _finish(summary, args)


def _ready_worktrees(root: Path) -> list[tuple[worktrees.Worktree, dict[str, str]]]:
    """Return submittable ``(worktree, fields)`` pairs, or SystemExit if none.

    Same classification (ready / in-flight / merged / not-ready) and same
    no-submittable-worktrees error as the single-select path; shared by the
    single picker and the batch selector (issue #1673). A worktree whose
    state is ready is still held back when GitHub already has a PR for it
    (:func:`_published_pr`, issue #4993): an open one is in flight, and a
    merged one is cleanup, as is one closed without merging at the reported
    head (issue #5038). When other worktrees go ahead, each one held back
    that way is named on stderr, since its state file says ready.
    """
    ready: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    in_flight: list[str] = []
    merged: list[str] = []
    closed: list[str] = []
    not_ready: list[str] = []
    # Ready by their state file but held back by GitHub: named when others go on.
    held_back: list[str] = []
    for wt in worktrees.list_worktrees(root):
        try:
            fields = submission.read_pr_fields(wt.path)
        except AlreadySubmittedError as exc:
            ref = f"PR #{exc.pr_number}" if exc.pr_number is not None else "open PR"
            in_flight.append(f"{wt.path.name}: {ref} ({exc.pr_url})")
            continue
        except FileNotFoundError:
            not_ready.append(f"{wt.path.name}: no .vergil/pr-workflow.json")
            continue
        except WorkflowError as exc:
            not_ready.append(f"{wt.path.name}: {exc}")
            continue
        pr = _published_pr(wt.branch, fields["head_sha"])
        if pr is None:
            ready.append((wt, fields))
        elif pr.state == "OPEN":
            in_flight.append(f"{wt.path.name}: PR #{pr.number} ({pr.url})")
            held_back.append(f"{wt.path.name}: PR #{pr.number} is already open ({pr.url})")
        elif pr.state == "MERGED":
            merged.append(f"{wt.path.name}: PR #{pr.number} ({pr.url})")
            held_back.append(
                f"{wt.path.name}: PR #{pr.number} has merged ({pr.url}); "
                "the worktree is cleanup, not a submit"
            )
        else:
            closed.append(f"{wt.path.name}: PR #{pr.number} ({pr.url})")
            held_back.append(
                f"{wt.path.name}: PR #{pr.number} was closed without merging "
                f"({pr.url}); {_closed_unmerged_ways_on(pr)}"
            )

    if ready:
        for entry in held_back:
            print(f"vrg-submit-pr: skipping {entry}.", file=sys.stderr)
    else:
        lines = ["vrg-submit-pr: no submittable worktrees found."]
        if in_flight:
            lines.append("")
            lines.append("  In flight (open PR — nothing to do):")
            lines.extend(f"    {entry}" for entry in in_flight)
        if merged:
            lines.append("")
            lines.append("  Merged (cleanup — run `vrg-finalize-pr --cleanup-only`):")
            lines.extend(f"    {entry}" for entry in merged)
        if closed:
            lines.append("")
            lines.append(
                "  Closed without merging (reopen the PR on GitHub, or run "
                "`vrg-finalize-pr --cleanup-only`):"
            )
            lines.extend(f"    {entry}" for entry in closed)
        if not_ready:
            lines.append("")
            lines.append("  Not ready (no submission metadata yet):")
            lines.extend(f"    {entry}" for entry in not_ready)
        if not in_flight and not merged and not closed and not not_ready:
            lines.append("  (no .worktrees/ entries exist)")
        raise SystemExit("\n".join(lines))
    return ready


def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the single template-ready worktree to submit from.

    One candidate is auto-picked (the selection table then names it);
    several prompt a menu; none is an error that names each skipped worktree
    and why. Root launches are interactive by requirement, so the TTY guard
    fires up front, not per-prompt.
    """
    worktrees.require_tty("vrg-submit-pr from the repo root")
    ready = _ready_worktrees(root)

    if len(ready) == 1:
        return ready[0][0].path

    labels = [f"{wt.path.name} — issue {f['issue']}: {f['title']}" for wt, f in ready]
    chosen = worktrees.select_worktree(
        [wt for wt, _ in ready],
        purpose="Multiple submittable worktrees",
        labels=labels,
    )
    return chosen.path


def _select_batch_worktrees(root: Path, args: argparse.Namespace) -> list[worktrees.Worktree]:
    """Resolve the batch's worktrees from --all, --select, or a checkbox menu."""
    ready = _ready_worktrees(root)
    candidates = [wt for wt, _ in ready]
    fields_by_name = {wt.path.name: f for wt, f in ready}
    if args.all_worktrees:
        return candidates
    if args.select is not None:
        tokens = [t.strip() for t in args.select.split(",") if t.strip()]
        try:
            return worktrees.match_worktrees(candidates, tokens)
        except ValueError as exc:
            raise SystemExit(f"vrg-submit-pr --select: {exc}") from exc
    labels = [
        f"{wt.path.name} — issue {fields_by_name[wt.path.name]['issue']}: "
        f"{fields_by_name[wt.path.name]['title']}"
        for wt in candidates
    ]
    return worktrees.select_worktrees(
        candidates, purpose="Select worktrees to batch-submit", labels=labels
    )


def _run_submit_batch(selected: list[worktrees.Worktree], args: argparse.Namespace) -> int:
    """Submit *selected* worktrees through the one submit path (#1673, spec §6.2).

    Each worktree is held to the head its latest ``report-ready`` recorded
    (issue #3815); then one table lists the whole selection, and
    :func:`_submit_all` opens every PR with auto-merge on.
    No branch is rebased: the PR opens at exactly the reported head.
    """
    selection: list[_Submission] = []
    for wt in selected:
        _require_reported_head(wt)
        fields = submission.read_pr_fields(wt.path)
        selection.append(
            _prepare(
                wt.branch,
                _target_branch(args.base, fields.get("base")),
                fields,
                push=True,
                expected_head=fields.get("head_sha", ""),
                record_in=wt.path,
            )
        )
    _print_selection(selection)
    if args.dry_run:
        print(_dry_run_plan(args))
        return 0
    summary = _submit_all(selection, args)
    return _finish(summary, args)


def _require_reported_head(wt: worktrees.Worktree) -> None:
    """Refuse a batch worktree whose HEAD is not the head report-ready recorded.

    Reads the worktree's ready-state (``submission.read_pr_fields``; the batch
    selector already proved it readable) and holds the worktree's checked-out
    HEAD to its ``head_sha`` (issue #3815).
    """
    fields = submission.read_pr_fields(wt.path)
    _require_local_head(wt.branch, fields.get("head_sha", ""), worktree=wt.path)


def _prepare(
    branch: str,
    base: str,
    metadata: dict[str, str],
    *,
    push: bool,
    expected_head: str,
    record_in: Path | None,
) -> _Submission:
    """Gate one PR from its ready-state *metadata*; return its submission.

    Runs the standards guards on every issue (``issues``, comma-joined in
    order, is required), resolves each linkage and builds the body; the
    caller prints the selection table. It takes *branch* / *base* / *metadata* as given — it never
    reads ``git.current_branch()`` nor looks a worktree up — so the relay
    path can drive it for a branch that is not checked out anywhere locally.
    Nothing is pushed or opened here: :func:`_submit_all` does that.

    Raises ``SystemExit`` on a forbidden linkage or a refused issue.
    """
    issue_refs = _ready_issue_refs(metadata)
    try:
        linkage, linkage_warning = normalize_linkage(metadata.get("linkage", "Ref"))
    except ValueError as exc:
        raise SystemExit(f"vrg-submit-pr: {exc}") from exc
    if linkage_warning:
        print(f"vrg-submit-pr: {linkage_warning}", file=sys.stderr)
    links = _checked_links(issue_refs, linkage)
    pr_body = build_pr_body(
        summary=metadata["summary"],
        links=links,
        notes=metadata.get("notes", ""),
        planning=_planning_close(issue_refs),
    )
    return _Submission(
        branch=branch,
        base=base,
        title=metadata["title"],
        pr_body=pr_body,
        issue_refs=issue_refs,
        links=links,
        expected_head=expected_head,
        push=push,
        record_in=record_in,
    )


def _held_back(pr: trust.PRRef) -> int:
    """Say why this worktree's branch is not submitted: GitHub already has its PR.

    An open PR is in flight, so there is nothing to do (exit 0, as for a
    worktree whose own state records the submit). A PR merged at the
    reported head has landed, so the worktree is cleanup: the run is
    refused (issue #5037). A PR closed without merging at the reported
    head is cleanup too, and the run is refused naming it, so a new PR is
    never opened at that head silently (issue #5038).
    """
    if pr.state == "OPEN":
        print(
            f"vrg-submit-pr: this worktree's branch already has an open PR #{pr.number} "
            f"({pr.url}).\n"
            "  Nothing to do; the PR is in flight. Run vrg-finalize-pr on it to "
            "watch it merge and settle develop.",
        )
        return 0
    if pr.state == "CLOSED":
        msg = (
            f"vrg-submit-pr: this worktree's PR #{pr.number} was closed without "
            f"merging at the reported head ({pr.url}); nothing was submitted.\n"
            f"  To go on: {_closed_unmerged_ways_on(pr)}."
        )
        raise SystemExit(msg)
    msg = (
        f"vrg-submit-pr: this worktree's PR #{pr.number} has merged at the reported "
        f"head ({pr.url}); nothing was submitted.\n"
        "  This worktree is cleanup, not a submit: run "
        "`vrg-finalize-pr --cleanup-only` to remove it."
    )
    raise SystemExit(msg)


def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())

    # Location resolution: from the main worktree (repo root), resolve
    # which `.worktrees/` worktree to submit from and move there. The
    # invoking shell is unaffected — chdir applies to this process only.
    picked = git.is_main_worktree()
    if picked:
        # Batch when --all/--select is given. A single selection falls through
        # to the unchanged single-PR path below (issue #1673). Interactive
        # no-flag multi-select is a deliberate follow-up: the no-flag path
        # stays single-select via _choose_submit_worktree.
        if args.all_worktrees or args.select is not None:
            return _run_submit_batch(_select_batch_worktrees(root, args), args)
        wt_path = _choose_submit_worktree(root)
        os.chdir(wt_path)
        root = wt_path

    try:
        fields = submission.read_pr_fields(root)
    except AlreadySubmittedError as exc:
        print(
            f"vrg-submit-pr: this worktree's PR is already submitted — {exc}.\n"
            "  Nothing to do; the PR is in flight. Run vrg-finalize-pr on it to "
            "watch it merge and settle develop.",
        )
        return 0
    except FileNotFoundError:
        print(
            "vrg-submit-pr: No .vergil/pr-workflow.json found,\n"
            "  and no CLI arguments provided. Either provide --issue, --summary, and\n"
            "  --title, or ensure the agent has run the workflow through to approval.",
            file=sys.stderr,
        )
        return 1
    except WorkflowError as exc:
        print(f"vrg-submit-pr: cannot read PR submission fields:\n  {exc}", file=sys.stderr)
        return 1

    # Every issue the record hands off, in order: a batch record's 'issue' is
    # only its first (epic vergil-project/.github#1019).
    issue_refs = _ready_issue_refs(fields)
    branch = git.current_branch()
    # Hold the worktree to the head the latest report-ready recorded before
    # anything is shown or pushed: the push publishes the checked-out
    # branch, so a HEAD that moved after report-ready would otherwise become
    # the PR head unreported (issue #3815).
    expected_head = fields.get("head_sha", "")
    _require_local_head(branch, expected_head)
    # The root picker has already asked GitHub (_ready_worktrees); a run from
    # inside the worktree asks here, so a PR opened by hand is never opened
    # twice nor a merged one pushed again (issue #5037), and a PR closed
    # without merging at the reported head is never reopened as a new one
    # (issue #5038).
    if not picked:
        published = _published_pr(branch, expected_head)
        if published is not None:
            return _held_back(published)
    target = _target_branch(args.base, fields.get("base"))
    title = fields["title"]
    notes = fields.get("notes", "")

    # Belt-and-suspenders: the oracle normalizes linkage at report-ready, but
    # guard the value used to build the PR body so a forbidden keyword can never
    # reach the PR regardless of how the fields were obtained. A keyword carrying
    # a stray issue number is unambiguous, so strip it and warn rather than fail.
    try:
        linkage, linkage_warning = normalize_linkage(fields.get("linkage", "Ref"))
    except ValueError as exc:
        print(f"vrg-submit-pr: {exc}", file=sys.stderr)
        return 1
    if linkage_warning:
        print(f"vrg-submit-pr: {linkage_warning}", file=sys.stderr)
    links = _checked_links(issue_refs, linkage)
    pr_body = build_pr_body(
        summary=fields["summary"], links=links, notes=notes, planning=_planning_close(issue_refs)
    )

    selection = [
        _Submission(
            branch=branch,
            base=target,
            title=title,
            pr_body=pr_body,
            issue_refs=issue_refs,
            links=links,
            expected_head=expected_head,
            push=True,
            record_in=root,
        )
    ]
    _print_selection(selection)
    if args.dry_run:
        print(_dry_run_plan(args))
        return 0
    summary = _submit_all(selection, args)
    return _finish(summary, args)


def _metadata_from_state(state: WorkflowState, branch: str) -> dict[str, str]:
    """Extract the PR-body metadata from a ready ``WorkflowState``.

    Mirrors ``submission.read_pr_fields`` but reads from an in-memory state
    (which the relay path may have fetched from the ref rather than a local
    file). Raises ``SystemExit`` when the state carries no PR metadata yet — the
    USER agent must ``report-ready`` before the branch can be relayed.
    """
    meta = state.pr_metadata
    if meta is None:
        raise SystemExit(
            f"vrg-submit-pr: branch '{branch}' has no PR metadata yet; run "
            "`report-ready` before relaying it."
        )
    return {
        "issue": state.issue,
        "issues": ",".join(state.all_issues),
        "title": meta["title"],
        "summary": meta["summary"],
        "notes": meta.get("notes", ""),
        "linkage": meta.get("linkage", "Ref"),
        "recorded_by": state.recorded_by or "",
    }


def _resolve_ready_state(root: Path, branch: str) -> WorkflowState:
    """Resolve *branch*'s ready-state for the relay path.

    Prefers a local worktree's ``pr-workflow.json`` when a worktree is checked
    out on *branch* and carries state; otherwise reads the relay ref
    (``GitHubTransport``). Raises ``SystemExit`` with an actionable message when
    neither source has a ready-state.
    """
    for wt in worktrees.list_worktrees(root):
        if wt.branch == branch:
            local = LocalFileTransport(wt.path).read()
            if local is not None:
                return local
            break
    remote = GitHubTransport(branch).read()
    if remote is None:
        raise SystemExit(
            f"vrg-submit-pr: no ready-state for branch '{branch}' — no local "
            "worktree pr-workflow.json and no relay ref on origin. Run "
            "`report-ready` first."
        )
    return remote


def _verify_origin_tip(branch: str, state: WorkflowState) -> None:
    """Refuse to relay unless origin's tip of *branch* matches the ready-state.

    The relay path never pushes — it trusts that origin already carries exactly
    the commit ``report-ready`` recorded. If the branch moved on origin since
    then (or was never pushed), the recorded metadata no longer describes the
    tip, so the submission would be stale. Fail loudly rather than open a PR for
    the wrong commit.
    """
    _check_origin_tip(branch, _recorded_head(state, branch))


def _recorded_head(state: WorkflowState, branch: str) -> str:
    """Return the ``head_sha`` the latest ``report-ready`` recorded for *branch*.

    Refuses a state with none: without it there is nothing to hold the
    submitted head to, and guessing one is how a stale head slips through
    (issue #3815).
    """
    recorded = state.git.get("head_sha")
    if not recorded:
        raise SystemExit(
            f"vrg-submit-pr: the ready-state for '{branch}' records no head_sha; "
            "re-run `vrg-pr-workflow report-ready` so the commit to submit is "
            "recorded. Nothing was pushed or opened."
        )
    return str(recorded)


def _check_origin_tip(branch: str, expected: str) -> None:
    """Refuse unless origin's tip of *branch* is exactly *expected*.

    The shared core of :func:`_verify_origin_tip` (the relay path's early
    check) and :func:`_require_origin_head` (the last check before every PR
    open). ``gh pr create --head`` opens the PR at whatever origin's branch
    holds, so this is the check that ties the PR to the recorded head.
    """
    origin_sha = git.remote_branch_tip(branch)
    if origin_sha is None:
        raise SystemExit(
            f"vrg-submit-pr: branch '{branch}' is not on origin; a relay submit "
            "needs the branch already pushed."
        )
    if origin_sha != expected:
        raise SystemExit(
            f"vrg-submit-pr: origin/{branch} tip ({origin_sha[:12]}) does not "
            f"match the ready-state head_sha ({expected[:12]}). The branch "
            "moved since `report-ready`; re-run `report-ready` on the current tip "
            "(or fetch and reconcile) before submitting. No PR was opened."
        )


def _require_origin_head(branch: str, expected: str) -> None:
    """The last check before a PR is opened: origin holds the recorded head.

    Runs after any push, immediately before
    ``gh pr create``, on every report-ready-driven path (template, batch and
    named-branch). The PR opens at origin's tip, so a branch that moved on
    origin, or a push that published something other than the recorded
    commit, is refused here rather than opened at the wrong head (#3815).
    """
    _check_origin_tip(branch, expected)


def _require_local_head(branch: str, expected: str, *, worktree: Path | None = None) -> None:
    """Refuse unless the checked-out HEAD of *branch* is the recorded head.

    *worktree* names the worktree to read (``git -C``); ``None`` reads the
    process CWD. A worktree checked out on the branch whose HEAD is not the
    commit the latest ``report-ready`` recorded means the ready-state is stale
    or the branch moved after it: submitting either would open a PR at a
    head nobody reported (the silent drop of issue #3815). Refuse loudly
    instead of picking one.
    """
    if not expected:
        raise SystemExit(
            f"vrg-submit-pr: the ready-state for '{branch}' records no head_sha; "
            "re-run `vrg-pr-workflow report-ready` so the commit to submit is "
            "recorded. Nothing was pushed or opened."
        )
    where = ("-C", str(worktree)) if worktree is not None else ()
    actual = git.read_output(*where, "rev-parse", "HEAD")
    if actual != expected:
        location = f" in {worktree}" if worktree is not None else ""
        raise SystemExit(
            f"vrg-submit-pr: refusing to submit '{branch}': its checked-out HEAD"
            f"{location} ({actual[:12]}) is not the head the latest report-ready "
            f"recorded ({expected[:12]}). The branch moved after report-ready, or "
            "the ready-state is stale. The freeze refuses a plain report-ready "
            "re-run on a moved HEAD, so recover from the branch's worktree with "
            "`vrg-pr-workflow unfreeze`, then `vrg-pr-workflow report-ready` at "
            "the commit to submit. Nothing was pushed or opened."
        )


def _require_ready(state: WorkflowState, branch: str) -> None:
    """Refuse a named-branch submit whose ready-state is not ``ready``.

    A branch reopened with ``vrg-pr-workflow unfreeze`` drops back to
    ``implementing`` but keeps its PR metadata and its old ``head_sha``.
    Submitting it would open the PR at the head reported *before* the
    unfreeze and drop every commit made since (issue #3815). The worktree
    paths already gate on :func:`freeze.is_frozen` in
    ``submission.read_pr_fields``; this is the same gate for the named-branch
    path, which reads the state directly.
    """
    if not freeze.is_frozen(state):
        raise SystemExit(
            f"vrg-submit-pr: branch '{branch}' is not ready (status={state.status!r}); "
            "it was reopened with `vrg-pr-workflow unfreeze` and has not been "
            "reported ready since. Re-run `vrg-pr-workflow report-ready` at the "
            "commit to submit. No PR was opened."
        )


def _require_relay_agrees(branch: str, local: WorkflowState) -> None:
    """Refuse when the relay ref disagrees with the local ready-state.

    The local worktree file is preferred over the relay ref, but the two are
    written separately and a relay push can fail after the local write (which
    fails report-ready but keeps the local file, #5035). When
    they disagree on the status or the recorded head, one of them is stale and
    there is no safe way to pick: refuse and say how to republish both. An
    absent relay ref is not a disagreement (a branch whose relay push never
    landed has only the local state).
    """
    remote = GitHubTransport(branch).read()
    if remote is None:
        return
    local_head = local.git.get("head_sha")
    remote_head = remote.git.get("head_sha")
    if remote.status != local.status or remote_head != local_head:
        raise SystemExit(
            f"vrg-submit-pr: refusing to submit '{branch}': the local ready-state "
            f"(status={local.status!r}, head {str(local_head)[:12]}) and the relay "
            f"ref (status={remote.status!r}, head {str(remote_head)[:12]}) disagree, "
            "so one of them is stale. Re-run `vrg-pr-workflow report-ready` from "
            "the branch's worktree to republish both. No PR was opened."
        )


def _local_worktree_with_state(root: Path, branch: str) -> Path | None:
    """Return the path of a canonical worktree checked out on *branch* whose
    local ``pr-workflow.json`` carries a ready-state, else ``None``.

    This is the signal that the named branch was developed in a local worktree
    (as opposed to a cloud VM that pushed it and relayed only the ref): a
    checked-out worktree on the branch *and* a local state file present. It
    mirrors the local-preference probe in :func:`_resolve_ready_state`.
    """
    for wt in worktrees.list_worktrees(root):
        if wt.branch == branch and LocalFileTransport(wt.path).read() is not None:
            return wt.path
    return None


def _origin_has_branch(branch: str) -> bool:
    """True when origin already carries *branch* (``ls-remote`` finds its exact ref)."""
    return git.remote_branch_tip(branch) is not None


def _relay_prepare(root: Path, branch: str, args: argparse.Namespace) -> _Submission:
    """Resolve and verify the PR for one named *branch*.

    Returns the branch's submission. A branch that already carries a
    submitted PR is refused, before anything opens, since this run would not
    finalize it: the refusal names the ``vrg-finalize-pr`` command to run.
    Resolves the ready-state (local file preferred, else the relay ref).

    The local-vs-relay choice is made from **actual state**, not from the mere
    fact that a branch was named on the command line (issue #2794). The relay
    path never pushes — it trusts origin already carries the branch — so a
    branch developed in a local worktree and never pushed would dead-end on
    :func:`_verify_origin_tip`'s "not on origin". When the named branch has a
    local worktree carrying a ready-state **and** origin lacks the branch, fall
    back to the local submit path (``push=True``: push, then open), which is
    exactly what selecting that worktree from the interactive list would do.
    Otherwise keep the relay path (``push=False``, verify origin's tip matches
    the recorded ``head_sha``). The fallback is a live-run concern only: a dry
    run only prints the selection table (push vs no-push does not change it)
    and never touches origin, so it stays on the relay path.

    Whenever the named branch has a local ready worktree, the open records the
    submission in that worktree's ``pr-workflow.json`` (*record_in*) — on the
    relay path as well as the push fallback (issue #3525). Without it a branch
    that was pushed before ``report-ready`` and submitted by name would keep
    ``status == "ready"`` with no submission, so the freeze would refuse every
    pr-watch fix. A branch with no local worktree has nothing to mark.

    The PR opens only at the ``head_sha`` the latest ``report-ready``
    recorded (issue #3815). A state that is not ``ready`` (reopened by
    ``unfreeze``) is refused; with a local ready worktree its HEAD must be the
    recorded head and the relay ref, when present, must agree with the local
    state; and origin's tip is checked against the recorded head again right
    before the PR is created. Each mismatch is refused with nothing opened.
    """
    state = _resolve_ready_state(root, branch)
    if state.submitted is not None:
        submitted_url = state.submitted.get("pr_url", "")
        raise SystemExit(
            f"vrg-submit-pr: branch '{branch}' already has a submitted PR "
            f"({submitted_url}); finalize it directly with: vrg-finalize-pr "
            f"{submitted_url}. No PR was opened."
        )
    # The PR may only open at the head the latest report-ready recorded, and
    # every way the resolved state could be stale is refused here rather than
    # silently submitted (issue #3815): a branch reopened by unfreeze and not
    # re-reported, a local worktree whose HEAD moved past the record, and a
    # local state and relay ref that disagree.
    _require_ready(state, branch)
    expected_head = _recorded_head(state, branch)
    metadata = _metadata_from_state(state, branch)
    base = _target_branch(args.base, state.base)
    wt_path = _local_worktree_with_state(root, branch)
    if wt_path is not None:
        _require_local_head(branch, expected_head, worktree=wt_path)
        _require_relay_agrees(branch, state)

    push = False
    if not args.dry_run:
        push = wt_path is not None and not _origin_has_branch(branch)
        if not push:
            _verify_origin_tip(branch, state)
    return _prepare(
        branch,
        base,
        metadata,
        push=push,
        expected_head=expected_head,
        record_in=wt_path,
    )


def _run_relay(branches: list[str], args: argparse.Namespace) -> int:
    """Open PRs for each named *branch*, worktree-free, through the one submit path.

    :func:`_relay_prepare` resolves and verifies every branch first (a
    refusal stops the run with nothing opened); then one table lists them
    all, and :func:`_submit_all` opens each PR with auto-merge on. Then
    :func:`_finish` finalizes the opened PRs in one run.
    """
    root = Path(git.repo_root())
    selection = [_relay_prepare(root, branch, args) for branch in branches]
    _print_selection(selection)
    if args.dry_run:
        print(_dry_run_plan(args))
        return 0
    summary = _submit_all(selection, args)
    return _finish(summary, args)


if __name__ == "__main__":
    sys.exit(main())
