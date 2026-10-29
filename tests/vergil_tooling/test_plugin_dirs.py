"""Tests for vergil_tooling.lib.plugin_dirs (the vrg-vm session --plugin-dir override)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vergil_tooling.lib import plugin_dirs
from vergil_tooling.lib.plugin_dirs import PluginDirsError, load_plugin_dirs

# Captured at import time, before the autouse conftest fixture patches it.
_REAL_DEFAULT_PATH = plugin_dirs.default_plugin_dirs_path


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return tmp_path / "plugin-dirs.toml"


def test_default_plugin_dirs_path() -> None:
    assert _REAL_DEFAULT_PATH() == Path.home() / ".config" / "vergil" / "plugin-dirs.toml"


def test_load_plugin_dirs_missing_file_is_empty(cfg: Path) -> None:
    assert load_plugin_dirs(cfg) == ()


def test_load_plugin_dirs_uses_default_path(tmp_path: Path) -> None:
    # The conftest fixture points the default at a per-test path; writing there
    # is read back when no path is passed.
    default = plugin_dirs.default_plugin_dirs_path()
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text('plugin_dirs = ["/d"]\n', encoding="utf-8")
    assert load_plugin_dirs() == ("/d",)


def test_load_plugin_dirs_reads_list(cfg: Path) -> None:
    cfg.write_text('plugin_dirs = ["/a/preview/paad"]\n', encoding="utf-8")
    assert load_plugin_dirs(cfg) == ("/a/preview/paad",)


def test_load_plugin_dirs_reads_several_in_order(cfg: Path) -> None:
    cfg.write_text('plugin_dirs = ["/b", "/a"]\n', encoding="utf-8")
    assert load_plugin_dirs(cfg) == ("/b", "/a")


def test_load_plugin_dirs_empty_file_is_empty(cfg: Path) -> None:
    cfg.write_text("", encoding="utf-8")
    assert load_plugin_dirs(cfg) == ()


def test_load_plugin_dirs_empty_list_is_empty(cfg: Path) -> None:
    cfg.write_text("plugin_dirs = []\n", encoding="utf-8")
    assert load_plugin_dirs(cfg) == ()


def test_load_plugin_dirs_rejects_relative(cfg: Path) -> None:
    cfg.write_text('plugin_dirs = ["preview/paad"]\n', encoding="utf-8")
    with pytest.raises(PluginDirsError, match=r"'preview/paad' is not an absolute path"):
        load_plugin_dirs(cfg)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("plugin_dirs = [", "not valid TOML"),
        ('plugin_dirs = "/a"\n', "must be a list"),
        ("plugin_dirs = [1]\n", "entry 1 is not a string"),
        ('plugin_dirs = ["/a"]\nextra = 1\n', r"unknown key\(s\) extra"),
    ],
    ids=["invalid-toml", "non-list", "non-string-entry", "unknown-key"],
)
def test_load_plugin_dirs_rejects_malformed(cfg: Path, body: str, match: str) -> None:
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(PluginDirsError, match=match):
        load_plugin_dirs(cfg)


def test_load_plugin_dirs_rejects_non_utf8(cfg: Path) -> None:
    cfg.write_bytes(b'plugin_dirs = ["/\xff"]\n')
    with pytest.raises(PluginDirsError, match=rf"{re.escape(str(cfg))}: cannot read"):
        load_plugin_dirs(cfg)


def test_load_plugin_dirs_rejects_unreadable(cfg: Path) -> None:
    cfg.mkdir()  # a directory where the file should be: reading it is an OSError
    with pytest.raises(PluginDirsError, match=rf"{re.escape(str(cfg))}: cannot read"):
        load_plugin_dirs(cfg)


def test_plugin_dirs_error_names_the_file(cfg: Path) -> None:
    cfg.write_text("plugin_dirs = [", encoding="utf-8")
    with pytest.raises(PluginDirsError, match=str(cfg)):
        load_plugin_dirs(cfg)
