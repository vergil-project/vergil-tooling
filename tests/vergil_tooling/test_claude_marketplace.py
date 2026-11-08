"""Tests for the shared marketplace refresh (vergil_tooling.lib.claude_marketplace)."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.lib import claude_marketplace
from vergil_tooling.lib.claude_marketplace import (
    Declared,
    MarketplaceError,
    _marketplace_adds,
    _update_plugin_entry,
    declared_marketplaces,
    local_run,
    refresh,
    registered_marketplaces,
    shell,
    update_installed_plugins,
    validate_settings,
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


def _declaring(
    names: tuple[str, ...] = ("paad", "vergil-marketplace"),
    enabled: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Settings that declare *names* (GitHub sources) and enable *enabled*."""
    return {
        "extraKnownMarketplaces": {
            name: {"source": {"source": "github", "repo": f"o/{name}"}} for name in names
        },
        "enabledPlugins": enabled or {},
    }


# Declares the marketplaces of the installs the listing tests report enabled.
_DECLARED = _declaring()


class FakeRun:
    """A ``Run`` that records each argv and answers the two ``--json`` listings."""

    def __init__(
        self,
        marketplace_list: str = _V1_LIST_V21,
        plugin_list: str = _V1_PLUGIN_LIST,
        fail_on: tuple[str, ...] = (),
        fail_stdout: str = "",
    ) -> None:
        self.marketplace_list = marketplace_list
        self.plugin_list = plugin_list
        self.fail_on = fail_on
        self.fail_stdout = fail_stdout
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        cmd = argv[-1]
        if any(f in cmd for f in self.fail_on):
            raise subprocess.CalledProcessError(
                1, list(argv), output=self.fail_stdout, stderr="boom\n"
            )
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
            "claude plugin marketplace update vergil-marketplace",
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
        # The failed add, the failed update and the failed verification name
        # the same marketplace by name, so it is listed once.
        assert failures == [
            "marketplace:vergil-marketplace",
            "vergil@vergil-marketplace (user)",
        ]
        err = capsys.readouterr().err
        # Each failed step is named with its exit status. Its stderr is the
        # Run's to echo, so it is not printed a second time here.
        assert "ERROR: marketplace update vergil-marketplace failed (exit status 1)" in err
        assert (
            "ERROR: marketplace add vergil-project/vergil-claude-plugin@v2.1 failed "
            "(exit status 1)" in err
        )
        assert "boom" not in err

    def test_updates_exactly_the_declared_marketplaces_by_name(self) -> None:
        # #3879: a registered marketplace the settings do not declare (a stale
        # leftover) is never updated, so it cannot fail the refresh.
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": SETTINGS_V21["extraKnownMarketplaces"]["vergil-marketplace"],
                "paad": {"source": {"source": "github", "repo": "o/paad"}},
            },
            "enabledPlugins": {"superpowers@claude-plugins-official": True},
        }
        listing = json.loads(_V1_LIST_V21) + [
            {"name": "paad", "source": "github", "repo": "o/paad"},
            {"name": "standard-tooling-marketplace", "source": "github", "repo": "o/old"},
        ]
        run = FakeRun(marketplace_list=json.dumps(listing), plugin_list="[]")
        assert refresh(run, settings, "") == []
        assert [c for c in run.cmds if "marketplace update" in c] == [
            "claude plugin marketplace update vergil-marketplace",
            "claude plugin marketplace update paad",
            "claude plugin marketplace update claude-plugins-official",
        ]
        assert not any("standard-tooling-marketplace" in c for c in run.cmds)
        assert "claude plugin marketplace update" not in run.cmds

    def test_update_quotes_the_marketplace_name(self) -> None:
        name = "m; touch x"
        settings = {"extraKnownMarketplaces": {name: {"source": {"repo": "o/r"}}}}
        run = FakeRun(plugin_list="[]")
        refresh(run, settings, "")
        assert f"claude plugin marketplace update {shlex.quote(name)}" in run.cmds

    def test_a_failed_update_is_named_by_marketplace(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": SETTINGS_V21["extraKnownMarketplaces"]["vergil-marketplace"],
                "paad": {"source": {"source": "url", "url": "https://x/paad.git"}},
            },
        }
        run = FakeRun(plugin_list="[]", fail_on=("marketplace update paad",))
        # One marketplace's failure is reported by name; the others still run.
        assert refresh(run, settings, "") == ["marketplace:paad"]
        assert "claude plugin marketplace update vergil-marketplace" in run.cmds
        assert "ERROR: marketplace update paad failed (exit status 1)" in capsys.readouterr().err

    def test_a_failed_command_reports_its_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        # #3879: claude writes some failure causes only to stdout.
        cause = "1 marketplace could not be refreshed (see --debug): vergil-marketplace\n"
        run = FakeRun(fail_on=("marketplace update",), fail_stdout=cause)
        assert refresh(run, SETTINGS_V21, "") == ["marketplace:vergil-marketplace"]
        err = capsys.readouterr().err
        assert cause + "ERROR: marketplace update vergil-marketplace failed (exit status 1)" in err

    def test_stdout_without_a_newline_ends_its_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = FakeRun(fail_on=("plugin update vergil",), fail_stdout="no newline")
        refresh(run, SETTINGS_V21, "")
        assert "no newline\nERROR: update vergil@" in capsys.readouterr().err

    @pytest.mark.parametrize("listing", ["claude plugin list --json", "marketplace list --json"])
    def test_a_failed_listing_reports_its_stdout_and_raises(
        self, listing: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = FakeRun(fail_on=(listing,), fail_stdout="listing cause\n")
        with pytest.raises(subprocess.CalledProcessError):
            refresh(run, SETTINGS_V21, "")
        err = capsys.readouterr().err
        assert "listing cause\nERROR: claude plugin " in err
        assert "failed (exit status 1)" in err

    @pytest.mark.parametrize("listing", ["claude plugin list --json", "marketplace list --json"])
    def test_a_failed_listing_after_a_failure_names_both(self, listing: str) -> None:
        # #3989: the add and update failures are kept, chained to the error.
        run = FakeRun(fail_on=("marketplace update", listing))
        with pytest.raises(MarketplaceError) as caught:
            refresh(run, SETTINGS_V21, "")
        assert str(caught.value).startswith("CalledProcessError: ")
        assert str(caught.value).endswith("; failed before it: marketplace:vergil-marketplace")
        assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)

    def test_a_malformed_listing_after_failures_names_each_once(self) -> None:
        run = FakeRun(
            marketplace_list='{"not": "a list"}',
            fail_on=("marketplace add", "marketplace update", "plugin update vergil"),
        )
        with pytest.raises(MarketplaceError) as caught:
            refresh(run, SETTINGS_V21, "")
        message = str(caught.value)
        assert message.startswith("MarketplaceError: claude plugin marketplace list --json ")
        assert message.endswith(
            "; failed before it: marketplace:vergil-marketplace, vergil@vergil-marketplace (user)"
        )
        assert isinstance(caught.value.__cause__, MarketplaceError)

    def test_a_plugin_enabled_as_false_is_neither_installed_nor_derived(self) -> None:
        settings = {"enabledPlugins": {"superpowers@claude-plugins-official": False}}
        run = FakeRun(plugin_list="[]")
        assert refresh(run, settings, "") == []
        assert not any("marketplace add" in c or "plugin install" in c for c in run.cmds)

    def test_failed_add_of_a_derived_marketplace_is_named_by_marketplace(self) -> None:
        settings = {"enabledPlugins": {"superpowers@claude-plugins-official": True}}
        run = FakeRun(plugin_list="[]", fail_on=("marketplace add",))
        assert refresh(run, settings, "") == ["marketplace:claude-plugins-official"]

    def test_refresh_adds_marketplaces_before_installing(self) -> None:
        # #2021: only `marketplace add` registers a declared marketplace on a
        # fresh box, so every add runs before an enabled-but-missing install.
        run = FakeRun(plugin_list="[]")
        assert refresh(run, SETTINGS_V21, "") == []
        add = run.cmds.index(
            "claude plugin marketplace add vergil-project/vergil-claude-plugin@v2.1"
        )
        install = run.cmds.index("claude plugin install vergil@vergil-marketplace --scope user")
        assert add < install

    def test_refresh_derives_and_installs_the_official_marketplace(self) -> None:
        # #2029: an enabled plugin of the undeclared official marketplace gets
        # that marketplace added, then the plugin installed.
        settings = {"enabledPlugins": {"superpowers@claude-plugins-official": True}}
        run = FakeRun(plugin_list="[]")
        assert refresh(run, settings, "") == []
        assert run.cmds[:2] == [
            "claude plugin marketplace add anthropics/claude-plugins-official",
            "claude plugin marketplace update claude-plugins-official",
        ]
        assert "claude plugin install superpowers@claude-plugins-official --scope user" in run.cmds

    @pytest.mark.parametrize(
        "settings",
        [
            {"extraKnownMarketplaces": []},
            {"extraKnownMarketplaces": {"m": "o/r"}},
            {"extraKnownMarketplaces": {"m": {"source": "o/r"}}},
            {"extraKnownMarketplaces": {"m": {"source": {"repo": ["o/r"]}}}},
            {"extraKnownMarketplaces": {"m": {"source": {"repo": "o/r", "ref": 2}}}},
            {"enabledPlugins": ["vergil@vergil-marketplace"]},
            {"enabledPlugins": {"vergil@vergil-marketplace": "false"}},
            {"enabledPlugins": {"vergil@vergil-marketplace": 1}},
            {"enabledPlugins": {"vergil@vergil-marketplace": None}},
        ],
    )
    def test_refresh_rejects_misshapen_settings(self, settings: dict[str, Any]) -> None:
        run = FakeRun()
        with pytest.raises(MarketplaceError, match=r"settings\.json "):
            refresh(run, settings, "")
        assert run.calls == []

    def test_empty_ref_reads_as_no_ref_on_add_and_verify(self) -> None:
        settings = _settings_with_ref("")
        run = _registered(
            [{"name": "vergil-marketplace", "repo": "vergil-project/vergil-claude-plugin"}]
        )
        run.plugin_list = "[]"
        assert refresh(run, settings, "") == []
        assert "claude plugin marketplace add vergil-project/vergil-claude-plugin" in run.cmds

    @pytest.mark.parametrize(
        ("entry", "problem"),
        [
            (None, "extraKnownMarketplaces.a is null"),
            ({"source": None}, "extraKnownMarketplaces.a has no source"),
            ({}, "extraKnownMarketplaces.a has no source"),
            (
                {"source": {"source": "github"}},
                "extraKnownMarketplaces.a.source is a github source with no repo",
            ),
            (
                {"source": {"source": "github", "repo": "", "url": "https://x/m.git"}},
                "extraKnownMarketplaces.a.source is a github source with no repo",
            ),
        ],
    )
    def test_null_entries_and_repoless_github_sources_are_errors(
        self, entry: Any, problem: str
    ) -> None:
        # #3992: such an entry was read as empty or skipped, so it was silently
        # neither added nor verified. Each is now a named error, raised before
        # any claude call and by the pre-copy check.
        settings = {"extraKnownMarketplaces": {"a": entry}}
        for read in (_marketplace_adds, declared_marketplaces, validate_settings):
            with pytest.raises(MarketplaceError, match=f"^settings\\.json {problem}$"):
                read(settings)
        run = FakeRun()
        with pytest.raises(MarketplaceError, match=problem):
            refresh(run, settings, "")
        assert run.calls == []

    def test_undeclared_marketplaces_fail_before_any_claude_call(self) -> None:
        # #3995: every offending plugin is named with its marketplace, by the
        # refresh and by the pre-copy check alike, and nothing has run.
        settings = {
            "extraKnownMarketplaces": SETTINGS_V21["extraKnownMarketplaces"],
            "enabledPlugins": {
                "vergil@vergil-marketplace": True,
                "paad@paad": True,
                "old@standard-tooling-marketplace": True,
                "off@elsewhere": False,
            },
        }
        named = (
            r"old@standard-tooling-marketplace \(marketplace standard-tooling-marketplace\), "
            r"paad@paad \(marketplace paad\)$"
        )
        with pytest.raises(MarketplaceError, match=named):
            validate_settings(settings)
        run = FakeRun()
        with pytest.raises(MarketplaceError, match=named):
            refresh(run, settings, "")
        assert run.calls == []

    def test_the_fleet_settings_shape_passes(self) -> None:
        # The fleet's settings declare paad, vergil-marketplace and
        # claude-plugins-official, and enable a plugin from each.
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": SETTINGS_V21["extraKnownMarketplaces"]["vergil-marketplace"],
                "paad": {"source": {"source": "github", "repo": "o/paad"}},
                "claude-plugins-official": {
                    "source": {"source": "github", "repo": "anthropics/claude-plugins-official"}
                },
            },
            "enabledPlugins": {
                "vergil@vergil-marketplace": True,
                "paad@paad": True,
                "superpowers@claude-plugins-official": True,
            },
        }
        validate_settings(settings)
        assert [name for name, _ in _marketplace_adds(settings)] == [
            "vergil-marketplace",
            "paad",
            "claude-plugins-official",
        ]

    @pytest.mark.parametrize(
        "enabled",
        [
            # The refresh adds and updates the official marketplace itself (#2029).
            {"superpowers@claude-plugins-official": True},
            # An id with no marketplace suffix names no marketplace.
            {"bare-id": True},
            # A disabled plugin is not enabled from anywhere.
            {"thing@some-third-party": False},
        ],
    )
    def test_plugins_that_need_no_declaration_pass(self, enabled: dict[str, bool]) -> None:
        validate_settings({"enabledPlugins": enabled})

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

    def test_local_run_runs_from_home(self) -> None:
        result = local_run("pwd")
        assert result.stdout.strip() == str(Path.home())

    def test_local_run_echoes_stderr_and_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            local_run("bash", "-c", "echo oops >&2; exit 3")
        assert capsys.readouterr().err == "oops\n"

    def test_local_run_failure_without_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            local_run("false")
        assert capsys.readouterr().err == ""


class TestMarketplaceAdds:
    # Moved from test_vm_guest as _marketplace_sources tests (#2021, #2029, #3751),
    # then pointed at _marketplace_adds, the list refresh runs (#3991).
    def test_marketplace_adds_keeps_github_ref(self) -> None:
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
        assert _marketplace_adds(settings) == [
            ("vergil-marketplace", "vergil-project/vergil-claude-plugin@main"),
            ("paad", "Ovid/paad"),
        ]

    def test_marketplace_adds_keeps_git_url_ref(self) -> None:
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
        assert _marketplace_adds(settings) == [
            ("pinned", "https://example.com/y.git#v1.0.0"),
            ("bare", "https://example.com/z.git"),
            ("p", "./local-mp"),
        ]

    @pytest.mark.parametrize(
        ("name", "entry", "problem"),
        [
            ("empty", {"source": {}}, "empty.source has no repo, url or path"),
            ("blank", {"source": {"source": "git", "url": ""}}, "blank.source has no repo"),
            ("nosrc", {}, "nosrc has no source"),
        ],
    )
    def test_sourceless_entries_are_errors(self, name: str, entry: Any, problem: str) -> None:
        # #3992: a sourceless entry was skipped, so the marketplace was never
        # added. The error names the marketplace even beside a good entry.
        settings = {
            "extraKnownMarketplaces": {
                "u": {"source": {"source": "git", "url": "https://example.com/y.git"}},
                name: entry,
            },
            "enabledPlugins": {},
        }
        with pytest.raises(MarketplaceError, match=f"extraKnownMarketplaces\\.{problem}"):
            _marketplace_adds(settings)

    def test_derives_official_marketplace_when_undeclared(self) -> None:
        settings = {
            "extraKnownMarketplaces": {},
            "enabledPlugins": {"superpowers@claude-plugins-official": True},
        }
        assert _marketplace_adds(settings) == [
            ("claude-plugins-official", "anthropics/claude-plugins-official")
        ]

    def test_official_repo_declared_under_another_name_is_added_once(self) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "anthropic": {
                    "source": {"source": "github", "repo": "anthropics/claude-plugins-official"}
                }
            },
            "enabledPlugins": {"superpowers@claude-plugins-official": True},
        }
        assert _marketplace_adds(settings) == [("anthropic", "anthropics/claude-plugins-official")]

    def test_a_plugin_from_an_undeclared_marketplace_is_an_error(self) -> None:
        # #3995: its catalog would never be refreshed, so the plugin could stay
        # stale while the refresh reports success. The error names it.
        enabled = {"thing@some-third-party": True}
        settings = {"extraKnownMarketplaces": {}, "enabledPlugins": enabled}
        with pytest.raises(
            MarketplaceError,
            match=(
                r"^settings\.json enabledPlugins enables a plugin from a marketplace "
                r"extraKnownMarketplaces does not declare: "
                r"thing@some-third-party \(marketplace some-third-party\)$"
            ),
        ):
            _marketplace_adds(settings)

    def test_enabled_id_without_marketplace_suffix_is_skipped(self) -> None:
        settings = {"extraKnownMarketplaces": {}, "enabledPlugins": {"bare-id": True}}
        assert _marketplace_adds(settings) == []


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
        assert update_installed_plugins(run, _DECLARED, "") == []
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
        update_installed_plugins(run, _DECLARED, "")
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
        assert update_installed_plugins(run, _DECLARED, "") == [f"{pid} (project: /p/gone)"]
        assert any("cd /p/here && claude plugin update" in c for c in run.cmds)
        assert f"claude plugin update {pid} --scope user" in run.cmds

    def test_project_entry_without_project_path_is_a_failure(self) -> None:
        entry = {"id": "vergil@vergil-marketplace", "scope": "project", "enabled": True}
        run = FakeRun()
        label = _update_plugin_entry(run, entry, "")
        assert label == "vergil@vergil-marketplace (project: <no projectPath>)"
        assert run.calls == []

    @pytest.mark.parametrize("scope", ["project", "local"])
    def test_id_installed_only_at_project_scope_gets_a_user_install(self, scope: str) -> None:
        # The #578 probe: vergil came back at project scope only (#3836).
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [{"id": pid, "scope": scope, "enabled": True, "projectPath": "/p/tooling"}]
        )
        run = FakeRun(plugin_list=listing)
        assert update_installed_plugins(run, _declaring(enabled={pid: True}), "") == []
        update = f"cd /p/tooling && claude plugin update {pid} --scope {scope}"
        install = f"claude plugin install {pid} --scope user"
        assert run.cmds.count(install) == 1
        assert run.cmds.index(update) < run.cmds.index(install)

    def test_failed_user_install_beside_a_project_install_is_named(self) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [{"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/tooling"}]
        )
        run = FakeRun(plugin_list=listing, fail_on=(f"claude plugin install {pid}",))
        assert update_installed_plugins(run, _declaring(enabled={pid: True}), "") == [
            f"{pid} (user)"
        ]

    def test_id_with_user_and_project_installs_is_not_reinstalled(self) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [
                {"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/tooling"},
                {"id": pid, "scope": "user", "enabled": True},
            ]
        )
        run = FakeRun(plugin_list=listing)
        assert update_installed_plugins(run, _declaring(enabled={pid: True}), "") == []
        assert not any("claude plugin install" in c for c in run.cmds)

    def test_entry_without_a_scope_counts_as_a_user_install(self) -> None:
        pid = "vergil@vergil-marketplace"
        run = FakeRun(plugin_list=json.dumps([{"id": pid, "enabled": True}]))
        assert update_installed_plugins(run, _declaring(enabled={pid: True}), "") == []
        assert f"claude plugin update {pid} --scope user" in run.cmds
        assert not any("claude plugin install" in c for c in run.cmds)

    def test_project_install_of_an_id_the_settings_do_not_enable_gets_no_user_install(
        self,
    ) -> None:
        pid = "vergil@vergil-marketplace"
        listing = json.dumps(
            [{"id": pid, "scope": "project", "enabled": True, "projectPath": "/p/tooling"}]
        )
        run = FakeRun(plugin_list=listing)
        assert update_installed_plugins(run, _declaring(enabled={pid: False}), "") == []
        assert not any("claude plugin install" in c for c in run.cmds)

    def test_installs_enabled_plugins_not_yet_installed(self) -> None:
        settings = _declaring(enabled={"superpowers@official": True, "paad@paad": True})
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
        assert update_installed_plugins(run, _DECLARED, "") == ["paad@paad (user)"]
        assert any("claude plugin update vergil@vergil-marketplace" in c for c in run.cmds)

    def test_enabled_install_from_an_undeclared_marketplace_is_fatal(self) -> None:
        # #3995 N1: a project's own settings can enable an install whose
        # marketplace the user settings.json does not declare; its catalog was
        # never refreshed, so the refresh stops before any update or install.
        listing = json.dumps(
            [
                {"id": "paad@paad", "scope": "user", "enabled": True},
                {"id": "x@proj-market", "scope": "project", "enabled": True, "projectPath": "/p"},
                {"id": "y@other", "scope": "user", "enabled": True},
                {"id": "z@unused", "scope": "user", "enabled": False},
            ]
        )
        run = FakeRun(plugin_list=listing)
        settings = _declaring(enabled={"new@paad": True})
        with pytest.raises(
            MarketplaceError,
            match=(
                r"^claude plugin list --json reports an enabled install from a marketplace "
                r"settings\.json extraKnownMarketplaces does not declare: "
                r"x@proj-market \(project: /p\) \(marketplace proj-market\), "
                r"y@other \(user\) \(marketplace other\)$"
            ),
        ):
            update_installed_plugins(run, settings, "")
        assert run.cmds == ["claude plugin list --json"]

    def test_official_install_counts_only_when_the_refresh_refreshed_it(self) -> None:
        listing = json.dumps(
            [{"id": "s@claude-plugins-official", "scope": "user", "enabled": True}]
        )
        with pytest.raises(MarketplaceError, match=r"\(marketplace claude-plugins-official\)$"):
            update_installed_plugins(FakeRun(plugin_list=listing), _DECLARED, "")
        # A settings plugin from it makes the refresh add and update it.
        settings = _declaring(enabled={"other@claude-plugins-official": True})
        run = FakeRun(plugin_list=listing)
        assert update_installed_plugins(run, settings, "") == []
        assert "claude plugin update s@claude-plugins-official --scope user" in run.cmds

    def test_enabled_install_without_a_marketplace_suffix_is_not_checked(self) -> None:
        run = FakeRun(plugin_list=json.dumps([{"id": "bare", "scope": "user", "enabled": True}]))
        assert update_installed_plugins(run, {}, "") == []
        assert "claude plugin update bare --scope user" in run.cmds

    def test_refresh_stops_on_an_undeclared_enabled_install(self) -> None:
        listing = json.dumps([{"id": "y@other", "scope": "user", "enabled": True}])
        run = FakeRun(plugin_list=listing)
        with pytest.raises(MarketplaceError, match=r"y@other \(user\) \(marketplace other\)$"):
            refresh(run, SETTINGS_V21, "")
        assert not any("plugin update" in c or "plugin install" in c for c in run.cmds)

    @pytest.mark.parametrize("stdout", ["nope", '{"id": "x"}', '[{"scope": "user"}]'])
    def test_malformed_plugin_list_raises(self, stdout: str) -> None:
        with pytest.raises(MarketplaceError):
            update_installed_plugins(FakeRun(plugin_list=stdout), {}, "")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("scope", None),
            ("scope", 2),
            ("projectPath", None),
            ("projectPath", ["/p"]),
            ("enabled", "false"),
            ("enabled", 1),
        ],
    )
    def test_misshapen_install_entry_raises_before_any_update(
        self, field: str, value: object
    ) -> None:
        entry = {"id": "vergil@vergil-marketplace", "scope": "user", "enabled": True}
        entry[field] = value
        run = FakeRun(plugin_list=json.dumps([entry]))
        with pytest.raises(MarketplaceError, match=field):
            update_installed_plugins(run, _declaring(enabled={entry["id"]: True}), "")
        assert not any("claude plugin update" in c for c in run.cmds)


def test_marketplace_error_is_a_runtime_error() -> None:
    assert issubclass(claude_marketplace.MarketplaceError, RuntimeError)
