"""Tests for vergil_tooling.bin.vrg_reword."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_reword
from vergil_tooling.bin.vrg_reword import (
    _dispatch_editor,
    _is_protected_branch,
    _msg_edit,
    _seq_edit,
    main,
    parse_args,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_reword"


# --------------------------------------------------------------------------
# parse_args
# --------------------------------------------------------------------------


def test_parse_args_required() -> None:
    args = parse_args(["abc123", "--type", "feat", "--scope", "core", "--message", "do thing"])
    assert args.sha == "abc123"
    assert args.commit_type == "feat"
    assert args.scope == "core"
    assert args.message == "do thing"
    assert args.allow_foreign_author is False
    assert args.no_push is False


def test_parse_args_missing_sha() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--type", "feat", "--scope", "core", "--message", "x"])


def test_parse_args_invalid_type() -> None:
    with pytest.raises(SystemExit):
        parse_args(["abc", "--type", "bogus", "--scope", "core", "--message", "x"])


# --------------------------------------------------------------------------
# protected-branch helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["develop", "main", "release/1.2.3"])
def test_is_protected_branch_true(branch: str) -> None:
    assert _is_protected_branch(branch) is True


@pytest.mark.parametrize("branch", ["feature/1-x", "bugfix/9-y", "chore/3-z"])
def test_is_protected_branch_false(branch: str) -> None:
    assert _is_protected_branch(branch) is False


# --------------------------------------------------------------------------
# scripted editors
# --------------------------------------------------------------------------


def test_seq_edit_flips_target_pick_to_reword(tmp_path: Path) -> None:
    todo = tmp_path / "todo"
    todo.write_text("pick aaaaaaa first\npick bbbbbbb second\npick ccccccc third\n")
    assert _seq_edit(todo, "bbbbbbbdeadbeef") == 0
    lines = todo.read_text().splitlines()
    assert lines == ["pick aaaaaaa first", "reword bbbbbbb second", "pick ccccccc third"]


def test_seq_edit_missing_target_fails(tmp_path: Path) -> None:
    todo = tmp_path / "todo"
    todo.write_text("pick aaaaaaa first\n")
    assert _seq_edit(todo, "ffffffffff") == 1


def test_msg_edit_overwrites_commit_message(tmp_path: Path) -> None:
    source = tmp_path / "new.msg"
    source.write_text("feat(core): reworded\n")
    target = tmp_path / "COMMIT_EDITMSG"
    target.write_text("old message\n")
    assert _msg_edit(target, source) == 0
    assert target.read_text() == "feat(core): reworded\n"


def test_dispatch_editor_routes_seq(tmp_path: Path) -> None:
    todo = tmp_path / "todo"
    todo.write_text("pick aaaaaaa first\n")
    with patch.dict("os.environ", {"VRG_REWORD_TARGET": "aaaaaaadead"}, clear=False):
        rc = _dispatch_editor(["--seq-edit", str(todo)])
    assert rc == 0
    assert todo.read_text().startswith("reword ")


def test_dispatch_editor_routes_msg(tmp_path: Path) -> None:
    source = tmp_path / "new.msg"
    source.write_text("feat(core): x\n")
    target = tmp_path / "COMMIT_EDITMSG"
    target.write_text("old\n")
    with patch.dict("os.environ", {"VRG_REWORD_MSG_FILE": str(source)}, clear=False):
        rc = _dispatch_editor(["--msg-edit", str(target)])
    assert rc == 0
    assert target.read_text() == "feat(core): x\n"


def test_dispatch_editor_returns_none_for_normal_args() -> None:
    assert _dispatch_editor(["abc", "--type", "feat"]) is None
    assert _dispatch_editor([]) is None


def test_dispatch_editor_seq_without_env_target_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VRG_REWORD_TARGET", raising=False)
    assert _dispatch_editor(["--seq-edit", str(tmp_path / "todo")]) == 1


def test_dispatch_editor_msg_without_env_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VRG_REWORD_MSG_FILE", raising=False)
    assert _dispatch_editor(["--msg-edit", str(tmp_path / "msg")]) == 1


def test_main_dispatches_editor_before_argparse(tmp_path: Path) -> None:
    """The bare editor flags must not reach argparse (which would reject them)."""
    todo = tmp_path / "todo"
    todo.write_text("pick aaaaaaa first\n")
    with patch.dict("os.environ", {"VRG_REWORD_TARGET": "aaaaaaadead"}, clear=False):
        assert main(["--seq-edit", str(todo)]) == 0


def test_main_reads_sys_argv_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main(None) falls back to sys.argv."""
    todo = tmp_path / "todo"
    todo.write_text("pick aaaaaaa first\n")
    monkeypatch.setenv("VRG_REWORD_TARGET", "aaaaaaadead")
    monkeypatch.setattr("sys.argv", ["vrg-reword", "--seq-edit", str(todo)])
    assert main() == 0


# --------------------------------------------------------------------------
# guard validation (mocked)
# --------------------------------------------------------------------------


def _args(
    *,
    sha: str = "deadbeef",
    body: str = "",
    allow_foreign_author: bool = False,
    no_push: bool = False,
) -> argparse.Namespace:
    argv = [sha, "--type", "feat", "--scope", "core", "--message", "do thing"]
    if body:
        argv += ["--body", body]
    if allow_foreign_author:
        argv.append("--allow-foreign-author")
    if no_push:
        argv.append("--no-push")
    return parse_args(argv)


def test_validate_rejects_protected_branch() -> None:
    with patch(f"{_MOD}.git.current_branch", return_value="develop"):
        rc, sha, branch = vrg_reword._validate(_args())
    assert rc == 1
    assert sha == ""


def test_validate_rejects_unknown_sha() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", return_value=None),
    ):
        rc, sha, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_rejects_commit_not_on_branch() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", return_value="fullsha"),
        patch(f"{_MOD}._is_ancestor", return_value=False),
    ):
        rc, _, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_rejects_ancestor_of_base() -> None:
    # reachable from HEAD, and also an ancestor of base → shared history.
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, True]),
    ):
        rc, _, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_rejects_unresolvable_base() -> None:
    # target resolves, but neither origin/develop nor develop exists.
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=["fullsha", None, None]),
        patch(f"{_MOD}._is_ancestor", return_value=True),
    ):
        rc, sha, _ = vrg_reword._validate(_args())
    assert rc == 1
    assert sha == ""


def test_validate_rejects_foreign_author_without_override() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, False]),
        patch(f"{_MOD}._author_email", return_value="someone@else.com"),
        patch(f"{_MOD}._current_identity_email", return_value="me@here.com"),
    ):
        rc, _, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_allows_foreign_author_with_override() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, False]),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
    ):
        rc, sha, branch = vrg_reword._validate(_args(allow_foreign_author=True))
    assert rc == 0
    assert sha == "fullsha"
    assert branch == "feature/1-x"


def test_validate_rejects_missing_identity() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, False]),
        patch(f"{_MOD}._author_email", return_value="me@here.com"),
        patch(f"{_MOD}._current_identity_email", return_value=""),
    ):
        rc, _, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_rejects_dirty_tree() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, False]),
        patch(f"{_MOD}._author_email", return_value="me@here.com"),
        patch(f"{_MOD}._current_identity_email", return_value="me@here.com"),
        patch(f"{_MOD}.git.working_tree_status", return_value=" M file.py"),
    ):
        rc, _, _ = vrg_reword._validate(_args())
    assert rc == 1


def test_validate_passes_all_guards() -> None:
    with (
        patch(f"{_MOD}.git.current_branch", return_value="feature/1-x"),
        patch(f"{_MOD}._rev_parse", side_effect=lambda ref: "fullsha"),
        patch(f"{_MOD}._is_ancestor", side_effect=[True, False]),
        patch(f"{_MOD}._author_email", return_value="me@here.com"),
        patch(f"{_MOD}._current_identity_email", return_value="ME@here.com"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
    ):
        rc, sha, branch = vrg_reword._validate(_args())
    assert rc == 0
    assert sha == "fullsha"


# --------------------------------------------------------------------------
# body auto-close guard in main()
# --------------------------------------------------------------------------


def test_main_rejects_autoclose_body() -> None:
    rc = main(["abc", "--type", "feat", "--scope", "core", "--message", "x", "--body", "Closes #5"])
    assert rc == 1


# --------------------------------------------------------------------------
# push behavior
# --------------------------------------------------------------------------


def test_maybe_push_skips_when_no_push() -> None:
    assert vrg_reword._maybe_push("feature/1-x", no_push=True) == 0


def test_maybe_push_skips_when_no_remote_branch() -> None:
    with patch(f"{_MOD}._rev_parse", return_value=None):
        assert vrg_reword._maybe_push("feature/1-x", no_push=False) == 0


def test_maybe_push_runs_force_with_lease() -> None:
    with (
        patch(f"{_MOD}._rev_parse", return_value="remotesha"),
        patch(f"{_MOD}.git.run") as run,
    ):
        rc = vrg_reword._maybe_push("feature/1-x", no_push=False)
    assert rc == 0
    run.assert_called_once_with("push", "--force-with-lease", "origin", "feature/1-x")


def test_maybe_push_reports_rejected_push() -> None:
    err = subprocess.CalledProcessError(1, ("git", "push"))
    with (
        patch(f"{_MOD}._rev_parse", return_value="remotesha"),
        patch(f"{_MOD}.git.run", side_effect=err),
    ):
        rc = vrg_reword._maybe_push("feature/1-x", no_push=False)
    assert rc == 1


# --------------------------------------------------------------------------
# rebase failure paths
# --------------------------------------------------------------------------


def test_run_reword_rebase_reports_failure() -> None:
    failed = subprocess.CompletedProcess(args=[], returncode=1)
    with patch(f"{_MOD}.subprocess.run", return_value=failed) as run:
        rc = vrg_reword._run_reword_rebase("deadbeef", "feat(core): x\n")
    assert rc == 1
    # The rebase is driven with the scripted editors via env.
    _, kwargs = run.call_args
    assert kwargs["env"]["GIT_SEQUENCE_EDITOR"].endswith("--seq-edit")
    assert kwargs["env"]["GIT_EDITOR"].endswith("--msg-edit")


def test_main_returns_rebase_failure_before_push() -> None:
    with (
        patch(f"{_MOD}._validate", return_value=(0, "fullsha", "feature/1-x")),
        patch(f"{_MOD}._later_commits", return_value=[]),
        patch(f"{_MOD}._run_reword_rebase", return_value=1),
        patch(f"{_MOD}._maybe_push") as push,
    ):
        rc = main(["fullsha", "--type", "feat", "--scope", "core", "--message", "x"])
    assert rc == 1
    push.assert_not_called()


# --------------------------------------------------------------------------
# end-to-end: a real git repo exercising the scripted rebase
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.fixture
def reword_repo(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A repo with a `develop` base and a feature branch of three commits.

    The target (B) is authored by the current identity (so the own-identity
    guard passes). The *later* commit (C) carries a distinct author and a
    distinct committer, so the test can prove rewording B preserves C's
    author while re-stamping its committer. Returns (repo, sha_of_B).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.name", "Me")
    _git(repo, "config", "user.email", "me@here.com")

    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "chore(core): base on develop")

    _git(repo, "switch", "-q", "-c", "feature/1-x")

    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "feat(core): commit A")

    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "feat(core): bad B message")
    sha_b = _git(repo, "rev-parse", "HEAD")

    # Commit C: distinct author and committer, both different from "me".
    c_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Other",
        "GIT_AUTHOR_EMAIL": "other@x.com",
        "GIT_COMMITTER_NAME": "Orig Committer",
        "GIT_COMMITTER_EMAIL": "orig-committer@x.com",
    }
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "feat(core): commit C", env=c_env)

    yield repo, sha_b


def test_reword_midchain_commit_end_to_end(
    reword_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha_b = reword_repo
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VRG_CO_AUTHOR", "Claude <noreply@anthropic.com>")

    rc = main(
        [
            sha_b,
            "--type",
            "feat",
            "--scope",
            "core",
            "--message",
            "good B message",
            "--no-push",
        ]
    )
    assert rc == 0

    subjects = _git(repo, "log", "--format=%s", "develop..HEAD").splitlines()
    # Newest first: C, reworded B, A.
    assert subjects == [
        "feat(core): commit C",
        "feat(core): good B message",
        "feat(core): commit A",
    ]

    # The reworded commit carries the co-author trailer (standards path).
    b_body = _git(repo, "log", "--format=%B", "-1", "HEAD~1")
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in b_body

    # Commit C (applied after B) keeps its author but gets a new committer —
    # the design note made observable.
    c_author = _git(repo, "log", "--format=%ae", "-1", "HEAD")
    c_committer = _git(repo, "log", "--format=%ce", "-1", "HEAD")
    assert c_author == "other@x.com"
    assert c_committer == "me@here.com"

    # The original B sha is gone; history was rewritten.
    assert sha_b not in _git(repo, "rev-parse", "HEAD~1")


def test_reword_head_commit_end_to_end(
    reword_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reword HEAD (no later commits) — exercises the no-note and override paths.

    HEAD here (commit C) is authored by a foreign identity, so the run also
    needs ``--allow-foreign-author`` end-to-end.
    """
    repo, _ = reword_repo
    monkeypatch.chdir(repo)
    monkeypatch.delenv("VRG_CO_AUTHOR", raising=False)
    head = _git(repo, "rev-parse", "HEAD")

    rc = main(
        [
            head,
            "--type",
            "feat",
            "--scope",
            "core",
            "--message",
            "renamed C",
            "--allow-foreign-author",
            "--no-push",
        ]
    )
    assert rc == 0

    assert _git(repo, "log", "--format=%s", "-1", "HEAD") == "feat(core): renamed C"
    # No co-author was configured, so no trailer is stamped.
    assert "Co-Authored-By" not in _git(repo, "log", "--format=%B", "-1", "HEAD")


def test_reword_refuses_shared_history_end_to_end(
    reword_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = reword_repo
    monkeypatch.chdir(repo)
    base_sha = _git(repo, "rev-parse", "develop")

    rc = main(
        [base_sha, "--type", "chore", "--scope", "core", "--message", "rewrite base", "--no-push"]
    )
    assert rc == 1
    # develop's tip is untouched.
    assert _git(repo, "rev-parse", "develop") == base_sha
