"""End-to-end tests for the run-and-done vrg-pr-workflow CLI (#1872)."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import local_settings_violation
from vergil_tooling.bin import vrg_pr_workflow
from vergil_tooling.lib import local_settings

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base commit")
    _git(repo, "checkout", "-b", "feature/42-x")
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "feature work")
    # A local bare origin, so report-ready's real branch push (#3602) lands
    # somewhere the tests can inspect.
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    return repo


def _origin(repo: Path) -> Path:
    return repo.parent / "origin.git"


def _rev(repo: Path, ref: str) -> str:
    """Resolve *ref* in *repo*, or '' when it does not exist."""
    result = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", ref),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temp repo on a feature branch and chdir into it."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture(autouse=True)
def _stub_relay_push() -> Iterator[None]:
    """Stub the relay-ref push for every test in this module (#2877).

    report-ready mirrors state onto refs/vergil/pr-workflow/<branch> via a real
    ``git push --force origin`` (github_transport.write). These tests used to run
    in a temp repo with no ``origin``, so that push failed with "could not read
    from remote" — which retry.is_retryable() treats as transient, so git.run
    burned the full 4-step backoff (~30s of real time.sleep) before report-ready
    swallowed the non-fatal failure. That made each report-ready test cost ~30-60s
    (11 tests = ~96% of the whole suite's wall-clock). The temp repo now has a
    bare local origin for report-ready's branch push (#3602); the relay stays
    stubbed so these tests do not depend on it. Stubbing GitHubTransport keeps the
    relay push a no-op. The real push contract is still covered by
    test_report_ready_pushes_relay_ref and
    test_report_ready_relay_push_failure_surfaces_but_local_persists (which
    re-patch GitHubTransport locally), and by test_github_transport.py.
    """
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport"):
        yield


def test_report_ready_initializes_and_records(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # in_git_repo: fixture that chdirs into a temp repo on a feature branch with
    # develop branch reachable as a local branch.
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "status": "ready"}

    rc = vrg_pr_workflow.main(["--base", "develop", "status"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["issue"] == "42"
    assert state["status"] == "ready"
    assert state["pr_metadata"]["title"] == "t"


def test_report_ready_pushes_relay_ref(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Task 3 (#2367): report-ready always mirrors the ready state onto the relay
    # ref via GitHubTransport, in addition to the local file write. The trigger
    # is unconditional (no off-platform detection).
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}
    # The relay was written with the ready state, after the local write.
    mock_transport.assert_called_once()
    write = mock_transport.return_value.write
    write.assert_called_once()
    (pushed_state,) = write.call_args.args
    assert pushed_state.status == "ready"
    assert pushed_state.pr_metadata is not None
    assert pushed_state.pr_metadata["title"] == "t"
    # And the durable local file was written too.
    assert (in_git_repo / ".vergil" / "pr-workflow.json").is_file()


def test_report_ready_relay_push_failure_surfaces_but_local_persists(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A relay push failure surfaces loudly on stderr but never undoes the durable
    # local write (which happens first and stays): report-ready still succeeds
    # and .vergil/pr-workflow.json still holds the ready state.
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        mock_transport.return_value.ref = "refs/vergil/pr-workflow/feature/42-x"
        mock_transport.return_value.write.side_effect = subprocess.CalledProcessError(
            1, ("git", "push")
        )
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    captured = capsys.readouterr()
    # Local write is durable: the command still succeeds and stdout is unchanged.
    assert rc == 0
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    # The push failure surfaced loudly on stderr.
    assert "warning" in captured.err.lower()
    assert "relay ref" in captured.err.lower()
    # And the local state file persists with the ready metadata.
    state_path = in_git_repo / ".vergil" / "pr-workflow.json"
    assert state_path.is_file()
    persisted = json.loads(state_path.read_text())
    assert persisted["status"] == "ready"
    assert persisted["pr_metadata"]["title"] == "t"


def test_report_ready_rejects_epic(in_git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses an epic linkage at
    # the point the value is entered, instead of only failing later at submit.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value="epic"),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "72",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "--issue (#72) is an epic, not a task" in capsys.readouterr().err


def test_report_ready_rejects_archive(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2842: a standing epic's per-quarter archive is no more a task than the
    # epic itself; report-ready refuses it with the shared refusal text.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.non_task_kind", return_value="archive"),
    ):
        rc = vrg_pr_workflow.main(_REPORT_READY_ARGS)
    assert rc == 1
    assert "--issue (#120) is an archive, not a task" in capsys.readouterr().err
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()


def test_report_ready_rejects_operational_task(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses an operational task —
    # it is not PR-workable (its result is a comment, not a PR).
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_operational_task", return_value=True),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "120",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "operational task" in capsys.readouterr().err.lower()


_REPORT_READY_ARGS = [
    "--base",
    "develop",
    "report-ready",
    "--issue",
    "120",
    "--title",
    "t",
    "--summary",
    "s",
    "--notes",
    "n",
]


@contextmanager
def _queue_guard(
    return_value: str | None = None, side_effect: Exception | None = None
) -> Generator[MagicMock]:
    """Pass the earlier guards and stub ``epics.queue_item_kind`` as given."""
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_operational_task", return_value=False),
        patch(
            "vergil_tooling.bin.vrg_pr_workflow.epics.queue_item_kind",
            return_value=return_value,
            side_effect=side_effect,
        ) as mock_queue,
    ):
        yield mock_queue


def test_report_ready_refuses_intake_item(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An item parented by the intake epic is unreviewed — not buildable (#2485).
    with _queue_guard(return_value="intake") as mock_queue:
        rc = vrg_pr_workflow.main(_REPORT_READY_ARGS)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--issue (#120) is in the intake queue" in err
    assert "Route it with intake-review first." in err
    mock_queue.assert_called_once_with("#120", default_repo="org/repo")
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()


def test_report_ready_refuses_planning_item(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _queue_guard(return_value="planning"):
        rc = vrg_pr_workflow.main(_REPORT_READY_ARGS)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--issue (#120) is in the planning queue" in err
    assert "Expand it with epic-create first." in err


def test_report_ready_allows_non_queue_item(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _queue_guard(return_value=None):
        rc = vrg_pr_workflow.main(_REPORT_READY_ARGS)
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    assert "queue" not in captured.err


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, ["gh"]), OSError("gh not found")])
def test_report_ready_defers_when_queue_check_cannot_run(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    # Best-effort, like the operational guard — but never silent: it says it
    # deferred to vrg-submit-pr's authoritative check.
    with _queue_guard(side_effect=exc):
        rc = vrg_pr_workflow.main(_REPORT_READY_ARGS)
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    assert "could not check whether --issue (#120) is in the intake or planning queue" in (
        captured.err
    )
    assert "vrg-submit-pr" in captured.err


def test_report_ready_bare_number_epic_is_caught(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2213 regression: a bare --issue number must be resolved to '#N' before the
    # epic guard. Patching the deeper epics.non_task_kind (not non_task_ref_kind)
    # exercises the real parse_issue_ref path — which rejects a bare number, so on
    # the old code the guard silently no-ops and this would pass through (rc 0).
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.non_task_kind", return_value="epic") as mock_kind,
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "120",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert "epic" in err.lower()
    assert "#120" in err  # the canonical ref, not a doubled '##120'
    # The guard actually reached the epic-ness check with a parseable ref.
    assert mock_kind.call_args.args[0].number == 120


def test_report_ready_bare_number_operational_is_caught(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2213 regression, operational variant: a bare --issue number must resolve
    # before the operational guard. epics.is_operational is patched so real
    # parse_issue_ref runs; the epic check (non_task_kind) is left to return None.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.non_task_kind", return_value=None),
        patch("vergil_tooling.lib.epics.is_operational", return_value=True),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "120",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert "operational task" in err.lower()
    assert "#120" in err


def test_report_ready_rejects_malformed_issue(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A --issue that is neither a number nor a cross-repo ref fails loudly at
    # report time (WorkflowError), rather than being stored and deferred (#2213).
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "not-a-ref",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    assert rc == 1
    assert "must be a number" in capsys.readouterr().err.lower()


def test_report_ready_rejects_cross_repo_issue(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses a cross-repo --issue.
    # A PR can only close an issue in its own repo, and because issue numbers are
    # not unique across repos, a cross-repo close is a genuine mis-close hazard.
    # The check runs first, so no epic/operational gh call hits the foreign repo.
    with patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "org/.github#127",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "different repo" in capsys.readouterr().err.lower()


def test_report_ready_defers_cross_repo_when_repo_unresolvable(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If the current repo cannot be resolved (offline), the cross-repo guard
    # defers rather than blocking; vrg-submit-pr's check still catches it later.
    with patch(
        "vergil_tooling.bin.vrg_pr_workflow.github.current_repo",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "org/.github#127",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_defers_cross_repo_when_repo_empty(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty current repo (nothing to compare against) defers rather than
    # blocking; a bare same-repo issue then proceeds normally.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value=""),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_allows_explicit_same_repo_ref(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An explicit owner/repo#N matching the current repo passes the cross-repo
    # guard (same repo) and proceeds.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_operational_task", return_value=False),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "org/repo#42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_allows_non_epic_task(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The guard resolves the repo and finds a task (not an epic): proceeds.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_defers_when_epicness_unresolvable(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If GitHub is unreachable, the guard defers (does not block report-ready);
    # vrg-submit-pr's authoritative check still catches a real epic later.
    with patch(
        "vergil_tooling.bin.vrg_pr_workflow.github.current_repo",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_rerun_overwrites(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t1",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t2",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["pr_metadata"]["title"] == "t2"


def test_report_ready_rejects_stale_issue(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "99",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    assert rc == 1  # stale workflow file guard


def _report_ready(issue: str = "42", title: str = "t") -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            issue,
            "--title",
            title,
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )


def test_unfreeze_reopens_after_report_ready(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # After report-ready the worktree is frozen (status ready); unfreeze drops it
    # back to implementing so commits are allowed again, retaining the metadata.
    _report_ready()
    capsys.readouterr()
    rc = vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "implementing"}

    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "implementing"
    assert state["pr_metadata"]["title"] == "t"


def test_unfreeze_with_no_file_errors(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    assert rc == 1
    assert "no workflow file" in capsys.readouterr().err.lower()


def test_report_ready_after_unfreeze_refreezes(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The full deliberate-unfreeze round trip: ready -> unfreeze -> report-ready
    # refreshes the metadata and re-freezes (status ready).
    _report_ready(title="t1")
    vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    _report_ready(title="t2")
    capsys.readouterr()
    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "ready"
    assert state["pr_metadata"]["title"] == "t2"


def test_status_with_no_file(in_git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = vrg_pr_workflow.main(["--base", "develop", "status"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}


def test_report_ready_rejects_invalid_linkage_keyword(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
            "--linkage",
            "Refs #42",
        ]
    )
    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_report_ready_strips_issue_number_from_linkage_and_warns(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
            "--linkage",
            "Ref #99",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "warning" in out


def test_report_ready_rejects_linkage_keyword_in_notes(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.non_task_ref_kind", return_value=None),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "Ref #157",
            ]
        )
    assert rc == 1
    assert "Ref #157" in capsys.readouterr().err


# --- report-ready publishes the branch itself (#3602) ---------------------------


def _run_report_ready(title: str = "t") -> int:
    return vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            title,
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )


def _install_pre_receive(origin: Path, script: str) -> None:
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\n{script}\n")
    hook.chmod(0o755)


def test_report_ready_pushes_branch_not_yet_on_origin(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _rev(in_git_repo, "HEAD")
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == ""

    rc = _run_report_ready()

    captured = capsys.readouterr()
    assert rc == 0
    # The push's chatter stays off stdout, which remains a pure JSON document.
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    assert "pushed feature/42-x to origin" in captured.err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == head
    # The upstream is set, as `vrg-git push -u` would have.
    assert _rev(in_git_repo, "feature/42-x@{upstream}") == head


def test_report_ready_rerun_with_branch_already_on_origin_is_a_noop_push(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Correcting the prose of a frozen branch already on origin at the reported
    # commit still works: the push is a no-op and the metadata is overwritten.
    head = _rev(in_git_repo, "HEAD")
    assert _run_report_ready(title="t1") == 0
    capsys.readouterr()

    assert _run_report_ready(title="t2") == 0

    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == head
    state = json.loads((in_git_repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["pr_metadata"]["title"] == "t2"
    assert state["git"]["head_sha"] == head


def test_report_ready_workflow_push_refusal_is_expected_and_still_records(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub refuses an agent identity's push of a .github/workflows/** change.
    # That is expected: report-ready says so, records the state, and writes the
    # relay ref; the human's vrg-submit-pr pushes the branch at submit time.
    _install_pre_receive(
        _origin(in_git_repo),
        'echo "refusing to allow a GitHub App to create or update workflow '
        "'.github/workflows/ci.yml' without 'workflows' permission\" >&2\nexit 1",
    )
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        rc = _run_report_ready()

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    assert "EXPECTED" in captured.err
    assert "vrg-submit-pr" in captured.err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == ""
    state = json.loads((in_git_repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "ready"
    mock_transport.return_value.write.assert_called_once()


def test_report_ready_push_failure_records_nothing(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Any other push failure fails loudly: no local state (so no freeze) and no
    # relay ref for a branch that is not on origin.
    _install_pre_receive(_origin(in_git_repo), "echo 'rejected by policy' >&2\nexit 1")
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        rc = _run_report_ready()

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "could not push feature/42-x to origin" in captured.err
    assert "nothing was recorded" in captured.err
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()
    mock_transport.return_value.write.assert_not_called()


def test_report_ready_refuses_drifted_rerun_without_pushing(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A re-run on a frozen branch whose HEAD moved past the reported commit must
    # not publish (or re-record) the drift.
    reported = _rev(in_git_repo, "HEAD")
    assert _run_report_ready(title="t1") == 0
    (in_git_repo / "more.py").write_text("y = 2\n")
    _git(in_git_repo, "add", "more.py")
    _git(in_git_repo, "commit", "-m", "straggler")
    capsys.readouterr()

    rc = _run_report_ready(title="t2")

    assert rc == 1
    assert "DRIFT DETECTED" in capsys.readouterr().err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == reported
    state = json.loads((in_git_repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["git"]["head_sha"] == reported
    assert state["pr_metadata"]["title"] == "t1"


def test_report_ready_after_unfreeze_pushes_new_commits(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The deliberate unfreeze lifts the drift refusal: the new HEAD is pushed.
    assert _run_report_ready(title="t1") == 0
    assert vrg_pr_workflow.main(["--base", "develop", "unfreeze"]) == 0
    (in_git_repo / "more.py").write_text("y = 2\n")
    _git(in_git_repo, "add", "more.py")
    _git(in_git_repo, "commit", "-m", "more work")
    capsys.readouterr()

    assert _run_report_ready(title="t2") == 0
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == _rev(in_git_repo, "HEAD")


# --- a rebase after unfreeze is pushed with a lease on the recorded head (#3783) --


def _rewrite_head(repo: Path, message: str = "feature work, rebased") -> str:
    """Rewrite the branch tip (a stand-in for a rebase) and return the new HEAD."""
    _git(repo, "commit", "--amend", "-m", message)
    return _rev(repo, "HEAD")


def _move_origin_branch(repo: Path) -> str:
    """Point origin's feature branch at an unrelated commit, as another pusher would."""
    _git(repo, "commit", "--allow-empty", "-m", "someone else's push")
    foreign = _rev(repo, "HEAD")
    _git(repo, "push", "--force", "origin", "HEAD:refs/heads/feature/42-x")
    _git(repo, "reset", "--hard", "HEAD~1")
    return foreign


def test_report_ready_after_unfreeze_force_pushes_rebased_branch(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The unfreeze -> rebase -> re-report recovery: origin is still at the
    # recorded head, so the rewritten branch is pushed over it under a lease.
    reported = _rev(in_git_repo, "HEAD")
    assert _run_report_ready(title="t1") == 0
    assert vrg_pr_workflow.main(["--base", "develop", "unfreeze"]) == 0
    rebased = _rewrite_head(in_git_repo)
    assert rebased != reported
    capsys.readouterr()

    rc = _run_report_ready(title="t2")

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    assert "pushed feature/42-x to origin" in captured.err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == rebased
    state = json.loads((in_git_repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "ready"
    assert state["git"]["head_sha"] == rebased
    assert state["pr_metadata"]["title"] == "t2"


def test_report_ready_after_unfreeze_refuses_unexpectedly_moved_origin(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The lease is on the recorded head: if origin moved somewhere else in the
    # meantime, the rewritten branch must not clobber it, and nothing is recorded.
    reported = _rev(in_git_repo, "HEAD")
    assert _run_report_ready(title="t1") == 0
    assert vrg_pr_workflow.main(["--base", "develop", "unfreeze"]) == 0
    foreign = _move_origin_branch(in_git_repo)
    _rewrite_head(in_git_repo)
    capsys.readouterr()

    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        rc = _run_report_ready(title="t2")

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "is not at the recorded head" in captured.err
    assert reported[:12] in captured.err
    assert "nothing was recorded" in captured.err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == foreign
    state = json.loads((in_git_repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "implementing"
    assert state["git"]["head_sha"] == reported
    assert state["pr_metadata"]["title"] == "t1"
    mock_transport.return_value.write.assert_not_called()


def test_report_ready_after_unfreeze_pushes_rebase_when_branch_never_reached_origin(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The first report's push was the expected workflow-file refusal, so the
    # recorded head never reached origin. There is nothing to lease against:
    # the rebased branch is pushed as a new branch, as before.
    hook = _origin(in_git_repo) / "hooks" / "pre-receive"
    _install_pre_receive(
        _origin(in_git_repo),
        'echo "refusing to allow a GitHub App to create or update workflow '
        "'.github/workflows/ci.yml' without 'workflows' permission\" >&2\nexit 1",
    )
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport"):
        assert _run_report_ready(title="t1") == 0
    hook.unlink()
    assert vrg_pr_workflow.main(["--base", "develop", "unfreeze"]) == 0
    rebased = _rewrite_head(in_git_repo)
    capsys.readouterr()

    assert _run_report_ready(title="t2") == 0
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == rebased


def test_first_report_ready_does_not_force_over_diverged_origin(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # With no recorded head there is no lease: a first report is a plain push,
    # so a diverged origin branch is still refused and nothing is recorded.
    foreign = _move_origin_branch(in_git_repo)
    capsys.readouterr()

    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport"):
        rc = _run_report_ready()

    captured = capsys.readouterr()
    assert rc == 1
    assert "could not push feature/42-x to origin" in captured.err
    assert "nothing was recorded" in captured.err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == foreign
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()


def test_report_ready_refuses_detached_head(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(in_git_repo, "checkout", "--detach")

    assert _run_report_ready() == 1
    assert "detached" in capsys.readouterr().err
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()


def test_report_ready_refuses_state_recorded_for_another_branch(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_report_ready() == 0
    _git(in_git_repo, "checkout", "-b", "feature/42-y")
    capsys.readouterr()

    assert _run_report_ready() == 1
    assert "records branch feature/42-x" in capsys.readouterr().err
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-y") == ""


# -- local-settings guard (epic vergil-project/.github#558) -------------------


def test_report_ready_refuses_for_agent(
    in_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = local_settings_violation(tmp_path, monkeypatch)
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert _run_report_ready() == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == local_settings.REFUSAL_TEMPLATE.format(path=path)
    assert lines[1] == local_settings.AGENT_LINE
    # Refused before anything is pushed or recorded.
    assert not (in_git_repo / ".vergil" / "pr-workflow.json").exists()
    assert _rev(_origin(in_git_repo), "refs/heads/feature/42-x") == ""


def test_report_ready_warns_for_human(
    in_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = local_settings_violation(tmp_path, monkeypatch)
    assert _run_report_ready() == 0
    assert local_settings.WARNING_TEMPLATE.format(path=path) in capsys.readouterr().err.splitlines()
    assert (in_git_repo / ".vergil" / "pr-workflow.json").exists()
