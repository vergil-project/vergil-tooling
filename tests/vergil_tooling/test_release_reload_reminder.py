from __future__ import annotations

from vergil_tooling.lib.release import reload_reminder


def test_reminder_names_the_command_and_the_hosts() -> None:
    assert reload_reminder.REMINDER.endswith(
        "Run /reload-plugins, or restart, in every running Claude Code session, "
        "on the host and in every VM."
    )


def test_reminder_says_why() -> None:
    assert "keep the plugin's agent definitions until they are reloaded" in (
        reload_reminder.REMINDER
    )
