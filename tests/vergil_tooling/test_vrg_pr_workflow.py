"""Tests for vrg-pr-workflow's batch paths (epic vergil-project/.github#1019).

``batch-start``, ``batch-extract`` and ``batch-show``, and ``report-ready``
with a repeated ``--issue``, run in a temporary git clone whose ``origin``
names a github.com repo; the hard rules are faked at
``batch_rules.check_hard_rules``. For report-ready, ``origin`` pushes to a
local bare repo, the GitHub reads of the per-issue refusals are faked, and
the commit-per-issue check runs for real on the clone's commits. The other
report-ready paths are covered in
``tests/vergil_tooling/pr_workflow/test_cli_e2e.py``.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_pr_workflow
from vergil_tooling.lib.pr_workflow.batch_marker import Marker, read_marker, write_markers
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from unittest.mock import MagicMock

RULES = "vergil_tooling.bin.vrg_pr_workflow.batch_rules.check_hard_rules"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of acme/tools on develop, with the working directory in it."""
    repo = tmp_path / "tools"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base commit")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/tools.git")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture()
def rules() -> Iterator[MagicMock]:
    with patch(RULES) as fake:
        yield fake


def _run(*args: str) -> int:
    return vrg_pr_workflow.main(["--base", "develop", *args])


def _state_dir(repo: Path, number: int) -> Path:
    return repo / "build" / ".vergil-state" / "issue-implement" / str(number)


def test_batch_start_writes_marker_for_every_member(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(clone, "rev-parse", "develop")
    assert _run("batch-start", "--issue", "10", "--issue", "#11", "--issue", "acme/Tools#12") == 0
    rules.assert_called_once_with(["#10", "#11", "#12"], repo="acme/tools")
    for issue in ("#10", "#11", "#12"):
        assert read_marker(clone, issue) == Marker(["#10", "#11", "#12"], base)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "resumed": False, "issues": ["#10", "#11", "#12"], "base_sha": base}


def test_batch_start_writes_to_the_main_clone_from_a_worktree(
    clone: Path, rules: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = clone.parent / "wt"
    _git(clone, "worktree", "add", "-b", "feature/10-batch-x", str(worktree))
    monkeypatch.chdir(worktree)
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 0
    assert read_marker(clone, "#10") is not None
    assert not (worktree / "build").exists()


def test_batch_start_refuses_and_writes_nothing_on_rule_break(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    rules.side_effect = BatchRuleError("no duplicate", "#10 is listed more than once")
    assert _run("batch-start", "--issue", "10", "--issue", "10") == 1
    assert "no duplicate" in capsys.readouterr().err
    assert not (clone / "build").exists()


def test_batch_start_accepts_identical_existing_batch(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    old = "c" * 40
    write_markers(clone, ["#10", "#11"], base_sha=old)
    before = (_state_dir(clone, 10) / "batch").stat().st_mtime_ns
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "resumed": True, "issues": ["#10", "#11"], "base_sha": old}
    assert read_marker(clone, "#11") == Marker(["#10", "#11"], old)
    assert (_state_dir(clone, 10) / "batch").stat().st_mtime_ns == before


def test_batch_start_refuses_different_list_over_existing_markers(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-start", "--issue", "11", "--issue", "12") == 1
    assert "#11 is already in the batch #10, #11" in capsys.readouterr().err
    assert read_marker(clone, "#12") is None
    assert read_marker(clone, "#11") == Marker(["#10", "#11"], "c" * 40)


def test_batch_start_exits_2_in_another_repos_clone(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-start", "--issue", "acme/other#10", "--issue", "11") == 2
    assert "this is a clone of acme/tools" in capsys.readouterr().err
    rules.assert_not_called()
    assert not (clone / "build").exists()


def test_batch_start_exits_2_when_the_clone_repo_cannot_be_told(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(clone, "remote", "set-url", "origin", "https://example.com/acme/tools.git")
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 2
    assert "cannot tell which repo" in capsys.readouterr().err
    rules.assert_not_called()


def test_batch_start_refuses_an_unresolvable_base(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        vrg_pr_workflow.main(
            ["--base", "origin/develop", "batch-start", "--issue", "10", "--issue", "11"]
        )
        == 1
    )
    assert "cannot resolve the base origin/develop" in capsys.readouterr().err
    assert read_marker(clone, "#10") is None


def test_batch_start_refuses_a_malformed_issue(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-start", "--issue", "ten", "--issue", "11") == 1
    assert "--issue must be N, #N or owner/repo#N" in capsys.readouterr().err
    rules.assert_not_called()


def test_batch_extract_rewrites_the_batch(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = "c" * 40
    write_markers(clone, ["#10", "#11", "#12"], base_sha=sha)
    assert _run("batch-extract", "--issue", "acme/tools#11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "extracted": "#11", "remaining": ["#10", "#12"], "batch": True}
    assert not _state_dir(clone, 11).exists()
    assert read_marker(clone, "#12") == Marker(["#10", "#12"], sha)


def test_batch_extract_to_one_member(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-extract", "--issue", "11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "extracted": "#11", "remaining": ["#10"], "batch": False}
    assert read_marker(clone, "#10") is None


def test_batch_extract_refuses_while_security_details_exist(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    review = _state_dir(clone, 11) / "merge-review"
    review.mkdir(mode=0o700)
    (review / "security-R1.md").write_text("details\n")
    assert _run("batch-extract", "--issue", "11") == 1
    assert "record step" in capsys.readouterr().err
    assert (review / "security-R1.md").exists()


def test_batch_extract_exits_2_in_another_repos_clone(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-extract", "--issue", "acme/other#11") == 2
    assert read_marker(clone, "#11") is not None
    assert "acme/other" in capsys.readouterr().err


def test_batch_show_prints_the_marker(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = "c" * 40
    write_markers(clone, ["#10", "#11"], base_sha=sha)
    assert _run("batch-show", "--issue", "acme/tools#11") == 0
    assert json.loads(capsys.readouterr().out) == {"issues": ["#10", "#11"], "base_sha": sha}


def test_batch_show_exits_1_without_marker(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("batch-show", "--issue", "10") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "#10 has no batch marker" in captured.err


def test_batch_show_exits_2_in_another_repos_clone(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Another repo's #10 is in a batch here, so a wrong-clone lookup must never
    # read this clone's marker, nor answer "no marker".
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-show", "--issue", "acme/other#10") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "this is a clone of acme/tools" in captured.err


def test_batch_show_exits_2_on_an_unreadable_marker(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    (_state_dir(clone, 10) / "batch").write_text("{")
    assert _run("batch-show", "--issue", "10") == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_batch_show_exits_2_on_a_malformed_issue(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-show", "--issue", "ten") == 2
    assert "--issue must be" in capsys.readouterr().err


def test_batch_show_exits_2_outside_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert _run("batch-show", "--issue", "10") == 2
    assert "error: " in capsys.readouterr().err


def test_other_commands_keep_raising_outside_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        _run("status")


def test_batch_show_exits_2_without_an_origin_remote(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(clone, "remote", "remove", "origin")
    assert _run("batch-show", "--issue", "acme/tools#10") == 2
    assert "its origin remote cannot be read" in capsys.readouterr().err


# --- report-ready with an ordered issue list ---------------------------------

_EPICS = "vergil_tooling.bin.vrg_pr_workflow.epics"
_SINGLE_KEYS = [
    "schema_version",
    "issue",
    "branch",
    "base",
    "status",
    "created_at",
    "updated_at",
    "git",
    "pr_metadata",
    "submitted",
]


def _commit(repo: Path, name: str, subject: str) -> None:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", subject)


def _branch(clone: Path, *subjects: str) -> None:
    """Check out a feature branch off develop with one commit per subject."""
    _git(clone, "checkout", "-b", "feature/10-batch-x")
    for index, subject in enumerate(subjects):
        _commit(clone, f"f{index}.txt", subject)


@pytest.fixture()
def ready_clone(clone: Path) -> Path:
    """The clone, pushing ``origin`` to a local bare repo (its fetch URL stays github)."""
    bare = clone.parent / "origin.git"
    _git(clone.parent, "init", "--bare", str(bare))
    _git(clone, "remote", "set-url", "--push", "origin", str(bare))
    return clone


@pytest.fixture()
def relay() -> Iterator[MagicMock]:
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as fake:
        yield fake


@pytest.fixture()
def per_issue() -> Iterator[dict[str, MagicMock]]:
    """The GitHub reads behind report-ready's per-issue refusals, all passing."""
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="acme/tools"),
        patch(f"{_EPICS}.non_task_ref_kind", return_value=None) as non_task,
        patch(f"{_EPICS}.is_operational_task", return_value=False) as operational,
        patch(f"{_EPICS}.queue_item_kind", return_value=None) as queue,
    ):
        yield {"non_task": non_task, "operational": operational, "queue": queue}


def _report(*issues: str) -> int:
    flags = [arg for issue in issues for arg in ("--issue", issue)]
    return _run("report-ready", *flags, "--title", "t", "--summary", "s", "--notes", "n")


def _state_file(repo: Path) -> Path:
    return repo / ".vergil" / "pr-workflow.json"


def _on_origin(repo: Path) -> bool:
    bare = repo.parent / "origin.git"
    result = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", "refs/heads/feature/10-batch-x"),
        cwd=bare,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _assert_nothing_recorded(repo: Path, relay: MagicMock) -> None:
    assert not _state_file(repo).exists()
    assert not _on_origin(repo)
    relay.return_value.write.assert_not_called()


def test_report_ready_single_issue_unchanged(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Review focus 5: one --issue is today's report-ready, byte for byte.
    _branch(ready_clone, "feat: one thing")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    with patch("vergil_tooling.bin.vrg_pr_workflow.batch_rules.check_commit_per_issue") as commits:
        assert _report("10") == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}
    rules.assert_not_called()
    commits.assert_not_called()
    payload = json.loads(_state_file(ready_clone).read_text())
    assert list(payload) == _SINGLE_KEYS
    assert payload["issue"] == "10"
    relayed = relay.return_value.write.call_args.args[0]
    assert relayed.issues is None
    assert "issues" not in json.loads(relayed.to_json())
    # A single-issue hand-off never touches batch markers.
    assert read_marker(ready_clone, "#10") == Marker(["#10", "#11"], "c" * 40)
    assert _on_origin(ready_clone)


def test_report_ready_batch_records_ordered_issues(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: b (#12)", "fix: a (#10)", "docs: c (#11)")
    assert _report("12", "10", "acme/tools#11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "status": "ready", "issues": ["12", "10", "acme/tools#11"]}
    rules.assert_called_once_with(["#12", "#10", "acme/tools#11"], repo="acme/tools")
    payload = json.loads(_state_file(ready_clone).read_text())
    assert payload["issue"] == "12"
    assert payload["issues"] == ["12", "10", "acme/tools#11"]
    assert payload["status"] == "ready"
    relayed = relay.return_value.write.call_args.args[0]
    assert json.loads(relayed.to_json())["issues"] == ["12", "10", "acme/tools#11"]
    assert _on_origin(ready_clone)


def test_report_ready_batch_rerun_with_the_same_list_succeeds(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    assert _report("10", "11") == 0
    assert _report("10", "11") == 0
    assert json.loads(_state_file(ready_clone).read_text())["issues"] == ["10", "11"]


def test_report_ready_batch_refuses_rule_break(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#10)")
    rules.side_effect = BatchRuleError("no duplicate", "#10 is listed more than once")
    assert _report("10", "10") == 1
    err = capsys.readouterr().err
    assert "no duplicate" in err
    assert "nothing was pushed or recorded" in err
    _assert_nothing_recorded(ready_clone, relay)


def test_report_ready_batch_refuses_commit_mismatch(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: b (#11)", "feat: a (#10)")
    first = _git(ready_clone, "rev-parse", "--short=12", "HEAD~1")
    assert _report("10", "11") == 1
    err = capsys.readouterr().err
    assert f"commit {first}" in err
    assert "it should end in (#10)" in err
    _assert_nothing_recorded(ready_clone, relay)


def test_report_ready_batch_refuses_a_clone_whose_repo_cannot_be_told(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    _git(ready_clone, "remote", "set-url", "origin", "https://example.com/acme/tools.git")
    assert _report("10", "11") == 1
    assert "cannot tell which repo" in capsys.readouterr().err
    rules.assert_not_called()
    _assert_nothing_recorded(ready_clone, relay)


@pytest.mark.parametrize("kind", ["cross_repo", "non_task", "operational", "queue"])
def test_report_ready_runs_existing_per_issue_refusals_on_every_issue(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    # The second issue of the batch is the bad one: each per-issue refusal
    # runs on every --issue, not only the first.
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    second = "11"
    if kind == "cross_repo":
        second = "acme/other#11"
    elif kind == "non_task":
        per_issue["non_task"].side_effect = lambda ref, **_: "epic" if ref == "#11" else None
    elif kind == "operational":
        per_issue["operational"].side_effect = lambda ref, **_: ref == "#11"
    else:
        per_issue["queue"].side_effect = lambda ref, **_: "intake" if ref == "#11" else None
    assert _report("10", second) == 1
    assert "11" in capsys.readouterr().err
    rules.assert_not_called()
    _assert_nothing_recorded(ready_clone, relay)


def test_report_ready_rerun_refuses_a_different_issue_list(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    assert _report("10", "11") == 0
    recorded = _state_file(ready_clone).read_text()
    capsys.readouterr()
    for rerun in (("10",), ("11", "10")):
        assert _report(*rerun) == 1
        err = capsys.readouterr().err
        assert "stale workflow file for issues #10, #11" in err
        assert f"you passed {', '.join('#' + issue for issue in rerun)}" in err
        assert _state_file(ready_clone).read_text() == recorded


def test_report_ready_rerun_of_a_single_issue_keeps_its_message(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: one thing")
    assert _report("10") == 0
    assert _report("11") == 1
    assert (
        "stale workflow file for issue #10; you passed #11. "
        "Delete .vergil/pr-workflow.json to start fresh." in capsys.readouterr().err
    )


def test_report_ready_batch_removes_markers_on_success(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    review = _state_dir(ready_clone, 10) / "merge-review"
    review.mkdir(mode=0o700)
    assert _report("10", "acme/tools#11") == 0
    assert read_marker(ready_clone, "#10") is None
    assert read_marker(ready_clone, "#11") is None
    # Only the marker goes: the merge-review state beside it is the skill's.
    assert review.is_dir()
    assert not _state_dir(ready_clone, 11).exists()


def test_report_ready_batch_removes_markers_from_a_worktree(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = ready_clone.parent / "wt"
    _git(ready_clone, "worktree", "add", "-b", "feature/10-batch-x", str(worktree))
    _commit(worktree, "a.txt", "feat: a (#10)")
    _commit(worktree, "b.txt", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    monkeypatch.chdir(worktree)
    assert _report("10", "11") == 0
    assert read_marker(ready_clone, "#10") is None
    assert read_marker(ready_clone, "#11") is None
    assert _state_file(worktree).is_file()


def test_report_ready_batch_removes_markers_when_relay_push_warns(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    relay.return_value.write.side_effect = subprocess.CalledProcessError(1, "git push")
    assert _report("10", "11") == 0
    assert "could not push pr-workflow relay ref" in capsys.readouterr().err
    assert read_marker(ready_clone, "#10") is None
    assert read_marker(ready_clone, "#11") is None


def test_report_ready_batch_keeps_markers_on_refusal(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "fixup! feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    assert _report("10", "11") == 1
    assert read_marker(ready_clone, "#10") == Marker(["#10", "#11"], "c" * 40)
    assert read_marker(ready_clone, "#11") == Marker(["#10", "#11"], "c" * 40)
    _assert_nothing_recorded(ready_clone, relay)


def test_report_ready_batch_marker_removal_failure_is_loud(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    with patch(
        "vergil_tooling.bin.vrg_pr_workflow.batch_marker.remove_markers",
        side_effect=OSError("read-only file system"),
    ):
        assert _report("10", "11") == 1
    err = capsys.readouterr().err
    assert "the ready state is recorded" in err
    assert "read-only file system" in err
    assert "re-run report-ready" in err
    assert json.loads(_state_file(ready_clone).read_text())["status"] == "ready"
    # The re-run it names finishes the job.
    assert _report("10", "11") == 0
    assert read_marker(ready_clone, "#10") is None
