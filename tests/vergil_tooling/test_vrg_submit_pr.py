"""Tests for vergil_tooling.bin.vrg_submit_pr."""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin.vrg_submit_pr import (
    HUMAN_ONLY_REMOVED,
    NO_FINALIZE_REMOVED,
    NO_INSTALL_REMOVED,
    OVERRIDE_MERGE_REVIEW_REMOVED,
    SubmitSummary,
    _create_pr,
    _finish,
    _local_worktree_with_state,
    _metadata_from_state,
    _origin_has_branch,
    _planning_close,
    _pr_label,
    _prepare,
    _print_selection,
    _push_branch,
    _recheck_batch,
    _reject_if_cross_repo,
    _reject_if_non_task,
    _reject_if_operational_task,
    _reject_if_queue_item,
    _require_local_head,
    _require_origin_head,
    _require_ready,
    _require_relay_agrees,
    _require_reported_head,
    _resolve_ready_state,
    _run_gates,
    _run_submit_batch,
    _select_batch_worktrees,
    _Submission,
    _submit_all,
    _target_branch,
    _task_linkage,
    _verify_origin_tip,
    main,
    parse_args,
)
from vergil_tooling.bin.vrg_submit_pr import (
    _chain_finalize as _real_chain_finalize,
)
from vergil_tooling.bin.vrg_submit_pr import (
    _published_pr as _real_published_pr,
)
from vergil_tooling.lib import (
    agent_gates,
    epics,
    github,
    identity_mode,
    pr_path,
    pr_provenance,
    progress,
    retry,
    trust,
    worktrees,
)
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.linkage import extract_tracking_refs
from vergil_tooling.lib.planning_close import PlanningClose
from vergil_tooling.lib.pr_body import build_pr_body
from vergil_tooling.lib.pr_workflow import batch_rules, engine, freeze
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.release.gate import unreleased_refusal as _real_unreleased_refusal
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.worktrees import Worktree

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vergil_tooling.lib.pr_workflow.state import WorkflowState

_MOD = "vergil_tooling.bin.vrg_submit_pr"


@pytest.fixture(autouse=True)
def _released_repo() -> Iterator[None]:
    """Default every test to a repo that declares [publish].release = true.

    The refusal tests override it — the innermost patch wins.
    """
    with patch(_MOD + ".gate.unreleased_refusal", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_epic_link_check() -> Iterator[None]:
    """Neutralize the epic-link guard and the task-linkage lookup for submit-flow
    tests (both would hit a real gh call). ``_task_linkage`` defaults to the
    requested linkage with no ``Closes`` upgrade. The dedicated tests for these
    functions call the imported functions directly, so these module-attr patches
    don't shadow them.
    """
    with (
        patch(_MOD + "._reject_if_cross_repo"),
        patch(_MOD + "._reject_if_non_task"),
        patch(_MOD + "._reject_if_operational_task"),
        patch(_MOD + "._reject_if_queue_item"),
        patch(_MOD + "._task_linkage", side_effect=lambda _ref, requested: requested),
        patch(_MOD + "._planning_close", return_value=None),
    ):
        yield


def _write_workflow_state(
    root: Path,
    *,
    issue: str = "42",
    branch: str = "feature/x",
    base: str = "develop",
    title: str = "fix: bug",
    summary: str = "Fix the bug",
    notes: str = "Verified locally",
    linkage: str = "Ref",
) -> None:
    """Write a ready-to-submit ``.vergil/pr-workflow.json`` state file.

    This is the sole metadata source vrg-submit-pr reads in template mode
    (issue #1700).
    """
    from vergil_tooling.lib.pr_workflow import engine
    from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

    now = "2026-06-08T00:00:00Z"
    state = engine.init_state(
        issues=[issue],
        branch=branch,
        base=base,
        head_sha="h0",
        base_sha="b0",
        now=now,
    )
    engine.apply_report_ready(
        state,
        title=title,
        summary=summary,
        notes=notes,
        linkage=linkage,
        head_sha="h0",
        now=now,
    )
    LocalFileTransport(root, base=base).write(state)


def _state_submitted(root: Path) -> bool:
    """True if the worktree's state file is marked submitted (in-flight)."""
    from vergil_tooling.lib.pr_workflow.state import WorkflowState

    path = root / ".vergil" / "pr-workflow.json"
    return path.is_file() and WorkflowState.from_json(path.read_text()).submitted is not None


@pytest.fixture(autouse=True)
def _heads_match() -> Iterator[None]:
    """Neutralize the recorded-head checks (issue #3815) for submit-flow tests.

    They read real git state (``rev-parse``, ``ls-remote``) and the relay ref.
    The dedicated tests below call the imported functions directly, or re-patch
    the module attribute with the real function to exercise a refusal end to
    end; the module-attr patches here don't shadow the imported names.
    """
    with (
        patch(_MOD + "._require_local_head"),
        patch(_MOD + "._require_origin_head"),
        patch(_MOD + "._require_relay_agrees"),
        patch(_MOD + "._require_reported_head"),
    ):
        yield


def _armed(*, merged: bool = False) -> pr_path.ArmResult:
    """What :func:`pr_path.arm` returns for a squash PR it armed (or merged directly)."""
    outcome: pr_path.ArmOutcome = "merged" if merged else "armed"
    return pr_path.ArmResult(outcome=outcome, pr_url="", head="", method="SQUASH")


@pytest.fixture(autouse=True)
def _auto_merge_on() -> Iterator[None]:
    """Default every submit to a clean provenance check and the one PR path arming it.

    Both would hit GitHub. The CLI mode pins the PR to the local HEAD, so
    ``git.commit_sha`` is neutralized too. Tests of the per-PR loop re-patch
    the innermost attribute to drive a violation or a refusal.
    """
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=pr_provenance.ProvenanceResult()),
        patch(_MOD + ".pr_path.arm", return_value=_armed()),
        patch(_MOD + ".git.commit_sha", return_value="c0ffee"),
    ):
        yield


_REAL_RUN_GATES = _run_gates


@pytest.fixture(autouse=True)
def _gates_pass() -> Iterator[MagicMock]:
    """Default every submit's gate step to a pass, without reading git or GitHub.

    The gate tests (epic vergil-project/.github#1384, Task 9) re-patch the
    module attribute with the real function and stub what it reads.
    """
    with patch(_MOD + "._run_gates") as gates:
        yield gates


@pytest.fixture(autouse=True)
def _finalize_ok() -> Iterator[MagicMock]:
    """Default every submit's finalize run to a full success, without running it.

    Submit finalizes by default, so every submit-flow test would otherwise
    start a real ``vrg-finalize-pr``. Tests of the chain itself re-patch the
    module attribute with the real function and patch ``subprocess.run``.
    """
    with patch(_MOD + "._chain_finalize", return_value=0) as chain:
        yield chain


@pytest.fixture(autouse=True)
def _no_published_pr() -> Iterator[None]:
    """Default every selection to a branch GitHub has no PR for (issue #4993).

    The lookup reads GitHub. Its own tests call the imported function, or
    re-patch the module attribute, to drive an open or merged PR.
    """
    with patch(_MOD + "._published_pr", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _in_worktree() -> Iterator[None]:
    """Default every test to running inside a worktree (legacy behavior).

    Root-launch tests override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=False):
        yield


_REAL_RUN_LOG = progress.RunLog


@pytest.fixture(autouse=True)
def _run_log_in_tmp(tmp_path: Path) -> Iterator[Path]:
    """Write every submit rollup's run log under *tmp_path*, never the real repo.

    ``_submit_all`` opens ``.vergil/vrg-submit-pr-<stamp>.log`` in the main
    worktree, which tests patch to paths like ``/repo``. Yields the root the
    logs land in.
    """
    with patch(
        "vergil_tooling.lib.progress.RunLog",
        side_effect=lambda command, _root, **kw: _REAL_RUN_LOG(command, tmp_path, **kw),
    ):
        yield tmp_path


def _submit_log(root: Path) -> str:
    """The text of the one submit run log written under *root*."""
    [log] = (root / ".vergil").glob("vrg-submit-pr-*.log")
    return log.read_text()


# The renderer is fixed to plain so the rollup's lines are the same under any CI.
_ARGS = parse_args(["--output-format", "plain"])


def test_target_branch_explicit_develop_accepted() -> None:
    """An explicit --base develop is accepted, with or without the origin/ prefix."""
    assert _target_branch("develop", oracle_base="origin/develop") == "develop"
    assert _target_branch("origin/develop", oracle_base=None) == "develop"


def test_target_branch_honors_oracle_base_origin_stripped() -> None:
    """The oracle-recorded develop base is honored, with any origin/ prefix stripped."""
    assert _target_branch(None, oracle_base="origin/develop") == "develop"
    assert _target_branch(None, oracle_base="develop") == "develop"


@pytest.mark.parametrize(
    ("base_override", "oracle_base"),
    [
        ("main", None),
        ("main", "origin/develop"),
        (None, "origin/main"),
        ("develop", "origin/main"),
        ("release/1.2.0", None),
    ],
)
def test_base_main_refused(base_override: str | None, oracle_base: str | None) -> None:
    """A --base or a recorded base naming any branch but develop is refused (spec §6.2)."""
    with pytest.raises(SystemExit) as excinfo:
        _target_branch(base_override, oracle_base=oracle_base)
    assert excinfo.value.code == (
        "vrg-submit-pr: PRs are opened against develop only; releases go through vrg-release"
    )


def test_target_branch_defaults_to_develop() -> None:
    """With neither override nor oracle base, the default is develop."""
    assert _target_branch(None, oracle_base=None) == "develop"
    assert _target_branch(None) == "develop"


def test_parse_args_cli_fields() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.issue == "42"
    assert args.summary == "Fix bug"
    assert args.title == "fix: bug"
    assert args.linkage == "Ref"
    assert args.dry_run is False


def test_parse_args_all_options() -> None:
    args = parse_args(
        [
            "--issue",
            "owner/repo#10",
            "--summary",
            "Add feature",
            "--linkage",
            "Ref",
            "--notes",
            "Tested",
            "--title",
            "My PR",
            "--dry-run",
        ]
    )
    assert args.linkage == "Ref"
    assert args.dry_run is True


def test_parse_args_base_flag() -> None:
    args = parse_args(
        ["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--base", "develop"]
    )
    assert args.base == "develop"


def test_parse_args_base_defaults_to_none() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.base is None


def test_base_flag_overrides_auto_detected_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A release/ branch normally targets main, but --base should override."""
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
            return_value="release/post-2.1.3",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Back-merge",
                "--title",
                "chore: back-merge",
                "--base",
                "develop",
                "--dry-run",
            ]
        )
    assert result == 0
    output = capsys.readouterr().out
    assert "develop" in output
    assert "main" not in output


def test_base_main_refused_before_any_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--base main exits 1 with nothing pushed or opened."""
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
        patch("vergil_tooling.bin.vrg_submit_pr.github.create_pr") as mock_create_pr,
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--base", "main"])
    assert "PRs are opened against develop only" in str(excinfo.value.code)
    mock_git_run.assert_not_called()
    mock_create_pr.assert_not_called()


def test_base_flag_develop_used_in_pr_creation(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ) as mock_create_pr,
    ):
        main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--base", "develop"])
    mock_create_pr.assert_called_once()
    assert mock_create_pr.call_args.kwargs["base"] == "develop"


def test_cli_dry_run_prints_the_table_not_the_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI dry run lists the PR in the selection table; the body is not printed."""
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "pull_request_template.md").write_text(
        "> **Do not create PRs manually.**\n> Use `vrg-submit-pr`.\n"
    )
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0
    output = capsys.readouterr().out
    assert "Submitting 1 PR to develop:" in output
    assert "Ref #42" in output
    assert "fix: bug" in output
    assert "## Summary" not in output
    assert "[dry-run] would finalize every opened PR in one run" in output


def test_cli_body_has_no_testing_section() -> None:
    body = build_pr_body(summary="Fix bug", links=[("Ref", "#42")], notes="", planning=None)
    assert "## Summary" in body
    assert "## Issue Linkage" in body
    assert "## Notes" in body
    assert "## Testing" not in body


def test_main_dry_run(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0


def test_main_dry_run_with_title(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Fix bug",
                "--title",
                "Custom Title",
                "--dry-run",
            ]
        )
    assert result == 0


def test_main_dry_run_release_branch(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
            return_value="release/1.0.0",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Release 1.0.0",
                "--title",
                "release: 1.0.0",
                "--dry-run",
            ]
        )
    assert result == 0


def test_main_submits_pr(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ) as mock_create_pr,
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert result == 0
    mock_git_run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
    mock_create_pr.assert_called_once()


def test_push_branch_uses_force_with_lease() -> None:
    """Issue #1557: submission follows a rebase, so the push must tolerate a
    diverged remote via --force-with-lease (never bare --force)."""
    with patch("vergil_tooling.bin.vrg_submit_pr.git.run") as run:
        _push_branch("feature/x")
    run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
    args = run.call_args[0]
    assert "--force" not in args  # the bare, unsafe variant is never used


def test_push_branch_raises_clear_error_on_rejection() -> None:
    """A refused force-with-lease (remote moved since fetch) must surface a
    clear, actionable message — not a raw CalledProcessError traceback."""
    err = subprocess.CalledProcessError(1, ["git", "push"])
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.run", side_effect=err),
        patch(_MOD + "._origin_has_branch", return_value=True),
        pytest.raises(SystemExit, match="force-with-lease was refused"),
    ):
        _push_branch("feature/x")


_PUSH_REFUSED = subprocess.CalledProcessError(1, ["git", "push"])


def test_push_to_a_gone_branch_whose_pr_merged_says_so() -> None:
    """Origin deleted the branch after its PR merged: the refused lease is
    explained as that, never as "the remote moved" (issue #4993)."""
    prs = [
        _open_pr(number=5, url="https://x/pull/5", state="CLOSED"),
        _open_pr(number=7, url="https://x/pull/7", state="MERGED"),
    ]
    with (
        patch(_MOD + ".git.run", side_effect=_PUSH_REFUSED),
        patch(_MOD + "._origin_has_branch", return_value=False),
        patch(_MOD + ".worktrees.branch_prs", return_value=prs) as lookup,
        pytest.raises(SystemExit) as exc,
    ):
        _push_branch("feature/x")
    msg = str(exc.value)
    assert "the branch is gone from origin and its PR #7 has merged (https://x/pull/7)" in msg
    assert "vrg-finalize-pr --cleanup-only" in msg
    assert "remote branch moved" not in msg
    lookup.assert_called_once_with("feature/x", state="all")


def test_push_to_a_gone_branch_with_no_merged_pr_keeps_the_lease_message() -> None:
    with (
        patch(_MOD + ".git.run", side_effect=_PUSH_REFUSED),
        patch(_MOD + "._origin_has_branch", return_value=False),
        patch(_MOD + ".worktrees.branch_prs", return_value=[_open_pr(state="CLOSED")]),
        pytest.raises(SystemExit, match="force-with-lease was refused"),
    ):
        _push_branch("feature/x")


def test_push_failure_whose_pr_check_fails_names_both() -> None:
    """A failed read while explaining the push is said, never hidden."""
    with (
        patch(_MOD + ".git.run", side_effect=_PUSH_REFUSED),
        patch(_MOD + "._origin_has_branch", return_value=False),
        patch(_MOD + ".worktrees.branch_prs", side_effect=trust.TrustReadError("prs", "boom")),
        pytest.raises(SystemExit) as exc,
    ):
        _push_branch("feature/x")
    assert str(exc.value) == (
        "vrg-submit-pr: pushing 'feature/x' to origin failed, and checking "
        "whether its PR has merged failed too: "
        "cannot read who wrote the content (prs): boom"
    )


class TestPublishedPR:
    """GitHub, not the local stamp, says whether a ready branch has a PR (#4993)."""

    def test_open_pr_is_in_flight_whatever_its_head(self) -> None:
        prs = [_open_pr(number=3, head_oid="other"), _open_pr(number=9, state="MERGED")]
        with patch(_MOD + ".worktrees.branch_prs", return_value=prs) as lookup:
            assert _real_published_pr("feature/x", "h0") == prs[0]
        lookup.assert_called_once_with("feature/x", state="all")

    def test_merged_pr_at_the_reported_head_is_published(self) -> None:
        prs = [_open_pr(number=3, state="MERGED"), _open_pr(number=4, state="CLOSED")]
        with patch(_MOD + ".worktrees.branch_prs", return_value=prs):
            assert _real_published_pr("feature/x", "h0") == prs[0]

    def test_merged_pr_at_another_head_is_a_reused_name(self) -> None:
        """A merged PR at another head is a reused branch name (#1719): still to submit."""
        prs = [_open_pr(state="MERGED", head_oid="old")]
        with patch(_MOD + ".worktrees.branch_prs", return_value=prs):
            assert _real_published_pr("feature/x", "h0") is None

    def test_closed_pr_at_the_reported_head_is_published(self) -> None:
        """A PR closed without merging at the reported head is cleanup (#5038)."""
        prs = [
            _open_pr(number=3, state="CLOSED", head_oid="old"),
            _open_pr(number=4, state="CLOSED"),
        ]
        with patch(_MOD + ".worktrees.branch_prs", return_value=prs):
            assert _real_published_pr("feature/x", "h0") == prs[1]

    def test_closed_pr_at_another_head_is_a_reused_name(self) -> None:
        prs = [_open_pr(state="CLOSED", head_oid="old")]
        with patch(_MOD + ".worktrees.branch_prs", return_value=prs):
            assert _real_published_pr("feature/x", "h0") is None

    def test_no_pr_is_none(self) -> None:
        with patch(_MOD + ".worktrees.branch_prs", return_value=[]):
            assert _real_published_pr("feature/x", "h0") is None

    def test_a_failed_read_refuses_the_selection(self) -> None:
        with (
            patch(_MOD + ".worktrees.branch_prs", side_effect=trust.TrustReadError("prs", "boom")),
            pytest.raises(SystemExit) as exc,
        ):
            _real_published_pr("feature/x", "h0")
        assert str(exc.value) == (
            "vrg-submit-pr: reading the PRs on 'feature/x' failed: "
            "cannot read who wrote the content (prs): boom; "
            "nothing was submitted."
        )


def test_main_submits_pr_with_notes(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Fix bug",
                "--title",
                "fix: bug",
                "--notes",
                "Tested on macOS",
            ]
        )
    assert result == 0


def test_main_dry_run_omits_pr_watch_oneliner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0
    assert "pr-watch" not in capsys.readouterr().out


class TestIdentityGate:
    """Agent identities are blocked from PR submission."""

    def test_user_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        assert "human maintainer" in capsys.readouterr().err.lower()

    def test_audit_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0

    def test_refusal_points_to_report_ready(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result == 1
        assert "vrg-pr-workflow report-ready" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    @pytest.mark.parametrize("mode", ["user", "audit"])
    def test_agent_help_shows_full_usage(
        self,
        flag: str,
        mode: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An agent's --help prints the full usage, not a refusal (issue #4178).

        The identity gate runs after parsing, so an agent can read the options
        and give the human a correct command line; the help itself says the
        command is human-only and names the agent's handoff.
        """
        monkeypatch.setenv("VRG_IDENTITY_MODE", mode)
        monkeypatch.setenv("COLUMNS", "500")
        with pytest.raises(SystemExit) as exc:
            main(["--issue", "42", flag])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "usage:" in out
        assert "--release" in out
        assert "human-only" in out
        assert "vrg-pr-workflow report-ready" in out

    def test_human_help_shows_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        with patch(_MOD + ".identity_mode.is_agent", return_value=False), pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "usage:" in out
        assert "--release" in out

    def test_human_mode_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--dry-run"])
        assert result == 0


class TestTemplateMode:
    """Template mode reads .vergil/pr-workflow.json when no CLI args given."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_already_submitted_worktree_does_not_resubmit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Running submit-pr inside a worktree whose PR is already submitted must
        not push a duplicate PR; it reports the existing PR and exits cleanly."""
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=AlreadySubmittedError(
                    pr_url="https://github.com/o/r/pull/312", pr_number=312
                ),
            ),
            patch(_MOD + ".github.create_pr") as create_pr,
        ):
            result = main([])
        assert result == 0
        create_pr.assert_not_called()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "already submitted" in combined
        assert "312" in combined

    def test_in_worktree_asks_github_for_the_branch_pr(self, tmp_path: Path) -> None:
        """Run inside the worktree, submit asks GitHub as the root picker does (#5037)."""
        _write_workflow_state(tmp_path)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._published_pr", return_value=None) as pub,
        ):
            assert main(["--dry-run"]) == 0
        pub.assert_called_once_with("feature/x", "h0")

    def test_in_worktree_with_an_open_pr_does_not_resubmit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A PR opened by hand leaves the state ready; it is in flight (#5037)."""
        _write_workflow_state(tmp_path)
        pr = _open_pr(number=313, url="https://x/pull/313")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._published_pr", return_value=pr),
            patch(_MOD + ".git.run") as run,
            patch(_MOD + ".github.create_pr") as create_pr,
        ):
            assert main([]) == 0
        run.assert_not_called()
        create_pr.assert_not_called()
        out = capsys.readouterr().out
        assert (
            "vrg-submit-pr: this worktree's branch already has an open PR #313 "
            "(https://x/pull/313).\n"
        ) in out
        assert "Run vrg-finalize-pr on it" in out

    def test_in_worktree_with_a_merged_pr_is_refused_as_cleanup(self, tmp_path: Path) -> None:
        """A PR merged at the reported head is cleanup, never pushed again (#5037)."""
        _write_workflow_state(tmp_path)
        pr = _open_pr(number=314, url="https://x/pull/314", state="MERGED")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._published_pr", return_value=pr),
            patch(_MOD + ".git.run") as run,
            patch(_MOD + ".github.create_pr") as create_pr,
            pytest.raises(SystemExit) as exc,
        ):
            main([])
        run.assert_not_called()
        create_pr.assert_not_called()
        assert str(exc.value) == (
            "vrg-submit-pr: this worktree's PR #314 has merged at the reported "
            "head (https://x/pull/314); nothing was submitted.\n"
            "  This worktree is cleanup, not a submit: run "
            "`vrg-finalize-pr --cleanup-only` to remove it."
        )

    def test_in_worktree_with_a_closed_pr_is_refused_naming_it(self, tmp_path: Path) -> None:
        """A PR closed without merging at the reported head is never reopened
        as a new one: the run names it and the two ways on (#5038)."""
        _write_workflow_state(tmp_path)
        pr = _open_pr(number=315, url="https://x/pull/315", state="CLOSED")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._published_pr", return_value=pr),
            patch(_MOD + ".git.run") as run,
            patch(_MOD + ".github.create_pr") as create_pr,
            pytest.raises(SystemExit) as exc,
        ):
            main([])
        run.assert_not_called()
        create_pr.assert_not_called()
        assert str(exc.value) == (
            "vrg-submit-pr: this worktree's PR #315 was closed without merging at "
            "the reported head (https://x/pull/315); nothing was submitted.\n"
            "  To go on: reopen PR #315 on GitHub to go on with it, or run "
            "`vrg-finalize-pr --cleanup-only` to remove the worktree."
        )

    def test_template_dry_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_workflow_state(tmp_path)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "fix: bug" in out
        assert "#42" in out

    def test_state_file_dry_run_previews_metadata(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Template mode reads pr_metadata from the workflow state file."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        now = "2026-06-08T00:00:00Z"
        state = engine.init_state(
            issues=["1534"],
            branch="feature/1534-x",
            base="develop",
            head_sha="h0",
            base_sha="b0",
            now=now,
        )
        engine.apply_report_ready(
            state,
            title="feat: oracle",
            summary="did it",
            notes="n",
            linkage="Ref",
            head_sha="h0",
            now=now,
        )
        LocalFileTransport(tmp_path, base="develop").write(state)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/1534-x",
            ),
        ):
            result = main(["--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "feat: oracle" in out
        assert "#1534" in out

    def test_oracle_base_overrides_release_branch_inference(self, tmp_path: Path) -> None:
        """Regression (#1609): a release/* branch whose oracle base is develop
        must target develop, not main. The legacy branch-name inference must
        not override the base the oracle recorded."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        now = "2026-06-08T00:00:00Z"
        state = engine.init_state(
            issues=["1606"],
            branch="release/post-2.1.27",
            base="origin/develop",
            head_sha="h0",
            base_sha="b0",
            now=now,
        )
        engine.apply_report_ready(
            state,
            title="chore(release): back-merge",
            summary="back-merge",
            notes="n",
            linkage="Ref",
            head_sha="h0",
            now=now,
        )
        LocalFileTransport(tmp_path, base="origin/develop").write(state)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="release/post-2.1.27",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
        ):
            result = main([])
        assert result == 0
        assert mock_pr.call_args.kwargs["base"] == "develop"

    def test_template_creates_pr_on_confirm(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
        ):
            result = main([])
        assert result == 0
        mock_pr.assert_called_once()
        # The state file is retained and marked submitted (in-flight tracking),
        # not deleted as the legacy template was.
        assert _state_submitted(tmp_path)

    def test_template_submits_without_prompting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Submit never reads stdin and never prints the PR body (vergil-tooling#4744)."""
        _write_workflow_state(tmp_path, summary="SUMMARY-TEXT")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
            patch("builtins.input", side_effect=AssertionError("stdin read")) as mock_input,
        ):
            result = main([])
        assert result == 0
        mock_pr.assert_called_once()
        mock_input.assert_not_called()
        out = capsys.readouterr().out
        assert "Submitting 1 PR to develop:" in out
        assert "SUMMARY-TEXT" not in out
        assert "Body Preview" not in out

    @pytest.mark.parametrize("flag", ["--yes", "-y"])
    def test_yes_flag_is_refused_by_name(
        self, flag: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--yes was removed with the confirmation; it is refused before any side effect."""
        with patch(_MOD + "._run_template_mode") as run:
            assert main([flag]) == 1
        run.assert_not_called()
        assert "--yes was removed" in capsys.readouterr().err

    def test_version_line_heads_every_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The installed vergil-tooling version is the run's first line, ahead of
        even a refusal, so a run's output ties it to a release (issue #4994)."""
        with (
            patch(_MOD + ".progress.metadata.version", return_value="2.1.400"),
            patch(_MOD + "._run_template_mode") as run,
        ):
            assert main(["--yes"]) == 1
        run.assert_not_called()
        assert capsys.readouterr().out.splitlines()[0] == "vrg-submit-pr: vergil-tooling 2.1.400"

    def test_template_rejects_forbidden_linkage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix", linkage="Fixes")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
        ):
            result = main([])
        assert result == 1
        err = capsys.readouterr().err
        assert "linkage" in err.lower()
        assert "Ref" in err

    def test_template_belt_and_suspenders_linkage_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Even if the fields carry a bad linkage, template mode rejects it."""
        fields = {
            "issue": "42",
            "issues": "42",
            "title": "fix",
            "summary": "Fix",
            "linkage": "Fixes",
        }
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        ):
            result = main([])
        assert result == 1
        err = capsys.readouterr().err
        assert "linkage" in err.lower()
        assert "Ref" in err

    def test_template_strips_issue_number_from_linkage_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A linkage carrying the issue number is stripped to the bare keyword,
        warned about, and the PR proceeds — no hard fail (issue #1765)."""
        _write_workflow_state(tmp_path, title="fix", summary="Fix", linkage="Ref #1761")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/8",
            ),
        ):
            result = main([])
        assert result == 0
        captured = capsys.readouterr()
        assert "Ref #1761" in captured.err
        # The selection table carries the bare keyword with the auto-appended number.
        assert "Ref #42" in captured.out

    def test_template_mode_reports_unready_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A state file with no PR metadata yet (the USER agent has not run
        `report-ready`) surfaces the WorkflowError cleanly and exits non-zero,
        rather than crashing."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        state = engine.init_state(
            issues=["42"],
            branch="feature/x",
            base="develop",
            head_sha="h0",
            base_sha="b0",
            now="2026-06-08T00:00:00Z",
        )
        LocalFileTransport(tmp_path, base="develop").write(state)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
        ):
            result = main([])
        assert result == 1
        assert "cannot read PR submission fields" in capsys.readouterr().err

    def test_missing_issue_arg_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        err = capsys.readouterr().err
        assert "--issue" in err

    def test_template_ensures_pushed(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
        ):
            main([])
        push_calls = [c for c in mock_git_run.call_args_list if c.args and c.args[0] == "push"]
        assert push_calls, "expected vrg-submit-pr to push the branch"
        mock_pr.assert_called_once()

    def test_no_metadata_and_no_args_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path):
            result = main([])
        assert result != 0
        assert "pr-workflow.json" in capsys.readouterr().err

    def test_partial_cli_args_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--issue", "42"])
        assert result != 0
        assert "required" in capsys.readouterr().err.lower()

    def test_template_base_other_than_develop_refused(self, tmp_path: Path) -> None:
        """A --base naming another branch is refused even on a dry run."""
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            pytest.raises(SystemExit, match="PRs are opened against develop only"),
        ):
            main(["--base", "main", "--dry-run"])

    def test_template_recorded_base_other_than_develop_refused(self, tmp_path: Path) -> None:
        """A ready-state that recorded another base is refused, nothing pushed."""
        _write_workflow_state(tmp_path, base="main", title="fix", summary="Fix")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + "._create_pr") as create,
            pytest.raises(SystemExit, match="PRs are opened against develop only"),
        ):
            main([])
        push.assert_not_called()
        create.assert_not_called()


# -- root-launch worktree resolution (issue #1423) ----------------------------


class TestRootLaunch:
    """From the repo root, submit-pr resolves the target worktree itself."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    @staticmethod
    def _wt(name: str, branch: str) -> worktrees.Worktree:
        return worktrees.Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=branch)

    def test_root_single_ready_worktree_chdirs_and_submits(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        fields = {
            "issue": "7",
            "issues": "7",
            "title": "Foo title",
            "summary": "S",
            "head_sha": "h7",
        }
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),  # pytest stdin is not a TTY
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
            patch(_MOD + ".os.chdir") as chdir,
            patch(_MOD + ".git.current_branch", return_value="feature/7-foo"),
            patch(_MOD + "._published_pr", return_value=None) as pub,
        ):
            rc = main(["--dry-run"])
        assert rc == 0
        chdir.assert_called_once_with(wt.path)
        # The picker asked GitHub; the chosen worktree is not asked again (#5037).
        pub.assert_called_once_with("feature/7-foo", "h7")

    def test_root_picker_holds_back_a_closed_pr_and_names_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The root picker skips a worktree whose PR was closed without
        merging at the reported head, naming it on stderr (#5038)."""
        ready = self._wt("issue-7-foo", "feature/7-foo")
        closed = self._wt("issue-8-bar", "feature/8-bar")
        fields = {"issue": "7", "issues": "7", "title": "T", "summary": "S", "head_sha": "h7"}
        prs = {
            "feature/7-foo": None,
            "feature/8-bar": _open_pr(number=81, url="https://x/pull/81", state="CLOSED"),
        }
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[ready, closed]),
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
            patch(_MOD + ".os.chdir") as chdir,
            patch(_MOD + ".git.current_branch", return_value="feature/7-foo"),
            patch(_MOD + "._published_pr", side_effect=lambda branch, _head: prs[branch]),
        ):
            assert main(["--dry-run"]) == 0
        chdir.assert_called_once_with(ready.path)
        assert (
            "vrg-submit-pr: skipping issue-8-bar: PR #81 was closed without merging "
            "(https://x/pull/81); reopen PR #81 on GitHub to go on with it, or run "
            "`vrg-finalize-pr --cleanup-only` to remove the worktree.\n"
        ) in capsys.readouterr().err

    def test_root_no_ready_worktrees_errors_with_reasons(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),  # pytest stdin is not a TTY
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".submission.read_pr_fields", side_effect=FileNotFoundError("x")),
            pytest.raises(SystemExit, match="no submittable worktrees"),
        ):
            main(["--dry-run"])

    def test_root_non_tty_fails_fast(self) -> None:
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(
                _MOD + ".worktrees.require_tty",
                side_effect=SystemExit("requires an interactive terminal"),
            ),
            patch(_MOD + ".worktrees.list_worktrees") as listing,
            pytest.raises(SystemExit, match="interactive terminal"),
        ):
            main([])
        listing.assert_not_called()

    def test_root_multiple_ready_worktrees_prompts(self) -> None:
        wts = [self._wt("issue-7-foo", "feature/7-foo"), self._wt("issue-8-bar", "feature/8-bar")]
        fields = {
            "issue": "8",
            "issues": "8",
            "title": "Bar title",
            "summary": "S",
            "head_sha": "h8",
        }
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=wts),
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
            patch(
                "vergil_tooling.lib.worktrees.prompt_choice",
                return_value="issue-8-bar — issue 8: Bar title",
            ),
            patch(_MOD + ".os.chdir") as chdir,
            patch(_MOD + ".git.current_branch", return_value="feature/8-bar"),
        ):
            rc = main(["--dry-run"])
        assert rc == 0
        chdir.assert_called_once_with(wts[1].path)

    def test_root_unready_workflow_skipped_with_reason(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=WorkflowError("the workflow has no PR metadata yet"),
            ),
            pytest.raises(SystemExit, match="no PR metadata yet"),
        ):
            main(["--dry-run"])

    def test_root_separates_in_flight_from_not_ready(self) -> None:
        submitted = self._wt("issue-141-spike", "feature/141-spike")
        not_ready = self._wt("issue-211-bootstrap", "feature/211-bootstrap")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(
                _MOD + ".worktrees.list_worktrees",
                return_value=[submitted, not_ready],
            ),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=[
                    AlreadySubmittedError(pr_url="https://github.com/o/r/pull/312", pr_number=312),
                    FileNotFoundError("x"),
                ],
            ),
            pytest.raises(SystemExit) as exc,
        ):
            main(["--dry-run"])
        msg = str(exc.value)
        assert "In flight" in msg
        assert "issue-141-spike" in msg
        assert "PR #312" in msg
        assert "https://github.com/o/r/pull/312" in msg
        assert "Not ready" in msg
        assert "issue-211-bootstrap" in msg

    def test_root_no_worktrees_at_all_errors(self) -> None:
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            pytest.raises(SystemExit, match="no .worktrees/ entries exist"),
        ):
            main(["--dry-run"])

    def test_all_skips_a_ready_worktree_whose_pr_merged_or_is_open(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A PR opened by hand leaves the state ready; --all asks GitHub and holds
        back a merged one as cleanup and an open one as in flight (#4993)."""
        ready = self._wt("issue-1319-a", "feature/1319-a")
        merged = self._wt("issue-1329-b", "feature/1329-b")
        opened = self._wt("issue-1340-c", "feature/1340-c")
        prs = {
            "feature/1319-a": None,
            "feature/1329-b": _open_pr(number=1330, url="https://x/pull/1330", state="MERGED"),
            "feature/1340-c": _open_pr(number=1341, url="https://x/pull/1341"),
        }
        with (
            patch(_MOD + ".worktrees.list_worktrees", return_value=[ready, merged, opened]),
            patch(_MOD + ".submission.read_pr_fields", return_value={"head_sha": "h0"}),
            patch(_MOD + "._published_pr", side_effect=lambda branch, _head: prs[branch]) as pub,
        ):
            selected = _select_batch_worktrees(Path("/repo"), parse_args(["--all"]))
        assert selected == [ready]
        assert [c.args for c in pub.call_args_list] == [
            ("feature/1319-a", "h0"),
            ("feature/1329-b", "h0"),
            ("feature/1340-c", "h0"),
        ]
        err = capsys.readouterr().err
        assert (
            "vrg-submit-pr: skipping issue-1329-b: PR #1330 has merged "
            "(https://x/pull/1330); the worktree is cleanup, not a submit.\n"
        ) in err
        assert (
            "vrg-submit-pr: skipping issue-1340-c: PR #1341 is already open "
            "(https://x/pull/1341).\n"
        ) in err

    def test_only_merged_worktrees_refuse_naming_the_cleanup(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        merged = self._wt("issue-1329-b", "feature/1329-b")
        pr = _open_pr(number=1330, url="https://x/pull/1330", state="MERGED")
        with (
            patch(_MOD + ".worktrees.list_worktrees", return_value=[merged]),
            patch(_MOD + ".submission.read_pr_fields", return_value={"head_sha": "h0"}),
            patch(_MOD + "._published_pr", return_value=pr),
            pytest.raises(SystemExit) as exc,
        ):
            _select_batch_worktrees(Path("/repo"), parse_args(["--all"]))
        assert str(exc.value) == (
            "vrg-submit-pr: no submittable worktrees found.\n\n"
            "  Merged (cleanup — run `vrg-finalize-pr --cleanup-only`):\n"
            "    issue-1329-b: PR #1330 (https://x/pull/1330)"
        )
        # The refusal names it; no separate skip line repeats it.
        assert capsys.readouterr().err == ""

    def test_all_skips_a_ready_worktree_whose_pr_was_closed_unmerged(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--all holds back a PR closed without merging at the reported head (#5038)."""
        ready = self._wt("issue-1319-a", "feature/1319-a")
        closed = self._wt("issue-1350-d", "feature/1350-d")
        prs = {
            "feature/1319-a": None,
            "feature/1350-d": _open_pr(number=1351, url="https://x/pull/1351", state="CLOSED"),
        }
        with (
            patch(_MOD + ".worktrees.list_worktrees", return_value=[ready, closed]),
            patch(_MOD + ".submission.read_pr_fields", return_value={"head_sha": "h0"}),
            patch(_MOD + "._published_pr", side_effect=lambda branch, _head: prs[branch]),
        ):
            selected = _select_batch_worktrees(Path("/repo"), parse_args(["--all"]))
        assert selected == [ready]
        assert (
            "vrg-submit-pr: skipping issue-1350-d: PR #1351 was closed without merging "
            "(https://x/pull/1351); reopen PR #1351 on GitHub to go on with it, or run "
            "`vrg-finalize-pr --cleanup-only` to remove the worktree.\n"
        ) in capsys.readouterr().err

    def test_select_refuses_a_worktree_whose_pr_was_closed_unmerged(self) -> None:
        """--select never submits a worktree whose PR was closed at its head (#5038)."""
        closed = self._wt("issue-1350-d", "feature/1350-d")
        pr = _open_pr(number=1351, url="https://x/pull/1351", state="CLOSED")
        with (
            patch(_MOD + ".worktrees.list_worktrees", return_value=[closed]),
            patch(_MOD + ".submission.read_pr_fields", return_value={"head_sha": "h0"}),
            patch(_MOD + "._published_pr", return_value=pr),
            pytest.raises(SystemExit) as exc,
        ):
            _select_batch_worktrees(Path("/repo"), parse_args(["--select", "1350"]))
        assert str(exc.value) == (
            "vrg-submit-pr: no submittable worktrees found.\n\n"
            "  Closed without merging (reopen the PR on GitHub, or run "
            "`vrg-finalize-pr --cleanup-only`):\n"
            "    issue-1350-d: PR #1351 (https://x/pull/1351)"
        )


_UNRELEASED_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]

[publish]
release = false
"""


class TestUnreleasedRepoRefusal:
    """--release on a repo that is not released is refused up front,
    before any push, PR or merge (issue #4175)."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"],
            ["--all", "--release"],
            ["feature/a", "--release"],
            ["feature/a", "--release", "--dry-run"],
        ],
    )
    def test_cascade_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        (tmp_path / "vergil.toml").write_text(_UNRELEASED_TOML)
        with (
            patch(_MOD + ".gate.unreleased_refusal", side_effect=_real_unreleased_refusal),
            patch("vergil_tooling.lib.release.gate.git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".git.run") as m_git_run,
            patch(_MOD + "._push_branch") as m_push,
            patch(_MOD + ".github.create_pr") as m_create,
            patch(_MOD + "._run_relay") as m_relay,
            patch(_MOD + "._run_submit_batch") as m_batch,
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            assert main(argv) == 1
        for m in (m_git_run, m_push, m_create, m_relay, m_batch, mock_run):
            m.assert_not_called()
        err = capsys.readouterr().err
        assert "vrg-submit-pr --release: this repo is not released" in err

    def test_submit_without_cascade_ignores_unreleased_repo(self, tmp_path: Path) -> None:
        """A plain submit, finalized by default, never consults the release gate."""
        with (
            patch(_MOD + ".gate.unreleased_refusal") as m_gate,
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result == 0
        m_gate.assert_not_called()


# -- finalize always (epic .github#1218, spec §6.2; --no-finalize removed, .github#1440) --


_CLI = ["--issue", "42", "--summary", "Fix", "--title", "fix: bug"]
_URL1 = "https://x/pull/1"
_URL2 = "https://x/pull/2"


def _cli_main(argv: list[str], *, finalize_rc: int = 0) -> tuple[int, MagicMock]:
    """Run the CLI mode with *argv*, the real chain and a finalize exiting *finalize_rc*."""
    with (
        patch(_MOD + "._chain_finalize", _real_chain_finalize),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.create_pr", return_value=_URL1),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + ".subprocess.run") as run,
    ):
        run.return_value.returncode = finalize_rc
        rc = main([*_CLI, *argv])
    return rc, run


def _refuse_first() -> list[object]:
    """Auto-merge refused on the first PR, turned on for the second."""
    return [pr_path.PRPathError("refused"), _armed()]


class TestFinalizeByDefault:
    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_FINALIZE_DEADLINE", raising=False)

    def test_finalize_by_default_one_run_over_opened(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One vrg-finalize-pr over every opened PR, from the main worktree."""
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        rc, calls = _run_batch([a, b], ["--release"], urls=[_URL1, _URL2])
        assert rc == 0
        assert calls == [("vrg-finalize-pr", f"{_URL1},{_URL2}", "--release")]
        out = capsys.readouterr().out
        assert "Done: 2 PR(s) submitted, finalized and released." in out
        assert "pr-watch" not in out

    def test_finalize_runs_from_the_main_worktree(self) -> None:
        rc, run = _cli_main([])
        assert rc == 0
        run.assert_called_once()
        assert run.call_args.args[0] == ["vrg-finalize-pr", _URL1]
        assert run.call_args.kwargs["cwd"] == Path("/repo")

    @pytest.mark.parametrize(
        ("argv", "flags"),
        [
            (["--release"], ["--release"]),
            (["--output-format", "plain"], ["--output-format", "plain"]),
            (["--output-window", "0"], ["--output-window", "0"]),
            (
                ["--release", "--output-format", "gha", "--output-window", "5"],
                ["--release", "--output-format", "gha", "--output-window", "5"],
            ),
        ],
    )
    def test_flags_passed_to_finalize(self, argv: list[str], flags: list[str]) -> None:
        rc, run = _cli_main(argv)
        assert rc == 0
        assert run.call_args.args[0] == ["vrg-finalize-pr", _URL1, *flags]

    def test_all_submits_failed_skips_finalize(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With nothing opened, finalize never runs (so never prompts): exit 3."""
        with patch(
            _MOD + ".pr_path.arm",
            side_effect=pr_path.PRPathError("refused"),
        ):
            rc, calls = _run_batch([_batch_wt("issue-1-a")], ["--release"], urls=[_URL1])
        assert rc == 3
        assert calls == []
        err = capsys.readouterr().err
        assert f"{_URL1} — opened at h1; turning auto-merge on failed: refused" in err
        assert "Nothing was finalized: 1 PR(s) opened but a later step failed" in err
        assert "No PR opened" not in err

    def test_release_skipped_after_failed_submit(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A failed submit drops --release from finalize and says so; exit at least 3."""
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            rc, calls = _run_batch([a, b], ["--release"], urls=[_URL1, _URL2])
        assert rc == 3
        assert calls == [("vrg-finalize-pr", _URL2)]
        err = capsys.readouterr().err
        assert "Release skipped: 1 PR(s) failed to submit." in err
        assert f"{_URL1} — opened at h1; turning auto-merge on failed: refused" in err

    def test_failed_submits_printed_after_finalize_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")

        def finalize(_cmd: tuple[str, ...]) -> int:
            print("FINALIZE REPORT", file=sys.stderr)
            return 0

        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            _run_batch([a, b], [], urls=[_URL1, _URL2], run=finalize)
        err = capsys.readouterr().err
        assert err.index("FINALIZE REPORT") < err.index("Failed to submit:")

    def test_exit_precedence_tool_error_wins(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Finalize's 1 plus a failed submit is 1, with the rerun command named."""
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            rc, _calls = _run_batch([a, b], [], urls=[_URL1, _URL2], run=lambda _c: 1)
        assert rc == 1
        err = capsys.readouterr().err
        assert "vrg-finalize-pr exited 1" in err
        assert f"Re-run the finalize alone with: vrg-finalize-pr {_URL2}" in err

    @pytest.mark.parametrize("code", [2, 130])
    def test_exit_undefined_finalize_code_passes_through(self, code: int) -> None:
        """A code finalize does not define as an outcome is a tool error and wins too."""
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            rc, _calls = _run_batch([a, b], [], urls=[_URL1, _URL2], run=lambda _c: code)
        assert rc == code

    def test_exit_mixed_outcomes_validate_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A failed submit, a CI failure and a failed validate: 4, the submit named, no release.

        Finalize owns the CI failure and the validate: its 4 ("develop did
        not validate") takes precedence over its own 3 for the CI failure, so
        submit sees 4, which wins over the failed submit's 3.
        """
        a, b, c = _batch_wt("issue-1-a"), _batch_wt("issue-2-b"), _batch_wt("issue-3-c")
        enable = [
            pr_path.PRPathError("refused"),
            _armed(),
            _armed(),
        ]
        url3 = "https://x/pull/3"
        with patch(_MOD + ".pr_path.arm", side_effect=enable):
            rc, calls = _run_batch(
                [a, b, c], ["--release"], urls=[_URL1, _URL2, url3], run=lambda _c: 4
            )
        assert rc == 4
        assert calls == [("vrg-finalize-pr", f"{_URL2},{url3}")]
        assert not any("vrg-release" in cmd for cmd in calls)
        err = capsys.readouterr().err
        assert "Failed to submit:" in err
        assert f"{_URL1} — opened at h1; turning auto-merge on failed: refused" in err
        assert "Release skipped: 1 PR(s) failed to submit." in err

    @pytest.mark.parametrize(("finalize_rc", "expected"), [(0, 3), (3, 3)])
    def test_exit_failed_submit_at_least_3(self, finalize_rc: int, expected: int) -> None:
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            rc, _calls = _run_batch([a, b], [], urls=[_URL1, _URL2], run=lambda _c: finalize_rc)
        assert rc == expected

    @pytest.mark.parametrize("finalize_rc", [4, 5])
    def test_exit_finalize_4_wins_over_3(self, finalize_rc: int) -> None:
        a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
        with patch(_MOD + ".pr_path.arm", side_effect=_refuse_first()):
            rc, _calls = _run_batch([a, b], [], urls=[_URL1, _URL2], run=lambda _c: finalize_rc)
        assert rc == finalize_rc

    @pytest.mark.parametrize("finalize_rc", [0, 3, 4, 5])
    def test_exit_is_finalize_code_when_every_submit_succeeded(self, finalize_rc: int) -> None:
        rc, _run = _cli_main([], finalize_rc=finalize_rc)
        assert rc == finalize_rc

    def test_deadline_checked_before_any_pr_opens(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed $VRG_FINALIZE_DEADLINE exits 1 before the first push."""
        monkeypatch.setenv("VRG_FINALIZE_DEADLINE", "soon")
        with (
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".github.create_pr") as create,
            patch(_MOD + "._chain_finalize") as chain,
        ):
            assert main(_CLI) == 1
        for mock in (push, git_run, create, chain):
            mock.assert_not_called()
        err = capsys.readouterr().err
        assert "VRG_FINALIZE_DEADLINE must be a positive whole number" in err
        assert "no PR was opened" in err

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--finalize"], "--finalize was removed; vrg-submit-pr always finalizes"),
            (["--no-finalize"], NO_FINALIZE_REMOVED.removeprefix("vrg-submit-pr: ")),
            (["--release", "--no-finalize"], "--no-finalize was removed"),
            (["--install"], "--install was removed; install is part of every release"),
            (["--release", "--install"], "--install was removed"),
            (["--no-install"], NO_INSTALL_REMOVED.removeprefix("vrg-submit-pr: ")),
            (["--release", "--no-install"], "--no-install was removed"),
            (
                ["--allow-provenance-violation"],
                "--allow-provenance-violation was removed; a provenance violation always "
                "fails. Fix its cause, or fix the classifier",
            ),
            (["--release", "--allow-provenance-violation"], "--allow-provenance-violation was"),
            (
                ["--override-merge-review", "no session"],
                OVERRIDE_MERGE_REVIEW_REMOVED.removeprefix("vrg-submit-pr: "),
            ),
            (["--release", "--override-merge-review", "x"], "--override-merge-review was"),
            (["--human-only", "my own change"], HUMAN_ONLY_REMOVED.removeprefix("vrg-submit-pr: ")),
            (["--release", "--human-only", "x"], "--human-only was removed"),
        ],
    )
    def test_removed_and_invalid_flag_combinations_fatal(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        """Refused before any side effect, in every mode."""
        for mode in (_CLI, [], ["--all"], ["feature/a"]):
            with (
                patch(_MOD + ".git.repo_root") as repo_root,
                patch(_MOD + ".git.run") as git_run,
                patch(_MOD + ".github.create_pr") as create,
                patch(_MOD + ".gate.unreleased_refusal") as gate_check,
                patch(_MOD + "._chain_finalize") as chain,
            ):
                assert main([*mode, *argv]) == 1
            for mock in (repo_root, git_run, create, gate_check, chain):
                mock.assert_not_called()
            assert f"vrg-submit-pr: {message}" in capsys.readouterr().err

    def test_removed_flags_hidden_from_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        for flag in ("--finalize", "--no-finalize", "--install", "--no-install"):
            assert flag not in out
        assert "--allow-provenance-violation" not in out
        assert "--override-merge-review" not in out
        assert "--human-only" not in out

    def _assert_removed(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        """*argv* exits 1 with *message* before any side effect."""
        with (
            patch(_MOD + ".git.repo_root") as repo_root,
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".github.create_pr") as create,
            patch(_MOD + "._chain_finalize") as chain,
            patch(_MOD + ".subprocess.run") as run,
        ):
            assert main(argv) == 1
        for mock in (repo_root, git_run, push, create, chain, run):
            mock.assert_not_called()
        assert capsys.readouterr().err == f"{message}\n"

    def test_no_finalize_removed_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--no-finalize is refused: submit returns once each PR has merged or cannot merge."""
        self._assert_removed(
            capsys,
            [*_CLI, "--no-finalize"],
            "vrg-submit-pr: --no-finalize was removed; "
            "vrg-submit-pr returns once each PR has merged or cannot merge",
        )

    def test_no_install_removed_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--no-install is refused, with or without --release: install is part of every release."""
        for argv in ([*_CLI, "--no-install"], [*_CLI, "--release", "--no-install"]):
            self._assert_removed(
                capsys,
                argv,
                "vrg-submit-pr: --no-install was removed; install is part of every release",
            )

    def test_dry_run_plans_without_side_effects(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A dry run previews every PR and names the finalize run, opening nothing."""
        with patch(_MOD + "._submit_all") as submit_all:
            rc, calls = _run_batch(
                [_batch_wt("issue-1-a"), _batch_wt("issue-2-b")],
                ["--dry-run", "--release"],
                urls=[],
            )
        assert rc == 0
        assert calls == []
        submit_all.assert_not_called()
        assert (
            "[dry-run] would finalize every opened PR in one run: "
            "vrg-finalize-pr <pr>,… --release\n"
        ) in capsys.readouterr().out

    @pytest.mark.parametrize("count", [1, 3])
    def test_selection_table_and_no_prompt(
        self, count: int, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One table lists the selection, one row per PR; nothing is asked."""
        wts = [_batch_wt(f"issue-{n}-x") for n in range(1, count + 1)]
        urls = [f"https://x/pull/{n}" for n in range(1, count + 1)]
        with (
            patch(_MOD + ".submission.read_pr_fields", side_effect=_batch_fields),
            patch(_MOD + "._push_branch"),
            patch(_MOD + "._create_pr", side_effect=urls),
            patch(_MOD + ".submission.record_submission"),
            patch("builtins.input") as typed,
        ):
            assert _run_submit_batch(wts, parse_args(["--all"])) == 0
        typed.assert_not_called()
        out = capsys.readouterr().out
        noun = "PR" if count == 1 else "PRs"
        assert f"Submitting {count} {noun} to develop:" in out
        table = out[out.index("Submitting") :]
        for wt in wts:
            assert wt.branch in table

    def test_template_mode_finalizes_the_opened_pr(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + "._chain_finalize", _real_chain_finalize),
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/7"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            assert main([]) == 0
        assert run.call_args.args[0] == ["vrg-finalize-pr", "https://github.com/pr/7"]
        assert _state_submitted(tmp_path)

    def test_relay_finalizes_every_opened_pr_in_one_run(self) -> None:
        states = {
            "feature/a": _ready_state(issue="1", branch="feature/a", head_sha="sha_a"),
            "feature/b": _ready_state(issue="2", branch="feature/b", head_sha="sha_b"),
        }

        def fake_read_output(*args: str) -> str:
            branch = args[-1].removeprefix("refs/heads/")
            return f"sha_{branch[-1]}\t{args[-1]}"

        def fake_transport(branch: str) -> MagicMock:
            t = MagicMock()
            t.read.return_value = states[branch]
            return t

        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", side_effect=fake_transport),
            patch(_MOD + ".git.read_output", side_effect=fake_read_output),
            patch(_MOD + ".github.create_pr", side_effect=[_URL1, _URL2]),
            patch(_MOD + "._chain_finalize", return_value=0) as chain,
        ):
            assert main(["feature/a", "feature/b", "--release"]) == 0
        chain.assert_called_once_with(["vrg-finalize-pr", f"{_URL1},{_URL2}", "--release"])

    def test_submit_failure_raised_never_reaches_finalize(self) -> None:
        """An unexpected error propagates loudly, with finalize never run."""
        with (
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run", side_effect=RuntimeError("push rejected")),
            patch(_MOD + ".github.create_pr") as create,
            patch(_MOD + "._chain_finalize") as chain,
            pytest.raises(RuntimeError, match="push rejected"),
        ):
            main(_CLI)
        create.assert_not_called()
        chain.assert_not_called()

    def test_agent_identity_blocked_before_finalize(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + "._chain_finalize") as chain:
            assert main(_CLI) == 1
        chain.assert_not_called()
        assert "human maintainer" in capsys.readouterr().err.lower()


def _submission(
    branch: str = "feature/1673-x",
    *,
    issues: tuple[str, ...] = ("#1673",),
    head: str = "h0",
    push: bool = True,
    record_in: Path | None = Path("/repo/.worktrees/issue-1673-x"),
) -> _Submission:
    return _Submission(
        branch=branch,
        base="develop",
        title="T",
        pr_body="BODY",
        issue_refs=list(issues),
        links=[("Closes", ref) for ref in issues],
        expected_head=head,
        push=push,
        record_in=record_in,
    )


def test_submit_all_pushes_creates_records_and_turns_auto_merge_on() -> None:
    sub = _submission()
    with (
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + "._create_pr", return_value="https://example/pull/9") as create,
        patch(_MOD + ".submission.record_submission") as record,
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        summary = _submit_all([sub], _ARGS)
    assert summary == SubmitSummary(opened=["https://example/pull/9"], failed=[])
    push.assert_called_once_with("feature/1673-x")
    create.assert_called_once_with(
        target_branch="develop", title="T", pr_body="BODY", head="feature/1673-x"
    )
    record.assert_called_once_with(sub.record_in, pr_url="https://example/pull/9")
    enable.assert_called_once_with("https://example/pull/9", head="h0", clone=ANY)


_PR21 = "https://github.com/o/r/pull/21"


def _gh_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "pr", "create"), output="", stderr=stderr)


_TRANSIENT = _gh_error("HTTP 504: Gateway Timeout")
_EXISTS = _gh_error(
    'a pull request for branch "feature/1673-x" into branch "develop" already exists:\n' + _PR21
)


def _open_pr(**overrides: Any) -> trust.PRRef:
    pr = trust.PRRef(
        number=21,
        url=_PR21,
        title="T",
        head="feature/1673-x",
        head_oid="h0",
        base="develop",
        author_role=trust.Role.AGENTS,
        state="OPEN",
    )
    return dataclasses.replace(pr, **overrides)


def _submit_with_create(
    create_effect: list[object], open_prs: list[object]
) -> tuple[SubmitSummary, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Run _submit_all on one PR with gh pr create and the open-PR lookup scripted."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", side_effect=create_effect) as create,
        patch("vergil_tooling.lib.pr_create.open_trusted_prs", side_effect=open_prs) as lookup,
        patch("vergil_tooling.lib.pr_create.time.sleep") as sleep,
        patch(_MOD + ".submission.record_submission") as record,
        patch(_MOD + ".pr_provenance.check_pr", return_value=pr_provenance.ProvenanceResult()),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        summary = _submit_all([_submission()], _ARGS)
    return summary, create, lookup, record, enable, sleep


def test_create_that_landed_is_adopted_not_retried(capsys: pytest.CaptureFixture[str]) -> None:
    """A create GitHub carried out but answered with an error is adopted (#4696)."""
    summary, create, lookup, record, enable, sleep = _submit_with_create(
        [_TRANSIENT], [[_open_pr()]]
    )
    assert summary == SubmitSummary(opened=[_PR21], failed=[])
    create.assert_called_once()
    [spec] = lookup.call_args.args
    assert (spec.head, spec.base) == ("feature/1673-x", "develop")
    sleep.assert_not_called()
    record.assert_called_once_with(Path("/repo/.worktrees/issue-1673-x"), pr_url=_PR21)
    enable.assert_called_once_with(_PR21, head="h0", clone=ANY)
    assert f"adopting {_PR21}" in capsys.readouterr().out


def test_already_exists_on_retry_is_adopted() -> None:
    """The lookup lags the create: the retry's "already exists" is checked and adopted."""
    summary, create, lookup, _record, enable, sleep = _submit_with_create(
        [_TRANSIENT, _EXISTS], [[], [_open_pr()]]
    )
    assert summary == SubmitSummary(opened=[_PR21], failed=[])
    assert create.call_count == 2
    assert lookup.call_count == 2
    sleep.assert_called_once()
    enable.assert_called_once_with(_PR21, head="h0", clone=ANY)


@pytest.mark.parametrize(
    ("other", "reason"),
    [
        (_open_pr(head_oid="ffff"), "its head commit is 'ffff', not 'h0'"),
        (_open_pr(title="Other"), "its title is 'Other', not 'T'"),
    ],
)
def test_other_open_pr_is_refused_by_name(
    other: trust.PRRef, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An open PR submit cannot show it opened fails loudly, naming it, and is not adopted."""
    summary, create, _lookup, record, enable, sleep = _submit_with_create([_EXISTS], [[other]])
    assert summary.opened == []
    [(branch, why)] = summary.failed
    assert branch == "feature/1673-x"
    assert _PR21 in why
    assert reason in why
    assert "not adopted" in why
    create.assert_called_once()
    sleep.assert_not_called()
    record.assert_not_called()
    enable.assert_not_called()
    out = capsys.readouterr().out
    assert "✗ feature/1673-x" in out
    assert "no PR opened: " in out


def test_several_open_prs_are_refused() -> None:
    second = _open_pr(url="https://github.com/o/r/pull/22")
    summary, *_mocks, enable, _sleep = _submit_with_create([_EXISTS], [[_open_pr(), second]])
    [(_branch, why)] = summary.failed
    assert "several PRs are open" in why
    assert _PR21 in why
    assert "pull/22" in why
    enable.assert_not_called()


def test_create_that_really_failed_is_retried() -> None:
    """A transient error with no PR open is retried, and the next attempt opens it."""
    url = "https://github.com/o/r/pull/30"
    summary, create, lookup, record, enable, sleep = _submit_with_create([_TRANSIENT, url], [[]])
    assert summary == SubmitSummary(opened=[url], failed=[])
    assert create.call_count == 2
    lookup.assert_called_once()
    sleep.assert_called_once()
    record.assert_called_once_with(Path("/repo/.worktrees/issue-1673-x"), pr_url=url)
    enable.assert_called_once_with(url, head="h0", clone=ANY)


def test_non_transient_create_failure_is_not_retried() -> None:
    refusal = _gh_error("pull request create failed: GraphQL: No commits between develop and x")
    summary, create, lookup, record, enable, sleep = _submit_with_create([refusal], [[]])
    assert summary.opened == []
    [(_branch, why)] = summary.failed
    assert "No commits between" in why
    create.assert_called_once()
    lookup.assert_called_once()
    sleep.assert_not_called()
    record.assert_not_called()
    enable.assert_not_called()


def test_transient_create_failure_gives_up_after_the_retries() -> None:
    attempts = retry.MAX_RETRIES + 1
    summary, create, lookup, _record, enable, sleep = _submit_with_create(
        [_TRANSIENT] * attempts, [[]] * attempts
    )
    [(_branch, why)] = summary.failed
    assert "Gateway Timeout" in why
    assert create.call_count == attempts
    assert lookup.call_count == attempts
    assert sleep.call_count == retry.MAX_RETRIES
    enable.assert_not_called()


def test_failed_lookup_after_failed_create_names_both() -> None:
    lookup_error = _gh_error("HTTP 401: Bad credentials (lookup)")
    summary, create, _lookup, _record, enable, sleep = _submit_with_create(
        [_TRANSIENT], [lookup_error]
    )
    [(_branch, why)] = summary.failed
    assert "Gateway Timeout" in why
    assert "Bad credentials (lookup)" in why
    create.assert_called_once()
    sleep.assert_not_called()
    enable.assert_not_called()


def test_create_pr_is_run_once_without_gh_retry() -> None:
    """_create_pr asks github.create_pr for one attempt: the retry is _create_or_adopt's."""
    with patch(_MOD + ".github.create_pr", return_value=_PR21) as create:
        assert _create_pr(target_branch="develop", title="T", pr_body="B", head="b") == _PR21
    assert create.call_args.kwargs["retry"] is False
    assert create.call_args.kwargs["head"] == "b"


def test_auto_merge_squash_single() -> None:
    """A single-issue PR is armed through the one PR path, pinned to its expected head."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        _submit_all([_submission(head="abc123")], _ARGS)
    enable.assert_called_once_with("https://x/pull/1", head="abc123", clone=ANY)


def test_auto_merge_rebase_batch() -> None:
    """A batch PR is armed through the path at its head; the path works out its rebase method.

    The method is the path's, read from the PR's body (``test_pr_path``).
    """
    with (
        patch(_MOD + "._recheck_batch"),
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        _submit_all([_submission(issues=("#10", "#11"), head="def456")], _ARGS)
    enable.assert_called_once_with("https://x/pull/1", head="def456", clone=ANY)


def test_already_mergeable_pr_counts_as_opened(capsys: pytest.CaptureFixture[str]) -> None:
    """A PR the helper merged at once (it could already merge) is reported, not failed."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(
            _MOD + ".pr_path.arm",
            return_value=_armed(merged=True),
        ),
    ):
        summary = _submit_all([_submission()], _ARGS)
    assert summary == SubmitSummary(opened=["https://x/pull/1"], failed=[])
    assert "merged directly (squash)" in capsys.readouterr().out


def test_provenance_runs_before_auto_merge() -> None:
    """The provenance check runs on the opened PR before auto-merge is turned on."""
    calls: list[str] = []
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(
            _MOD + ".pr_provenance.check_pr",
            side_effect=lambda pr: (
                calls.append(f"provenance {pr}") or pr_provenance.ProvenanceResult()
            ),
        ),
        patch(
            _MOD + ".pr_path.arm",
            side_effect=lambda pr, **_kw: calls.append(f"auto-merge {pr}") or _armed(),
        ),
    ):
        _submit_all([_submission()], _ARGS)
    assert calls == ["provenance https://x/pull/1", "auto-merge https://x/pull/1"]


def _violation() -> pr_provenance.ProvenanceResult:
    return pr_provenance.ProvenanceResult(
        violations=[
            pr_provenance.Action("bot-vergil-user", pr_provenance.Role.USER, "auto_merge_enabled")
        ]
    )


def test_provenance_violation_fails_pr_others_continue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A violation fails that PR with auto-merge left off; the next PR still submits."""
    a = _submission("feature/1-a", record_in=None)
    b = _submission("feature/2-b", record_in=None)
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", side_effect=["https://x/pull/1", "https://x/pull/2"]),
        patch(
            _MOD + ".pr_provenance.check_pr",
            side_effect=[_violation(), pr_provenance.ProvenanceResult()],
        ),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        summary = _submit_all([a, b], _ARGS)
    assert summary.opened == ["https://x/pull/2"]
    assert len(summary.failed) == 1
    label, reason = summary.failed[0]
    assert label == "https://x/pull/1"
    assert "bot-vergil-user (user): auto_merge_enabled" in reason
    assert "auto-merge was left off" in reason
    enable.assert_called_once_with("https://x/pull/2", head="h0", clone=ANY)
    out = capsys.readouterr().out
    assert "✗ feature/1-a" in out
    assert "PR #1 opened at h0; the provenance check failed: provenance violations found" in out
    assert "✓ feature/2-b" in out
    assert "PR #2 auto-merge on (squash, pinned to h0)" in out


def test_provenance_check_that_cannot_run_fails_pr() -> None:
    """A provenance check that cannot run leaves auto-merge off and fails the PR."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(
            _MOD + ".pr_provenance.check_pr",
            side_effect=subprocess.CalledProcessError(1, ["gh", "api"]),
        ),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        summary = _submit_all([_submission()], _ARGS)
    assert summary.opened == []
    assert summary.failed[0][0] == "https://x/pull/1"
    enable.assert_not_called()


def test_provenance_without_trusted_authors_fails_pr_others_continue() -> None:
    """A provenance check with no trusted-authors configuration fails that PR,
    auto-merge left off and the reason named; the next PR is still attempted."""
    a = _submission("feature/1-a", record_in=None)
    b = _submission("feature/2-b", record_in=None)
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", side_effect=["https://x/pull/1", "https://x/pull/2"]),
        patch(
            _MOD + ".pr_provenance.check_pr",
            side_effect=[
                TrustedAuthorsNotConfiguredError("no trusted authors are configured"),
                pr_provenance.ProvenanceResult(),
            ],
        ),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        summary = _submit_all([a, b], _ARGS)
    assert summary.opened == ["https://x/pull/2"]
    assert summary.failed == [
        (
            "https://x/pull/1",
            "opened at h0; the provenance check failed: no trusted authors are configured",
        )
    ]
    assert summary.left_open == ["https://x/pull/1"]
    enable.assert_called_once_with("https://x/pull/2", head="h0", clone=ANY)


def test_auto_merge_refusal_fails_pr_others_continue() -> None:
    """A refused auto-merge fails that PR, naming it; the next PR still submits."""
    a = _submission("feature/1-a", record_in=None)
    b = _submission("feature/2-b", record_in=None)
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", side_effect=["https://x/pull/1", "https://x/pull/2"]),
        patch(
            _MOD + ".pr_path.arm",
            side_effect=[
                pr_path.PRPathError("https://x/pull/1: enabling auto-merge failed: nope"),
                _armed(),
            ],
        ),
    ):
        summary = _submit_all([a, b], _ARGS)
    assert summary.opened == ["https://x/pull/2"]
    assert summary.failed == [
        (
            "https://x/pull/1",
            "opened at h0; turning auto-merge on failed: "
            "https://x/pull/1: enabling auto-merge failed: nope",
        )
    ]
    assert summary.left_open == ["https://x/pull/1"]


def test_open_failure_names_the_branch_others_continue() -> None:
    """A PR that fails before it opens is named by its branch; the next PR still opens."""
    a = _submission("feature/1-a", record_in=None)
    b = _submission("feature/2-b", record_in=None)
    with (
        patch(
            _MOD + "._push_branch",
            side_effect=[SystemExit("vrg-submit-pr: pushing 'feature/1-a' failed"), None],
        ),
        patch(_MOD + "._create_pr", return_value="https://x/pull/2") as create,
    ):
        summary = _submit_all([a, b], _ARGS)
    assert summary.opened == ["https://x/pull/2"]
    assert summary.failed == [("feature/1-a", "pushing 'feature/1-a' failed")]
    create.assert_called_once()
    assert summary.left_open == []


def test_failure_before_the_pr_opens_says_no_pr_opened(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A submit that fails before ``gh pr create`` says no PR was opened (#5198)."""
    with (
        patch(_MOD + "._push_branch", side_effect=SystemExit("vrg-submit-pr: push failed")),
        patch(_MOD + "._create_pr") as create,
    ):
        summary = _submit_all([_submission(record_in=None)], _ARGS)
        rc = _finish(summary, _ARGS)
    create.assert_not_called()
    assert rc == 3
    assert summary.left_open == []
    out, err = capsys.readouterr()
    assert "no PR opened: push failed" in out
    assert "No PR opened, so nothing was finalized." in err


@pytest.mark.parametrize(
    ("failing", "step"),
    [
        ("record", "recording the submission"),
        ("provenance", "the provenance check"),
        ("auto_merge", "turning auto-merge on"),
    ],
)
def test_failure_after_the_pr_opens_reports_the_pr_head_and_step(
    failing: str, step: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A step that fails after the PR opened names its URL, head and the step (#5198)."""

    def fail_if(name: str, value: object = None) -> MagicMock:
        if failing == name:
            return MagicMock(side_effect=ValueError("server error"))
        return MagicMock(return_value=value)

    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/9"),
        patch(_MOD + ".submission.record_submission", fail_if("record")),
        patch(
            _MOD + ".pr_provenance.check_pr",
            fail_if("provenance", pr_provenance.ProvenanceResult()),
        ),
        patch(
            _MOD + ".pr_path.arm",
            fail_if("auto_merge", _armed()),
        ),
    ):
        summary = _submit_all([_submission(record_in=tmp_path)], _ARGS)
        rc = _finish(summary, _ARGS)
    reason = f"opened at h0; {step} failed: server error"
    assert rc == 3
    assert summary.opened == []
    assert summary.failed == [("https://x/pull/9", reason)]
    assert summary.left_open == ["https://x/pull/9"]
    out, err = capsys.readouterr()
    assert f"PR #9 {reason}" in out
    assert f"https://x/pull/9 — {reason}" in err
    assert "Nothing was finalized: 1 PR(s) opened but a later step failed" in err
    assert "No PR opened" not in err


def test_unexpected_error_after_the_pr_opens_names_the_pr() -> None:
    """An error outside the per-PR set after the PR opened still names it (#5198)."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/9"),
        patch(_MOD + ".pr_provenance.check_pr", side_effect=KeyError("boom")),
        patch(_MOD + ".pr_path.arm") as enable,
        pytest.raises(KeyError) as raised,
    ):
        _submit_all([_submission(record_in=None)], _ARGS)
    enable.assert_not_called()
    assert raised.value.__notes__ == [
        "https://x/pull/9 was opened at h0; the provenance check failed, "
        "so its auto-merge is not on."
    ]


def test_submission_recorded_when_auto_merge_fails() -> None:
    """An opened PR is recorded even when auto-merge then fails, so no second PR opens."""
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission") as record,
        patch(
            _MOD + ".pr_path.arm",
            side_effect=pr_path.PRPathError("refused"),
        ),
    ):
        _submit_all([_submission()], _ARGS)
    record.assert_called_once()


def test_arm_refused_fails_the_pr_with_its_reason() -> None:
    """A PR the one PR path refuses fails its submit with the path's reason, left open."""
    refused = pr_path.ArmResult(
        outcome="refused",
        pr_url="https://x/pull/1",
        head="h0",
        method="SQUASH",
        detail="the gates refused its head h0: merge review: no record covers it",
    )
    with (
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", return_value="https://x/pull/1"),
        patch(_MOD + ".submission.record_submission"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + ".pr_path.arm", return_value=refused) as arm,
    ):
        summary = _submit_all([_submission()], _ARGS)
    arm.assert_called_once_with("https://x/pull/1", clone=Path("/repo"), head="h0")
    assert summary.opened == []
    assert summary.left_open == ["https://x/pull/1"]
    assert summary.failed == [
        (
            "https://x/pull/1",
            "opened at h0; turning auto-merge on failed: "
            "the gates refused its head h0: merge review: no record covers it",
        )
    ]


class TestSubmitRollup:
    """The submit runs as a progress rollup, one stage per PR (vergil-tooling#4709)."""

    def test_one_stage_per_pr_with_its_outcome_and_the_log(
        self, capsys: pytest.CaptureFixture[str], _run_log_in_tmp: Path
    ) -> None:
        """Each PR is a stage named after its branch, ending ✓ with what it did,
        and the summary points at the run log holding the steps' output."""
        a = _submission("feature/1-a", head="aaaabbbbccccdddd", record_in=None)
        b = _submission("feature/2-b", record_in=None)
        with (
            patch(_MOD + "._push_branch"),
            patch(_MOD + "._create_pr", side_effect=["https://x/pull/11", "https://x/pull/12"]),
            patch(
                _MOD + ".pr_path.arm",
                side_effect=[
                    _armed(),
                    _armed(merged=True),
                ],
            ),
        ):
            summary = _submit_all([a, b], _ARGS)
        assert summary == SubmitSummary(
            opened=["https://x/pull/11", "https://x/pull/12"], failed=[]
        )
        out = capsys.readouterr().out
        assert re.search(
            r"^✓ feature/1-a  \d+\.\ds — PR #11 auto-merge on \(squash, pinned to aaaabbbbcccc\)$",
            out,
            re.MULTILINE,
        )
        assert re.search(
            r"^✓ feature/2-b  \d+\.\ds — PR #12 merged directly \(squash\)$", out, re.MULTILINE
        )
        assert "✓  vrg-submit-pr complete" in out
        [log_path] = (_run_log_in_tmp / ".vergil").glob("vrg-submit-pr-*.log")
        assert f"   full log → {log_path}" in out
        log = _submit_log(_run_log_in_tmp)
        # The log's first line names the tooling version that wrote it (#4994).
        assert log.splitlines()[0] == progress.tooling_version_line("vrg-submit-pr")
        assert "=== feature/1-a: started ===" in log
        assert "PR created: https://x/pull/11" in log
        assert "https://x/pull/12: merged directly (squash); it could already merge." in log
        assert "feature/2-b: ok (PR #12 merged directly (squash)" in log

    def test_failed_pr_is_a_failed_stage_and_the_next_still_runs(
        self, capsys: pytest.CaptureFixture[str], _run_log_in_tmp: Path
    ) -> None:
        """A PR that fails ends its stage ✗ with the reason; the next PR's stage
        still runs, and the summary names no exit status (finalize decides it)."""
        a = _submission("feature/1-a", record_in=None)
        b = _submission("feature/2-b", record_in=None)
        with (
            patch(
                _MOD + "._push_branch",
                side_effect=[SystemExit("vrg-submit-pr: pushing 'feature/1-a' failed"), None],
            ),
            patch(_MOD + "._create_pr", return_value="https://x/pull/2"),
        ):
            summary = _submit_all([a, b], _ARGS)
        assert summary.failed == [("feature/1-a", "pushing 'feature/1-a' failed")]
        out = capsys.readouterr().out
        assert re.search(
            r"^✗ feature/1-a  \d+\.\ds — no PR opened: pushing 'feature/1-a' failed$",
            out,
            re.MULTILINE,
        )
        assert "✓ feature/2-b" in out
        assert "✗  failures:\n   feature/1-a — no PR opened: pushing 'feature/1-a' failed" in out
        assert "vrg-submit-pr completed with errors" in out
        assert "PipelineError: 1 stage failed (feature/1-a)\n" in out
        assert "· exit" not in out
        assert "feature/1-a: failed (no PR opened" in _submit_log(_run_log_in_tmp)

    def test_unexpected_error_stops_the_rollup_and_is_raised(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An error outside the per-PR failures is a bug, not a failed PR: the
        PRs after it are not attempted, and it is raised after the summary."""
        a = _submission("feature/1-a", record_in=None)
        b = _submission("feature/2-b", record_in=None)
        with (
            patch(_MOD + "._push_branch"),
            patch(_MOD + "._create_pr", side_effect=RuntimeError("boom")) as create,
            pytest.raises(RuntimeError, match="boom"),
        ):
            _submit_all([a, b], _ARGS)
        create.assert_called_once()
        out = capsys.readouterr().out
        assert "✗ feature/1-a" in out
        assert "RuntimeError: boom" in out
        assert "✗ feature/2-b" in out
        assert "not attempted: an earlier PR's submit stopped on an unexpected error" in out

    def test_interrupt_is_raised_after_the_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + "._push_branch", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            _submit_all([_submission("feature/1-a")], _ARGS)
        assert "✗ feature/1-a" in capsys.readouterr().out

    def test_failed_rollup_with_no_failed_pr_is_an_internal_error(self) -> None:
        """A rollup that reports a failure no PR recorded is never read as success."""
        with (
            patch(_MOD + ".progress.run_pipeline", return_value=1),
            pytest.raises(RuntimeError, match="no PR recorded as failed"),
        ):
            _submit_all([_submission()], _ARGS)

    def test_rollup_starts_after_the_table(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The selection table prints plainly first; the rollup follows it."""
        _write_workflow_state(tmp_path)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._push_branch"),
            patch(_MOD + "._create_pr", return_value="https://x/pull/5"),
        ):
            assert main(["--output-format", "plain"]) == 0
        out = capsys.readouterr().out
        assert out.index("Submitting 1 PR to develop:") < out.index("→ feature/x  starting...")
        assert "✓ feature/x" in out
        assert "PR #5 auto-merge on (squash, pinned to h0)" in out

    def test_progress_flags_parse(self) -> None:
        args = parse_args(["--output-format", "gha", "--output-window", "0"])
        assert args.output_format == "gha"
        assert args.output_window == 0

    @pytest.mark.parametrize(
        ("url", "label"),
        [
            ("https://github.com/o/r/pull/123", "PR #123"),
            ("https://github.com/o/r/pull/123/", "PR #123"),
            ("https://example/not-a-pr", "https://example/not-a-pr"),
        ],
    )
    def test_pr_label(self, url: str, label: str) -> None:
        assert _pr_label(url) == label


def test_single_and_batch_share_submit_all(tmp_path: Path) -> None:
    """Template, CLI and worktree-batch modes all open their PRs through _submit_all."""
    _write_workflow_state(tmp_path)
    summary = SubmitSummary(opened=["https://x/pull/1"], failed=[])
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + "._submit_all", return_value=summary) as submit_all,
    ):
        assert main([]) == 0
        assert main(["--issue", "42", "--summary", "S", "--title", "T"]) == 0
    assert submit_all.call_count == 2
    template_sel, cli_sel = (c.args[0] for c in submit_all.call_args_list)
    assert [s.branch for s in template_sel] == ["feature/x"]
    assert template_sel[0].record_in == tmp_path
    assert [s.expected_head for s in cli_sel] == ["c0ffee"]
    assert cli_sel[0].record_in is None

    a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
    with (
        patch(_MOD + ".submission.read_pr_fields", side_effect=_batch_fields),
        patch(_MOD + "._submit_all", return_value=summary) as submit_all,
    ):
        assert _run_submit_batch([a, b], parse_args(["--all"])) == 0
    selection, _args = submit_all.call_args.args
    assert [s.branch for s in selection] == ["feature/1-x", "feature/2-x"]
    assert [s.record_in for s in selection] == [a.path, b.path]


def _batch_wt(name: str) -> Worktree:
    num = name.split("-")[1]
    return Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=f"feature/{num}-x")


def _batch_fields(path: Path) -> dict[str, str]:
    """The ready-state fields of a ``_batch_wt`` worktree."""
    num = path.name.split("-")[1]
    return {
        "issue": num,
        "issues": num,
        "title": f"T{num}",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
        "base": "origin/develop",
        "head_sha": f"h{num}",
    }


def _run_batch(
    wts: list[Worktree],
    argv: list[str],
    *,
    urls: list[str],
    run: object = None,
) -> tuple[int, list[tuple[str, ...]]]:
    """Run ``_run_submit_batch`` over *wts* with *argv*; return its code and subprocesses."""
    calls: list[tuple[str, ...]] = []

    def fake_run(cmd: tuple[str, ...], **_kwargs: object) -> MagicMock:
        calls.append(tuple(cmd))
        code = run(tuple(cmd)) if callable(run) else 0
        return MagicMock(returncode=code)

    with (
        patch(_MOD + ".submission.read_pr_fields", side_effect=_batch_fields),
        patch(_MOD + "._push_branch"),
        patch(_MOD + "._create_pr", side_effect=urls),
        patch(_MOD + ".submission.record_submission"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._chain_finalize", _real_chain_finalize),
        patch(_MOD + ".subprocess.run", side_effect=fake_run),
    ):
        rc = _run_submit_batch(wts, parse_args(["--all", *argv]))
    return rc, calls


def test_no_rebase_at_submit() -> None:
    """Submit never rebases: no git rebase runs and each PR opens at its recorded head."""
    a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
    with (
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".git.read_output") as read_output,
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        rc, _calls = _run_batch([a, b], [], urls=["https://x/pull/1", "https://x/pull/2"])
    assert rc == 0
    for call in (*git_run.call_args_list, *read_output.call_args_list):
        assert "rebase" not in call.args
    assert [c.kwargs["head"] for c in enable.call_args_list] == ["h1", "h2"]


def test_submit_batch_failure_labels_by_branch_not_dir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A worktree whose directory name is a bare slug that hides the issue number
    # (the issue #2550 scenario). A failed submit must be attributed to the
    # full branch, not the ambiguous directory name.
    wt = Worktree(
        path=Path("/repo/.worktrees/issue-911-authz-apply-tag"),
        branch="feature/911-authz-apply-tag",
    )
    with (
        patch(_MOD + "._create_pr", side_effect=subprocess.CalledProcessError(1, "gh")),
        patch(_MOD + ".submission.read_pr_fields", side_effect=_batch_fields),
        patch(_MOD + "._push_branch"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
    ):
        rc = _run_submit_batch([wt], parse_args(["--all"]))
    assert rc == 3
    err = capsys.readouterr().err
    assert "  feature/911-authz-apply-tag — " in err


def test_all_and_select_flags_parse() -> None:
    assert parse_args(["--all"]).all_worktrees is True
    assert parse_args(["--select", "1,2"]).select == "1,2"


def _ready_pair(name: str, num: str) -> tuple[Worktree, dict[str, str]]:
    wt = Worktree(path=Path(f"/r/.worktrees/{name}"), branch=f"feature/{num}-x")
    return (wt, {"issue": num, "title": "T"})


def test_select_batch_all_returns_all_candidates() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with patch(_MOD + "._ready_worktrees", return_value=ready):
        out = _select_batch_worktrees(Path("/r"), parse_args(["--all"]))
    assert [w.path.name for w in out] == ["issue-1-a", "issue-2-b"]


def test_select_batch_select_tokens() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with patch(_MOD + "._ready_worktrees", return_value=ready):
        out = _select_batch_worktrees(Path("/r"), parse_args(["--select", "2"]))
    assert [w.path.name for w in out] == ["issue-2-b"]


def test_select_batch_bad_token_exits() -> None:
    ready = [_ready_pair("issue-1-a", "1")]
    with (
        patch(_MOD + "._ready_worktrees", return_value=ready),
        pytest.raises(SystemExit, match="--select"),
    ):
        _select_batch_worktrees(Path("/r"), parse_args(["--select", "999"]))


def test_select_batch_interactive_uses_multi_select() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with (
        patch(_MOD + "._ready_worktrees", return_value=ready),
        patch(_MOD + ".worktrees.select_worktrees", return_value=[ready[0][0]]) as sw,
    ):
        out = _select_batch_worktrees(Path("/r"), parse_args([]))
    assert out == [ready[0][0]]
    sw.assert_called_once()


def test_prepare_bad_linkage_exits() -> None:
    fields = {
        "issue": "1",
        "issues": "1",
        "title": "T",
        "summary": "s",
        "notes": "",
        "linkage": "Fixes",
    }
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#1"),
        pytest.raises(SystemExit, match="bare keyword"),
    ):
        _prepare("feature/1-x", "develop", fields, push=True, expected_head="h0", record_in=None)


def test_prepare_strips_issue_number_from_linkage_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A linkage carrying the issue number is unambiguous: strip it, warn, proceed."""
    fields = {
        "issue": "1",
        "issues": "1",
        "title": "T",
        "summary": "s",
        "notes": "",
        "linkage": "Ref #1761",
    }
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#1"),
        patch(_MOD + ".build_pr_body", return_value="BODY") as build,
    ):
        sub = _prepare(
            "feature/1-x", "develop", fields, push=True, expected_head="h0", record_in=None
        )
    assert sub.pr_body == "BODY"
    assert build.call_args.kwargs["links"] == [("Ref", "#1")]
    assert "Ref #1761" in capsys.readouterr().err


def test_submit_batch_dry_run_opens_nothing() -> None:
    """A batch dry run previews every PR and opens nothing."""
    with patch(_MOD + "._submit_all") as submit_all:
        rc, calls = _run_batch(
            [_batch_wt("issue-1-a"), _batch_wt("issue-2-b")], ["--dry-run"], urls=[]
        )
    assert rc == 0
    assert calls == []
    submit_all.assert_not_called()


def test_main_batch_routes_to_run_submit_batch() -> None:
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=False),
        patch(_MOD + ".git.is_main_worktree", return_value=True),
        patch(_MOD + ".git.repo_root", return_value="/r"),
        patch(_MOD + "._select_batch_worktrees", return_value=["wt"]),
        patch(_MOD + "._run_submit_batch", return_value=0) as run_batch,
    ):
        rc = main(["--all", "--base", "develop"])
    assert rc == 0
    run_batch.assert_called_once()


# -- _reject_if_cross_repo (a PR closes only same-repo issues) ----------------


def test_reject_if_cross_repo_raises_for_other_repo() -> None:
    # A cross-repo ref is refused: a PR here cannot Closes an issue elsewhere.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        pytest.raises(SystemExit, match="different repo"),
    ):
        _reject_if_cross_repo("org/.github#127")


def test_reject_if_cross_repo_passes_for_same_repo_bare_ref() -> None:
    # A bare #N resolves to the current repo, so it is same-repo and passes.
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        _reject_if_cross_repo("#42")


def test_reject_if_cross_repo_passes_for_explicit_same_repo_ref() -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        _reject_if_cross_repo("org/repo#42")


def test_reject_if_cross_repo_passes_for_case_insensitive_match() -> None:
    # Differently cased spellings of the current repo are not a false refusal.
    with patch(f"{_MOD}.github.current_repo", return_value="Org/Repo"):
        _reject_if_cross_repo("org/repo#42")


def test_reject_if_cross_repo_noop_when_repo_unresolvable() -> None:
    # No current repo to compare against: defer to downstream validation.
    with patch(f"{_MOD}.github.current_repo", return_value=""):
        _reject_if_cross_repo("org/repo#42")


def test_reject_if_cross_repo_noop_for_unparseable_ref() -> None:
    # A ref with no resolvable repo defers rather than erroring.
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        _reject_if_cross_repo("not-a-ref")


# -- _reject_if_non_task (PRs link tasks, not epics or archives) ------------


@pytest.mark.parametrize("kind", ["epic", "archive"])
def test_reject_if_non_task_raises_for_epic_or_archive(kind: str) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.non_task_kind", return_value=kind),
        pytest.raises(
            SystemExit,
            match=rf"^vrg-submit-pr: --issue \(org/\.github#40\) is an {kind}, not a task",
        ),
    ):
        _reject_if_non_task("org/.github#40")


def test_reject_if_non_task_passes_for_task() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.non_task_kind", return_value=None) as mock_kind,
    ):
        _reject_if_non_task("#42")
    mock_kind.assert_called_once_with(epics.IssueRef("org", "repo", 42))


# -- _reject_if_operational_task (operational tasks are not PR-workable) -------


def test_reject_if_operational_task_raises_for_operational() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.is_operational", return_value=True),
        pytest.raises(SystemExit, match="operational task"),
    ):
        _reject_if_operational_task("org/repo#7")


def test_reject_if_operational_task_passes_for_plain_task() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.is_operational", return_value=False) as mock_is_operational,
    ):
        _reject_if_operational_task("#42")
    mock_is_operational.assert_called_once_with(epics.IssueRef("org", "repo", 42))


# -- _reject_if_queue_item (intake/planning items are not buildable, #2485) ----


@pytest.mark.parametrize(
    ("kind", "phrases"),
    [
        ("intake", ("is in the intake queue", "Route it with epic-review intake first.")),
        ("planning", ("is in the planning queue", "Expand it with epic-create first.")),
    ],
)
def test_reject_if_queue_item_refuses_queue_item(kind: str, phrases: tuple[str, str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.native_parent_of", return_value=epics.IssueRef("org", ".github", 9)),
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
        patch(f"{_MOD}.epics._labels", return_value={"epic", kind}),
        pytest.raises(SystemExit) as excinfo,
    ):
        _reject_if_queue_item("#42")
    message = str(excinfo.value)
    assert message.startswith("vrg-submit-pr: --issue (#42) ")
    for phrase in phrases:
        assert phrase in message


def test_submit_refuses_ops_issue() -> None:
    # An ops issue is closed by hand, never by a PR (epic vergil-project/.github#1494).
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.native_parent_of", return_value=epics.IssueRef("org", ".github", 9)),
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
        patch(f"{_MOD}.epics._labels", return_value={"epic", "operations"}),
        pytest.raises(SystemExit) as excinfo,
    ):
        _reject_if_queue_item("#42")
    assert str(excinfo.value) == (
        "vrg-submit-pr: --issue (#42) is under the operations epic; it is an ops issue, "
        "which no PR closes: file the fix as a follow-up task in its own repo, and close "
        "the ops issue by hand once the problem is dealt with."
    )


def test_reject_if_queue_item_passes_for_routed_task() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.native_parent_of", return_value=epics.IssueRef("org", ".github", 9)
        ) as mock_parent,
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
        patch(f"{_MOD}.epics._labels", return_value={"epic", "ad-hoc"}),
    ):
        _reject_if_queue_item("#42")
    mock_parent.assert_called_once_with(epics.IssueRef("org", "repo", 42))


def test_reject_if_queue_item_lookup_failure_is_not_swallowed() -> None:
    # Authoritative check: a failed lookup aborts rather than reading as "buildable".
    err = subprocess.CalledProcessError(1, ["gh"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.native_parent_of", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _reject_if_queue_item("#42")


def test_main_cli_mode_refuses_queue_item(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + "._reject_if_queue_item", side_effect=SystemExit("queued")) as guard,
        patch(_MOD + ".github.create_pr") as create,
        pytest.raises(SystemExit, match="queued"),
    ):
        main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    guard.assert_called_once_with("#42")
    create.assert_not_called()


def test_prepare_refuses_queue_item() -> None:
    metadata = {
        "issue": "42",
        "issues": "42",
        "title": "fix: bug",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
    }
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#42"),
        patch(_MOD + "._reject_if_queue_item", side_effect=SystemExit("queued")) as guard,
        pytest.raises(SystemExit, match="queued"),
    ):
        _prepare(
            "feature/x", "develop", metadata, push=True, expected_head="abc123", record_in=None
        )
    guard.assert_called_once_with("#42")


# -- _task_linkage (managed tasks auto-close via Closes) ----------------------


def test_task_linkage_upgrades_managed_task_to_closes() -> None:
    parent = epics.IssueRef("org", ".github", 75)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.parent_of", return_value=parent),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
    ):
        assert _task_linkage("#42", "Ref") == "Closes"


def test_task_linkage_keeps_ref_for_legacy_issue() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
    ):
        assert _task_linkage("#42", "Ref") == "Ref"


def test_task_linkage_keeps_ref_when_parent_not_epic() -> None:
    parent = epics.IssueRef("org", "repo", 5)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.parent_of", return_value=parent),
        patch(f"{_MOD}.epics.is_epic", return_value=False),
    ):
        assert _task_linkage("#42", "Ref") == "Ref"


def test_task_linkage_noop_when_ref_unresolvable() -> None:
    with patch(f"{_MOD}.github.current_repo", return_value=""):
        assert _task_linkage("#42", "Ref") == "Ref"


def _linkage_through_layer(layer: trust_stubs.FakeItems, monkeypatch: pytest.MonkeyPatch) -> str:
    """:func:`_task_linkage` for org/repo#42, with no native parent, through the real layer."""
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)

    def graphql(query: str, **variables: Any) -> object:
        if query in (trust._ITEM_QUERY, trust._EVENTS_QUERY):
            return layer(query, **variables)
        assert query == epics._PARENT_WITH_LABELS_QUERY, "only the parent lookup is answered here"
        return {"node": {"parent": None}}

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", side_effect=graphql),
        # The Parent: line's epic is a head, not a continuation (parent_of reads its labels).
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}),
        patch(f"{_MOD}.epics.is_epic", return_value=True) as is_epic,
    ):
        linkage = _task_linkage("#42", "Ref")
    if linkage == "Ref":
        is_epic.assert_not_called()
    return linkage


def test_submit_linkage_reads_trusted_parent_line(monkeypatch: pytest.MonkeyPatch) -> None:
    layer = trust_stubs.FakeItems(
        {("org/repo", 42): trust_stubs.item(body="Parent: org/.github#75\n")}, strict=True
    )
    assert _linkage_through_layer(layer, monkeypatch) == "Closes"


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login", "cause", "at"),
    [
        (trust_stubs.OUTSIDER, None, "issue", "drive-by", "untrusted", trust_stubs.CREATED),
        (
            trust_stubs.HUMAN,
            trust_stubs.OUTSIDER,
            "edit",
            "drive-by",
            "untrusted",
            trust_stubs.EDITED,
        ),
        (trust_stubs.IMPOSTOR, None, "issue", "vergil-user", "wrong-type", trust_stubs.CREATED),
    ],
)
def test_submit_linkage_ignores_untrusted_parent_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    author: dict[str, str],
    editor: dict[str, str] | None,
    kind: str,
    login: str,
    cause: str,
    at: str,
) -> None:
    # A forged Parent: line in a body an outsider wrote or edited never upgrades to Closes.
    layer = trust_stubs.FakeItems(
        {
            ("org/repo", 42): trust_stubs.item(
                body=f"{trust_stubs.SPAM_TEXT}\nParent: org/.github#75\n",
                author=author,
                editor=editor,
            )
        },
        strict=True,
    )
    assert _linkage_through_layer(layer, monkeypatch) == "Ref"
    err = capsys.readouterr().err
    assert (
        err == f"skipped untrusted content: org/repo#42 {kind} {login} at {at} on body ({cause})\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


@pytest.mark.parametrize(
    "error",
    [
        trust.TrustReadError("read_issue org/repo#42", "the GraphQL call failed: HTTP 502"),
        TrustedAuthorsNotConfiguredError("trusted authors are not configured"),
        trust.UntrustedContentError(
            trust.Skipped("org/repo", 42, "issue", "drive-by", "t", "body", "untrusted"),
            "the issue body is untrusted",
        ),
    ],
)
def test_trust_failure_before_any_pr_is_one_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    # A trust-layer failure in the per-issue checks is one line, never a traceback.
    _write_workflow_state(tmp_path)
    with (
        patch(_MOD + ".git.repo_root", return_value=str(tmp_path)),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.commit_sha", return_value="abc123"),
        patch(_MOD + "._task_linkage", side_effect=error),
        patch(_MOD + ".github.create_pr") as create,
        pytest.raises(SystemExit) as exited,
    ):
        main([])
    assert exited.value.code == 1
    create.assert_not_called()
    assert capsys.readouterr().err == f"vrg-submit-pr: {error}\n"


def test_relay_untrusted_payload_opens_no_pr(capsys: pytest.CaptureFixture[str]) -> None:
    stop = trust.UntrustedContentError(
        trust.Skipped("org/repo", 0, "commit", "drive-by", "t", "the relay ref", "untrusted"),
        "the commit's author is untrusted",
    )
    transport = MagicMock()
    transport.read.side_effect = stop
    with (
        patch(_MOD + ".git.repo_root", return_value="/repo"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".GitHubTransport", return_value=transport),
        patch(_MOD + ".github.create_pr") as create,
        pytest.raises(SystemExit) as exited,
    ):
        main(["feature/a"])
    assert exited.value.code == 1
    create.assert_not_called()
    err = capsys.readouterr().err
    assert err == f"vrg-submit-pr: {stop}\n"
    assert "org/repo commit drive-by at t on the relay ref (untrusted)" in err


# -- _planning_close (a docs task's PR also closes its planning issue) --------

_DOCS = epics.IssueRef("acme", ".github", 20)
_PLAN = epics.IssueRef("acme", ".github", 7)


def test_planning_close_returns_the_planning_issue_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="acme/.github"),
        patch(
            f"{_MOD}.planning_close.find", return_value=PlanningClose(_DOCS, _PLAN, None)
        ) as find,
    ):
        assert _planning_close(["#20"]) == "acme/.github#7"
    find.assert_called_once_with(_DOCS)
    assert "the PR also closes it on merge" in capsys.readouterr().out


def test_planning_close_skipped_returns_none_and_says_why(
    capsys: pytest.CaptureFixture[str],
) -> None:
    skipped = PlanningClose(_DOCS, _PLAN, "it is already closed")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="acme/.github"),
        patch(f"{_MOD}.planning_close.find", return_value=skipped),
    ):
        assert _planning_close(["#20"]) is None
    assert "does not close it: it is already closed." in capsys.readouterr().out


def test_planning_close_ordinary_task_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="acme/.github"),
        patch(f"{_MOD}.planning_close.find", return_value=None),
    ):
        assert _planning_close(["#20"]) is None
    assert capsys.readouterr().out == ""


def test_planning_close_unresolvable_ref_is_not_looked_up() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value=""),
        patch(f"{_MOD}.planning_close.find") as find,
    ):
        assert _planning_close(["#20"]) is None
    find.assert_not_called()


def test_planning_close_never_for_a_batch() -> None:
    with patch(f"{_MOD}.planning_close.find") as find:
        assert _planning_close(["#20", "#21"]) is None
    find.assert_not_called()


def test_planning_close_failed_read_propagates() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="acme/.github"),
        patch(f"{_MOD}.planning_close.find", side_effect=subprocess.CalledProcessError(1, ["gh"])),
        pytest.raises(subprocess.CalledProcessError),
    ):
        _planning_close(["#20"])


def test_prepare_body_carries_the_planning_close(tmp_path: Path) -> None:
    with patch(f"{_MOD}._planning_close", return_value="acme/.github#7"):
        sub = _prepare(
            "feature/20-docs",
            "develop",
            {"issues": "20", "title": "docs: spec", "summary": "s", "notes": ""},
            push=False,
            expected_head="a" * 40,
            record_in=None,
        )
    assert "- Ref #20\n- Planning issue: Closes acme/.github#7\n" in sub.pr_body
    assert sub.issue_refs == ["#20"]


def test_reject_if_non_task_noop_when_repo_unresolvable() -> None:
    # A bare ref with no resolvable default repo is skipped, not an error.
    with patch(f"{_MOD}.github.current_repo", return_value=""):
        _reject_if_non_task("#42")


# -- shared _open_pr core + worktree-free relay branch path (issue #2368) ------


def _ready_state(
    *,
    issue: str = "42",
    branch: str = "feature/x",
    base: str = "develop",
    head_sha: str = "abc123def456",
    title: str = "fix: bug",
    summary: str = "Fix the bug",
    notes: str = "Verified",
    linkage: str = "Ref",
    submitted: dict[str, str] | None = None,
) -> WorkflowState:
    """Build a report-ready ``WorkflowState`` for the relay tests."""
    now = "2026-06-08T00:00:00Z"
    state = engine.init_state(
        issues=[issue], branch=branch, base=base, head_sha=head_sha, base_sha="b0", now=now
    )
    engine.apply_report_ready(
        state,
        title=title,
        summary=summary,
        notes=notes,
        linkage=linkage,
        head_sha=head_sha,
        now=now,
    )
    if submitted is not None:
        state.submitted = submitted
    return state


def _single_metadata() -> dict[str, str]:
    return {
        "issue": "42",
        "issues": "42",
        "title": "fix: bug",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
    }


def test_relay_submission_opens_without_pushing() -> None:
    """push=False opens the PR with --head and never pushes the branch."""
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#42"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/9") as create,
    ):
        sub = _prepare(
            "feature/x",
            "develop",
            _single_metadata(),
            push=False,
            expected_head="abc123",
            record_in=None,
        )
        summary = _submit_all([sub], _ARGS)
    assert summary.opened == ["https://github.com/pr/9"]
    push.assert_not_called()
    assert create.call_args.kwargs["head"] == "feature/x"
    assert create.call_args.kwargs["base"] == "develop"


def test_local_submission_pushes_before_creating() -> None:
    """push=True pushes the branch first, then opens the PR."""
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#42"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/9"),
    ):
        sub = _prepare(
            "feature/x",
            "develop",
            _single_metadata(),
            push=True,
            expected_head="abc123",
            record_in=None,
        )
        summary = _submit_all([sub], _ARGS)
    assert summary.opened == ["https://github.com/pr/9"]
    push.assert_called_once_with("feature/x")


def test_prepare_prints_nothing_and_opens_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_MOD + ".resolve_issue_ref", return_value="#42"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + ".github.create_pr") as create,
    ):
        sub = _prepare(
            "feature/x",
            "develop",
            _single_metadata(),
            push=False,
            expected_head="abc123",
            record_in=None,
        )
    assert sub.pr_body == "BODY"
    assert sub.links == [("Ref", "#42")]
    assert "BODY" not in capsys.readouterr().out
    push.assert_not_called()
    create.assert_not_called()


def test_metadata_from_state_extracts_fields() -> None:
    state = _ready_state(issue="7", title="T", summary="S", notes="N", linkage="Closes")
    meta = _metadata_from_state(state, "feature/x")
    assert meta == {
        "issue": "7",
        "issues": "7",
        "title": "T",
        "summary": "S",
        "notes": "N",
        "linkage": "Closes",
        "recorded_by": "",
    }


def test_metadata_from_state_carries_recorded_by() -> None:
    state = _ready_state()
    state.recorded_by = "user"
    assert _metadata_from_state(state, "feature/x")["recorded_by"] == "user"


def test_metadata_from_state_errors_without_pr_metadata() -> None:
    now = "2026-06-08T00:00:00Z"
    state = engine.init_state(
        issues=["7"], branch="feature/x", base="develop", head_sha="h", base_sha="b", now=now
    )
    with pytest.raises(SystemExit, match="no PR metadata yet"):
        _metadata_from_state(state, "feature/x")


def test_resolve_ready_state_prefers_local_worktree_file() -> None:
    """A branch with a local worktree resolves via the file — the ref is untouched."""
    wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
    state = _ready_state()
    local = MagicMock()
    local.read.return_value = state
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".LocalFileTransport", return_value=local) as local_cls,
        patch(_MOD + ".GitHubTransport") as gh_cls,
    ):
        out = _resolve_ready_state(Path("/repo"), "feature/x")
    assert out is state
    local_cls.assert_called_once_with(wt.path)
    gh_cls.assert_not_called()


def test_resolve_ready_state_falls_back_to_relay_ref() -> None:
    """A worktree list with no branch match → read the relay ref via GitHubTransport."""
    other = Worktree(path=Path("/repo/.worktrees/issue-99-y"), branch="feature/other")
    state = _ready_state()
    transport = MagicMock()
    transport.read.return_value = state
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[other]),
        patch(_MOD + ".GitHubTransport", return_value=transport) as gh_cls,
    ):
        out = _resolve_ready_state(Path("/repo"), "feature/x")
    assert out is state
    gh_cls.assert_called_once_with("feature/x")


def test_resolve_ready_state_worktree_without_file_falls_back_to_ref() -> None:
    """A worktree on the branch but with no state file falls through to the ref."""
    wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
    state = _ready_state()
    local = MagicMock()
    local.read.return_value = None
    transport = MagicMock()
    transport.read.return_value = state
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".LocalFileTransport", return_value=local),
        patch(_MOD + ".GitHubTransport", return_value=transport),
    ):
        out = _resolve_ready_state(Path("/repo"), "feature/x")
    assert out is state


def test_resolve_ready_state_errors_when_no_source() -> None:
    transport = MagicMock()
    transport.read.return_value = None
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".GitHubTransport", return_value=transport),
        pytest.raises(SystemExit, match="no ready-state for branch"),
    ):
        _resolve_ready_state(Path("/repo"), "feature/x")


def test_local_worktree_with_state_returns_matching_worktree() -> None:
    """A worktree on the branch with a readable state file yields its path;
    a non-matching worktree earlier in the list is skipped without a state read
    (short-circuit on the branch name)."""
    other = Worktree(path=Path("/repo/.worktrees/issue-99-y"), branch="feature/other")
    match = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
    local = MagicMock()
    local.read.return_value = _ready_state()
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[other, match]),
        patch(_MOD + ".LocalFileTransport", return_value=local) as local_cls,
    ):
        out = _local_worktree_with_state(Path("/repo"), "feature/x")
    assert out == match.path
    # Only the matching worktree's state file is read — the non-match short-circuits.
    local_cls.assert_called_once_with(match.path)


def test_local_worktree_with_state_none_when_no_match() -> None:
    other = Worktree(path=Path("/repo/.worktrees/issue-99-y"), branch="feature/other")
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[other]),
        patch(_MOD + ".LocalFileTransport") as local_cls,
    ):
        assert _local_worktree_with_state(Path("/repo"), "feature/x") is None
    local_cls.assert_not_called()


def test_local_worktree_with_state_none_when_file_absent() -> None:
    """A worktree on the branch but with no state file is not a local candidate."""
    match = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
    local = MagicMock()
    local.read.return_value = None
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[match]),
        patch(_MOD + ".LocalFileTransport", return_value=local),
    ):
        assert _local_worktree_with_state(Path("/repo"), "feature/x") is None


def test_origin_has_branch_reflects_ls_remote() -> None:
    with patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"):
        assert _origin_has_branch("feature/x") is True
    with patch(_MOD + ".git.read_output", return_value=""):
        assert _origin_has_branch("feature/x") is False


def test_verify_origin_tip_passes_on_match() -> None:
    state = _ready_state(head_sha="abc123")
    with patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"):
        _verify_origin_tip("feature/x", state)  # no raise


def test_verify_origin_tip_errors_on_mismatch() -> None:
    state = _ready_state(head_sha="abc123")
    with (
        patch(_MOD + ".git.read_output", return_value="deadbeef\trefs/heads/feature/x"),
        pytest.raises(SystemExit, match="does not"),
    ):
        _verify_origin_tip("feature/x", state)


def test_verify_origin_tip_ignores_a_tail_matching_ref() -> None:
    """GHSA-f4j7-x3f8-59gv: a ref ending in the branch's ref, sorted first, is not the branch."""
    state = _ready_state(head_sha="abc123")
    listing = "abc123\trefs/heads/a/refs/heads/feature/x\ndeadbeef\trefs/heads/feature/x"
    with (
        patch(_MOD + ".git.read_output", return_value=listing),
        pytest.raises(SystemExit, match=r"tip \(deadbeef\) does not"),
    ):
        _verify_origin_tip("feature/x", state)


def test_origin_has_branch_ignores_a_tail_matching_ref() -> None:
    with patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/a/refs/heads/feature/x"):
        assert _origin_has_branch("feature/x") is False


def test_verify_origin_tip_errors_when_branch_absent_on_origin() -> None:
    state = _ready_state(head_sha="abc123")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(SystemExit, match="not on origin"),
    ):
        _verify_origin_tip("feature/x", state)


class TestRelayBranchPath:
    """`vrg-submit-pr <branch>` opens a PR worktree-free (issue #2368)."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_relay_opens_pr_without_pushing(self) -> None:
        state = _ready_state(branch="feature/x", head_sha="abc123")
        transport = MagicMock()
        transport.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/5") as create,
        ):
            rc = main(["feature/x"])
        assert rc == 0
        push.assert_not_called()
        assert not any(c.args and c.args[0] == "push" for c in git_run.call_args_list)
        assert create.call_args.kwargs["head"] == "feature/x"

    def test_relay_prefers_local_file_over_ref(self) -> None:
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        state = _ready_state(branch="feature/x", head_sha="abc123")
        local = MagicMock()
        local.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            patch(_MOD + ".GitHubTransport") as gh_cls,
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._push_branch"),
            patch(_MOD + ".submission.record_submission"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/5"),
        ):
            rc = main(["feature/x"])
        assert rc == 0
        gh_cls.assert_not_called()

    def test_named_branch_local_worktree_not_on_origin_pushes_and_opens(self) -> None:
        """Regression (issue #2794): a named branch with a local ready worktree
        that was never pushed to origin falls back to the local push+open path
        instead of dead-ending on `_verify_origin_tip`'s "not on origin"."""
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        state = _ready_state(branch="feature/x", head_sha="abc123")
        local = MagicMock()
        local.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            # origin lacks the branch: ls-remote returns nothing.
            patch(_MOD + ".git.read_output", return_value="") as read_output,
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".submission.record_submission") as record,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/7") as create,
        ):
            rc = main(["feature/x"])
        assert rc == 0
        push.assert_called_once_with("feature/x")
        assert create.call_args.kwargs["head"] == "feature/x"
        # The submission is recorded on the local worktree so the scanner shows
        # it in-flight, exactly like the worktree submit path.
        record.assert_called_once_with(wt.path, pr_url="https://github.com/pr/7")
        # Only the origin-presence probe runs — no `_verify_origin_tip` ls-remote
        # against a branch that is not there. (The other read is the rollup's
        # main-worktree lookup for its run log.)
        ls_remotes = [c for c in read_output.call_args_list if c.args[0] == "ls-remote"]
        assert len(ls_remotes) == 1

    def test_named_branch_local_worktree_already_on_origin_uses_relay(self) -> None:
        """A local worktree branch that *is* on origin keeps the relay path
        (verify tip, push=False) — the fallback is gated on origin lacking it."""
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        state = _ready_state(branch="feature/x", head_sha="abc123")
        local = MagicMock()
        local.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".submission.record_submission") as record,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/7") as create,
        ):
            rc = main(["feature/x"])
        assert rc == 0
        push.assert_not_called()
        assert create.call_args.kwargs["head"] == "feature/x"
        # Regression (issue #3525): the relay open records the submission on
        # the local worktree too, so the hybrid branch does not stay frozen.
        record.assert_called_once_with(wt.path, pr_url="https://github.com/pr/7")

    def test_relay_open_with_local_worktree_lifts_the_freeze(self, tmp_path: Path) -> None:
        """Regression (issue #3525): a branch on origin that also has a local
        ready worktree is marked submitted after the relay open, so the freeze
        that refuses pr-watch fix commits and pushes lifts."""
        wt = Worktree(path=tmp_path, branch="feature/x")
        LocalFileTransport(tmp_path).write(_ready_state(branch="feature/x", head_sha="abc123"))
        assert freeze.check_worktree(tmp_path, action="commit").frozen
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/o/r/pull/7"),
        ):
            rc = main(["feature/x"])
        assert rc == 0
        push.assert_not_called()
        recorded = LocalFileTransport(tmp_path).read()
        assert recorded is not None
        assert recorded.submitted is not None
        assert recorded.submitted["pr_url"] == "https://github.com/o/r/pull/7"
        assert not freeze.check_worktree(tmp_path, action="commit").frozen

    def test_relay_open_without_local_worktree_records_nothing(self) -> None:
        """The pure cloud-to-Mac relay has no local state to mark (issue #3525)."""
        transport = MagicMock()
        transport.read.return_value = _ready_state(branch="feature/x", head_sha="abc123")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + ".submission.record_submission") as record,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/5"),
        ):
            rc = main(["feature/x"])
        assert rc == 0
        record.assert_not_called()

    def test_relay_open_failure_with_local_worktree_records_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed relay open records nothing (issue #3525): only an opened PR
        is a submission, so the local worktree stays ready and frozen."""
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        local = MagicMock()
        local.read.return_value = _ready_state(branch="feature/x", head_sha="abc123")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + ".submission.record_submission") as record,
            patch(
                _MOD + ".github.create_pr",
                side_effect=subprocess.CalledProcessError(1, ["gh", "pr", "create"]),
            ),
        ):
            assert main(["feature/x"]) == 3
        record.assert_not_called()
        captured = capsys.readouterr()
        assert "✗ feature/x" in captured.out
        assert "no PR opened: creating the PR failed" in captured.out
        assert "Failed to submit:\n  feature/x — creating the PR failed" in captured.err

    def test_relay_head_sha_mismatch_errors(self) -> None:
        state = _ready_state(branch="feature/x", head_sha="abc123")
        transport = MagicMock()
        transport.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="deadbeef\trefs/heads/feature/x"),
            patch(_MOD + ".github.create_pr") as create,
            pytest.raises(SystemExit, match="does not"),
        ):
            main(["feature/x"])
        create.assert_not_called()

    def test_relay_missing_ready_state_errors(self) -> None:
        transport = MagicMock()
        transport.read.return_value = None
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            pytest.raises(SystemExit, match="no ready-state for branch"),
        ):
            main(["feature/x"])

    def test_relay_batches_two_branches(self) -> None:
        states = {
            "feature/a": _ready_state(issue="1", branch="feature/a", head_sha="sha_a"),
            "feature/b": _ready_state(issue="2", branch="feature/b", head_sha="sha_b"),
        }

        def fake_read_output(*args: str) -> str:
            branch = args[-1].removeprefix("refs/heads/")
            return f"sha_{branch[-1]}\t{args[-1]}"

        def fake_transport(branch: str) -> MagicMock:
            t = MagicMock()
            t.read.return_value = states[branch]
            return t

        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", side_effect=fake_transport),
            patch(_MOD + ".git.read_output", side_effect=fake_read_output),
            patch(_MOD + "._push_branch") as push,
            patch(
                _MOD + ".github.create_pr",
                side_effect=["https://github.com/pr/1", "https://github.com/pr/2"],
            ) as create,
            patch(_MOD + ".pr_path.arm") as enable,
        ):
            enable.return_value = _armed()
            rc = main(["feature/a", "feature/b"])
        assert rc == 0
        assert create.call_count == 2
        push.assert_not_called()
        heads = [c.kwargs["head"] for c in create.call_args_list]
        assert heads == ["feature/a", "feature/b"]
        assert [c.kwargs["head"] for c in enable.call_args_list] == ["sha_a", "sha_b"]

    def test_relay_mode_uses_submit_all(self) -> None:
        """Named branches are gated first, then opened through _submit_all in one call."""
        states = {
            "feature/a": _ready_state(issue="1", branch="feature/a", head_sha="sha_a"),
            "feature/b": _ready_state(issue="2", branch="feature/b", head_sha="sha_b"),
        }

        def fake_read_output(*args: str) -> str:
            branch = args[-1].removeprefix("refs/heads/")
            return f"sha_{branch[-1]}\t{args[-1]}"

        def fake_transport(branch: str) -> MagicMock:
            t = MagicMock()
            t.read.return_value = states[branch]
            return t

        summary = SubmitSummary(opened=["https://x/pull/1", "https://x/pull/2"], failed=[])
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", side_effect=fake_transport),
            patch(_MOD + ".git.read_output", side_effect=fake_read_output),
            patch(_MOD + "._submit_all", return_value=summary) as submit_all,
        ):
            rc = main(["feature/a", "feature/b"])
        assert rc == 0
        submit_all.assert_called_once()
        selection, _args = submit_all.call_args.args
        assert [(s.branch, s.expected_head, s.push) for s in selection] == [
            ("feature/a", "sha_a", False),
            ("feature/b", "sha_b", False),
        ]
        assert submit_all.call_args.kwargs == {}

    def test_relay_failed_submit_exits_3_and_names_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = _ready_state(branch="feature/x", head_sha="abc123")
        transport = MagicMock()
        transport.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + ".github.create_pr", return_value="https://x/pull/7"),
            patch(
                _MOD + ".pr_path.arm",
                side_effect=pr_path.PRPathError("https://x/pull/7: refused"),
            ),
        ):
            assert main(["feature/x"]) == 3
        err = capsys.readouterr().err
        assert "Failed to submit:" in err
        assert (
            "https://x/pull/7 — opened at abc123; turning auto-merge on failed: "
            "https://x/pull/7: refused"
        ) in err

    def test_relay_dry_run_skips_verify_and_create(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = _ready_state(branch="feature/x")
        transport = MagicMock()
        transport.read.return_value = state
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output") as read_output,
            patch(_MOD + ".github.create_pr") as create,
        ):
            rc = main(["feature/x", "--dry-run"])
        assert rc == 0
        create.assert_not_called()
        read_output.assert_not_called()  # no ls-remote verification on dry-run
        assert "fix: bug" in capsys.readouterr().out

    def test_relay_rejects_all_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["feature/x", "--all"])
        assert rc == 1
        assert "--all/--select" in capsys.readouterr().err

    def test_relay_rejects_cli_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["feature/x", "--issue", "42", "--summary", "s", "--title", "t"])
        assert rc == 1
        assert "--issue/--summary/--title" in capsys.readouterr().err

    def test_relay_already_submitted_branch_aborts_when_finalizing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When finalizing, an already-submitted branch is refused before anything
        opens: this run would not finalize it, so it names the command that can."""
        states = {
            "feature/a": _ready_state(
                issue="1",
                branch="feature/a",
                submitted={"pr_url": "https://x/pull/9", "pr_number": "9"},
            ),
            "feature/b": _ready_state(issue="2", branch="feature/b", head_sha="sha_b"),
        }

        def fake_transport(branch: str) -> MagicMock:
            t = MagicMock()
            t.read.return_value = states[branch]
            return t

        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", side_effect=fake_transport),
            patch(_MOD + ".git.read_output", return_value="sha_b\trefs/heads/feature/b"),
            patch(_MOD + ".github.create_pr") as create,
            patch(_MOD + ".subprocess.run") as run,
            pytest.raises(SystemExit) as excinfo,
        ):
            main(["feature/a", "feature/b"])
        create.assert_not_called()
        run.assert_not_called()
        msg = str(excinfo.value.code)
        assert "already has a submitted PR" in msg
        assert "vrg-finalize-pr https://x/pull/9" in msg

    def test_relay_blocked_for_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".github.create_pr") as create:
            rc = main(["feature/x"])
        assert rc != 0
        create.assert_not_called()


def test_no_arg_local_path_unchanged_regression(tmp_path: Path) -> None:
    """Regression guard for issue #2368: extracting the shared _open_pr core must
    leave the no-arg local worktree path byte-for-byte behaviorally unchanged —
    it force-with-lease pushes the branch, opens the PR, and marks the local
    state file submitted."""
    _write_workflow_state(tmp_path, title="fix: bug", summary="Fix")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1") as create,
    ):
        rc = main([])
    assert rc == 0
    git_run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
    create.assert_called_once()
    assert _state_submitted(tmp_path)


# -- the PR opens only at the head the latest report-ready recorded (#3815) ---


def test_require_local_head_passes_on_match() -> None:
    with patch(_MOD + ".git.read_output", return_value="abc123") as read:
        _require_local_head("feature/x", "abc123")
    read.assert_called_once_with("rev-parse", "HEAD")


def test_require_local_head_reads_the_named_worktree() -> None:
    wt = Path("/repo/.worktrees/issue-42-x")
    with patch(_MOD + ".git.read_output", return_value="abc123") as read:
        _require_local_head("feature/x", "abc123", worktree=wt)
    read.assert_called_once_with("-C", str(wt), "rev-parse", "HEAD")


def test_require_local_head_refuses_a_moved_head() -> None:
    with (
        patch(_MOD + ".git.read_output", return_value="258cf59aaaaa"),
        pytest.raises(SystemExit, match=r"HEAD \(258cf59aaaaa\) is not the head") as exc,
    ):
        _require_local_head("feature/x", "e4570bebbbbb")
    message = str(exc.value.code)
    assert "e4570bebbbbb" in message
    assert "Nothing was pushed or opened" in message
    # A frozen branch whose HEAD moved (e.g. a batch item that failed after its
    # rebase) cannot simply re-run report-ready: the freeze refuses it. The
    # refusal names the real recovery, unfreeze then report-ready, in order.
    unfreeze = message.index("`vrg-pr-workflow unfreeze`")
    report = message.index("`vrg-pr-workflow report-ready` at the commit to submit")
    assert unfreeze < report


def test_require_local_head_refuses_a_missing_record() -> None:
    with (
        patch(_MOD + ".git.read_output") as read,
        pytest.raises(SystemExit, match="records no head_sha"),
    ):
        _require_local_head("feature/x", "")
    read.assert_not_called()


def test_require_origin_head_passes_on_match() -> None:
    with patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"):
        _require_origin_head("feature/x", "abc123")  # no raise


def test_require_origin_head_refuses_a_moved_origin() -> None:
    with (
        patch(_MOD + ".git.read_output", return_value="deadbeef\trefs/heads/feature/x"),
        pytest.raises(SystemExit, match="does not match the ready-state head_sha"),
    ):
        _require_origin_head("feature/x", "abc123")


def test_verify_origin_tip_refuses_a_state_with_no_head() -> None:
    state = _ready_state(head_sha="abc123")
    state.git.pop("head_sha")
    with (
        patch(_MOD + ".git.read_output") as read,
        pytest.raises(SystemExit, match="records no head_sha"),
    ):
        _verify_origin_tip("feature/x", state)
    read.assert_not_called()


def test_require_ready_passes_a_ready_state() -> None:
    _require_ready(_ready_state(), "feature/x")  # no raise


def test_require_ready_refuses_an_unfrozen_state() -> None:
    state = _ready_state(head_sha="e4570be")
    engine.apply_unfreeze(state, now="2026-06-08T00:00:00Z")
    with pytest.raises(SystemExit, match="status='implementing'"):
        _require_ready(state, "feature/x")


def test_require_relay_agrees_passes_without_a_relay_ref() -> None:
    transport = MagicMock()
    transport.read.return_value = None
    with patch(_MOD + ".GitHubTransport", return_value=transport):
        _require_relay_agrees("feature/x", _ready_state())  # no raise


def test_require_relay_agrees_passes_when_they_match() -> None:
    transport = MagicMock()
    transport.read.return_value = _ready_state(head_sha="abc123")
    with patch(_MOD + ".GitHubTransport", return_value=transport):
        _require_relay_agrees("feature/x", _ready_state(head_sha="abc123"))  # no raise


def test_require_relay_agrees_refuses_a_different_head() -> None:
    transport = MagicMock()
    transport.read.return_value = _ready_state(head_sha="e4570be")
    with (
        patch(_MOD + ".GitHubTransport", return_value=transport),
        pytest.raises(SystemExit, match="disagree"),
    ):
        _require_relay_agrees("feature/x", _ready_state(head_sha="258cf59"))


def test_require_relay_agrees_refuses_a_different_status() -> None:
    relay = _ready_state(head_sha="abc123")
    engine.apply_unfreeze(relay, now="2026-06-08T00:00:00Z")
    transport = MagicMock()
    transport.read.return_value = relay
    with (
        patch(_MOD + ".GitHubTransport", return_value=transport),
        pytest.raises(SystemExit, match="disagree"),
    ):
        _require_relay_agrees("feature/x", _ready_state(head_sha="abc123"))


def test_require_reported_head_holds_the_worktree_to_its_record(tmp_path: Path) -> None:
    _write_workflow_state(tmp_path)  # records head_sha "h0"
    wt = Worktree(path=tmp_path, branch="feature/x")
    with (
        patch(_MOD + "._require_local_head", _require_local_head),
        patch(_MOD + ".git.read_output", return_value="h1"),
        pytest.raises(SystemExit, match=r"HEAD in .* \(h1\) is not the head"),
    ):
        _require_reported_head(wt)


def test_submit_refuses_when_origin_is_not_the_expected_head() -> None:
    """The last check before `gh pr create`: a push that left origin at another
    commit (or an origin that moved during the prompt) opens nothing."""
    with (
        patch(_MOD + "._require_origin_head", _require_origin_head),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + ".git.read_output", return_value="e4570be\trefs/heads/feature/x"),
        patch(_MOD + ".github.create_pr") as create,
    ):
        summary = _submit_all([_submission("feature/x", head="258cf59", record_in=None)], _ARGS)
    push.assert_called_once_with("feature/x")
    create.assert_not_called()
    assert summary.opened == []
    ((label, reason),) = summary.failed
    assert label == "feature/x"
    assert "does not match" in reason


def test_template_refuses_a_head_other_than_the_recorded_one(tmp_path: Path) -> None:
    _write_workflow_state(tmp_path)  # records head_sha "h0"
    with (
        patch(_MOD + "._require_local_head", _require_local_head),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.read_output", return_value="h1"),
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".github.create_pr") as create,
        pytest.raises(SystemExit, match="is not the head the latest report-ready recorded"),
    ):
        main([])
    git_run.assert_not_called()
    create.assert_not_called()
    assert not _state_submitted(tmp_path)


def test_template_holds_origin_to_the_recorded_head(tmp_path: Path) -> None:
    _write_workflow_state(tmp_path)  # records head_sha "h0"
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._require_origin_head") as origin_check,
        patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
    ):
        assert main([]) == 0
    origin_check.assert_called_once_with("feature/x", "h0")


def test_submit_batch_refuses_a_worktree_off_its_recorded_head() -> None:
    wt = _batch_wt("issue-1-a")
    with (
        patch(
            _MOD + "._require_reported_head",
            side_effect=SystemExit("vrg-submit-pr: refusing to submit 'feature/1-x'"),
        ),
        patch(_MOD + "._submit_all") as submit_all,
        pytest.raises(SystemExit, match="refusing to submit 'feature/1-x'"),
    ):
        _run_submit_batch([wt], parse_args(["--all"]))
    submit_all.assert_not_called()


def test_submit_batch_holds_origin_to_the_recorded_head() -> None:
    """With no rebase, each batch PR opens (and auto-merges) at its recorded head."""
    with patch(_MOD + "._require_origin_head") as origin_check:
        rc, _calls = _run_batch([_batch_wt("issue-1-a")], [], urls=["https://x/pull/1"])
    assert rc == 0
    origin_check.assert_called_once_with("feature/1-x", "h1")


class TestRelayRecordedHead:
    """Named-branch submit opens only at the latest report-ready's head (#3815)."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_stale_relay_ref_after_unfreeze_is_refused(self) -> None:
        """The incident: report-ready at e4570be, then unfreeze. The relay ref
        unfreeze now publishes says implementing, so the submit refuses instead
        of opening the PR at e4570be while origin still holds it."""
        relay = _ready_state(branch="feature/x", head_sha="e4570be")
        engine.apply_unfreeze(relay, now="2026-06-08T00:00:00Z")
        transport = MagicMock()
        transport.read.return_value = relay
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="e4570be\trefs/heads/feature/x"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".github.create_pr") as create,
            pytest.raises(SystemExit, match="not ready"),
        ):
            main(["feature/x"])
        push.assert_not_called()
        create.assert_not_called()

    def test_unfrozen_local_worktree_is_refused(self, tmp_path: Path) -> None:
        state = _ready_state(branch="feature/x", head_sha="e4570be")
        engine.apply_unfreeze(state, now="2026-06-08T00:00:00Z")
        LocalFileTransport(tmp_path).write(state)
        wt = Worktree(path=tmp_path, branch="feature/x")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".git.read_output", return_value="e4570be\trefs/heads/feature/x"),
            patch(_MOD + ".github.create_pr") as create,
            pytest.raises(SystemExit, match="not ready"),
        ):
            main(["feature/x"])
        create.assert_not_called()

    def test_local_worktree_past_its_recorded_head_is_refused(self) -> None:
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        local = MagicMock()
        local.read.return_value = _ready_state(branch="feature/x", head_sha="e4570be")
        with (
            patch(_MOD + "._require_local_head", _require_local_head),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            patch(_MOD + ".git.read_output", return_value="258cf59"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + ".github.create_pr") as create,
            pytest.raises(SystemExit, match="is not the head the latest report-ready recorded"),
        ):
            main(["feature/x"])
        push.assert_not_called()
        create.assert_not_called()

    def test_local_state_and_relay_ref_that_disagree_are_refused(self) -> None:
        wt = Worktree(path=Path("/repo/.worktrees/issue-42-x"), branch="feature/x")
        local = MagicMock()
        local.read.return_value = _ready_state(branch="feature/x", head_sha="258cf59")
        relay = MagicMock()
        relay.read.return_value = _ready_state(branch="feature/x", head_sha="e4570be")
        with (
            patch(_MOD + "._require_relay_agrees", _require_relay_agrees),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".LocalFileTransport", return_value=local),
            patch(_MOD + ".GitHubTransport", return_value=relay),
            patch(_MOD + ".git.read_output", return_value="258cf59\trefs/heads/feature/x"),
            patch(_MOD + ".github.create_pr") as create,
            pytest.raises(SystemExit, match="disagree"),
        ):
            main(["feature/x"])
        create.assert_not_called()

    def test_relay_open_holds_origin_to_the_recorded_head(self) -> None:
        transport = MagicMock()
        transport.read.return_value = _ready_state(branch="feature/x", head_sha="abc123")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._require_origin_head") as origin_check,
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/5"),
        ):
            assert main(["feature/x"]) == 0
        origin_check.assert_called_once_with("feature/x", "abc123")


# -- batch PRs: per-issue checks and one Closes line per issue (epic .github#1019)


_BATCH_ISSUES = ["10", "11"]


def _batch_state(
    *,
    issues: list[str] | None = None,
    branch: str = "feature/10-x",
    head_sha: str = "abc123def456",
) -> WorkflowState:
    """A report-ready batch ``WorkflowState`` (``issues`` set, ``issue`` the first)."""
    now = "2026-06-08T00:00:00Z"
    state = engine.init_state(
        issues=list(issues or _BATCH_ISSUES),
        branch=branch,
        base="develop",
        head_sha=head_sha,
        base_sha="b0",
        now=now,
    )
    engine.apply_report_ready(
        state,
        title="feat: batch",
        summary="Two small changes",
        notes="Merge review: done: eligible",
        linkage="Ref",
        head_sha=head_sha,
        now=now,
    )
    return state


def _all_closes(_ref: str, _requested: str) -> str:
    """Every batch issue is a task under the batch's epic, so each links Closes."""
    return "Closes"


def _batch_metadata(issues: str = "10,11") -> dict[str, str]:
    return {
        "issue": issues.split(",")[0],
        "issues": issues,
        "title": "feat: batch",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
    }


class TestBatchSubmit:
    """vrg-submit-pr hands a batch record off as one PR closing every issue."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_submit_batch_closes_every_issue(self, tmp_path: Path) -> None:
        """Template mode: the body has one Closes line per issue, in order, and
        every issue runs the per-issue refusals."""
        LocalFileTransport(tmp_path, base="develop").write(_batch_state(head_sha="h0"))
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/10-x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + "._reject_if_cross_repo") as cross,
            patch(_MOD + "._reject_if_queue_item") as queue,
            patch(_MOD + "._recheck_batch") as recheck,
            patch(_MOD + "._push_branch"),
            patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/9") as create,
        ):
            assert main([]) == 0
        body = create.call_args.kwargs["pr_body"]
        assert "## Issue Linkage\n\n- Closes #10\n- Closes #11\n\n## Notes" in body
        assert extract_tracking_refs(body) == [("Closes", "#10"), ("Closes", "#11")]
        assert [c.args[0] for c in cross.call_args_list] == ["#10", "#11"]
        assert [c.args[0] for c in queue.call_args_list] == ["#10", "#11"]
        recheck.assert_called_once_with(
            ["#10", "#11"], branch="feature/10-x", target="develop", head="h0", push=True
        )
        assert _state_submitted(tmp_path)

    def test_submit_batch_rechecks_rules_and_commits_before_create(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """On a commit mismatch nothing is pushed and no PR is created."""
        LocalFileTransport(tmp_path, base="develop").write(_batch_state(head_sha="h0"))
        mismatch = BatchRuleError("one commit per issue", "commit abc carries no issue ref")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/10-x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".github.current_repo", return_value="acme/tools"),
            patch(_MOD + ".batch_rules.check_hard_rules") as rules,
            patch(_MOD + ".batch_rules.check_commit_per_issue", side_effect=mismatch) as commits,
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + "._create_pr") as create,
        ):
            assert main([]) == 3
        # A submit that pushes fetches only the target: fetching the branch would
        # move the lease ref that --force-with-lease holds (merge review R1).
        git_run.assert_called_once_with("fetch", "origin", "develop")
        rules.assert_called_once_with(["#10", "#11"], repo="acme/tools")
        commits.assert_called_once_with(["#10", "#11"], base="origin/develop", head="h0")
        push.assert_not_called()
        create.assert_not_called()
        assert not _state_submitted(tmp_path)
        assert "carries no issue ref" in capsys.readouterr().err

    def test_submit_batch_hard_rule_break_opens_no_pr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        LocalFileTransport(tmp_path, base="develop").write(_batch_state(head_sha="h0"))
        broken = BatchRuleError("same epic", "#11 is under acme/.github#8")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/10-x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".github.current_repo", return_value="acme/tools"),
            patch(_MOD + ".batch_rules.check_hard_rules", side_effect=broken),
            patch(_MOD + ".batch_rules.check_commit_per_issue") as commits,
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + "._create_pr") as create,
        ):
            assert main([]) == 3
        # Template mode pushes, so only the target is fetched.
        git_run.assert_called_once_with("fetch", "origin", "develop")
        commits.assert_not_called()
        push.assert_not_called()
        create.assert_not_called()
        assert re.search(r"batch refused \(same epic\).*No PR was opened", capsys.readouterr().err)

    def test_submit_batch_per_issue_refusal_on_a_later_issue(self, tmp_path: Path) -> None:
        """The per-issue refusals run on every issue, not only the first."""
        LocalFileTransport(tmp_path, base="develop").write(_batch_state(head_sha="h0"))

        def refuse_eleven(ref: str) -> None:
            if ref == "#11":
                raise SystemExit("vrg-submit-pr: --issue (#11) is an epic")

        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/10-x"),
            patch(_MOD + "._reject_if_non_task", side_effect=refuse_eleven),
            patch(_MOD + "._create_pr") as create,
            pytest.raises(SystemExit, match=r"#11\) is an epic"),
        ):
            main([])
        create.assert_not_called()

    def test_submit_single_issue_template_body_and_no_batch_check(self, tmp_path: Path) -> None:
        """Review focus 5: a one-issue record gets one linkage line and no batch check."""
        _write_workflow_state(tmp_path, issue="42", linkage="Ref")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".batch_rules.check_hard_rules") as rules,
            patch(_MOD + ".batch_rules.check_commit_per_issue") as commits,
            patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/9") as create,
        ):
            assert main([]) == 0
        body = create.call_args.kwargs["pr_body"]
        assert body == (
            "# Pull Request\n\n## Summary\n\n- Fix the bug\n\n"
            "## Issue Linkage\n\n- Closes #42\n\n## Notes\n\n- Verified locally"
        )
        # Only the push runs: no fetch, no batch rule read.
        git_run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
        rules.assert_not_called()
        commits.assert_not_called()

    def test_submit_cli_mode_stays_single_issue(self) -> None:
        """The CLI emergency mode keeps one --issue: its re-check is the one-issue no-op."""
        with (
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + "._push_branch"),
            patch(_MOD + ".batch_rules.check_hard_rules") as rules,
            patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/9") as create,
        ):
            rc = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
        assert rc == 0
        body = create.call_args.kwargs["pr_body"]
        assert extract_tracking_refs(body) == [("Closes", "#42")]
        rules.assert_not_called()

    def test_relay_submit_batch_closes_every_issue(self) -> None:
        """The named-branch path reads the relay record's whole issue list."""
        transport = MagicMock()
        transport.read.return_value = _batch_state(head_sha="abc123")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/10-x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.current_repo", return_value="acme/tools"),
            patch(_MOD + ".batch_rules.check_hard_rules"),
            patch(_MOD + ".batch_rules.check_commit_per_issue"),
            patch(_MOD + "._push_branch") as push,
            patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/5") as create,
        ):
            assert main(["feature/10-x"]) == 0
        push.assert_not_called()
        body = create.call_args.kwargs["pr_body"]
        assert extract_tracking_refs(body) == [("Closes", "#10"), ("Closes", "#11")]
        assert create.call_args.kwargs["head"] == "feature/10-x"

    def test_relay_submit_batch_rechecks_after_fetch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The relay path fetches the branch and base before the commit check,
        and a mismatch opens no PR."""
        transport = MagicMock()
        transport.read.return_value = _batch_state(head_sha="abc123")
        order = MagicMock()
        order.check_commit_per_issue.side_effect = BatchRuleError(
            "one commit per issue", "2 issues (#10, #11) need 2 commits, found 1"
        )
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/10-x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run", order.run),
            patch(_MOD + ".github.current_repo", return_value="acme/tools"),
            patch(_MOD + ".batch_rules.check_hard_rules", order.check_hard_rules),
            patch(_MOD + ".batch_rules.check_commit_per_issue", order.check_commit_per_issue),
            patch(_MOD + "._create_pr") as create,
        ):
            assert main(["feature/10-x"]) == 3
        names = [c[0] for c in order.mock_calls]
        assert names == ["run", "check_hard_rules", "check_commit_per_issue"]
        assert order.run.call_args.args == ("fetch", "origin", "feature/10-x", "develop")
        assert order.check_commit_per_issue.call_args.kwargs == {
            "base": "origin/develop",
            "head": "abc123",
        }
        create.assert_not_called()
        assert "found 1" in capsys.readouterr().err

    def test_relay_submit_single_issue_unchanged(self) -> None:
        """Review focus 5: a one-issue relay record gets one linkage line, no batch check."""
        transport = MagicMock()
        transport.read.return_value = _ready_state(branch="feature/x", head_sha="abc123")
        with (
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            patch(_MOD + ".GitHubTransport", return_value=transport),
            patch(_MOD + ".git.read_output", return_value="abc123\trefs/heads/feature/x"),
            patch(_MOD + "._task_linkage", side_effect=_all_closes),
            patch(_MOD + ".git.run") as git_run,
            patch(_MOD + ".batch_rules.check_hard_rules") as rules,
            patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/5") as create,
        ):
            assert main(["feature/x"]) == 0
        body = create.call_args.kwargs["pr_body"]
        assert "## Issue Linkage\n\n- Closes #42\n\n## Notes" in body
        git_run.assert_not_called()
        rules.assert_not_called()

    def test_metadata_from_state_carries_the_batch_list(self) -> None:
        meta = _metadata_from_state(_batch_state(issues=["10", "11", "12"]), "feature/10-x")
        assert meta["issue"] == "10"
        assert meta["issues"] == "10,11,12"


def test_prepare_requires_issues() -> None:
    """Metadata without 'issues' is refused, never read as its first issue."""
    metadata = {"issue": "42", "title": "fix: bug", "summary": "s", "notes": "", "linkage": "Ref"}
    with pytest.raises(SystemExit, match="no 'issues' list"):
        _prepare(
            "feature/x", "develop", metadata, push=True, expected_head="abc123", record_in=None
        )


def test_batch_submission_rechecks_before_push() -> None:
    """A batch is re-checked at the expected head before the branch is pushed."""
    order = MagicMock()
    with (
        patch(_MOD + "._task_linkage", side_effect=_all_closes),
        patch(_MOD + "._recheck_batch", order.recheck),
        patch(_MOD + "._push_branch", order.push),
        patch(_MOD + "._create_pr", order.create),
    ):
        order.create.return_value = "https://github.com/o/r/pull/3"
        sub = _prepare(
            "feature/10-x",
            "develop",
            _batch_metadata(),
            push=True,
            expected_head="reported1",
            record_in=None,
        )
        summary = _submit_all([sub], _ARGS)
    assert summary.opened == ["https://github.com/o/r/pull/3"]
    assert [c[0] for c in order.mock_calls] == ["recheck", "push", "create"]
    order.recheck.assert_called_once_with(
        ["#10", "#11"], branch="feature/10-x", target="develop", head="reported1", push=True
    )


def test_batch_prepare_previews_without_checking() -> None:
    with (
        patch(_MOD + "._task_linkage", side_effect=_all_closes),
        patch(_MOD + "._recheck_batch") as recheck,
        patch(_MOD + "._create_pr") as create,
    ):
        sub = _prepare(
            "feature/10-x",
            "develop",
            _batch_metadata(),
            push=False,
            expected_head="abc",
            record_in=None,
        )
    assert sub.issue_refs == ["#10", "#11"]
    recheck.assert_not_called()
    create.assert_not_called()


def test_recheck_batch_is_a_no_op_for_one_issue() -> None:
    with (
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".batch_rules.check_hard_rules") as rules,
    ):
        _recheck_batch(["#42"], branch="feature/x", target="develop", head="abc", push=False)
    git_run.assert_not_called()
    rules.assert_not_called()


def test_recheck_batch_refuses_when_the_fetch_fails() -> None:
    with (
        patch(
            _MOD + ".git.run",
            side_effect=subprocess.CalledProcessError(1, "git", stderr="no network"),
        ),
        patch(_MOD + ".batch_rules.check_hard_rules") as rules,
        pytest.raises(SystemExit, match="could not fetch feature/10-x, develop"),
    ):
        _recheck_batch(
            ["#10", "#11"], branch="feature/10-x", target="develop", head="abc", push=False
        )
    rules.assert_not_called()


# -- the serial batch (--select) re-checks the rebased head, on real git -------


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_file(repo: Path, name: str, text: str, subject: str) -> str:
    (repo / name).write_text(text)
    _run_git(repo, "add", name)
    _run_git(repo, "commit", "-q", "-m", subject)
    return _run_git(repo, "rev-parse", "HEAD")


def test_batch_checks_the_reported_head_without_rebasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --select batch is checked, opened and auto-merged at the head report-ready
    recorded, though develop moved since: submit never rebases (spec §6.2)."""
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "Test")
        monkeypatch.setenv(f"{key}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.delenv("VRG_APP_ID", raising=False)

    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    other = tmp_path / "other"
    _run_git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    _run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    _run_git(clone, "checkout", "-q", "-b", "develop")
    _commit_file(clone, "base.txt", "base\n", "chore: base")
    _run_git(clone, "push", "-q", "origin", "develop")

    _run_git(clone, "checkout", "-q", "-b", "feature/10-x")
    _commit_file(clone, "a.txt", "a\n", "feat: a (#10)")
    reported = _commit_file(clone, "b.txt", "b\n", "feat: b (#11)")

    # Meanwhile develop moves on.
    _run_git(tmp_path, "clone", "-q", "-b", "develop", str(origin), str(other))
    _commit_file(other, "c.txt", "c\n", "feat: c landed elsewhere")
    _run_git(other, "push", "-q", "origin", "develop")

    fields = {
        **_batch_metadata(),
        "base": "origin/develop",
        "head_sha": reported,
    }
    monkeypatch.chdir(clone)
    checked: list[str] = []
    real_check = batch_rules.check_commit_per_issue

    def spy(issues: list[str], *, base: str, head: str) -> None:
        checked.append(head)
        real_check(issues, base=base, head=head)

    wt = Worktree(path=clone, branch="feature/10-x")
    with (
        patch(_MOD + ".git.main_worktree_root", return_value=clone),
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + "._task_linkage", side_effect=_all_closes),
        patch(_MOD + ".github.current_repo", return_value="acme/tools"),
        patch(_MOD + ".batch_rules.check_hard_rules") as rules,
        patch(_MOD + ".batch_rules.check_commit_per_issue", side_effect=spy),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + "._create_pr", return_value="https://x/pull/1") as create,
        patch(_MOD + ".submission.record_submission"),
        patch(_MOD + ".pr_path.arm") as enable,
    ):
        enable.return_value = _armed()
        assert _run_submit_batch([wt], parse_args(["--select", "10"])) == 0
    assert _run_git(clone, "rev-parse", "HEAD") == reported
    assert checked == [reported]
    rules.assert_called_once_with(["#10", "#11"], repo="acme/tools")
    push.assert_called_once_with("feature/10-x")
    create.assert_called_once()
    enable.assert_called_once_with("https://x/pull/1", head=reported, clone=ANY)


# -- the selection table (vergil-tooling#4744) ---------------------------------


def _table_sub(branch: str, title: str, links: list[tuple[str, str]]) -> _Submission:
    return _Submission(
        branch=branch,
        base="develop",
        title=title,
        pr_body="BODY",
        issue_refs=[ref for _, ref in links],
        links=links,
        expected_head="a" * 40,
        push=True,
        record_in=None,
    )


def test_print_selection_aligns_one_row_per_pr(capsys: pytest.CaptureFixture[str]) -> None:
    _print_selection(
        [
            _table_sub("feature/1-a", "fix: a", [("Closes", "#1")]),
            _table_sub("feature/20-batch-b", "feat: b", [("Closes", "#20"), ("Closes", "#21")]),
        ]
    )
    assert capsys.readouterr().out == (
        "Submitting 2 PRs to develop:\n"
        "  ISSUES           BRANCH              TITLE\n"
        "  Closes #1        feature/1-a         fix: a\n"
        "  Closes #20, #21  feature/20-batch-b  feat: b\n"
        "\n"
    )


def test_print_selection_groups_mixed_linkage(capsys: pytest.CaptureFixture[str]) -> None:
    _print_selection([_table_sub("feature/x", "t", [("Closes", "#1"), ("Ref", "#2")])])
    assert "Closes #1; Ref #2" in capsys.readouterr().out


def test_print_selection_empty_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    _print_selection([])
    assert capsys.readouterr().out == ""


# --- every submission passes the gate (epic vergil-project/.github#1384, Task 9) ---

_GATE_AUTHORS = trust_stubs.AUTHORS


def _gate_sub(*, push: bool = True, issues: tuple[str, ...] = ("#42",)) -> _Submission:
    return _submission("feature/42-x", issues=issues, head="f" * 40, push=push, record_in=None)


@pytest.fixture()
def real_gates() -> Iterator[dict[str, MagicMock]]:
    """The real gate step, with git, GitHub and the evaluator stubbed."""
    with (
        patch(_MOD + "._run_gates", _REAL_RUN_GATES),
        patch(_MOD + ".load_trusted_authors", return_value=_GATE_AUTHORS),
        patch(_MOD + ".git.repo_root", return_value="/repo"),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".git.run") as git_run,
        patch(
            _MOD + ".agent_gates.evaluate",
            return_value=agent_gates.GateResult(ok=True, reasons=()),
        ) as evaluate,
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + "._create_pr", return_value="https://github.com/o/r/pull/9") as create,
    ):
        yield {
            "git_run": git_run,
            "evaluate": evaluate,
            "push": push,
            "create": create,
        }


def _as_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Run as identity *mode*; the automation App's mode cannot be set by env var."""
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode(mode))


def _fetches(git_run: MagicMock) -> list[tuple[str, ...]]:
    return [c.args for c in git_run.call_args_list if c.args and c.args[0] == "fetch"]


class TestAgentIdentityGate:
    """Who may submit, and in which modes (spec §4.6)."""

    def test_user_agent_submits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _gates_pass: MagicMock
    ) -> None:
        """The user agent submits in the template mode report-ready drives."""
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        _write_workflow_state(tmp_path)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1") as create,
        ):
            assert main([]) == 0
        create.assert_called_once()
        _gates_pass.assert_called_once()
        assert _state_submitted(tmp_path)

    def test_user_agent_submits_a_batch_selection(
        self, monkeypatch: pytest.MonkeyPatch, _gates_pass: MagicMock
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + "._run_submit_batch", return_value=0) as batch,
            patch(_MOD + "._select_batch_worktrees", return_value=[]),
        ):
            assert main(["--all"]) == 0
        batch.assert_called_once()

    def test_agent_release_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".github.create_pr") as create, patch(_MOD + "._push_branch") as push:
            assert main(["--release"]) == 1
        create.assert_not_called()
        push.assert_not_called()
        assert "--release is human-only" in capsys.readouterr().err

    def test_agent_cli_mode_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".github.create_pr") as create:
            assert main(["--issue", "42", "--summary", "S", "--title", "fix: t"]) == 1
        create.assert_not_called()
        err = capsys.readouterr().err
        assert "emergency path" in err
        assert "vrg-pr-workflow report-ready" in err

    @pytest.mark.parametrize("partial", [["--issue", "42"], ["--title", "t"]])
    def test_agent_partial_cli_fields_refused(
        self, partial: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert main(partial) == 1

    def test_agent_relay_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".GitHubTransport") as relay, patch(_MOD + ".github.create_pr") as create:
            assert main(["feature/x"]) == 1
        relay.assert_not_called()
        create.assert_not_called()
        assert "relay path is for the human" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv", [[], ["--all"], ["feature/x"], ["--issue", "1", "--summary", "s", "--title", "t"]]
    )
    def test_audit_refused(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with patch(_MOD + ".github.create_pr") as create:
            assert main(argv) == 1
        create.assert_not_called()
        assert "audit agent cannot submit" in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["user", "automation"])
    @pytest.mark.parametrize(
        ("flag", "message"),
        [
            ("--override-merge-review", OVERRIDE_MERGE_REVIEW_REMOVED),
            ("--human-only", HUMAN_ONLY_REMOVED),
        ],
    )
    def test_removed_gate_skips_refused_to_each_agent_identity(
        self,
        mode: str,
        flag: str,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An agent passing a removed skip of the gate is refused by name, nothing done."""
        _as_mode(monkeypatch, mode)
        with (
            patch(_MOD + "._submit_all") as submit_all,
            patch(_MOD + "._run_relay") as relay,
            patch(_MOD + ".git.run") as git_run,
        ):
            assert main([flag, "no session"]) == 1
        for mock in (submit_all, relay, git_run):
            mock.assert_not_called()
        assert capsys.readouterr().err == f"{message}\n"


class TestGateStep:
    """The merge-review gate runs before any push or ``gh pr create`` (spec §4.6).

    It runs on every submission, the human's included; nothing skips it.
    """

    def test_single_issue_submit_runs_gates(self, real_gates: dict[str, MagicMock]) -> None:
        summary = _submit_all([_gate_sub()], _ARGS)
        assert summary.opened == ["https://github.com/o/r/pull/9"]
        real_gates["evaluate"].assert_called_once_with(
            Path("/repo"),
            base="origin/develop",
            head="f" * 40,
            issue_refs=["o/r#42"],
            authors=_GATE_AUTHORS,
        )
        # A pushing submit fetches the base only: the branch's lease stays put.
        assert _fetches(real_gates["git_run"]) == [("fetch", "origin", "develop")]

    def test_batch_submit_gates_every_issue(self, real_gates: dict[str, MagicMock]) -> None:
        with patch(_MOD + "._recheck_batch"):
            _submit_all([_gate_sub(issues=("#42", "#43"))], _ARGS)
        assert real_gates["evaluate"].call_args.kwargs["issue_refs"] == ["o/r#42", "o/r#43"]

    def test_relay_mode_gates_fetch_branch(self, real_gates: dict[str, MagicMock]) -> None:
        """Run as the human, on the relay path: the branch is fetched too, never pushed."""
        summary = _submit_all([_gate_sub(push=False)], _ARGS)
        assert summary.opened == ["https://github.com/o/r/pull/9"]
        assert _fetches(real_gates["git_run"]) == [("fetch", "origin", "feature/42-x", "develop")]
        real_gates["evaluate"].assert_called_once()
        real_gates["push"].assert_not_called()

    def test_gate_failure_leaves_no_pr_and_pushes_nothing(
        self, real_gates: dict[str, MagicMock]
    ) -> None:
        real_gates["evaluate"].return_value = agent_gates.GateResult(
            ok=False, reasons=("no merge-review record covers the change",)
        )
        summary = _submit_all([_gate_sub()], _ARGS)
        assert summary.opened == []
        [(branch, why)] = summary.failed
        assert branch == "feature/42-x"
        assert "no merge-review record covers the change" in why
        assert "no PR was opened" in why
        real_gates["push"].assert_not_called()
        real_gates["create"].assert_not_called()

    def test_gates_run_before_the_batch_recheck(self, real_gates: dict[str, MagicMock]) -> None:
        real_gates["evaluate"].return_value = agent_gates.GateResult(ok=False, reasons=("x",))
        with patch(_MOD + "._recheck_batch") as recheck:
            _submit_all([_gate_sub(issues=("#42", "#43"))], _ARGS)
        recheck.assert_not_called()

    def test_fetch_failure_refuses(self, real_gates: dict[str, MagicMock]) -> None:
        real_gates["git_run"].side_effect = subprocess.CalledProcessError(1, ["git", "fetch"])
        summary = _submit_all([_gate_sub()], _ARGS)
        [(_branch, why)] = summary.failed
        assert "could not fetch develop" in why
        real_gates["evaluate"].assert_not_called()
        real_gates["create"].assert_not_called()

    def test_trusted_authors_missing_refuses(self, real_gates: dict[str, MagicMock]) -> None:
        with patch(
            _MOD + ".load_trusted_authors",
            side_effect=TrustedAuthorsNotConfiguredError("no trusted authors"),
        ):
            summary = _submit_all([_gate_sub()], _ARGS)
        assert summary.opened == []
        real_gates["create"].assert_not_called()

    def test_rerun_after_merge_review_submits(self, real_gates: dict[str, MagicMock]) -> None:
        real_gates["evaluate"].side_effect = [
            agent_gates.GateResult(ok=False, reasons=("no merge-review record",)),
            agent_gates.GateResult(ok=True, reasons=()),
        ]
        first = _submit_all([_gate_sub()], _ARGS)
        assert first.opened == []
        real_gates["create"].assert_not_called()
        second = _submit_all([_gate_sub()], _ARGS)
        assert second.opened == ["https://github.com/o/r/pull/9"]

    def test_human_submission_runs_the_gate(
        self, real_gates: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The human's own submission is gated too: nothing skips the gate."""
        _as_mode(monkeypatch, "human")
        real_gates["evaluate"].return_value = agent_gates.GateResult(
            ok=False, reasons=("no merge-review record covers the change",)
        )
        summary = _submit_all([_gate_sub()], _ARGS)
        real_gates["evaluate"].assert_called_once()
        assert summary.opened == []
        [(_branch, why)] = summary.failed
        assert "no merge-review record covers the change" in why
        real_gates["push"].assert_not_called()
        real_gates["create"].assert_not_called()

    @pytest.mark.parametrize("recorded_by", ["human", "user", "automation", None])
    def test_template_mode_gates_whoever_recorded_the_state(
        self,
        recorded_by: str | None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _gates_pass: MagicMock,
    ) -> None:
        """A ready-state recorded by the human is gated like any other."""
        _as_mode(monkeypatch, "human")
        _write_workflow_state(tmp_path)
        state = LocalFileTransport(tmp_path, base="develop").read()
        assert state is not None
        state.recorded_by = recorded_by
        LocalFileTransport(tmp_path, base="develop").write(state)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
        ):
            assert main([]) == 0
        _gates_pass.assert_called_once()

    def test_human_cli_mode_runs_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, _gates_pass: MagicMock
    ) -> None:
        """The human's CLI emergency mode is gated too."""
        _as_mode(monkeypatch, "human")
        with (
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
        ):
            assert main(["--issue", "42", "--summary", "S", "--title", "fix: t"]) == 0
        _gates_pass.assert_called_once()

    def test_unqualifiable_ref_is_passed_on_for_the_gates_to_refuse(
        self, real_gates: dict[str, MagicMock]
    ) -> None:
        with patch(_MOD + ".github.current_repo", return_value="no-slash"):
            _submit_all([_gate_sub()], _ARGS)
        assert real_gates["evaluate"].call_args.kwargs["issue_refs"] == ["#42"]


# --- push mode (epic vergil-project/.github#1664, spec §5.4) ----------------------------

_PUSH_PR = "https://github.com/o/r/pull/7"
_PUSH_BRANCH = "feature/7-fix"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def push_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone on the PR's branch, as the user agent, with a remote-tracking ref for it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", _PUSH_BRANCH)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    _git(repo, "update-ref", f"refs/remotes/origin/{_PUSH_BRANCH}", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "fix")
    monkeypatch.chdir(repo)
    _as_mode(monkeypatch, "user")
    return repo


def _pushed(detail: str = "pushed it") -> pr_path.PushResult:
    return pr_path.PushResult(pushed=True, pr_url=_PUSH_PR, detail=detail)


class TestPushMode:
    """``vrg-submit-pr --push [--force-with-lease]``: a push to an open PR's branch."""

    def test_push_mode_pushes_passing_head(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + ".pr_path.push_to_open_pr", return_value=_pushed()) as push,
            patch(_MOD + "._run_template_mode") as template,
        ):
            assert main(["--push"]) == 0
        head = _git(push_worktree, "rev-parse", "HEAD")
        push.assert_called_once_with(_PUSH_BRANCH, head=head, lease=None)
        template.assert_not_called()
        assert "auto-merge stays on" in capsys.readouterr().out

    def test_push_mode_rebased_head_uses_lease(self, push_worktree: Path) -> None:
        with patch(_MOD + ".pr_path.push_to_open_pr", return_value=_pushed()) as push:
            assert main(["--push", "--force-with-lease"]) == 0
        lease = _git(push_worktree, "rev-parse", f"refs/remotes/origin/{_PUSH_BRANCH}")
        assert push.call_args.kwargs["lease"] == lease
        assert lease != _git(push_worktree, "rev-parse", "HEAD")

    def test_push_mode_lease_with_no_tracking_ref_is_refused(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _git(push_worktree, "update-ref", "-d", f"refs/remotes/origin/{_PUSH_BRANCH}")
        with patch(_MOD + ".pr_path.push_to_open_pr") as push:
            assert main(["--push", "--force-with-lease"]) == 1
        push.assert_not_called()
        assert "run `vrg-git fetch origin` first" in capsys.readouterr().err

    def test_push_mode_refuses_head_failing_gates(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        refused = pr_path.PushResult(
            pushed=False, pr_url=_PUSH_PR, detail="the gates refused the head abc: no record"
        )
        with patch(_MOD + ".pr_path.push_to_open_pr", return_value=refused):
            assert main(["--push"]) == 1
        err = capsys.readouterr().err
        assert f"refused ({_PUSH_PR}): the gates refused the head abc: no record" in err

    def test_push_mode_refuses_branch_with_no_open_develop_pr(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        none = pr_path.PushResult(pushed=False, pr_url="", detail=f"{_PUSH_BRANCH} has no open PR")
        with patch(_MOD + ".pr_path.push_to_open_pr", return_value=none):
            assert main(["--push"]) == 1
        assert f"refused: {_PUSH_BRANCH} has no open PR" in capsys.readouterr().err

    def test_push_mode_path_error_exits_one(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            _MOD + ".pr_path.push_to_open_pr", side_effect=pr_path.PRPathError("push failed")
        ):
            assert main(["--push"]) == 1
        assert "push failed; nothing was pushed" in capsys.readouterr().err

    def test_push_mode_leaves_auto_merge_on(self, push_worktree: Path) -> None:
        """Push mode neither arms, disarms nor finalizes: it only pushes through the path."""
        with (
            patch(_MOD + ".pr_path.push_to_open_pr", return_value=_pushed()),
            patch(_MOD + ".pr_path.arm") as arm,
            patch(_MOD + "._chain_finalize") as finalize,
        ):
            assert main(["--push"]) == 0
        arm.assert_not_called()
        finalize.assert_not_called()

    def test_push_mode_refuses_a_frozen_branch(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        frozen = freeze.FreezeCheck(frozen=True, message="the branch is frozen")
        with (
            patch(_MOD + ".freeze.check_worktree", return_value=frozen),
            patch(_MOD + ".pr_path.push_to_open_pr") as push,
        ):
            assert main(["--push"]) == 1
        push.assert_not_called()
        assert "the branch is frozen" in capsys.readouterr().err

    def test_push_mode_warns_on_an_unreadable_state_file(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        unreadable = freeze.FreezeCheck(frozen=False, read_error="a corrupt state file")
        with (
            patch(_MOD + ".freeze.check_worktree", return_value=unreadable),
            patch(_MOD + ".pr_path.push_to_open_pr", return_value=_pushed()),
        ):
            assert main(["--push"]) == 0
        assert "warning: a corrupt state file" in capsys.readouterr().err

    def test_push_mode_outside_a_worktree_is_refused(
        self,
        push_worktree: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        monkeypatch.chdir(plain)
        with patch(_MOD + ".pr_path.push_to_open_pr") as push:
            assert main(["--push"]) == 1
        push.assert_not_called()
        assert "cannot read the worktree's branch" in capsys.readouterr().err

    def test_push_mode_refuses_a_detached_head(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _git(push_worktree, "checkout", "-q", "--detach")
        with patch(_MOD + ".pr_path.push_to_open_pr") as push:
            assert main(["--push"]) == 1
        push.assert_not_called()
        assert "HEAD is detached" in capsys.readouterr().err

    def test_lease_without_push_is_refused(
        self, push_worktree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + ".pr_path.push_to_open_pr") as push,
            patch(_MOD + "._run_template_mode") as template,
        ):
            assert main(["--force-with-lease"]) == 1
        push.assert_not_called()
        template.assert_not_called()
        assert "pass it with --push" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("extra", "named"),
        [
            (["--all"], "--all"),
            (["--select", "7"], "--select"),
            (["--dry-run"], "--dry-run"),
            (["--base", "develop"], "--base"),
        ],
    )
    def test_push_mode_takes_no_other_modes_arguments(
        self,
        push_worktree: Path,
        capsys: pytest.CaptureFixture[str],
        extra: list[str],
        named: str,
    ) -> None:
        with patch(_MOD + ".pr_path.push_to_open_pr") as push:
            assert main(["--push", *extra]) == 1
        push.assert_not_called()
        assert f"takes no other mode's arguments, got {named}" in capsys.readouterr().err

    def test_push_mode_human_may_push(
        self, push_worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _as_mode(monkeypatch, "human")
        with patch(_MOD + ".pr_path.push_to_open_pr", return_value=_pushed()) as push:
            assert main(["--push", "--release"]) == 1
            assert main(["--push"]) == 0
        push.assert_called_once()

    def test_push_mode_audit_agent_is_refused(
        self, push_worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _as_mode(monkeypatch, "audit")
        with patch(_MOD + ".pr_path.push_to_open_pr") as push:
            assert main(["--push"]) == 1
        push.assert_not_called()
