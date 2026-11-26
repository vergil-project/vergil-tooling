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

import itertools
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_pr_workflow
from vergil_tooling.lib import epics, pr_path, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.github import ClosingIssue, MissingGitHubTokenError, PRClosingReferences
from vergil_tooling.lib.planning_close import PlanningClose
from vergil_tooling.lib.pr_workflow import engine, freeze
from vergil_tooling.lib.pr_workflow.batch_marker import Marker, read_marker, write_markers
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

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
# Captured before any patching, to run the real parent test under a fixture
# that stubs it.
_REAL_UNBUILDABLE_PARENT_KIND = epics.unbuildable_parent_kind
_SINGLE_KEYS = [
    "schema_version",
    "issue",
    "branch",
    "base",
    "status",
    "recorded_by",
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
    """The GitHub reads behind report-ready's per-issue refusals, all passing.

    The planning-close lookup finds no documentation task, and the branch has
    no open PR into ``develop``.
    """
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="acme/tools"),
        patch(f"{_EPICS}.non_task_ref_kind", return_value=None) as non_task,
        patch(f"{_EPICS}.is_operational_task", return_value=False) as operational,
        patch(f"{_EPICS}.unbuildable_parent_kind", return_value=None) as queue,
        patch(f"{_WF}.planning_close.find", return_value=None) as planning,
        patch(f"{_WF}.pr_path.open_develop_pr_url", return_value=None) as open_pr,
    ):
        yield {
            "non_task": non_task,
            "operational": operational,
            "queue": queue,
            "planning": planning,
            "open_pr": open_pr,
        }


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
    # Who recorded it, the hermetic default identity, in the file and the relay.
    assert payload["recorded_by"] == "human"
    assert relayed.recorded_by == "human"
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
    # Printed in batch-show's #N form, recorded as given (#4297).
    assert out == {"ok": True, "status": "ready", "issues": ["#12", "#10", "#11"]}
    rules.assert_called_once_with(["#12", "#10", "acme/tools#11"], repo="acme/tools")
    payload = json.loads(_state_file(ready_clone).read_text())
    assert payload["issue"] == "12"
    assert payload["issues"] == ["12", "10", "acme/tools#11"]
    assert payload["status"] == "ready"
    relayed = relay.return_value.write.call_args.args[0]
    assert json.loads(relayed.to_json())["issues"] == ["12", "10", "acme/tools#11"]
    assert _on_origin(ready_clone)
    # A batch PR never closes a planning issue, so it is never looked up.
    per_issue["planning"].assert_not_called()


_DOCS_TASK = IssueRef("acme", "tools", 10)
_PLANNING = IssueRef("acme", "tools", 7)


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (PlanningClose(_DOCS_TASK, _PLANNING, None), {"closes": "acme/tools#7"}),
        (
            PlanningClose(_DOCS_TASK, _PLANNING, "it is already closed"),
            {"planning": "acme/tools#7", "skipped": "it is already closed"},
        ),
    ],
)
def test_report_ready_previews_the_planning_close(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
    found: PlanningClose,
    expected: dict[str, str],
) -> None:
    _branch(ready_clone, "docs: the spec and plan")
    per_issue["planning"].return_value = found
    assert _report("10") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "status": "ready", "planning_close": expected}
    per_issue["planning"].assert_called_once_with(_DOCS_TASK)


def test_report_ready_reports_a_planning_close_it_could_not_check(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "docs: the spec and plan")
    per_issue["planning"].side_effect = subprocess.CalledProcessError(1, ["gh"])
    assert _report("10") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ready"
    assert out["planning_close"]["unknown"].startswith(
        "could not check whether #10 is an epic's documentation task"
    )


@pytest.mark.parametrize(
    "error",
    [
        trust.TrustReadError("read_issue acme/.github#7", "HTTP 502"),
        trust.UntrustedContentError(
            trust.Skipped("acme/.github", 7, "edit", "drive-by", "t", "body", trust.UNTRUSTED),
            "the issue body is untrusted",
        ),
    ],
    ids=["read-failure", "untrusted-epic-body"],
)
def test_report_ready_reports_a_planning_close_the_trust_layer_stopped(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """A trust-layer stop in the preview is reported as unknown, never a failed report-ready."""
    _branch(ready_clone, "docs: the spec and plan")
    per_issue["planning"].side_effect = error
    assert _report("10") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ready"
    unknown = out["planning_close"]["unknown"]
    assert unknown.startswith("could not check whether #10 is an epic's documentation task")
    assert str(error) in unknown


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


def test_report_ready_push_refused_on_open_pr_branch(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Spec §5.4: a push to an open PR's branch goes through the path, which
    # report-ready's own push never is.
    _branch(ready_clone, "feat: one thing")
    per_issue["open_pr"].return_value = "https://github.com/acme/tools/pull/7"
    assert _report("10") == 1
    err = capsys.readouterr().err
    assert "feature/10-batch-x has an open PR into develop" in err
    assert "https://github.com/acme/tools/pull/7" in err
    assert "vrg-submit-pr --push" in err
    assert "nothing was pushed or recorded" in err
    per_issue["open_pr"].assert_called_once_with("feature/10-batch-x")
    _assert_nothing_recorded(ready_clone, relay)


def test_open_pr_lookup_failure_refuses_push(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: one thing")
    per_issue["open_pr"].side_effect = pr_path.PRPathError(
        "push mode: reading the open PRs on feature/10-batch-x failed: HTTP 502"
    )
    # Fail closed: a lookup that fails refuses the push, naming the error.
    assert _report("10") == 1
    err = capsys.readouterr().err
    assert "could not tell whether feature/10-batch-x has an open PR into develop" in err
    assert "HTTP 502" in err
    assert "nothing was pushed or recorded" in err
    _assert_nothing_recorded(ready_clone, relay)


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


def test_report_ready_refuses_ops_issue(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An ops issue (native parent: the operations epic) is never the task a
    # PR closes (epic vergil-project/.github#1494). The parent test runs for
    # real on the faked parent and its labels.
    _branch(ready_clone, "feat: one thing")
    ops_epic = IssueRef("acme", ".github", 3)
    per_issue["queue"].side_effect = _REAL_UNBUILDABLE_PARENT_KIND
    with (
        patch(f"{_EPICS}.native_parent_of", return_value=ops_epic) as parent,
        patch(f"{_EPICS}._labels", return_value={"epic", "operations"}) as labels,
    ):
        assert _report("10") == 1
    parent.assert_called_once_with(IssueRef("acme", "tools", 10))
    labels.assert_called_once_with(ops_epic)
    err = capsys.readouterr().err
    assert "--issue (#10) is under the operations epic; it is an ops issue, which no PR" in err
    assert "close the ops issue by hand once the problem is dealt with." in err
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


def test_report_ready_batch_relay_push_failure_fails_after_removing_markers(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #5035: an unwritten relay ref fails report-ready, but only once the rest of
    # the recording (the batch markers included) is done, so the re-run it names
    # only has the relay ref left to write.
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    relay.return_value.ref = "refs/vergil/pr-workflow/feature/10-x"
    relay.return_value.write.side_effect = subprocess.CalledProcessError(1, "git push")
    assert _report("10", "11") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the relay ref refs/vergil/pr-workflow/feature/10-x was NOT written" in captured.err
    assert "Re-run report-ready" in captured.err
    assert read_marker(ready_clone, "#10") is None
    assert read_marker(ready_clone, "#11") is None
    assert json.loads(_state_file(ready_clone).read_text())["status"] == "ready"
    # The re-run it names writes the relay ref and succeeds.
    relay.return_value.write.side_effect = None
    assert _report("10", "11") == 0


def test_report_ready_batch_relay_and_marker_failures_are_both_reported(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branch(ready_clone, "feat: a (#10)", "feat: b (#11)")
    write_markers(ready_clone, ["#10", "#11"], base_sha="c" * 40)
    relay.return_value.ref = "refs/vergil/pr-workflow/feature/10-x"
    relay.return_value.write.side_effect = subprocess.CalledProcessError(1, "git push")
    with patch(
        "vergil_tooling.bin.vrg_pr_workflow.batch_marker.remove_markers",
        side_effect=OSError("read-only file system"),
    ):
        assert _report("10", "11") == 1
    err = capsys.readouterr().err
    assert "the relay ref refs/vergil/pr-workflow/feature/10-x was NOT written" in err
    assert "the batch markers of #10, #11 could not be removed" in err
    assert "read-only file system" in err


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


# -- batch-check (epic vergil-project/.github#1218, Task 18) ---------------------

_PR = "https://github.com/acme/tools/pull/50"
_PR_BRANCH = "feature/10-batch-x"
_BATCH_BODY = "## Issue Linkage\n\nCloses #10\nCloses #11\n"
_WF = "vergil_tooling.bin.vrg_pr_workflow"
_AUTHORS = itertools.count()


def _closing(*numbers: int) -> PRClosingReferences:
    return PRClosingReferences(
        repo="acme/tools",
        number=50,
        state="OPEN",
        merged_at=None,
        issues=tuple(ClosingIssue(repo="acme/tools", number=n, state="OPEN") for n in numbers),
    )


@pytest.fixture()
def origin(clone: Path) -> Path:
    """A local bare origin holding develop; the clone fetches from it."""
    bare = clone.parent / "origin.git"
    _git(clone.parent, "init", "--bare", str(bare))
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(clone, "remote", "set-url", "origin", str(bare))
    _git(clone, "push", "origin", "develop")
    return bare


def _push_pr_branch(origin: Path, *subjects: str) -> str:
    """Push a PR branch off develop from another clone; return its head SHA.

    The commits are never in the checking clone, so batch-check must fetch them.
    """
    author = origin.parent / f"author-{next(_AUTHORS)}"
    _git(origin.parent, "clone", "-q", "-b", "develop", str(origin), str(author))
    _git(author, "config", "user.email", "t@example.com")
    _git(author, "config", "user.name", "Test")
    _git(author, "checkout", "-q", "-B", _PR_BRANCH)
    for index, subject in enumerate(subjects):
        _commit(author, f"pr{index}.txt", subject)
    _git(author, "push", "-q", "-f", "origin", _PR_BRANCH)
    return _git(author, "rev-parse", "HEAD")


@pytest.fixture()
def github_pr(origin: Path) -> Iterator[dict[str, MagicMock]]:
    """GitHub's view of PR 50 (a batch of #10 and #11) and this clone's repo.

    The body comes only from the trust layer's read (``read_issue``).
    """
    with (
        patch(f"{_WF}.github.pr_repo_and_number", return_value=("acme/tools", 50)) as number,
        patch(f"{_WF}.github.read_json") as read_json,
        patch(f"{_WF}.trust.read_issue") as read_issue,
        patch(f"{_WF}.github.pr_closing_references", return_value=_closing(10, 11)) as closing,
        patch(f"{_WF}._clone_repo", return_value="acme/tools"),
        patch(RULES) as rules,
    ):
        yield {
            "number": number,
            "read_json": read_json,
            "read_issue": read_issue,
            "closing": closing,
            "rules": rules,
        }


def _body(text: str) -> trust.IssueContent:
    return trust.IssueContent(
        body=text,
        body_role=trust.Role.AGENTS,
        author_role=trust.Role.AGENTS,
        comments=(),
        skipped=(),
    )


def _pr_is(github_pr: dict[str, MagicMock], head: str, body: str = _BATCH_BODY) -> None:
    github_pr["read_json"].return_value = {"baseRefName": "develop", "headRefOid": head}
    github_pr["read_issue"].return_value = _body(body)


def test_batch_check_passes_a_batch_pr_at_its_head(
    clone: Path, origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    head = _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)")
    _pr_is(github_pr, head)
    assert _run("batch-check", "--pr", _PR) == 0
    assert f"Batch check passed for PR {_PR} at {head}" in capsys.readouterr().out
    github_pr["read_json"].assert_called_once_with(
        "pr", "view", _PR, "--json", "baseRefName,headRefOid"
    )
    github_pr["read_issue"].assert_called_once_with("acme/tools", 50, require_body=True)
    github_pr["rules"].assert_not_called()
    # The head was fetched by its SHA: the PR branch's tracking ref never moved.
    branch_ref = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{_PR_BRANCH}"),
        cwd=clone,
        check=False,
        capture_output=True,
    )
    assert branch_ref.returncode != 0


def test_batch_check_passes_non_batch_pr(
    clone: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, "a" * 40, body="## Issue Linkage\n\nCloses #10\nRef #11\n")
    assert _run("batch-check", "--pr", _PR) == 0
    assert "is not a batch PR" in capsys.readouterr().out
    github_pr["closing"].assert_not_called()
    github_pr["rules"].assert_not_called()


def test_batch_check_fixup_commit_fails(
    origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    head = _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)", "fixup! feat: b (#11)")
    _pr_is(github_pr, head)
    assert _run("batch-check", "--pr", _PR) == 1
    err = capsys.readouterr().err
    assert f"batch-check: PR {_PR} at {head[:12]}" in err
    assert "unfolded fixup" in err


def test_batch_check_uses_given_head(
    clone: Path, origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    """The PR's head on GitHub fails, but --head (a local, unpushed rebase) passes,
    and the other way round: only the given head is checked."""
    bad = _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)", "fixup! feat: b (#11)")
    _git(clone, "checkout", "-q", "-b", _PR_BRANCH)
    _commit(clone, "a.txt", "fix: a (#10)")
    _commit(clone, "b.txt", "feat: b (#11)")
    good = _git(clone, "rev-parse", "HEAD")
    _pr_is(github_pr, bad)
    assert _run("batch-check", "--pr", _PR, "--head", good) == 0
    assert f"at {good}" in capsys.readouterr().out
    _pr_is(github_pr, good)
    assert _run("batch-check", "--pr", _PR, "--head", bad) == 1
    assert "unfolded fixup" in capsys.readouterr().err


def test_batch_check_allowed_when_frozen(
    clone: Path, origin: Path, github_pr: dict[str, MagicMock]
) -> None:
    _git(clone, "checkout", "-q", "-b", _PR_BRANCH)
    _commit(clone, "a.txt", "fix: a (#10)")
    _commit(clone, "b.txt", "feat: b (#11)")
    head = _git(clone, "rev-parse", "HEAD")
    state = engine.init_state(
        issues=["10", "11"],
        branch=_PR_BRANCH,
        base="origin/develop",
        head_sha=head,
        base_sha=_git(clone, "rev-parse", "develop"),
        now="2026-11-17T00:00:00Z",
    )
    LocalFileTransport(clone).write(
        engine.apply_report_ready(
            state,
            title="t",
            summary="s",
            notes="n",
            linkage="Ref",
            head_sha=head,
            now="2026-11-17T01:00:00Z",
        )
    )
    assert freeze.check_worktree(clone, action="commit").frozen
    before = _state_file(clone).read_text()
    _pr_is(github_pr, head)
    assert _run("batch-check", "--pr", _PR) == 0
    assert _state_file(clone).read_text() == before


@pytest.mark.parametrize(
    ("closing", "expected"),
    [((10,), "missing acme/tools#11"), ((10, 11, 12), "its body does not close acme/tools#12")],
)
def test_batch_check_refuses_a_closing_refs_mismatch(
    origin: Path,
    github_pr: dict[str, MagicMock],
    capsys: pytest.CaptureFixture[str],
    closing: tuple[int, ...],
    expected: str,
) -> None:
    _pr_is(github_pr, _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)"))
    github_pr["closing"].return_value = _closing(*closing)
    assert _run("batch-check", "--pr", _PR) == 1
    err = capsys.readouterr().err
    assert "batch-check: PR acme/tools#50: batch refused (closing references)" in err
    assert expected in err


def test_batch_check_refuses_another_repos_clone(
    origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)"))
    with patch(f"{_WF}._clone_repo", return_value="acme/other"):
        assert _run("batch-check", "--pr", _PR) == 1
    assert "this is a clone of acme/other" in capsys.readouterr().err


def test_batch_check_refuses_a_failed_pr_read(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    github_pr["read_json"].side_effect = subprocess.CalledProcessError(1, "gh pr view")
    assert _run("batch-check", "--pr", _PR) == 1
    assert f"batch-check: cannot read PR {_PR}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "response",
    [{}, {"baseRefName": "develop"}, {"headRefOid": "a" * 40}, {"baseRefName": ""}, []],
)
def test_batch_check_refuses_a_malformed_pr_read(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str], response: object
) -> None:
    github_pr["read_json"].return_value = response
    assert _run("batch-check", "--pr", _PR) == 1
    assert f"batch-check: cannot read PR {_PR}" in capsys.readouterr().err


_MALLORY = trust.Skipped(
    "acme/tools", 50, "edit", "mallory", "2026-11-20T00:00:00Z", "body", "untrusted"
)


@pytest.mark.parametrize("body", [_BATCH_BODY, "Closes #10\n"])
def test_batch_check_fails_on_an_untrusted_body(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str], body: str
) -> None:
    """Batch or not: an untrusted body leaves nothing to check by, so the check fails."""
    _pr_is(github_pr, "a" * 40, body=body)
    github_pr["read_issue"].side_effect = trust.UntrustedContentError(
        _MALLORY, "the pr body is untrusted"
    )
    assert _run("batch-check", "--pr", _PR) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err.strip()
    assert err == (
        f"error: batch-check: PR {_PR}: its body is untrusted, so its Closes lines cannot be "
        "checked: acme/tools#50 edit mallory at 2026-11-20T00:00:00Z on body (untrusted). "
        "The human decides next."
    )
    github_pr["closing"].assert_not_called()
    github_pr["rules"].assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        trust.TrustReadError("read_issue acme/tools#50", "the GraphQL call failed: HTTP 502"),
        TrustedAuthorsNotConfiguredError("no trusted authors configured"),
    ],
)
def test_batch_check_refuses_a_body_it_cannot_judge(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    _pr_is(github_pr, "a" * 40)
    github_pr["read_issue"].side_effect = failure
    assert _run("batch-check", "--pr", _PR) == 1
    err = capsys.readouterr().err
    assert f"error: batch-check: cannot read the body of PR {_PR}: {failure}" in err
    assert len(err.strip().splitlines()) == 1
    github_pr["closing"].assert_not_called()


def test_batch_check_never_reads_a_missing_body_as_empty(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, "a" * 40)
    github_pr["read_issue"].return_value = trust.IssueContent(
        body=None, body_role=None, author_role=None, comments=(), skipped=()
    )
    assert _run("batch-check", "--pr", _PR) == 1
    assert f"cannot read the body of PR {_PR}: no trusted body" in capsys.readouterr().err


def test_batch_check_refuses_a_failed_closing_refs_read(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, "a" * 40)
    github_pr["closing"].side_effect = RuntimeError("GraphQL error")
    assert _run("batch-check", "--pr", _PR) == 1
    assert "cannot read the closing issue references" in capsys.readouterr().err


@pytest.mark.parametrize("read", ["number", "read_json", "read_issue", "closing"])
def test_batch_check_lets_a_missing_token_reach_the_cli(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str], read: str
) -> None:
    _pr_is(github_pr, "a" * 40)
    github_pr[read].side_effect = MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN")
    with pytest.raises(SystemExit) as exit_info:
        _run("batch-check", "--pr", _PR)
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "no GitHub token available" in err
    assert "batch-check: cannot read" not in err


def test_batch_check_refuses_a_clone_whose_repo_cannot_be_told(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, "a" * 40)
    with patch(f"{_WF}._clone_repo", side_effect=vrg_pr_workflow.WrongCloneError("no origin")):
        assert _run("batch-check", "--pr", _PR) == 1
    assert "batch-check: no origin" in capsys.readouterr().err


def test_batch_check_refuses_an_unknown_head(
    origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, _push_pr_branch(origin, "fix: a (#10)", "feat: b (#11)"))
    assert _run("batch-check", "--pr", _PR, "--head", "no-such-branch") == 1
    assert "cannot find the commit no-such-branch" in capsys.readouterr().err


@pytest.mark.parametrize("head", ["", "--all"])
def test_batch_check_refuses_an_option_as_head(
    github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str], head: str
) -> None:
    assert _run("batch-check", "--pr", _PR, f"--head={head}") == 1
    assert "--head is not a git revision" in capsys.readouterr().err
    github_pr["read_json"].assert_not_called()


def test_batch_check_refuses_a_base_it_cannot_fetch(
    origin: Path, github_pr: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    _pr_is(github_pr, "a" * 40)
    github_pr["read_json"].return_value = {"baseRefName": "no-such-base", "headRefOid": "a" * 40}
    assert _run("batch-check", "--pr", _PR) == 1
    assert "cannot fetch PR" in capsys.readouterr().err


def test_report_ready_records_the_agent_identity_in_file_and_relay(
    ready_clone: Path,
    rules: MagicMock,
    relay: MagicMock,
    per_issue: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report-ready writes recorded_by for submit to judge (epic .github#1384, Task 9)."""
    _branch(ready_clone, "feat: one thing")
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    with patch("vergil_tooling.bin.vrg_pr_workflow.local_settings.enforce", return_value=0):
        assert _report("10") == 0
    payload = json.loads(_state_file(ready_clone).read_text())
    assert payload["recorded_by"] == "user"
    assert relay.return_value.write.call_args.args[0].recorded_by == "user"
