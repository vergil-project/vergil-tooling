"""Tests for vergil_tooling.bin.vrg_branch_sweep (issue #3333)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin.vrg_branch_sweep import main, parse_args
from vergil_tooling.lib import branch_sweep, github
from vergil_tooling.lib.branch_sweep import Action, Outcome, PullRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

REPO = "acme/widgets"


@pytest.fixture
def orgs(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record the org each sweep is scoped to, without minting tokens."""
    seen: list[str | None] = []

    @contextlib.contextmanager
    def fake_target_org(org: str | None) -> Generator[None]:
        seen.append(org)
        yield

    monkeypatch.setattr(github, "target_org", fake_target_org)
    return seen


def _sweep_returning(
    outcomes: list[Outcome], calls: list[tuple[str, bool]]
) -> Callable[..., list[Outcome]]:
    def fake(repo: str, *, apply: bool) -> list[Outcome]:
        calls.append((repo, apply))
        return outcomes

    return fake


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.repo is None
    assert args.apply is False


def test_parse_args_rejects_bad_repo(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repo", "not-a-repo"])
    assert "expected OWNER/NAME" in capsys.readouterr().err


def test_dry_run_prints_lines_and_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], orgs: list[str | None]
) -> None:
    calls: list[tuple[str, bool]] = []
    outcomes = [
        Outcome(
            "feature/1-x",
            PullRequest(5, "MERGED", "a" * 40),
            Action.WOULD_DELETE,
            "PR #5 merged",
        ),
        Outcome("feature/2-y", None, Action.KEPT, "no PR"),
    ]
    monkeypatch.setattr(branch_sweep, "sweep", _sweep_returning(outcomes, calls))
    assert main(["--repo", REPO]) == 0
    out = capsys.readouterr().out
    assert f"Sweeping finished branches in {REPO} (dry run)" in out
    assert "feature/1-x  #5      MERGED  would delete (PR #5 merged)" in out
    assert "feature/2-y  -       -       kept (no PR)" in out
    assert "1 would delete, 1 kept, 0 errors (dry run" in out
    assert calls == [(REPO, False)]
    assert orgs == ["acme"]


def test_apply_defaults_to_current_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], orgs: list[str | None]
) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(github, "current_repo", lambda: "other/thing")
    monkeypatch.setattr(branch_sweep, "sweep", _sweep_returning([], calls))
    assert main(["--apply"]) == 0
    assert calls == [("other/thing", True)]
    assert orgs == ["other"]
    assert "(apply)" in capsys.readouterr().out


def test_branch_errors_exit_nonzero_after_reporting_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], orgs: list[str | None]
) -> None:
    del orgs
    outcomes = [
        Outcome("feature/1-x", None, Action.ERROR, "HTTP 502"),
        Outcome("feature/2-y", None, Action.DELETED, "PR #6 closed"),
    ]
    monkeypatch.setattr(branch_sweep, "sweep", _sweep_returning(outcomes, []))
    assert main(["--repo", REPO, "--apply"]) == 1
    captured = capsys.readouterr()
    assert "feature/1-x" in captured.out
    assert "deleted (PR #6 closed)" in captured.out
    assert "1 branch(es) failed: feature/1-x" in captured.err


def test_listing_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], orgs: list[str | None]
) -> None:
    del orgs

    def boom(repo: str, *, apply: bool) -> list[Outcome]:
        del repo, apply
        raise github.GitHubAPIError(1, "gh api", "", "HTTP 404:\nNot Found")

    monkeypatch.setattr(branch_sweep, "sweep", boom)
    assert main(["--repo", REPO]) == 1
    err = capsys.readouterr().err
    assert f"cannot list branches of {REPO}" in err
    assert "HTTP 404: Not Found" in err


def test_missing_token_uses_cli_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], orgs: list[str | None]
) -> None:
    del orgs

    def boom(repo: str, *, apply: bool) -> list[Outcome]:
        del repo, apply
        raise github.MissingGitHubTokenError(1, "gh", "", "set GH_TOKEN")

    monkeypatch.setattr(branch_sweep, "sweep", boom)
    with pytest.raises(SystemExit) as exc:
        main(["--repo", REPO])
    assert exc.value.code == 1
    assert "vrg-branch-sweep: no GitHub token available" in capsys.readouterr().err
