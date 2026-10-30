"""Tests for the shared marketplace refresh (vergil_tooling.lib.claude_marketplace)."""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

import pytest

from vergil_tooling.lib import claude_marketplace
from vergil_tooling.lib.claude_marketplace import (
    Declared,
    MarketplaceError,
    _marketplace_sources,
    _update_plugin_entry,
    declared_marketplaces,
    local_run,
    refresh,
    registered_marketplaces,
    shell,
    update_installed_plugins,
    verify,
)

# V1's recorded `claude plugin marketplace list --json` outputs, copied verbatim
# (vergil-project/vergil-tooling#3850, Claude Code 2.1.312).
_V1_LIST_MAIN = """[
  {
    "name": "vergil-marketplace",
    "source": "github",
    "repo": "vergil-project/vergil-claude-plugin",
    "ref": "main",
    "installLocation": "/tmp/claude-501/-Users-pmoore-dev-projects-vergil-project--github/1b1f06dc-5aad-497c-9cd7-67edf4e6640f/scratchpad/cfg-host/plugins/marketplaces/vergil-marketplace"
  }
]"""  # noqa: E501

_V1_LIST_V21 = """[
  {
    "name": "vergil-marketplace",
    "source": "github",
    "repo": "vergil-project/vergil-claude-plugin",
    "ref": "v2.1",
    "installLocation": "/tmp/claude-501/-Users-pmoore-dev-projects-vergil-project--github/1b1f06dc-5aad-497c-9cd7-67edf4e6640f/scratchpad/cfg-host/plugins/marketplaces/vergil-marketplace"
  }
]"""  # noqa: E501

# V1's `claude plugin list --json` vergil entry after the switch to v2.1.
_V1_PLUGIN_LIST = """[
  {
    "id": "vergil@vergil-marketplace",
    "version": "2.1.51",
    "scope": "user",
    "enabled": true,
    "installPath": "/tmp/claude-501/-Users-pmoore-dev-projects-vergil-project--github/1b1f06dc-5aad-497c-9cd7-67edf4e6640f/scratchpad/cfg-host/plugins/cache/vergil-marketplace/vergil/2.1.51",
    "installedAt": "2026-10-30T17:01:01.282Z",
    "lastUpdated": "2026-10-30T17:01:22.731Z",
    "projectEnabled": false
  }
]"""  # noqa: E501


def _settings_with_ref(ref: str) -> dict[str, Any]:
    return {
        "extraKnownMarketplaces": {
            "vergil-marketplace": {
                "source": {
                    "source": "github",
                    "repo": "vergil-project/vergil-claude-plugin",
                    "ref": ref,
                }
            }
        },
        "enabledPlugins": {"vergil@vergil-marketplace": True},
    }


SETTINGS_V21 = _settings_with_ref("v2.1")


class FakeRun:
    """A ``Run`` that records each argv and answers the two ``--json`` listings."""

    def __init__(
        self,
        marketplace_list: str = _V1_LIST_V21,
        plugin_list: str = _V1_PLUGIN_LIST,
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self.marketplace_list = marketplace_list
        self.plugin_list = plugin_list
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        cmd = argv[-1]
        if any(f in cmd for f in self.fail_on):
            raise subprocess.CalledProcessError(1, list(argv), stderr="boom\n")
        if "marketplace list --json" in cmd:
            out = self.marketplace_list
        elif "plugin list --json" in cmd:
            out = self.plugin_list
        else:
            out = ""
        return subprocess.CompletedProcess(list(argv), 0, stdout=out, stderr="")

    @property
    def cmds(self) -> list[str]:
        return [c[-1] for c in self.calls]


def _registered(entries: list[dict[str, Any]]) -> FakeRun:
    return FakeRun(marketplace_list=json.dumps(entries))


def fake_returning(stdout: str) -> FakeRun:
    return FakeRun(marketplace_list=stdout)


fake_post_switch = FakeRun(marketplace_list=_V1_LIST_V21)
fake_stale = FakeRun(marketplace_list=_V1_LIST_MAIN)


class TestRefresh:
    def test_refresh_re_adds_at_declared_ref_then_updates(self) -> None:
        run = FakeRun()
        assert refresh(run, SETTINGS_V21, "") == []
        assert run.cmds == [
            "claude plugin marketplace add vergil-project/vergil-claude-plugin@v2.1",
            "claude plugin marketplace update",
            "claude plugin list --json",
            "claude plugin update vergil@vergil-marketplace --scope user",
            "claude plugin marketplace list --json",
        ]
        assert all(c[:2] == ("bash", "-c") for c in run.calls)

    def test_refresh_quotes_declared_values(self) -> None:
        bad_ref = "v2.1'; touch x"
        bad_repo = "o/r $(touch y)"
        settings = _settings_with_ref(bad_ref)
        settings["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["repo"] = bad_repo
        run = FakeRun()
        refresh(run, settings, "")
        adds = [c for c in run.cmds if "marketplace add" in c]
        quoted = f"{shlex.quote(bad_repo)}@{shlex.quote(bad_ref)}"
        assert adds == [f"claude plugin marketplace add {quoted}"]
        # The raw ref (with its unbalanced quote) never reaches a shell string.
        for cmd in run.cmds:
            assert bad_ref not in cmd

    def test_refresh_reports_a_stale_ref(self) -> None:
        # The add "succeeded" but the registration did not move: a failure.
        assert refresh(FakeRun(marketplace_list=_V1_LIST_MAIN), SETTINGS_V21, "") == [
            "marketplace:vergil-marketplace"
        ]

    def test_refresh_collects_every_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        run = FakeRun(
            fail_on=("marketplace add", "marketplace update", "plugin update vergil"),
            marketplace_list="[]",
        )
        failures = refresh(run, SETTINGS_V21, "")
        assert failures == [
            "marketplace:vergil-project/vergil-claude-plugin@v2.1",
            "marketplace update",
            "vergil@vergil-marketplace (user)",
            "marketplace:vergil-marketplace",
        ]
        err = capsys.readouterr().err
        # Each failed step prints its own error output.
        assert "boom" in err

    def test_refresh_raises_on_malformed_listing(self) -> None:
        with pytest.raises(MarketplaceError):
            refresh(FakeRun(marketplace_list='{"not": "a list"}'), SETTINGS_V21, "")

    def test_refresh_without_declared_marketplaces_does_not_verify(self) -> None:
        run = FakeRun(plugin_list="[]")
        assert refresh(run, {}, "") == []
        assert not any("marketplace list" in c for c in run.cmds)

    def test_refresh_uses_the_prefix(self) -> None:
        run = FakeRun()
        refresh(run, SETTINGS_V21, "export PATH=x")
        assert all(c[-1].startswith("export PATH=x && claude ") for c in run.calls)


class TestVerify:
    def test_verify_passes_on_switched_state(self) -> None:
        assert verify(fake_post_switch, SETTINGS_V21, "") == []

    def test_verify_fails_when_ref_did_not_move(self) -> None:
        assert verify(fake_stale, SETTINGS_V21, "") == ["marketplace:vergil-marketplace"]

    def test_verify_fails_on_mismatched_repo(self) -> None:
        fake_other_repo = _registered(
            [{"name": "vergil-marketplace", "repo": "other/repo", "ref": "v2.1"}]
        )
        assert verify(fake_other_repo, SETTINGS_V21, "") == ["marketplace:vergil-marketplace"]

    def test_verify_treats_missing_registered_ref_as_mismatch(self) -> None:
        fake_no_ref = _registered(
            [{"name": "vergil-marketplace", "repo": "vergil-project/vergil-claude-plugin"}]
        )
        assert verify(fake_no_ref, SETTINGS_V21, "") == ["marketplace:vergil-marketplace"]

    def test_verify_fails_on_unregistered_marketplace(self) -> None:
        fake_empty = _registered([])
        assert verify(fake_empty, SETTINGS_V21, "") == ["marketplace:vergil-marketplace"]

    def test_verify_names_both_sides(self, capsys: pytest.CaptureFixture[str]) -> None:
        verify(fake_stale, SETTINGS_V21, "")
        err = capsys.readouterr().err
        assert "ref main" in err
        assert "ref v2.1" in err

    def test_verify_ignores_undeclared_registrations(self) -> None:
        run = _registered(
            [
                {"name": "anthropic-plugin-directory", "repo": "x/y", "ref": None},
                {
                    "name": "vergil-marketplace",
                    "repo": "vergil-project/vergil-claude-plugin",
                    "ref": "v2.1",
                },
            ]
        )
        assert verify(run, SETTINGS_V21, "") == []


class TestRegisteredMarketplaces:
    def test_reads_repo_and_ref(self) -> None:
        assert registered_marketplaces(fake_stale, "") == {
            "vergil-marketplace": ("vergil-project/vergil-claude-plugin", "main")
        }

    def test_registered_marketplaces_rejects_malformed_json(self) -> None:
        with pytest.raises(MarketplaceError):
            registered_marketplaces(fake_returning('{"not": "a list"}'), "")

    @pytest.mark.parametrize("stdout", ["not json", '[{"repo": "o/r"}]', "[1]", '[{"name": 3}]'])
    def test_rejects_entries_without_a_name(self, stdout: str) -> None:
        with pytest.raises(MarketplaceError):
            registered_marketplaces(fake_returning(stdout), "")


class TestDeclared:
    def test_declared_marketplaces_skips_non_github_sources(self) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "a": {"source": {"source": "github", "repo": "o/a", "ref": "v2.1"}},
                "b": {"source": {"source": "url", "url": "https://x/m.json"}},
            }
        }
        assert declared_marketplaces(settings) == [Declared("a", "o/a", "v2.1")]

    def test_ref_is_optional(self) -> None:
        source = {"source": "github", "repo": "o/a"}
        settings = {"extraKnownMarketplaces": {"a": {"source": source}}}
        assert declared_marketplaces(settings) == [Declared("a", "o/a", None)]


class TestShell:
    def test_shell_without_prefix_has_no_leading_and(self) -> None:
        assert shell("", "claude plugin marketplace update") == [
            "bash",
            "-c",
            "claude plugin marketplace update",
        ]
        assert shell("export PATH=x", "claude plugin list --json") == [
            "bash",
            "-c",
            "export PATH=x && claude plugin list --json",
        ]

    def test_local_run_runs_and_raises(self) -> None:
        assert local_run("true").returncode == 0
        with pytest.raises(subprocess.CalledProcessError):
            local_run("false")


class TestMarketplaceSources:
    # Moved from test_vm_guest with _marketplace_sources (#2021, #2029, #3751).
    def test_marketplace_sources_keeps_github_ref(self) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": {
                    "source": {
                        "source": "github",
                        "repo": "vergil-project/vergil-claude-plugin",
                        "ref": "main",
                    }
                },
                "paad": {"source": {"source": "github", "repo": "Ovid/paad"}},
            },
            "enabledPlugins": {},
        }
        assert _marketplace_sources(settings) == [
            "vergil-project/vergil-claude-plugin@main",
            "Ovid/paad",
        ]

    def test_marketplace_sources_keeps_git_url_ref(self) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "pinned": {
                    "source": {
                        "source": "git",
                        "url": "https://example.com/y.git",
                        "ref": "v1.0.0",
                    }
                },
                "bare": {"source": {"source": "git", "url": "https://example.com/z.git"}},
                "p": {"source": {"path": "./local-mp"}},
            },
            "enabledPlugins": {},
        }
        assert _marketplace_sources(settings) == [
            "https://example.com/y.git#v1.0.0",
            "https://example.com/z.git",
            "./local-mp",
        ]

    def test_skips_sourceless_entries(self) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "u": {"source": {"source": "git", "url": "https://example.com/y.git"}},
                "empty": {"source": {}},
                "nosrc": {},
            },
            "enabledPlugins": {},
        }
        assert _marketplace_sources(settings) == ["https://example.com/y.git"]

    def test_derives_official_marketplace_when_undeclared(self) -> None:
        settings = {
            "extraKnownMarketplaces": {},
            "enabledPlugins": {"superpowers@claude-plugins-official": True},
        }
        assert _marketplace_sources(settings) == ["anthropics/claude-plugins-official"]

    def test_undecidable_undeclared_marketplace_is_not_added(self) -> None:
        enabled = {"thing@some-third-party": True}
        settings = {"extraKnownMarketplaces": {}, "enabledPlugins": enabled}
        assert _marketplace_sources(settings) == []

    def test_enabled_id_without_marketplace_suffix_is_skipped(self) -> None:
        settings = {"extraKnownMarketplaces": {}, "enabledPlugins": {"bare-id": True}}
        assert _marketplace_sources(settings) == []


class TestUpdateInstalledPlugins:
    # Moved from test_vm_guest with _update_plugin_entry and its caller (#2006, #3764).
    _LISTING = json.dumps(
        [
            {"id": "paad@paad", "scope": "user", "enabled": True},
            {"id": "frontend-design@official", "scope": "user", "enabled": False},
            {
                "id": "vergil@vergil-marketplace",
                "scope": "project",
                "enabled": True,
                "projectPath": "/p/tooling",
            },
        ]
    )

    def test_updates_enabled_installs_and_leaves_disabled_alone(self) -> None:
        run = FakeRun(plugin_list=self._LISTING)
        assert update_installed_plugins(run, {}, "") == []
        assert "claude plugin update paad@paad --scope user" in run.cmds
        assert (
            "cd /p/tooling && claude plugin update vergil@vergil-marketplace --scope project"
            in run.cmds
        )
        assert not any("frontend-design" in c for c in run.cmds)

    def test_updates_every_install_entry_at_its_own_scope_and_project(self) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [
                {"id": pid, "scope": "user", "enabled": True},
                {"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/tooling"},
                {"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/my proj"},
            ]
        )
        run = FakeRun(plugin_list=listing)
        update_installed_plugins(run, {}, "")
        updates = [c for c in run.cmds if f"claude plugin update {pid}" in c]
        assert len(updates) == 3
        assert f"claude plugin update {pid} --scope user" in updates
        assert f"cd /p/tooling && claude plugin update {pid} --scope project" in updates
        quoted = shlex.quote("/p/my proj")
        assert f"cd {quoted} && claude plugin update {pid} --scope project" in updates
        assert not any("claude plugin install" in c for c in run.cmds)

    def test_missing_project_dir_fails_that_entry_naming_scope_and_project(self) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [
                {"id": pid, "scope": "user", "enabled": True},
                {"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/gone"},
                {"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/here"},
            ]
        )
        run = FakeRun(plugin_list=listing, fail_on=("cd /p/gone",))
        assert update_installed_plugins(run, {}, "") == [f"{pid} (project: /p/gone)"]
        assert any("cd /p/here && claude plugin update" in c for c in run.cmds)
        assert f"claude plugin update {pid} --scope user" in run.cmds

    def test_project_entry_without_project_path_is_a_failure(self) -> None:
        entry = {"id": "vergil@vergil-marketplace", "scope": "project", "enabled": True}
        run = FakeRun()
        label = _update_plugin_entry(run, entry, "")
        assert label == "vergil@vergil-marketplace (project: <no projectPath>)"
        assert run.calls == []

    def test_id_installed_only_at_project_scope_is_not_reinstalled(self) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [{"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/tooling"}]
        )
        run = FakeRun(plugin_list=listing)
        update_installed_plugins(run, {"enabledPlugins": {pid: True}}, "")
        assert not any("claude plugin install" in c for c in run.cmds)

    def test_installs_enabled_plugins_not_yet_installed(self) -> None:
        settings = {"enabledPlugins": {"superpowers@official": True, "paad@paad": True}}
        listing = json.dumps([{"id": "paad@paad", "scope": "user", "enabled": True}])
        run = FakeRun(plugin_list=listing)
        assert update_installed_plugins(run, settings, "") == []
        assert "claude plugin install superpowers@official --scope user" in run.cmds
        assert "claude plugin update paad@paad --scope user" in run.cmds
        assert not any("claude plugin install paad@paad" in c for c in run.cmds)

    def test_install_failure_is_surfaced_with_its_scope(self) -> None:
        settings = {"enabledPlugins": {"superpowers@official": True}}
        run = FakeRun(plugin_list="[]", fail_on=("claude plugin install superpowers@official",))
        assert update_installed_plugins(run, settings, "") == ["superpowers@official (user)"]

    def test_attempts_all_when_one_fails(self) -> None:
        run = FakeRun(plugin_list=self._LISTING, fail_on=("claude plugin update paad@paad",))
        assert update_installed_plugins(run, {}, "") == ["paad@paad (user)"]
        assert any("claude plugin update vergil@vergil-marketplace" in c for c in run.cmds)

    @pytest.mark.parametrize("stdout", ["nope", '{"id": "x"}', '[{"scope": "user"}]'])
    def test_malformed_plugin_list_raises(self, stdout: str) -> None:
        with pytest.raises(MarketplaceError):
            update_installed_plugins(FakeRun(plugin_list=stdout), {}, "")


def test_marketplace_error_is_a_runtime_error() -> None:
    assert issubclass(claude_marketplace.MarketplaceError, RuntimeError)
