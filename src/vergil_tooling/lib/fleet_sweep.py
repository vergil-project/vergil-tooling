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
4. If something changed, ensures a tracking issue (under ``spec.epic`` when set
   and in the repo's own org, else the repo's ad-hoc epic), renames the probe worktree to the repo's
   ``.worktrees/issue-<N>-<slug>`` convention, creates the
   ``feature/<issue>-<slug>`` branch carrying the working-tree change, **stages**
   it, commits via ``vrg-commit``, and hands off via ``vrg-pr-workflow
   report-ready`` — recording ``"ready"``.

Each repo runs inside its own ``try``/``except`` so one repo's failure is
recorded (``"error"``) and never aborts the sweep. A failure before the probe
worktree is renamed removes the probe, so it never blocks the next sweep of that
repo; a cleanup failure is reported alongside the original error. The driver
**never** runs
``vrg-submit-pr`` or merges — PR submission and merge stay human actions.
"""

from __future__ import annotations

import re
import subprocess  # noqa: S404 - all invocations are fixed vrg-* argv, never shell
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git, github
from vergil_tooling.lib.process_errors import error_text

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


def _epic_owner(epic: str) -> str | None:
    """Return the owner of an ``owner/repo#N`` epic ref, else ``None``.

    ``adhoc`` and a bare ``#N`` carry no owner: ``vrg-issue-create`` resolves
    both inside the target repo's own org, so they can never cross orgs.
    """
    repo_part, sep, _number = epic.partition("#")
    if not sep or "/" not in repo_part:
        return None
    return repo_part.split("/", 1)[0]


def _resolve_epic(spec: SweepSpec, repo_dir: str) -> str:
    """Pick the epic *repo_dir*'s tracking issue is filed under.

    ``spec.epic`` when set and in the repo's own org; otherwise the repo's
    ad-hoc epic. ``vrg-issue-create`` refuses a cross-org sub-issue link, so a
    repo outside the epic's org falls back to ``adhoc`` rather than failing
    (#3447). The repo's owner is read from the clone exactly as
    ``vrg-issue-create`` reads it (``gh repo view`` in the checkout), and only
    when there is an owner to compare against.
    """
    if not spec.epic:
        return "adhoc"
    epic_owner = _epic_owner(spec.epic)
    if epic_owner is None:
        return spec.epic
    repo_owner = github.current_repo(cwd=repo_dir).split("/", 1)[0]
    return spec.epic if repo_owner == epic_owner else "adhoc"


def _issue_body(spec: SweepSpec, epic: str) -> str:
    """Return the tracking-issue body, naming ``spec.epic`` when there is one.

    The epic is named in both the linked and the cross-org fallback case, so an
    issue filed under the repo's ad-hoc epic still says which initiative it
    belongs to.
    """
    if not spec.epic:
        return spec.body
    if epic == spec.epic:
        note = f"Part of epic {spec.epic}."
    else:
        note = (
            f"Part of epic {spec.epic}. Filed under this repository's ad-hoc epic "
            "because that epic is in another organization, and a cross-org "
            "sub-issue link is out of scope."
        )
    return f"{spec.body}\n\n{note}"


def _ensure_issue(spec: SweepSpec, repo: str) -> int:
    """Create the tracking issue for *repo* and return its number.

    Targets the epic :func:`_resolve_epic` picks. Runs with the repo clone as
    cwd so ``vrg-issue-create`` resolves the target repo from the checkout (no
    path/name derivation needed).
    """
    epic = _resolve_epic(spec, repo)
    stdout = _run_tool(
        [
            "vrg-issue-create",
            "--epic",
            epic,
            "--title",
            spec.title,
            "--body",
            _issue_body(spec, epic),
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


def _discard_probe(repo_dir: str, probe: Path, cause: Exception) -> None:
    """Remove the probe worktree after *cause* aborted the probe phase.

    Called only for a probe this sweep created. If the removal itself fails,
    raise a :class:`RuntimeError` naming **both** the original failure and the
    cleanup failure (chained from *cause*), so neither is masked and the
    stranded probe is reported rather than silently left behind. On success it
    returns and the caller re-raises *cause* unchanged.
    """
    try:
        git.run("-C", repo_dir, "worktree", "remove", "--force", str(probe))
    except Exception as cleanup_exc:
        msg = (
            f"{error_text(cause)}; additionally failed to remove probe worktree "
            f"{probe}: {error_text(cleanup_exc)}"
        )
        raise RuntimeError(msg) from cause


def _process_repo(spec: SweepSpec, applicator: Applicator, repo: str) -> RepoResult:
    """Run the full work-chain for one *repo*; the caller isolates failures.

    *repo* is resolved to an absolute path first. The worktree paths are handed
    to ``git -C <repo> worktree ...``, which interprets a relative path against
    ``<repo>`` itself — so a relative *repo* would double the prefix
    (``<repo>/<repo>/.worktrees/...``) and the chain would fail (#2979). The
    reported :attr:`RepoResult.repo` keeps the caller's spelling.
    """
    repo_path = Path(repo).resolve()
    repo_dir = str(repo_path)
    worktrees = repo_path / ".worktrees"
    # The issue number is not known until *after* the applicator has run (an
    # unchanged repo mints no issue), so the worktree starts under a probe name
    # and is renamed to the ``issue-<N>-<slug>`` convention once the issue is
    # minted.
    probe = worktrees / f"probe-{spec.branch_slug}"

    # 1. Detached probe worktree at origin/develop: no branch, so an unchanged
    #    repo leaves nothing behind and never needs an issue.
    git.run("-C", repo_dir, "worktree", "add", "--detach", str(probe), "origin/develop")

    # Probe phase: until the probe is renamed to ``issue-<N>-<slug>`` it holds
    # nothing worth keeping, so any failure here discards it — otherwise the
    # stranded probe makes the next sweep of this repo fail at ``worktree add``
    # (#3015). Once renamed, the worktree is left in place on failure: it is the
    # issue's worktree and may hold the applicator's change for a human to
    # finish.
    try:
        result = applicator(probe)

        if not result.changed:
            # Nothing to propagate: tear down the probe and skip. No issue, no
            # branch, no PR.
            git.run("-C", repo_dir, "worktree", "remove", "--force", str(probe))
            return RepoResult(
                repo=repo,
                status="skipped",
                detail=result.summary,
                needs_followup=result.needs_followup,
            )

        # 2. There is a change worth a PR: mint the tracking issue, then rename
        #    the worktree to the repo convention ``issue-<N>-<slug>`` and name
        #    the branch after the issue, carrying the working-tree change (which
        #    moves with the worktree) onto the branch.
        issue = _ensure_issue(spec, repo_dir)
        branch = f"feature/{issue}-{spec.branch_slug}"
        worktree = worktrees / f"issue-{issue}-{spec.branch_slug}"
        git.run("-C", repo_dir, "worktree", "move", str(probe), str(worktree))
    except Exception as exc:
        _discard_probe(repo_dir, probe, exc)
        raise

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
    toward), including the epic each repo's issue would be filed under, touching
    no git or GitHub state (resolving that epic may *read* the repo's owner).
    """
    results: list[RepoResult] = []
    for repo in spec.repos:
        try:
            if dry_run:
                epic = _resolve_epic(spec, str(Path(repo).resolve()))
                results.append(
                    RepoResult(
                        repo=repo,
                        status="ready",
                        detail=(
                            f"dry-run: would ensure issue under epic {epic}, "
                            f"create feature/<issue>-{spec.branch_slug}, "
                            f"apply the change, commit, and report-ready"
                        ),
                    )
                )
            else:
                results.append(_process_repo(spec, applicator, repo))
        except Exception as exc:  # noqa: BLE001 - failure isolation is the contract
            results.append(RepoResult(repo=repo, status="error", detail=error_text(exc)))
    return results
