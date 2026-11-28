from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.release import reload_reminder

_TOML = """\
[project]
repository-type = "tooling"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]

[publish]
release = true
"""


def test_reload_plugins_enabled_false_when_absent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_TOML)
    assert reload_reminder.reload_plugins_enabled(tmp_path) is False


def test_reload_plugins_enabled_true_when_set(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_TOML + "reload-plugins = true\n")
    assert reload_reminder.reload_plugins_enabled(tmp_path) is True


def test_reminder_names_the_command_and_the_hosts() -> None:
    assert reload_reminder.REMINDER.endswith(
        "Run /reload-plugins, or restart, in every running Claude Code session, "
        "on the host and in every VM."
    )


@pytest.mark.parametrize("value", ['"yes"', "2"])
def test_reload_plugins_enabled_rejects_non_bool(tmp_path: Path, value: str) -> None:
    (tmp_path / "vergil.toml").write_text(_TOML + f"reload-plugins = {value}\n")
    with pytest.raises(ConfigError, match=r"reload-plugins must be a boolean"):
        reload_reminder.reload_plugins_enabled(tmp_path)
