"""Tests for the ``vrg-claude-md-sync`` single-repo applicator CLI (#3868)."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_claude_md_sync
from vergil_tooling.lib import repo_config
from vergil_tooling.lib.repo_config import (
    CLAUDE_MD_MARKER_BEGIN,
    CLAUDE_MD_MARKER_END,
    ClaudeMdMarkerError,
    claude_md_marker_lines,
    load_claude_md_template,
)

if TYPE_CHECKING:
    from pathlib import Path

BEGIN = CLAUDE_MD_MARKER_BEGIN
END = CLAUDE_MD_MARKER_END

HEAD = "# CLAUDE.md\n\nRepo-local intro.  \n\n"
TAIL = "\n## Project overview\n\nRepo-local tail, no final newline"


def _template() -> str:
    return load_claude_md_template()


def _doc(region: str, head: str = HEAD, tail: str = TAIL) -> str:
    return f"{head}{BEGIN}\n{region}{END}\n{tail}"


def _write(root: Path, text: str | bytes) -> Path:
    path = root / "CLAUDE.md"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


def _audit_items(root: Path) -> list[repo_config.DiffItem]:
    items: list[repo_config.DiffItem] = []
    repo_config._check_claude_md(root, items)
    return items


def _main(root: Path, *mode: str) -> int:
    return vrg_claude_md_sync.main(["--repo", str(root), *mode])


def _stale_region() -> str:
    """The template with one line changed and one line appended."""
    lines = _template().splitlines(keepends=True)
    lines[2] = "An outdated rule that the template no longer carries.\n"
    return "".join(lines) + "A line the template does not have.\n"


# --- the shared marker parser ------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (["text"], "no template markers"),
        ([BEGIN, BEGIN, END], "multiple begin markers"),
        ([BEGIN, END, END], "multiple end markers"),
        ([BEGIN], "begin marker without end marker"),
        ([END], "end marker without begin marker"),
        ([END, "x", BEGIN], "end marker before begin marker"),
    ],
)
def test_marker_parser_rejects_malformed(lines: list[str], message: str) -> None:
    with pytest.raises(ClaudeMdMarkerError, match=f"^{message}$"):
        claude_md_marker_lines(lines)


def test_marker_parser_returns_indexes_and_strips_whitespace() -> None:
    assert claude_md_marker_lines(["a", f"  {BEGIN} ", "b", END, "c"]) == (1, 3)


# --- --check -----------------------------------------------------------------


def test_check_match_exits_0(tmp_path: Path) -> None:
    _write(tmp_path, _doc(_template()))
    assert _main(tmp_path, "--check") == 0


def test_check_is_default_and_writes_nothing(tmp_path: Path) -> None:
    text = _doc(_stale_region())
    path = _write(tmp_path, text)
    assert _main(tmp_path) == 1
    assert path.read_bytes() == text.encode("utf-8")


def test_check_drift_names_the_first_divergence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, _doc(_stale_region()))
    assert _main(tmp_path, "--check") == 1
    err = capsys.readouterr().err
    assert "CLAUDE.md template region differs from the template" in err
    assert "template line 3" in err


def test_check_ignores_blank_lines_next_to_markers(tmp_path: Path) -> None:
    """The audit's comparison: blank padding inside the markers is not drift."""
    _write(tmp_path, _doc(f"\n\n{_template()}\n"))
    assert _main(tmp_path, "--check") == 0
    assert _audit_items(tmp_path) == []


def test_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, _doc(_template()))
    monkeypatch.chdir(tmp_path)
    assert vrg_claude_md_sync.main(["--check"]) == 0


# --- cannot sync: a hard, named failure in both modes ------------------------


@pytest.mark.parametrize("mode", ["--check", "--write"])
def test_missing_claude_md_is_a_named_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    assert _main(tmp_path, mode) == 2
    assert "vrg-claude-md-sync: cannot sync: missing CLAUDE.md" in capsys.readouterr().err
    assert not (tmp_path / "CLAUDE.md").exists()


@pytest.mark.parametrize("mode", ["--check", "--write"])
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# CLAUDE.md\n\nNo markers here.\n", "no template markers"),
        (f"{BEGIN}\nx\n{BEGIN}\ny\n{END}\n", "multiple begin markers"),
        (f"{BEGIN}\nx\n{END}\ny\n{END}\n", "multiple end markers"),
        (f"{BEGIN}\nx\n", "begin marker without end marker"),
        (f"x\n{END}\n", "end marker without begin marker"),
        (f"{END}\nx\n{BEGIN}\n", "end marker before begin marker"),
    ],
)
def test_malformed_markers_are_a_named_error_and_write_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    text: str,
    message: str,
) -> None:
    path = _write(tmp_path, text)
    assert _main(tmp_path, mode) == 2
    err = capsys.readouterr().err
    assert f"vrg-claude-md-sync: cannot sync: CLAUDE.md has {message}" in err
    assert path.read_bytes() == text.encode("utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["CLAUDE.md"]


# --- --write -----------------------------------------------------------------


def test_write_replaces_only_the_region(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _doc(_stale_region()))
    path.chmod(0o664)
    assert _main(tmp_path, "--write") == 0
    assert capsys.readouterr().out.strip() == "wrote the CLAUDE.md template region"
    assert path.read_bytes() == _doc(_template()).encode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    assert [p.name for p in tmp_path.iterdir()] == ["CLAUDE.md"]
    assert _audit_items(tmp_path) == []


def test_write_keeps_bytes_outside_the_markers(tmp_path: Path) -> None:
    """CRLF line ends, marker indentation and trailing spaces outside survive unchanged."""
    head = b"# CLAUDE.md\r\n\r\nLocal \xc3\xa9 text\t \r\n"
    begin = f"  {BEGIN} \r\n".encode()
    end = f"{END}\r\n".encode()
    tail = b"\r\n## Local\r\nlast line"
    stale = b"old\r\nregion\r\n"
    _write(tmp_path, head + begin + stale + end + tail)
    assert _main(tmp_path, "--write") == 0
    expected = head + begin + _template().encode("utf-8") + end + tail
    assert (tmp_path / "CLAUDE.md").read_bytes() == expected


def test_write_fills_an_empty_region(tmp_path: Path) -> None:
    _write(tmp_path, _doc(""))
    assert _main(tmp_path, "--write") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _doc(_template())


def test_write_ends_a_template_without_final_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end marker always starts its own line, so the region stays parseable."""
    monkeypatch.setattr(repo_config, "_load_template", lambda: "## Rule\n\nNo final newline")
    _write(tmp_path, _doc("stale\n"))
    assert _main(tmp_path, "--write") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _doc(
        "## Rule\n\nNo final newline\n"
    )
    assert _main(tmp_path, "--check") == 0


def test_write_in_sync_leaves_file_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _doc(f"\n{_template()}\n")
    path = _write(tmp_path, text)
    before = path.stat().st_mtime_ns
    assert _main(tmp_path, "--write") == 0
    assert capsys.readouterr().out.strip() == "already in sync"
    assert path.read_bytes() == text.encode("utf-8")
    assert path.stat().st_mtime_ns == before


def test_write_failure_keeps_original_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replace leaves the old file intact, no temp file behind, and raises."""
    text = _doc("stale\n")
    path = _write(tmp_path, text)

    def failing_replace(self: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vrg_claude_md_sync.Path, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        vrg_claude_md_sync._run_write(tmp_path)
    assert path.read_bytes() == text.encode("utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["CLAUDE.md"]


@pytest.mark.parametrize("case", ["stale", "empty", "match", "padded"])
def test_check_predicts_write_and_write_passes_audit(tmp_path: Path, case: str) -> None:
    """``--check`` drifts exactly when ``--write`` changes the file; after it the audit passes."""
    regions = {
        "stale": _stale_region(),
        "empty": "",
        "match": _template(),
        "padded": f"\n{_template()}\n\n",
    }
    path = _write(tmp_path, _doc(regions[case]))
    before = path.read_bytes()

    drift = _main(tmp_path, "--check")
    assert _main(tmp_path, "--write") == 0
    assert drift == (1 if path.read_bytes() != before else 0)
    assert drift == (0 if case in {"match", "padded"} else 1)
    assert _audit_items(tmp_path) == []
    assert _main(tmp_path, "--check") == 0


def test_check_and_write_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _main(tmp_path, "--check", "--write")
    assert exc.value.code == 2
