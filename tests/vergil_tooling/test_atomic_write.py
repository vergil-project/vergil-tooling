"""Tests for the atomic file-replacement command (lib/atomic_write, #4035)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from tests.vergil_tooling.conftest import failing_command
from vergil_tooling.lib.atomic_write import atomic_write_cmd

if TYPE_CHECKING:
    from pathlib import Path


def _run(cmd: str, content: str, home: Path, fake_bin: Path | None = None) -> None:
    path = "/usr/bin:/bin" if fake_bin is None else f"{fake_bin}:/usr/bin:/bin"
    subprocess.run(  # noqa: S603
        ["bash", "-c", cmd],  # noqa: S607
        input=content,
        capture_output=True,
        text=True,
        check=True,
        env={"HOME": str(home), "PATH": path},
    )


def test_command_shape() -> None:
    # A temp file beside the target, its final mode set there, then a rename; any
    # failure removes the temp file and exits non-zero.
    assert atomic_write_cmd("~/.config/vergil/app.pem", 0o600) == (
        "tmp=$(mktemp ~/.config/vergil/.app.pem.XXXXXX) && "
        '{ cat > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" ~/.config/vergil/app.pem'
        ' || { rm -f "$tmp"; false; }; }'
    )


def test_absolute_path_is_kept_absolute() -> None:
    cmd = atomic_write_cmd("/etc/apt/preferences.d/vergil-tooling", 0o644)
    assert "mktemp /etc/apt/preferences.d/.vergil-tooling.XXXXXX" in cmd
    assert 'mv -f "$tmp" /etc/apt/preferences.d/vergil-tooling ' in cmd


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o444])
def test_writes_new_file_with_its_mode(mode: int, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()

    _run(atomic_write_cmd("~/d/f.txt", mode), "content\n", tmp_path)

    written = tmp_path / "d" / "f.txt"
    assert written.read_text() == "content\n"
    assert written.stat().st_mode & 0o777 == mode
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["f.txt"]


def test_replaces_a_read_only_file(tmp_path: Path) -> None:
    # The rename needs only the directory to be writable, so a locked file is
    # replaced without being reopened.
    target = tmp_path / "f"
    target.write_text("old\n")
    target.chmod(0o444)

    _run(atomic_write_cmd(str(target), 0o644), "new\n", tmp_path)

    assert target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_quotes_a_path_the_shell_would_split(tmp_path: Path) -> None:
    (tmp_path / "a dir").mkdir()

    _run(atomic_write_cmd("~/a dir/it's $x.md", 0o644), "spaced\n", tmp_path)

    assert (tmp_path / "a dir" / "it's $x.md").read_text() == "spaced\n"
    assert [p.name for p in (tmp_path / "a dir").iterdir()] == ["it's $x.md"]


@pytest.mark.parametrize("failing", ["cat", "chmod", "mv"])
def test_failed_write_leaves_the_original_and_no_temp_file(failing: str, tmp_path: Path) -> None:
    # A write that breaks part-way (a fake command that writes a fragment, then
    # fails) exits non-zero, keeps the file exactly as it was, mode included, and
    # removes its temp file.
    target_dir = tmp_path / "d"
    target_dir.mkdir()
    target = target_dir / "f"
    target.write_text("original\n")
    target.chmod(0o600)
    fake_bin = tmp_path / "bin"
    failing_command(fake_bin, failing)

    with pytest.raises(subprocess.CalledProcessError):
        _run(atomic_write_cmd("~/d/f", 0o644), "new content that never lands\n", tmp_path, fake_bin)

    assert target.read_text() == "original\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in target_dir.iterdir()] == ["f"]


def test_missing_directory_fails_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _run(atomic_write_cmd("~/absent/f", 0o644), "x\n", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path", ["relative/f", "f", "~user/f", "~/d/"])
def test_rejects_a_path_it_cannot_place(path: str) -> None:
    with pytest.raises(ValueError, match="atomic write path"):
        atomic_write_cmd(path, 0o644)


@pytest.mark.parametrize("mode", [-1, 0o1000, 0o4755])
def test_rejects_a_mode_that_is_not_a_permission_mode(mode: int) -> None:
    with pytest.raises(ValueError, match="not a permission mode"):
        atomic_write_cmd("~/f", mode)
