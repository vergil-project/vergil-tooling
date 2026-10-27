"""Tests for vergil_tooling.lib.local_settings (epic vergil-project/.github#558)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import local_settings
from vergil_tooling.lib.local_settings import (
    AGENT_LINE,
    IN_SCOPE_ORGS,
    REFUSAL_TEMPLATE,
    WARNING_TEMPLATE,
    LocalSettingsError,
    enforce,
    find_violations,
    in_scope,
    local_settings_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

_IN_SCOPE_URL = "https://github.com/vergil-project/example.git"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(path: Path, origin: str | None = _IN_SCOPE_URL) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "develop")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base commit")
    if origin is not None:
        _git(path, "remote", "add", "origin", origin)
    return path


def _add_worktree(main: Path) -> Path:
    worktree = main / ".worktrees" / "issue-1-x"
    _git(main, "worktree", "add", "-b", "feature/1-x", str(worktree))
    return worktree


def _write_local(root: Path, text: str) -> Path:
    path = root / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _resolved(paths: list[Path]) -> set[Path]:
    return {p.resolve() for p in paths}


# --- the texts ---------------------------------------------------------------


def test_templates_match_the_global_constraints() -> None:
    assert REFUSAL_TEMPLATE.format(path="X") == (
        "refusing: X must be absent or exactly {} — move any rule it holds into the "
        "repo template or your user settings, then delete it "
        "(epic vergil-project/.github#558)."
    )
    assert WARNING_TEMPLATE.format(path="X") == (
        "warning: X must be absent or exactly {} — move any rule it holds into the "
        "repo template or your user settings, then delete it "
        "(epic vergil-project/.github#558)."
    )
    assert AGENT_LINE == "Agents: stop and ask the human."
    assert IN_SCOPE_ORGS == ("vergil-project", "logical-minds-foundry", "mnemosys-project")


# --- find_violations ---------------------------------------------------------


def test_absent_passes(tmp_path: Path) -> None:
    assert find_violations([tmp_path / ".claude" / "settings.local.json"]) == []


@pytest.mark.parametrize("text", ["{}", "{ }\n", "\n{\n}\n"])
def test_empty_object_passes(tmp_path: Path, text: str) -> None:
    path = _write_local(tmp_path, text)
    assert find_violations([path]) == []


def test_key_in_worktree_copy_violates(tmp_path: Path) -> None:
    path = _write_local(tmp_path, '{"permissions": {"allow": ["Bash(ls)"]}}')
    assert find_violations([path]) == [path]


@pytest.mark.parametrize("text", ["", "{", "not json", "[]", "null", '"{}"'])
def test_unparseable_violates(tmp_path: Path, text: str) -> None:
    path = _write_local(tmp_path, text)
    assert find_violations([path]) == [path]


def test_unreadable_violates(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.mkdir(parents=True)  # a directory: exists, cannot be read as a file
    assert find_violations([path]) == [path]


def test_invalid_utf8_violates(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{}")
    assert find_violations([path]) == [path]


# --- local_settings_paths ----------------------------------------------------


def test_paths_in_main_checkout(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    paths, notes = local_settings_paths(repo)
    assert _resolved(paths) == {(repo / ".claude" / "settings.local.json").resolve()}
    assert len(paths) == 1
    assert notes == []


def test_key_in_main_checkout_copy_violates_from_worktree(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(main)
    main_copy = _write_local(main, '{"env": {"X": "1"}}')
    paths, _notes = local_settings_paths(worktree)
    assert _resolved(paths) == {
        (worktree / ".claude" / "settings.local.json").resolve(),
        main_copy.resolve(),
    }
    assert _resolved(find_violations(paths)) == {main_copy.resolve()}


def test_subdirectory_copy_violates(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(main)
    sub = worktree / "src" / "pkg"
    sub.mkdir(parents=True)
    sub_copy = _write_local(worktree / "src", '{"model": "x"}')
    paths, _notes = local_settings_paths(sub)
    assert _resolved(paths) == {
        (sub / ".claude" / "settings.local.json").resolve(),
        sub_copy.resolve(),
        (worktree / ".claude" / "settings.local.json").resolve(),
        (main / ".claude" / "settings.local.json").resolve(),
    }
    assert _resolved(find_violations(paths)) == {sub_copy.resolve()}


def test_rev_parse_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(LocalSettingsError, match="git rev-parse failed"):
        local_settings_paths(tmp_path)


def test_cwd_outside_reported_root_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(local_settings, "_rev_parse", lambda _cwd: (elsewhere, elsewhere / ".git"))
    with pytest.raises(LocalSettingsError, match="does not contain it"):
        local_settings_paths(tmp_path)


def test_unowned_root_is_noted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(local_settings, "_current_uid", lambda: repo.stat().st_uid + 1)
    paths, notes = local_settings_paths(repo)
    assert len(paths) == 1
    assert len(notes) == 1
    assert "not owned by you" in notes[0]
    assert "not checked" in notes[0]


# --- in_scope ----------------------------------------------------------------


@pytest.mark.parametrize("org", IN_SCOPE_ORGS)
def test_in_scope_org(tmp_path: Path, org: str) -> None:
    repo = _init_repo(tmp_path / "repo", origin=f"git@github.com:{org}/x.git")
    assert in_scope(repo) is True


def test_out_of_scope_org_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/mq-rest-admin-project/x.git")
    _write_local(repo, '{"a": 1}')
    assert in_scope(repo) is False
    assert enforce(repo) == 0
    assert capsys.readouterr().err == ""


def test_no_origin_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path / "repo", origin=None)
    _write_local(repo, '{"a": 1}')
    assert in_scope(repo) is False
    assert enforce(repo) == 0
    assert capsys.readouterr().err == ""


def test_not_a_git_repo_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_local(tmp_path, '{"a": 1}')
    assert in_scope(tmp_path) is False
    assert enforce(tmp_path) == 0
    assert capsys.readouterr().err == ""


# --- enforce -----------------------------------------------------------------


def _agent_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "user") -> None:
    mode_file = tmp_path / "vergil-config" / "identity-mode"
    mode_file.parent.mkdir(parents=True)
    mode_file.write_text(f"{mode}\n")
    monkeypatch.setattr(local_settings.identity_mode, "_MODE_FILE", str(mode_file))


def test_absent_everywhere_is_silent_for_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(main)
    _agent_vm(tmp_path, monkeypatch)
    assert enforce(worktree) == 0
    assert capsys.readouterr().err == ""


def test_empty_object_is_silent_for_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_local(repo, "{}\n")
    _agent_vm(tmp_path, monkeypatch)
    assert enforce(repo) == 0
    assert capsys.readouterr().err == ""


def test_agent_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(main)
    main_copy = _write_local(main, '{"a": 1}')
    _agent_vm(tmp_path, monkeypatch)
    assert enforce(worktree) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines == [REFUSAL_TEMPLATE.format(path=main_copy.resolve()), AGENT_LINE]


def test_agent_refuses_listing_every_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main = _init_repo(tmp_path / "repo")
    worktree = _add_worktree(main)
    main_copy = _write_local(main, '{"a": 1}')
    wt_copy = _write_local(worktree, "garbage")
    _agent_vm(tmp_path, monkeypatch)
    assert enforce(worktree) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[-1] == AGENT_LINE
    assert set(lines[:-1]) == {
        REFUSAL_TEMPLATE.format(path=main_copy.resolve()),
        REFUSAL_TEMPLATE.format(path=wt_copy.resolve()),
    }


def test_env_human_on_agent_vm_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_local(repo, '{"a": 1}')
    _agent_vm(tmp_path, monkeypatch, mode="user")
    monkeypatch.setenv("VRG_IDENTITY_MODE", "human")
    assert enforce(repo) == 1
    assert capsys.readouterr().err.splitlines()[-1] == AGENT_LINE


def test_no_agent_signal_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path / "repo")
    copy = _write_local(repo, '{"a": 1}')
    assert enforce(repo) == 0
    assert capsys.readouterr().err.splitlines() == [WARNING_TEMPLATE.format(path=copy.resolve())]


def test_rev_parse_failure_in_scope_is_an_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(local_settings, "in_scope", lambda _cwd: True)
    assert enforce(tmp_path) == 1
    assert "git rev-parse failed" in capsys.readouterr().err


def test_notes_are_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(local_settings, "_current_uid", lambda: repo.stat().st_uid + 1)
    assert enforce(repo) == 0
    err = capsys.readouterr().err
    assert err.startswith("note: ")
    assert "not checked" in err
