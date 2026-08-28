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
    record: dict[str, list] = {"git": [], "proc": [], "seq": []}

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
    # Build an origin repo with a committed file on develop, then clone it so the
    # clone carries an ``origin/develop`` remote-tracking ref for the probe.
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

    def applicator(worktree: Path) -> AppResult:
        (worktree / "managed.txt").write_text("propagated content\n")
        return AppResult(changed=True, summary="added managed.txt")

    def fake_run_tool(args: list[str], *, cwd: Path | str | None = None) -> str:
        """Fake the host-tool seam so ``git.run`` stays REAL.

        Faking ``_run_tool`` (not ``subprocess.run``) is deliberate: patching the
        shared ``subprocess`` module would also intercept this test's own real
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

    # git.run stays REAL; only the host-tool shell-outs (_run_tool) are faked.
    monkeypatch.setattr(fleet_sweep, "_run_tool", fake_run_tool)

    results = run_sweep(_spec([str(clone)]), applicator, dry_run=False)

    assert results == [RepoResult(repo=str(clone), status="ready", detail="added managed.txt")], (
        "sweep must succeed with the applicator's change committed"
    )

    # The worktree was renamed to the issue-<N>-<slug> convention...
    worktree = clone / ".worktrees" / "issue-42-gitignore-sync"
    assert worktree.is_dir()
    # ...and its HEAD commit actually contains the applicator's new file.
    committed = _git("-C", str(worktree), "show", "--stat", "--name-only", "HEAD")
    assert "managed.txt" in committed
    # the branch follows the convention too.
    branch = _git("-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert branch == "feature/42-gitignore-sync"


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


def test_main_requires_repos() -> None:
    """main errors (argparse) when no repos are given."""
    with pytest.raises(SystemExit):
        vrg_fleet_sync.main([])
