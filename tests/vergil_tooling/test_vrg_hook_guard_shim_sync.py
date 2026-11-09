"""Tests for the ``vrg-hook-guard-shim-sync`` single-repo applicator CLI (#4109)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from vergil_tooling.bin import vrg_hook_guard_shim_sync
from vergil_tooling.lib import repo_config
from vergil_tooling.lib.repo_config import render_hook_guard_shim

_STALE = b"#!/usr/bin/env bash\nexec vrg-hook-guard\n"


def _managed() -> bytes:
    return render_hook_guard_shim().encode("utf-8")


def _shim(root: Path, data: bytes | None = None, mode: int = 0o755) -> Path:
    path = root / ".claude" / "hooks" / "guard.sh"
    if data is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    return path


def _audit_items(root: Path) -> list[repo_config.DiffItem]:
    items: list[repo_config.DiffItem] = []
    repo_config._check_hook_guard_shim(root, items)
    return items


def _main(*args: str) -> int:
    return vrg_hook_guard_shim_sync.main(list(args))


# --- --check ---------------------------------------------------------------


def test_check_missing_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _main("--repo", str(tmp_path), "--check") == 1
    assert "missing .claude/hooks/guard.sh" in capsys.readouterr().err


def test_check_is_default_and_writes_nothing(tmp_path: Path) -> None:
    path = _shim(tmp_path)
    assert _main("--repo", str(tmp_path)) == 1
    assert not path.exists()


def test_check_stale_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _shim(tmp_path, _STALE)
    assert _main("--repo", str(tmp_path), "--check") == 1
    assert ".claude/hooks/guard.sh differs from the packaged shim" in capsys.readouterr().err
    assert path.read_bytes() == _STALE


def test_check_not_executable_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _shim(tmp_path, _managed(), mode=0o644)
    assert _main("--repo", str(tmp_path), "--check") == 1
    assert ".claude/hooks/guard.sh is not executable" in capsys.readouterr().err


def test_check_match_exits_0(tmp_path: Path) -> None:
    _shim(tmp_path, _managed())
    assert _main("--repo", str(tmp_path), "--check") == 0
    assert _audit_items(tmp_path) == []


def test_check_unreadable_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable shim is an error (exit 2), never drift (1) or in sync (0)."""
    path = _shim(tmp_path, _managed())
    real_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self == path:
            raise PermissionError("Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)
    assert _main("--repo", str(tmp_path), "--check") == 2
    assert _main("--repo", str(tmp_path), "--write") == 2
    err = capsys.readouterr().err
    assert "vrg-hook-guard-shim-sync: cannot read .claude/hooks/guard.sh: Permission denied" in err


def test_missing_packaged_shim_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing packaged data file raises; it is never read as an empty shim."""
    _shim(tmp_path, _managed())

    def missing(name: str) -> str:
        raise FileNotFoundError(name)

    monkeypatch.setattr(repo_config, "_read_data_file", missing)
    with pytest.raises(FileNotFoundError, match="hook_guard_shim.sh"):
        _main("--repo", str(tmp_path), "--check")


def test_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _shim(tmp_path, _managed())
    monkeypatch.chdir(tmp_path)
    assert _main("--check") == 0


# --- --write ---------------------------------------------------------------


def test_write_creates_missing_file_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _shim(tmp_path)
    assert _main("--repo", str(tmp_path), "--write") == 0
    assert path.read_bytes() == _managed()
    assert capsys.readouterr().out.strip() == "wrote .claude/hooks/guard.sh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert [p.name for p in path.parent.iterdir()] == ["guard.sh"]


def test_write_replaces_stale_shim(tmp_path: Path) -> None:
    path = _shim(tmp_path, _STALE, mode=0o644)
    assert _main("--repo", str(tmp_path), "--write") == 0
    assert path.read_bytes() == _managed()
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_write_replaces_crlf_copy(tmp_path: Path) -> None:
    """Bytes are compared and written as they are: a CRLF copy is replaced with LF."""
    path = _shim(tmp_path, _managed().replace(b"\n", b"\r\n"))
    assert _main("--repo", str(tmp_path), "--write") == 0
    assert path.read_bytes() == _managed()


def test_write_in_sync_prints_already_in_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _shim(tmp_path, _managed())
    before = path.stat().st_mtime_ns
    assert _main("--repo", str(tmp_path), "--write") == 0
    assert capsys.readouterr().out.strip() == "already in sync"
    assert path.stat().st_mtime_ns == before


def test_write_failure_keeps_original_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replace leaves the old file intact, no temp file behind, and raises."""
    path = _shim(tmp_path, _STALE)

    def failing_replace(self: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vrg_hook_guard_shim_sync.Path, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        vrg_hook_guard_shim_sync._run_write(tmp_path)
    assert path.read_bytes() == _STALE
    assert [p.name for p in path.parent.iterdir()] == ["guard.sh"]


@pytest.mark.parametrize("case", ["missing", "stale", "not executable", "crlf", "match"])
def test_check_predicts_write_and_write_passes_audit(tmp_path: Path, case: str) -> None:
    """``--check`` drifts exactly when ``--write`` changes the file; after it the audit passes."""
    shapes: dict[str, tuple[bytes | None, int]] = {
        "missing": (None, 0o755),
        "stale": (_STALE, 0o755),
        "not executable": (_managed(), 0o644),
        "crlf": (_managed().replace(b"\n", b"\r\n"), 0o755),
        "match": (_managed(), 0o755),
    }
    data, mode = shapes[case]
    path = _shim(tmp_path, data, mode)
    before = (path.read_bytes(), path.stat().st_mode) if path.exists() else None

    drift = _main("--check", "--repo", str(tmp_path))
    assert _main("--write", "--repo", str(tmp_path)) == 0
    assert drift == (1 if (path.read_bytes(), path.stat().st_mode) != before else 0)
    assert drift == (0 if case == "match" else 1)
    assert _audit_items(tmp_path) == []
    assert _main("--check", "--repo", str(tmp_path)) == 0


def test_check_and_write_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _main("--repo", str(tmp_path), "--check", "--write")
    assert exc.value.code == 2
