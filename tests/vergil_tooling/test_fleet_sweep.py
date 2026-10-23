"""Tests for the generic fleet-sweep driver and the ``vrg-fleet-sync`` entry.

Most tests mock every external effect: :mod:`vergil_tooling.lib.git` (worktree
add/move/remove/checkout) and ``subprocess.run`` (the ``vrg-issue-create`` /
``vrg-commit`` / ``vrg-pr-workflow`` host-tool shell-outs), touching no git, gh,
or filesystem state.

The exception is ``test_run_sweep_commits_the_change_in_a_real_repo``, the
regression guard for #2944: it drives the sweep against a **real** throwaway git
repo (``tmp_path``) with ``git.run`` unmocked, faking only the ``vrg-*`` host
tools, so a missing ``git add`` — invisible to the fully-mocked tests — makes the
commit fail exactly as it did in production.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - real git in the integration-style commit test
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_fleet_sync
from vergil_tooling.lib import fleet_sweep
from vergil_tooling.lib.fleet_sweep import AppResult, RepoResult, SweepSpec, run_sweep

if TYPE_CHECKING:
    from collections.abc import Callable


def _spec(repos: list[str], *, epic: str | None = None) -> SweepSpec:
    return SweepSpec(
        repos=repos,
        branch_slug="gitignore-sync",
        title="chore(gitignore): sync managed .gitignore block",
        body="Propagate the composed managed .gitignore block.",
        commit_type="chore",
        commit_scope="gitignore",
        epic=epic,
    )


class _FakeProc:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record every git.run and subprocess.run invocation the driver makes.

    ``seq`` is a single interleaved timeline of both — ``("git", args)`` and
    ``("proc", args)`` in call order — so a test can assert one call *precedes*
    another across the two mocks (e.g. staging before commit).
    """
    record: dict[str, list] = {"git": [], "proc": [], "seq": [], "repo_lookup": []}

    def fake_current_repo(*, cwd: str | Path | None = None) -> str:
        record["repo_lookup"].append(cwd)
        return "vergil-project/demo"

    def fake_git_run(*args: str) -> None:
        record["git"].append(tuple(args))
        record["seq"].append(("git", tuple(args)))

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        record["proc"].append((tuple(args), kwargs.get("cwd")))
        record["seq"].append(("proc", tuple(args)))
        if args[0] == "vrg-issue-create":
            return _FakeProc(
                stdout="Created https://github.com/vergil-project/demo/issues/77, "
                "linked under epic adhoc.\n"
            )
        return _FakeProc()

    monkeypatch.setattr(fleet_sweep.git, "run", fake_git_run)
    monkeypatch.setattr(fleet_sweep.subprocess, "run", fake_proc_run)
    monkeypatch.setattr(fleet_sweep.github, "current_repo", fake_current_repo)
    return record


def _progs(proc_calls: list) -> list[str]:
    return [args[0] for args, _cwd in proc_calls]


def test_changed_repo_runs_full_chain(calls: dict[str, list]) -> None:
    """A changed repo drives issue-create -> worktree -> commit -> report-ready."""
    seen: list[Path] = []

    def applicator(worktree: Path) -> AppResult:
        seen.append(worktree)
        return AppResult(changed=True, summary="synced managed block")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results == [
        RepoResult(repo="/clones/demo", status="ready", detail="synced managed block")
    ]
    # applicator was handed the worktree path.
    assert len(seen) == 1
    # issue-create, commit, report-ready all shelled out.
    progs = _progs(calls["proc"])
    assert "vrg-issue-create" in progs
    assert "vrg-commit" in progs
    assert "vrg-pr-workflow" in progs

    def _call(prog: str) -> tuple:
        return next(args for args, _cwd in calls["proc"] if args[0] == prog)

    issue = _call("vrg-issue-create")
    assert "--epic" in issue and "adhoc" in issue
    assert "chore(gitignore): sync managed .gitignore block" in issue

    commit = _call("vrg-commit")
    assert "--type" in commit and "chore" in commit
    assert "--scope" in commit and "gitignore" in commit

    ready = _call("vrg-pr-workflow")
    assert "report-ready" in ready
    assert "--issue" in ready and "77" in ready
    assert "chore(gitignore): sync managed .gitignore block" in ready

    # The probe worktree was renamed to the issue-<N>-<slug> convention, and
    # commit + report-ready run inside that renamed worktree (not the probe).
    issue_worktree = Path("/clones/demo/.worktrees/issue-77-gitignore-sync")
    move = next(call for call in calls["git"] if len(call) >= 4 and call[3] == "move")
    assert move == (
        "-C",
        "/clones/demo",
        "worktree",
        "move",
        "/clones/demo/.worktrees/probe-gitignore-sync",
        str(issue_worktree),
    )
    commit_cwd = next(cwd for args, cwd in calls["proc"] if args[0] == "vrg-commit")
    ready_cwd = next(cwd for args, cwd in calls["proc"] if args[0] == "vrg-pr-workflow")
    assert commit_cwd == issue_worktree
    assert ready_cwd == issue_worktree

    # a named feature branch was created for the changed repo.
    git_flat = [tok for call in calls["git"] for tok in call]
    assert "feature/77-gitignore-sync" in git_flat

    # The applicator's changes are STAGED before vrg-commit — the missing step
    # that made every real sweep fail with "no staged changes" (#2944).
    stage_call = ("git", ("-C", str(issue_worktree), "add", "-A"))
    assert stage_call in calls["seq"]
    stage_idx = calls["seq"].index(stage_call)
    commit_idx = next(
        i
        for i, (kind, args) in enumerate(calls["seq"])
        if kind == "proc" and args[0] == "vrg-commit"
    )
    assert stage_idx < commit_idx, "stage must precede commit"


def test_epic_repo_links_issue_under_epic(calls: dict[str, list]) -> None:
    """When spec.epic is set, issue-create targets that epic, not adhoc."""

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=True, summary="synced")

    run_sweep(_spec(["/clones/demo"], epic="vergil-project/.github#325"), applicator, dry_run=False)

    issue = next(args for args, _cwd in calls["proc"] if args[0] == "vrg-issue-create")
    assert "vergil-project/.github#325" in issue
    assert "adhoc" not in issue


def _changed(_worktree: Path) -> AppResult:
    return AppResult(changed=True, summary="synced")


def _issue_create_args(calls: dict[str, list]) -> tuple:
    return next(args for args, _cwd in calls["proc"] if args[0] == "vrg-issue-create")


def _flag(args: tuple, flag: str) -> str:
    return args[args.index(flag) + 1]


def test_same_org_repo_keeps_the_spec_epic(calls: dict[str, list]) -> None:
    """A repo in the epic's org is linked under spec.epic, resolved from its clone."""

    run_sweep(_spec(["/clones/demo"], epic="vergil-project/.github#420"), _changed, dry_run=False)

    issue = _issue_create_args(calls)
    assert _flag(issue, "--epic") == "vergil-project/.github#420"
    assert "Part of epic vergil-project/.github#420." in _flag(issue, "--body")
    # The owner comes from the clone itself, as vrg-issue-create resolves it.
    assert calls["repo_lookup"] == ["/clones/demo"]


def test_cross_org_repo_falls_back_to_its_adhoc_epic(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo outside the epic's org goes under its own ad-hoc epic (#3447).

    vrg-issue-create refuses a cross-org sub-issue link, so the sweep must not
    ask for one; the body still names the initiative's epic.
    """
    monkeypatch.setattr(
        fleet_sweep.github, "current_repo", lambda *, cwd=None: "logical-minds-foundry/docs"
    )

    results = run_sweep(
        _spec(["/clones/docs"], epic="vergil-project/.github#420"), _changed, dry_run=False
    )

    assert results[0].status == "ready"
    issue = _issue_create_args(calls)
    assert _flag(issue, "--epic") == "adhoc"
    body = _flag(issue, "--body")
    assert body.startswith("Propagate the composed managed .gitignore block.")
    assert "vergil-project/.github#420" in body
    assert "ad-hoc epic" in body


def test_no_epic_uses_adhoc_without_a_repo_lookup(calls: dict[str, list]) -> None:
    """epic=None -> adhoc, body unchanged, and no owner lookup is needed."""

    run_sweep(_spec(["/clones/demo"]), _changed, dry_run=False)

    issue = _issue_create_args(calls)
    assert _flag(issue, "--epic") == "adhoc"
    assert _flag(issue, "--body") == "Propagate the composed managed .gitignore block."
    assert calls["repo_lookup"] == []


def test_ownerless_epic_ref_is_used_as_is(calls: dict[str, list]) -> None:
    """A bare ``#N`` resolves inside the repo's own epic home: no owner to compare."""

    run_sweep(_spec(["/clones/demo"], epic="#12"), _changed, dry_run=False)

    assert _flag(_issue_create_args(calls), "--epic") == "#12"
    assert calls["repo_lookup"] == []


def test_dry_run_names_the_chosen_epic(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run line reports which epic each repo would be filed under."""
    owners = {"/clones/same": "vergil-project/same", "/clones/other": "mnemosys-project/other"}
    monkeypatch.setattr(fleet_sweep.github, "current_repo", lambda *, cwd=None: owners[str(cwd)])

    results = run_sweep(
        _spec(["/clones/same", "/clones/other"], epic="vergil-project/.github#420"),
        _changed,
        dry_run=True,
    )

    assert "would ensure issue under epic vergil-project/.github#420," in results[0].detail
    assert "would ensure issue under epic adhoc," in results[1].detail
    assert calls["git"] == []
    assert calls["proc"] == []

    no_epic = run_sweep(_spec(["/clones/same"]), _changed, dry_run=True)
    assert "would ensure issue under epic adhoc," in no_epic[0].detail


def test_dry_run_owner_lookup_failure_is_an_error(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo whose owner cannot be resolved is a recorded error, not a guess."""

    def failing_lookup(*, cwd: str | Path | None = None) -> str:
        raise RuntimeError("no git remote")

    monkeypatch.setattr(fleet_sweep.github, "current_repo", failing_lookup)

    results = run_sweep(
        _spec(["/clones/a", "/clones/b"], epic="vergil-project/.github#420"),
        _changed,
        dry_run=True,
    )

    assert [r.status for r in results] == ["error", "error"]
    assert "no git remote" in results[0].detail


def test_unchanged_repo_is_skipped_without_issue_or_branch(calls: dict[str, list]) -> None:
    """changed=False -> skipped: no issue, no feature branch, no report-ready."""

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=False, summary="already in sync")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results == [RepoResult(repo="/clones/demo", status="skipped", detail="already in sync")]
    progs = _progs(calls["proc"])
    assert "vrg-issue-create" not in progs
    assert "vrg-commit" not in progs
    assert "vrg-pr-workflow" not in progs
    # no named feature branch was created (the probe worktree is detached).
    git_flat = [tok for call in calls["git"] for tok in call]
    assert not any(tok.startswith("feature/") for tok in git_flat)
    # the probe is torn down; nothing is moved to an issue-<N> name or staged.
    assert (
        "-C",
        "/clones/demo",
        "worktree",
        "remove",
        "--force",
        "/clones/demo/.worktrees/probe-gitignore-sync",
    ) in calls["git"]
    assert not any(len(call) >= 4 and call[3] == "move" for call in calls["git"])
    assert not any(len(call) >= 4 and call[2:4] == ("add", "-A") for call in calls["git"])


def test_failure_is_isolated_and_sweep_continues(calls: dict[str, list]) -> None:
    """One repo raising is recorded status=error; the next repo still runs."""

    def applicator(worktree: Path) -> AppResult:
        if worktree.parts and "boom" in str(worktree):
            raise RuntimeError("kaboom")
        return AppResult(changed=True, summary="synced")

    results = run_sweep(_spec(["/clones/boom", "/clones/ok"]), applicator, dry_run=False)

    assert results[0].repo == "/clones/boom"
    assert results[0].status == "error"
    assert "kaboom" in results[0].detail
    assert results[1].repo == "/clones/ok"
    assert results[1].status == "ready"


def test_dry_run_performs_no_mutations(calls: dict[str, list]) -> None:
    """dry_run=True touches no git/gh state and reports intended actions."""

    def applicator(_worktree: Path) -> AppResult:  # pragma: no cover - must not run
        raise AssertionError("applicator must not run in a dry run")

    results = run_sweep(_spec(["/clones/a", "/clones/b"]), applicator, dry_run=True)

    assert calls["git"] == []
    assert calls["proc"] == []
    assert [r.repo for r in results] == ["/clones/a", "/clones/b"]
    assert all(r.status == "ready" for r in results)
    assert all("dry-run" in r.detail for r in results)


def test_tool_failure_surfaces_stderr(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero host-tool exit is recorded as an error carrying its stderr."""

    def failing_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(returncode=1, stderr="issue-create exploded")

    monkeypatch.setattr(fleet_sweep.subprocess, "run", failing_run)

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=True, summary="synced")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)
    assert results[0].status == "error"
    assert "issue-create exploded" in results[0].detail


def test_unparseable_issue_url_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vrg-issue-create output with no /issues/<N> URL is an isolated error."""

    def fake_git_run(*args: str) -> None:
        pass

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(stdout="something unexpected without a url\n")

    monkeypatch.setattr(fleet_sweep.git, "run", fake_git_run)
    monkeypatch.setattr(fleet_sweep.subprocess, "run", fake_proc_run)

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=True, summary="synced")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)
    assert results[0].status == "error"
    assert "could not parse issue number" in results[0].detail


# --- probe cleanup on failure (#3015) -------------------------------------

_PROBE_REMOVE = (
    "-C",
    "/clones/demo",
    "worktree",
    "remove",
    "--force",
    "/clones/demo/.worktrees/probe-gitignore-sync",
)


def test_applicator_failure_removes_probe(calls: dict[str, list]) -> None:
    """An applicator raising discards the probe so the next sweep can re-add it."""

    def applicator(_worktree: Path) -> AppResult:
        raise RuntimeError("applicator exploded")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results == [
        RepoResult(repo="/clones/demo", status="error", detail="applicator exploded")
    ]
    assert _PROBE_REMOVE in calls["git"]


def test_issue_create_failure_removes_probe(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-org vrg-issue-create refusal discards the probe, keeping the error."""

    def refusing_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(returncode=1, stderr="cross-org operation is out of scope")

    monkeypatch.setattr(fleet_sweep.subprocess, "run", refusing_run)

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=True, summary="synced")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results[0].status == "error"
    assert "cross-org operation is out of scope" in results[0].detail
    assert _PROBE_REMOVE in calls["git"]
    assert not any(len(call) >= 4 and call[3] == "move" for call in calls["git"])


def test_failure_after_rename_keeps_issue_worktree(
    calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the probe is renamed removes NO worktree.

    The renamed ``issue-<N>-<slug>`` worktree carries the change for a human to
    finish, so the driver must not discard it (nor try to remove the vanished
    probe).
    """

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        if args[0] == "vrg-issue-create":
            return _FakeProc(stdout="Created https://github.com/o/demo/issues/77\n")
        return _FakeProc(returncode=1, stderr="commit refused")

    monkeypatch.setattr(fleet_sweep.subprocess, "run", fake_proc_run)

    def applicator(_worktree: Path) -> AppResult:
        return AppResult(changed=True, summary="synced")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results[0].status == "error"
    assert "commit refused" in results[0].detail
    assert not any(len(call) >= 4 and call[3] == "remove" for call in calls["git"])


def test_probe_cleanup_failure_reports_both_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """If removing the probe also fails, the detail names BOTH failures."""
    git_calls: list[tuple[str, ...]] = []

    def fake_git_run(*args: str) -> None:
        git_calls.append(args)
        if args[2:4] == ("worktree", "remove"):
            raise RuntimeError("remove denied")

    monkeypatch.setattr(fleet_sweep.git, "run", fake_git_run)

    def applicator(_worktree: Path) -> AppResult:
        raise RuntimeError("applicator exploded")

    results = run_sweep(_spec(["/clones/demo"]), applicator, dry_run=False)

    assert results[0].status == "error"
    detail = results[0].detail
    assert "applicator exploded" in detail
    assert "remove denied" in detail
    assert "/clones/demo/.worktrees/probe-gitignore-sync" in detail
    assert _PROBE_REMOVE in git_calls


# --- integration-style: a real git repo proves the change is committed ----


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run real git for the integration test's fixture setup and assertions."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _make_real_clone(tmp_path: Path) -> Path:
    """Build an origin repo with a commit on develop and return a clone of it.

    The clone carries an ``origin/develop`` remote-tracking ref for the probe.
    """
    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    _git("init", "-q", "-b", "develop", str(origin))
    _git("config", "user.email", "t@example.com", cwd=origin)
    _git("config", "user.name", "Test", cwd=origin)
    (origin / "existing.txt").write_text("original\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "init", cwd=origin)
    _git("clone", "-q", str(origin), str(clone))
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    return clone


def _real_applicator(worktree: Path) -> AppResult:
    (worktree / "managed.txt").write_text("propagated content\n")
    return AppResult(changed=True, summary="added managed.txt")


def _fake_run_tool(args: list[str], *, cwd: Path | str | None = None) -> str:
    """Fake the host-tool seam so ``git.run`` stays REAL.

    Faking ``_run_tool`` (not ``subprocess.run``) is deliberate: patching the
    shared ``subprocess`` module would also intercept the tests' own real
    ``git`` calls. Here only the ``vrg-*`` shell-outs are simulated; every
    ``git.run`` in the driver runs against the real repo.
    """
    prog = args[0]
    if prog == "vrg-issue-create":
        return "Created https://github.com/o/r/issues/42\n"
    if prog == "vrg-commit":
        # Mimic real vrg-commit: raise (as _run_tool would) when nothing is
        # staged, else make a real commit so the change lands on the branch.
        staged = subprocess.run(  # noqa: S603
            ["git", "-C", str(cwd), "diff", "--cached", "--quiet"],  # noqa: S607
            check=False,
        )
        if staged.returncode == 0:
            msg = "vrg-commit failed (exit 1): ERROR: no staged changes"
            raise RuntimeError(msg)
        _git("-C", str(cwd), "commit", "-qm", "chore(gitignore): sync managed block")
        return ""
    # vrg-pr-workflow report-ready: no-op success (no relay ref in the test).
    return ""


def _assert_committed_in_issue_worktree(clone: Path) -> None:
    # The worktree was renamed to the issue-<N>-<slug> convention...
    worktree = clone / ".worktrees" / "issue-42-gitignore-sync"
    assert worktree.is_dir()
    # ...and its HEAD commit actually contains the applicator's new file.
    committed = _git("-C", str(worktree), "show", "--stat", "--name-only", "HEAD")
    assert "managed.txt" in committed
    # the branch follows the convention too.
    branch = _git("-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert branch == "feature/42-gitignore-sync"


def test_run_sweep_commits_the_change_in_a_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end over a REAL git repo: the applicator's change lands in a commit.

    This is the regression guard for #2944. The unit tests mock ``git.run``, so a
    missing ``git add`` was invisible: nothing stages the applicator's edit, yet a
    mocked ``vrg-commit`` "succeeds". Here ``git.run`` is real and the fake
    ``vrg-commit`` mimics the real tool — it refuses when nothing is staged
    (``git diff --cached --quiet``) — so an un-staged tree yields NO commit and
    the sweep records ``error`` instead of ``ready``. The test therefore fails on
    the pre-fix code and passes only once the driver stages before committing.
    """
    clone = _make_real_clone(tmp_path)
    # git.run stays REAL; only the host-tool shell-outs (_run_tool) are faked.
    monkeypatch.setattr(fleet_sweep, "_run_tool", _fake_run_tool)

    results = run_sweep(_spec([str(clone)]), _real_applicator, dry_run=False)

    assert results == [RepoResult(repo=str(clone), status="ready", detail="added managed.txt")], (
        "sweep must succeed with the applicator's change committed"
    )
    _assert_committed_in_issue_worktree(clone)


def test_run_sweep_handles_a_relative_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative repo path resolves to the right worktree location (#2979).

    ``git -C <repo> worktree add <path>`` interprets a relative ``<path>``
    against ``<repo>``, so an unresolved relative repo doubled the prefix
    (``clone/clone/.worktrees/...``) and the chain failed with a spurious
    error. The driver now resolves the repo first; the reported repo keeps the
    caller's spelling.
    """
    clone = _make_real_clone(tmp_path)
    monkeypatch.setattr(fleet_sweep, "_run_tool", _fake_run_tool)
    monkeypatch.chdir(tmp_path)

    results = run_sweep(_spec(["clone"]), _real_applicator, dry_run=False)

    assert results == [RepoResult(repo="clone", status="ready", detail="added managed.txt")]
    _assert_committed_in_issue_worktree(clone)
    # Nothing was created under a doubled ``clone/clone`` prefix.
    assert not (clone / "clone").exists()


def test_failed_sweep_does_not_strand_probe_in_a_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed repo leaves no probe behind, so a re-run succeeds (#3015).

    Before the fix, an error after ``worktree add`` stranded
    ``.worktrees/probe-<slug>`` and the next sweep of that repo failed at
    ``worktree add`` because the path already existed.
    """
    clone = _make_real_clone(tmp_path)
    monkeypatch.setattr(fleet_sweep, "_run_tool", _fake_run_tool)

    def failing_applicator(_worktree: Path) -> AppResult:
        raise RuntimeError("applicator exploded")

    first = run_sweep(_spec([str(clone)]), failing_applicator, dry_run=False)
    assert first == [RepoResult(repo=str(clone), status="error", detail="applicator exploded")]
    assert not (clone / ".worktrees" / "probe-gitignore-sync").exists()

    second = run_sweep(_spec([str(clone)]), _real_applicator, dry_run=False)
    assert second == [RepoResult(repo=str(clone), status="ready", detail="added managed.txt")]
    _assert_committed_in_issue_worktree(clone)


# --- vrg_fleet_sync entry -------------------------------------------------


def test_main_builds_gitignore_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """main builds the gitignore SweepSpec and hands it to run_sweep."""
    captured: dict[str, object] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["spec"] = spec
        captured["applicator"] = applicator
        captured["dry_run"] = dry_run
        return []

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)

    rc = vrg_fleet_sync.main(["--repos", "/clones/x", "/clones/y", "--dry-run"])
    assert rc == 0

    spec = captured["spec"]
    assert isinstance(spec, SweepSpec)
    assert spec.repos == ["/clones/x", "/clones/y"]
    assert spec.branch_slug == "gitignore-sync"
    assert spec.commit_type == "chore"
    assert spec.commit_scope == "gitignore"
    assert spec.epic is None
    assert spec.title
    assert spec.body
    assert captured["dry_run"] is True


def test_main_applicator_maps_gitignore_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gitignore applicator maps the CLI's output to AppResult."""
    captured: dict[str, Callable[[Path], AppResult]] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["applicator"] = applicator
        return []

    proc_calls: list[tuple] = []

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        proc_calls.append((tuple(args), kwargs.get("cwd")))
        if "already" in str(kwargs.get("_mode", "")):
            return _FakeProc(stdout="already in sync\n")
        return _FakeProc(stdout="synced managed block in /w/.gitignore\n")

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", fake_proc_run)

    vrg_fleet_sync.main(["--repos", "/clones/x"])
    applicator = captured["applicator"]

    # changed case: the CLI reports a rewrite.
    res = applicator(Path("/w"))
    assert res.changed is True
    assert "synced managed block" in res.summary
    # the applicator shelled out to vrg-gitignore-sync --write --repo <worktree>.
    args, _cwd = proc_calls[-1]
    assert args[0] == "vrg-gitignore-sync"
    assert "--write" in args
    assert "--repo" in args and "/w" in args


def test_main_applicator_detects_already_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """'already in sync' output maps to changed=False."""
    captured: dict[str, Callable[[Path], AppResult]] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["applicator"] = applicator
        return []

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(stdout="already in sync\n")

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", fake_proc_run)

    vrg_fleet_sync.main(["--repos", "/clones/x"])
    res = captured["applicator"](Path("/w"))
    assert res.changed is False
    assert "already in sync" in res.summary


def test_main_applicator_raises_on_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero vrg-gitignore-sync exit raises so the driver records an error."""
    captured: dict[str, Callable[[Path], AppResult]] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["applicator"] = applicator
        return []

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(returncode=1, stderr="bad repo")

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", fake_proc_run)

    vrg_fleet_sync.main(["--repos", "/clones/x"])
    with pytest.raises(RuntimeError, match="bad repo"):
        captured["applicator"](Path("/w"))


def test_main_returns_nonzero_when_a_repo_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """main exits non-zero if any repo ended in error."""

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        return [RepoResult(repo="/clones/x", status="error", detail="boom")]

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    assert vrg_fleet_sync.main(["--repos", "/clones/x"]) == 1


def test_main_reports_ready_and_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """main prints a per-repo status line and exits 0 when nothing errored."""

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        return [
            RepoResult(repo="/clones/x", status="ready", detail="synced"),
            RepoResult(repo="/clones/y", status="skipped", detail="already in sync"),
        ]

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    rc = vrg_fleet_sync.main(["--repos", "/clones/x", "/clones/y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/clones/x" in out
    assert "ready" in out
    assert "skipped" in out


def _capture_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> tuple[SweepSpec, Callable[[Path], AppResult]]:
    """Run ``vrg_fleet_sync.main`` with ``run_sweep`` captured, not executed."""
    specs: list[SweepSpec] = []
    applicators: list[Callable[[Path], AppResult]] = []

    def fake_run_sweep(
        spec: SweepSpec, applicator: Callable[[Path], AppResult], *, dry_run: bool
    ) -> list[RepoResult]:
        specs.append(spec)
        applicators.append(applicator)
        return []

    monkeypatch.setattr(vrg_fleet_sync, "run_sweep", fake_run_sweep)
    assert vrg_fleet_sync.main(argv) == 0
    return specs[0], applicators[0]


def test_fleet_target_default_is_gitignore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with no --target the sweep is exactly the gitignore sweep."""
    spec, _app = _capture_main(monkeypatch, ["--repos", "/clones/x"])
    explicit, _app = _capture_main(monkeypatch, ["--repos", "/clones/x", "--target", "gitignore"])
    assert spec == explicit
    assert spec.branch_slug == "gitignore-sync"
    assert spec.title == "chore(gitignore): sync the vergil-managed .gitignore block"
    assert spec.commit_scope == "gitignore"
    assert spec.epic is None


def test_fleet_target_dependabot_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """--target dependabot builds the dependabot SweepSpec and applicator."""
    spec, applicator = _capture_main(
        monkeypatch, ["--repos", "/clones/x", "--target", "dependabot"]
    )
    assert spec.repos == ["/clones/x"]
    assert spec.branch_slug == "dependabot-config"
    assert spec.commit_type == "chore"
    assert spec.commit_scope == "dependabot"
    assert spec.epic == "vergil-project/.github#420"
    assert spec.title == "chore(dependabot): add the report-only dependabot.yml"
    assert spec.body

    proc_calls: list[list[str]] = []
    outputs = iter(["wrote .github/dependabot.yml (python)\n", "already in sync\n"])

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        proc_calls.append(list(args))
        return _FakeProc(stdout=next(outputs))

    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", fake_proc_run)

    changed = applicator(Path("/w"))
    assert proc_calls[-1] == ["vrg-dependabot-sync", "--write", "--repo", "/w"]
    assert changed.changed is True
    assert changed.summary == "wrote .github/dependabot.yml (python)"

    unchanged = applicator(Path("/w"))
    assert unchanged.changed is False
    assert unchanged.summary == "already in sync"


def test_fleet_target_dependabot_failure_names_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _spec_unused, applicator = _capture_main(
        monkeypatch, ["--repos", "/clones/x", "--target", "dependabot"]
    )

    def fake_proc_run(args, **kwargs):  # noqa: ANN001, ANN003
        return _FakeProc(returncode=2, stdout="usage")

    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", fake_proc_run)
    with pytest.raises(RuntimeError, match=r"vrg-dependabot-sync failed \(exit 2\): usage"):
        applicator(Path("/w"))


@pytest.mark.parametrize(
    ("target", "module"),
    [
        ("dependabot", "vergil_tooling.bin.vrg_dependabot_sync"),
        ("gitignore", "vergil_tooling.bin.vrg_gitignore_sync"),
    ],
)
def test_fleet_applicator_surfaces_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, module: str
) -> None:
    """#3451: a repo whose vergil.toml fails to load is an error row naming the cause.

    The real applicator CLI runs (as ``python -m``) against a worktree holding a
    malformed ``vergil.toml``; its exit-2 config error must reach the sweep as
    the raised message, never a silently rendered language-less file.
    """
    (tmp_path / "vergil.toml").write_text("not = [valid toml", encoding="utf-8")
    _spec_unused, applicator = _capture_main(
        monkeypatch, ["--repos", "/clones/x", "--target", target]
    )
    real_run = subprocess.run

    def run_real_cli(args, **kwargs):  # noqa: ANN001, ANN003, ANN202
        return real_run([sys.executable, "-m", module, *args[1:]], **kwargs)  # noqa: S603

    monkeypatch.setattr(vrg_fleet_sync.subprocess, "run", run_real_cli)
    with pytest.raises(RuntimeError, match=r"failed \(exit 2\): .*not valid TOML"):
        applicator(tmp_path)
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_fleet_target_rejects_unknown() -> None:
    with pytest.raises(SystemExit):
        vrg_fleet_sync.main(["--repos", "/clones/x", "--target", "nope"])


def test_main_requires_repos() -> None:
    """main errors (argparse) when no repos are given."""
    with pytest.raises(SystemExit):
        vrg_fleet_sync.main([])
