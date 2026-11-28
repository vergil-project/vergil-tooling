"""The reload-plugins reminder a plugin release ends with (issue #5434).

A running Claude Code session keeps the plugin agent definitions it loaded
until ``/reload-plugins`` or a restart, while skills are read fresh when
invoked. After a plugin release a session that is not reloaded pairs new
skills with old agents. ``[publish].reload-plugins`` in ``vergil.toml`` opts a
repo in (default false); only a plugin repo sets it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config

if TYPE_CHECKING:
    from pathlib import Path

# The reminder, as the release prints it last and the tracking summary ends
# with it. The action is the final line so it is what the human reads last.
REMINDER = (
    "Running Claude Code sessions keep the plugin's agent definitions until they "
    "are reloaded, so after this release their new skills would pair with old agents.\n"
    "Run /reload-plugins, or restart, in every running Claude Code session, "
    "on the host and in every VM."
)


def reload_plugins_enabled(repo_root: Path) -> bool:
    """Whether the repo at *repo_root* declares ``[publish].reload-plugins = true``."""
    return config.read_config(repo_root).publish.reload_plugins
