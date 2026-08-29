"""Generic per-repo work-chain driver for a fleet sweep.

A *fleet sweep* propagates one bespoke file change across many repositories,
opening a standards-compliant PR per repo that actually needs it. This module
owns the **generic** git/PR work-chain; the bespoke change is injected as an
:data:`Applicator`, so the driver knows nothing about *what* is being changed —
only how to turn a per-repo change into an issue, branch, commit, and PR
hand-off.

The single consumer in epic vergil-project/.github#325 is ``vrg-fleet-sync``
(``.gitignore`` propagation). A second consumer, config-driven applicator
discovery, or a general "run an arbitrary change-script" CLI are explicit
non-goals here (follow-on #328): this is only the seam
(:data:`Applicator`, :class:`SweepSpec`, :class:`RepoResult`) plus
:func:`run_sweep`.

Per repo, :func:`run_sweep`:

1. Creates a **detached** probe worktree at ``origin/develop`` — detached so a
   repo the applicator leaves unchanged carries *no* branch and needs *no*
   issue.
2. Runs the applicator against that worktree.
3. If nothing changed, removes the probe worktree and records ``"skipped"`` —
   no issue, no branch, no PR.
4. If something changed, ensures a tracking issue (under ``spec.epic`` when set,
   else the repo's ad-hoc epic), renames the probe worktree to the repo's
   ``.worktrees/issue-<N>-<slug>`` convention, creates the
   ``feature/<issue>-<slug>`` branch carrying the working-tree change, **stages**
   it, commits via ``vrg-commit``, and hands off via ``vrg-pr-workflow
   report-ready`` — recording ``"ready"``.

Each repo runs inside its own ``try``/``except`` so one repo's failure is
recorded (``"error"``) and never aborts the sweep. The driver **never** runs
``vrg-submit-pr`` or merges — PR submission and merge stay human actions.
"""

from __future__ import annotations

import re
import subprocess  # noqa: S404 - all invocations are fixed vrg-* argv, never shell
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git

_ISSUE_URL_RE = re.compile(r"/issues/(\d+)")
# Strips a leading conventional-commit prefix ("chore(gitignore): ") off the
# PR title to recover the bare commit subject; vrg-commit re-adds type/scope.
_COMMIT_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]*\))?!?:\s*")


@dataclass
class AppResult:
    """Outcome of applying the bespoke change to one repo's worktree.

    ``needs_followup`` signals that the applicator recognized the repo but
    could **not** safely make its change (e.g. an unclassifiable dev-dependency
    shape), so it made *no* edit and the driver must surface it loudly and file
    a follow-up rather than silently no-op (repo "no silent failures" policy).
    It defaults ``False`` so applicators that never defer stay unaffected.
    """

    changed: bool
    summary: str
    needs_followup: bool = False


# The bespoke file change for ONE repo's worktree. It receives the worktree
# path, performs its change in place, and reports whether anything changed plus
# a human-readable summary. gitignore's applicator shells out to
# ``vrg-gitignore-sync --write --repo <worktree>``.
Applicator = Callable[[Path], "AppResult"]


@dataclass
class SweepSpec:
    """The static description of a fleet sweep, shared across all its repos."""

    repos: list[str]
    branch_slug: str
    title: str
    body: str
    commit_type: str
    commit_scope: str
    epic: str | None


@dataclass
class RepoResult:
    """The terminal outcome for one repo in a sweep.

    ``needs_followup`` propagates :attr:`AppResult.needs_followup` for a repo
    the applicator recognized but could not safely change, so a consumer can
    report those repos prominently and file follow-ups after the sweep. It
    defaults ``False`` for the ``ready``/``error``/dry-run paths, where no
    deferral is possible.
    """

    repo: str
    status: str  # "ready" | "skipped" | "error"
    detail: str
    needs_followup: bool = False


def _run_tool(args: list[str], *, cwd: str | Path | None = None) -> str:
    """Run a fixed ``vrg-*`` host tool, returning stdout; raise on failure.

    A non-zero exit raises :class:`RuntimeError` carrying the tool's stderr (or
    stdout) so the failure surfaces in the repo's :class:`RepoResult` detail
    rather than being silently swallowed.
    """
    result = subprocess.run(  # noqa: S603 - fixed vrg-* argv, no shell, no user interpolation
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}): {detail}")
    return result.stdout


def _ensure_issue(spec: SweepSpec, repo: str) -> int:
    """Create the tracking issue for *repo* and return its number.

    Targets ``spec.epic`` when set, else the repo's ad-hoc epic. Runs with the
    repo clone as cwd so ``vrg-issue-create`` resolves the target repo from the
    checkout (no path/name derivation needed).
    """
    epic = spec.epic if spec.epic else "adhoc"
    stdout = _run_tool(
        [
            "vrg-issue-create",
            "--epic",
            epic,
            "--title",
            spec.title,
            "--body",
            spec.body,
        ],
        cwd=repo,
    )
    match = _ISSUE_URL_RE.search(stdout)
    if match is None:
        raise RuntimeError(f"could not parse issue number from vrg-issue-create output: {stdout!r}")
    return int(match.group(1))


def _commit_subject(title: str) -> str:
    """Recover the bare commit subject from a conventional-commit PR *title*."""
    return _COMMIT_PREFIX_RE.sub("", title).strip() or title


def _process_repo(spec: SweepSpec, applicator: Applicator, repo: str) -> RepoResult:
    """Run the full work-chain for one *repo*; the caller isolates failures."""
    worktrees = Path(repo) / ".worktrees"
    # The issue number is not known until *after* the applicator has run (an
    # unchanged repo mints no issue), so the worktree starts under a probe name
    # and is renamed to the ``issue-<N>-<slug>`` convention once the issue is
    # minted.
    probe = worktrees / f"probe-{spec.branch_slug}"

    # 1. Detached probe worktree at origin/develop: no branch, so an unchanged
    #    repo leaves nothing behind and never needs an issue.
    git.run("-C", repo, "worktree", "add", "--detach", str(probe), "origin/develop")

    result = applicator(probe)

    if not result.changed:
        # Nothing to propagate: tear down the probe and skip. No issue, no
        # branch, no PR.
        git.run("-C", repo, "worktree", "remove", "--force", str(probe))
        return RepoResult(
            repo=repo,
            status="skipped",
            detail=result.summary,
            needs_followup=result.needs_followup,
        )

    # 2. There is a change worth a PR: mint the tracking issue, then rename the
    #    worktree to the repo convention ``issue-<N>-<slug>`` and name the branch
    #    after the issue, carrying the working-tree change (which moves with the
    #    worktree) onto the branch.
    issue = _ensure_issue(spec, repo)
    branch = f"feature/{issue}-{spec.branch_slug}"
    worktree = worktrees / f"issue-{issue}-{spec.branch_slug}"
    git.run("-C", repo, "worktree", "move", str(probe), str(worktree))
    git.run("-C", str(worktree), "checkout", "-b", branch)

    # 3. Stage the applicator's working-tree changes, then commit and hand off.
    #    Without this stage vrg-commit sees nothing staged and fails ("no staged
    #    changes"), which broke every real sweep (#2944). vrg-commit re-adds the
    #    type/scope prefix, so the message is the bare subject recovered from the
    #    PR title.
    git.run("-C", str(worktree), "add", "-A")
    _run_tool(
        [
            "vrg-commit",
            "--type",
            spec.commit_type,
            "--scope",
            spec.commit_scope,
            "--message",
            _commit_subject(spec.title),
        ],
        cwd=worktree,
    )
    _run_tool(
        [
            "vrg-pr-workflow",
            "report-ready",
            "--issue",
            str(issue),
            "--title",
            spec.title,
            "--summary",
            spec.body,
            "--notes",
            result.summary,
        ],
        cwd=worktree,
    )
    return RepoResult(repo=repo, status="ready", detail=result.summary)


def run_sweep(spec: SweepSpec, applicator: Applicator, *, dry_run: bool) -> list[RepoResult]:
    """Run the sweep across ``spec.repos``, one PR per repo that needs one.

    Each repo is processed in isolation: a failure is caught, recorded as
    ``status="error"`` with the exception message, and the sweep continues to
    the next repo — one repo can never abort the whole run.

    ``dry_run=True`` performs **no** mutation at all — no worktree, no issue, no
    commit, no PR. It resolves the repo list and reports the intended action per
    repo (``status="ready"``, the terminal action a live run would drive
    toward), touching no git or GitHub state.
    """
    results: list[RepoResult] = []
    for repo in spec.repos:
        if dry_run:
            results.append(
                RepoResult(
                    repo=repo,
                    status="ready",
                    detail=(
                        f"dry-run: would ensure issue "
                        f"({spec.epic or 'adhoc'}), create feature/<issue>-{spec.branch_slug}, "
                        f"apply the change, commit, and report-ready"
                    ),
                )
            )
            continue
        try:
            results.append(_process_repo(spec, applicator, repo))
        except Exception as exc:  # noqa: BLE001 - failure isolation is the contract
            results.append(RepoResult(repo=repo, status="error", detail=str(exc)))
    return results
