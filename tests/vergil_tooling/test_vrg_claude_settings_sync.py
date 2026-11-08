"""Tests for the ``vrg-claude-settings-sync`` single-repo applicator CLI (#4056)."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_claude_settings_sync
from vergil_tooling.lib import repo_config
from vergil_tooling.lib.repo_init import render_claude_settings

if TYPE_CHECKING:
    from pathlib import Path


def _settings(root: Path, text: str | None = None) -> Path:
    path = root / ".claude" / "settings.json"
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path


def _audit_items(root: Path) -> list[repo_config.DiffItem]:
    items: list[repo_config.DiffItem] = []
    repo_config._check_claude_settings(root, items)
    return items


def _template() -> dict[str, object]:
    return json.loads(render_claude_settings())


# --- --check ---------------------------------------------------------------


def test_check_missing_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check"]) == 1
    assert "missing .claude/settings.json" in capsys.readouterr().err


def test_check_is_default_and_writes_nothing(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path)]) == 1
    assert not path.exists()


def test_check_drift_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _template()
    data["extraKnownMarketplaces"] = {"x": {}}
    _settings(tmp_path, json.dumps(data))
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check"]) == 1
    assert ".claude/settings.json differs from the template" in capsys.readouterr().err


def test_check_invalid_json_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _settings(tmp_path, "{not json")
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check"]) == 1
    assert ".claude/settings.json is not valid JSON" in capsys.readouterr().err


def test_check_match_exits_0(tmp_path: Path) -> None:
    _settings(tmp_path, render_claude_settings())
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check"]) == 0


def test_check_match_ignores_key_order_and_whitespace(tmp_path: Path) -> None:
    """The audit's comparison: equal after JSON parsing is in sync."""
    reordered = dict(reversed(list(_template().items())))
    _settings(tmp_path, json.dumps(reordered, separators=(",", ":")))
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check"]) == 0
    assert _audit_items(tmp_path) == []


def test_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(tmp_path, render_claude_settings())
    monkeypatch.chdir(tmp_path)
    assert vrg_claude_settings_sync.main(["--check"]) == 0


# --- --write ---------------------------------------------------------------


def test_write_creates_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _settings(tmp_path)
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == render_claude_settings()
    assert capsys.readouterr().out.strip() == "wrote .claude/settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_write_replaces_whole_file_never_merges(tmp_path: Path) -> None:
    data = _template()
    data["model"] = "local-choice"
    data["permissions"] = {"allow": ["Bash(ls)"]}
    path = _settings(tmp_path, json.dumps(data))
    path.chmod(0o664)
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == render_claude_settings()
    assert stat.S_IMODE(path.stat().st_mode) == 0o664


def test_write_replaces_invalid_json(tmp_path: Path) -> None:
    path = _settings(tmp_path, "{not json")
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == render_claude_settings()


def test_write_in_sync_prints_already_in_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _settings(tmp_path, render_claude_settings())
    before = path.stat().st_mtime_ns
    assert vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert capsys.readouterr().out.strip() == "already in sync"
    assert path.stat().st_mtime_ns == before


def test_write_failure_keeps_original_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replace leaves the old file intact, no temp file behind, and raises."""
    path = _settings(tmp_path, "{}")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vrg_claude_settings_sync.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        vrg_claude_settings_sync._run_write(tmp_path)
    assert path.read_text(encoding="utf-8") == "{}"
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


@pytest.mark.parametrize("case", ["missing", "drift", "invalid json", "match", "reformatted"])
def test_check_predicts_write_and_write_passes_audit(tmp_path: Path, case: str) -> None:
    """``--check`` drifts exactly when ``--write`` changes the file; after it the audit passes."""
    drifted = {**_template(), "model": "x"}
    texts = {
        "missing": None,
        "drift": json.dumps(drifted),
        "invalid json": "{",
        "match": render_claude_settings(),
        "reformatted": json.dumps(_template()),
    }
    path = _settings(tmp_path, texts[case])
    before = path.read_bytes() if path.exists() else None

    drift = vrg_claude_settings_sync.main(["--check", "--repo", str(tmp_path)])
    assert vrg_claude_settings_sync.main(["--write", "--repo", str(tmp_path)]) == 0
    assert drift == (1 if path.read_bytes() != before else 0)
    assert drift == (0 if case in {"match", "reformatted"} else 1)
    assert _audit_items(tmp_path) == []
    assert vrg_claude_settings_sync.main(["--check", "--repo", str(tmp_path)]) == 0


def test_check_and_write_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_claude_settings_sync.main(["--repo", str(tmp_path), "--check", "--write"])
    assert exc.value.code == 2
