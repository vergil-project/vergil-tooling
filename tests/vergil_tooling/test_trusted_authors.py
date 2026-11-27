"""Tests for the trusted-authors configuration and the vrg-trusted-authors CLI.

Every login, slug and org here is invented (``example-*``); no real one
appears in a fixture.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin.vrg_trusted_authors import main
from vergil_tooling.lib import epic_size, epics, trusted_authors
from vergil_tooling.lib.release import tracking
from vergil_tooling.lib.rulings import comments
from vergil_tooling.lib.trusted_authors import (
    OrgConfig,
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
    org_config,
    parse_trusted_authors,
    render_actions_value,
    render_vm_file,
    vergil_orgs,
)

if TYPE_CHECKING:
    from pathlib import Path

_ORGS: dict[str, object] = {"example-org": {"automation_app": "example-org-automation"}}
_ORG_CONFIGS = {"example-org": OrgConfig(automation_app="example-org-automation")}

_IDENTITIES_STANZA = """\
[trusted_authors]
human = ["example-human"]
agents = ["example-agent", "example-audit-agent"]

[orgs.example-org]
automation_app = "example-org-automation"

[orgs.example-other-org]
automation_app = "example-other-automation"

[identities.vergil-user]
vm_instance = "vergil-user"
"""

_STANZA_AUTHORS = TrustedAuthors(
    human=("example-human",),
    agents=("example-agent", "example-audit-agent"),
    orgs={
        "example-org": OrgConfig(automation_app="example-org-automation"),
        "example-other-org": OrgConfig(automation_app="example-other-automation"),
    },
)

_VM_FILE = """\
human = ["example-vm-human"]
agents = ["example-vm-agent"]

[orgs.example-org]
automation_app = "example-org-automation"
"""

_ACTIONS_VALUE = json.dumps(
    {
        "human": ["example-env-human"],
        "agents": ["example-env-agent"],
        "orgs": {"example-org": {"automation_app": "example-org-automation"}},
    }
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at tmp_path so the real ~/.config/vergil is never read.

    The Actions variables are cleared too, so a run under CI sees what a local
    run sees unless a test sets them.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


def _vergil_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "vergil"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_identities(tmp_path: Path, text: str) -> None:
    (_vergil_dir(tmp_path) / "identities.toml").write_text(text)


def _write_vm_file(tmp_path: Path, text: str) -> None:
    (_vergil_dir(tmp_path) / "trusted-authors.toml").write_text(text)


class TestParse:
    def test_parse_trusted_authors_reads_both_lists(self) -> None:
        authors = parse_trusted_authors(
            {"human": ["example-human"], "agents": ["example-agent", "example-audit-agent"]},
            _ORGS,
        )
        assert authors == TrustedAuthors(
            human=("example-human",),
            agents=("example-agent", "example-audit-agent"),
            orgs=_ORG_CONFIGS,
        )

    def test_parse_orgs_stanza(self) -> None:
        orgs = {
            "example-org": {"automation_app": "example-org-automation"},
            "example-other-org": {"automation_app": "example-other-automation"},
        }
        authors = parse_trusted_authors(
            {"human": ["example-human"], "agents": ["example-agent"]}, orgs
        )
        assert authors.orgs == {
            "example-org": OrgConfig(automation_app="example-org-automation"),
            "example-other-org": OrgConfig(automation_app="example-other-automation"),
        }
        assert vergil_orgs(authors) == ("example-org", "example-other-org")

    @pytest.mark.parametrize(
        "raw", [{"agents": ["example-agent"]}, {"human": [], "agents": ["example-agent"]}]
    )
    def test_parse_trusted_authors_requires_a_human(self, raw: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="'human'"):
            parse_trusted_authors(raw, _ORGS)

    @pytest.mark.parametrize(
        "raw", [{"human": ["example-human"]}, {"human": ["example-human"], "agents": []}]
    )
    def test_parse_trusted_authors_requires_an_agent(self, raw: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="'agents'"):
            parse_trusted_authors(raw, _ORGS)

    def test_parse_trusted_authors_rejects_login_in_both_lists(self) -> None:
        with pytest.raises(ValueError, match="both 'human' and 'agents': example-human"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent", "example-human"]},
                _ORGS,
            )

    def test_parse_rejects_automation_app_in_another_role(self) -> None:
        with pytest.raises(ValueError, match=r"orgs\.example-org.*also a 'human' or 'agents'"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": {"automation_app": "example-agent"}},
            )

    def test_parse_trusted_authors_rejects_app_prefixed_login(self) -> None:
        with pytest.raises(ValueError, match="'app/' prefix"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["app/example-agent"]}, _ORGS
            )

    @pytest.mark.parametrize(
        ("raw", "orgs", "where"),
        [
            (
                {"human": ["example-human"], "agents": ["example-agent[bot]"]},
                _ORGS,
                "'agents'",
            ),
            (
                {"human": ["example-human[bot]"], "agents": ["example-agent"]},
                _ORGS,
                "'human'",
            ),
            (
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": {"automation_app": "example-org-automation[bot]"}},
                r"orgs\.example-org: 'automation_app'",
            ),
            (
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": {"automation_app": "app/example-org-automation"}},
                r"orgs\.example-org: 'automation_app'",
            ),
        ],
    )
    def test_parse_rejects_bot_suffix(
        self, raw: dict[str, object], orgs: dict[str, object], where: str
    ) -> None:
        with pytest.raises(ValueError, match=rf"{where} entry .*(\[bot\]' suffix|'app/' prefix)"):
            parse_trusted_authors(raw, orgs)

    @pytest.mark.parametrize(
        "stanza",
        [{}, {"automation_app": ""}, {"automation_app": "  "}, {"automation_app": 7}],
    )
    def test_parse_rejects_org_without_automation_app(self, stanza: dict[str, object]) -> None:
        with pytest.raises(ValueError, match=r"orgs\.example-org: 'automation_app'"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": stanza},
            )

    def test_parse_rejects_padded_automation_app(self) -> None:
        with pytest.raises(ValueError, match="surrounding whitespace"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": {"automation_app": " example-org-automation"}},
            )

    @pytest.mark.parametrize("org", ["", " ", " example-org"])
    def test_parse_rejects_bad_org_name(self, org: str) -> None:
        with pytest.raises(ValueError, match="org name"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {org: {"automation_app": "example-org-automation"}},
            )

    def test_parse_rejects_non_table_org(self) -> None:
        with pytest.raises(ValueError, match=r"\[orgs\.example-org\] must be a table"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {"example-org": "example-org-automation"},
            )

    def test_parse_rejects_orgs_differing_only_in_case(self) -> None:
        with pytest.raises(ValueError, match="differ only in case"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent"]},
                {
                    "example-org": {"automation_app": "example-a"},
                    "Example-Org": {"automation_app": "example-b"},
                },
            )

    def test_parse_rejects_no_orgs(self) -> None:
        with pytest.raises(ValueError, match=r"no \[orgs\.<org>\] stanza"):
            parse_trusted_authors({"human": ["example-human"], "agents": ["example-agent"]}, {})

    @pytest.mark.parametrize("entry", ["", "   ", 7, None, " example-agent"])
    def test_parse_trusted_authors_rejects_bad_entry(self, entry: object) -> None:
        with pytest.raises(ValueError, match="'agents'"):
            parse_trusted_authors({"human": ["example-human"], "agents": [entry]}, _ORGS)

    def test_parse_trusted_authors_rejects_non_list(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_trusted_authors({"human": "example-human", "agents": ["example-agent"]}, _ORGS)


def test_org_config_matches_case_insensitively() -> None:
    assert org_config(_STANZA_AUTHORS, "EXAMPLE-ORG") == OrgConfig(
        automation_app="example-org-automation"
    )
    assert org_config(_STANZA_AUTHORS, "example-unlisted-org") is None


class TestLoad:
    def test_load_reads_identities_stanza(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert load_trusted_authors() == _STANZA_AUTHORS

    def test_load_prefers_vm_file_over_identities(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        _write_vm_file(tmp_path, _VM_FILE)
        assert load_trusted_authors() == TrustedAuthors(
            human=("example-vm-human",), agents=("example-vm-agent",), orgs=_ORG_CONFIGS
        )

    def test_load_raises_when_not_configured(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, '[identities.vergil-user]\nvm_instance = "vergil-user"\n')
        with pytest.raises(TrustedAuthorsNotConfiguredError) as excinfo:
            load_trusted_authors()
        message = str(excinfo.value)
        assert "trusted-authors.toml" in message
        assert "identities.toml" in message

    def test_load_raises_when_no_files_exist(self) -> None:
        with pytest.raises(TrustedAuthorsNotConfiguredError):
            load_trusted_authors()

    def test_load_rejects_invalid_vm_file(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        _write_vm_file(tmp_path, 'human = ["example-human"]\n')
        with pytest.raises(ValueError, match=r"trusted-authors\.toml.*'agents'"):
            load_trusted_authors()

    def test_load_rejects_malformed_toml(self, tmp_path: Path) -> None:
        _write_vm_file(tmp_path, "human = [\n")
        with pytest.raises(ValueError, match="invalid TOML"):
            load_trusted_authors()

    def test_load_rejects_invalid_stanza(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, '[trusted_authors]\nhuman = ["example-human"]\nagents = []\n')
        with pytest.raises(ValueError, match=r"identities\.toml.*'agents'"):
            load_trusted_authors()

    def test_load_rejects_non_table_stanza(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, 'trusted_authors = "example-human"\n')
        with pytest.raises(ValueError, match=r"\[trusted_authors\] must be a table"):
            load_trusted_authors()

    def test_load_rejects_no_orgs_naming_the_file(self, tmp_path: Path) -> None:
        _write_identities(
            tmp_path, '[trusted_authors]\nhuman = ["example-human"]\nagents = ["example-agent"]\n'
        )
        with pytest.raises(ValueError, match=r"identities\.toml: no \[orgs\.<org>\] stanza"):
            load_trusted_authors()

    def test_load_rejects_vm_file_without_orgs(self, tmp_path: Path) -> None:
        _write_vm_file(tmp_path, 'human = ["example-human"]\nagents = ["example-agent"]\n')
        with pytest.raises(ValueError, match=r"trusted-authors\.toml: no \[orgs\.<org>\] stanza"):
            load_trusted_authors()

    def test_load_rejects_non_table_orgs(self, tmp_path: Path) -> None:
        _write_identities(
            tmp_path,
            '[trusted_authors]\nhuman = ["example-human"]\nagents = ["example-agent"]\n'
            '[orgs]\n"example-org" = "example-org-automation"\n',
        )
        with pytest.raises(ValueError, match=r"identities\.toml: \[orgs\.example-org\] must be"):
            load_trusted_authors()

    def test_load_under_actions_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", _ACTIONS_VALUE)
        assert load_trusted_authors() == TrustedAuthors(
            human=("example-env-human",), agents=("example-env-agent",), orgs=_ORG_CONFIGS
        )

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_load_under_actions_without_env_raises(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        if value is not None:
            monkeypatch.setenv("VRG_TRUSTED_AUTHORS", value)
        with pytest.raises(TrustedAuthorsNotConfiguredError, match=r"\$VRG_TRUSTED_AUTHORS"):
            load_trusted_authors()

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            ("{", "invalid JSON"),
            ("[]", "must be a JSON object"),
            ('{"human": ["example-human"], "agents": ["example-agent"]}', "no \\[orgs"),
            ('{"human": ["example-human"], "agents": ["example-agent"], "orgs": []}', "\\[orgs\\]"),
        ],
    )
    def test_load_under_actions_rejects_invalid_env(
        self, monkeypatch: pytest.MonkeyPatch, value: str, match: str
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", value)
        with pytest.raises(ValueError, match=rf"\$VRG_TRUSTED_AUTHORS: .*{match}"):
            load_trusted_authors()

    def test_load_env_ignored_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", _ACTIONS_VALUE)
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert load_trusted_authors() == _STANZA_AUTHORS

    def test_load_env_ignored_when_identities_has_no_stanza(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", _ACTIONS_VALUE)
        _write_identities(tmp_path, '[identities.vergil-user]\nvm_instance = "vergil-user"\n')
        with pytest.raises(TrustedAuthorsNotConfiguredError):
            load_trusted_authors()

    @pytest.mark.parametrize("actions", [None, "false", "1"])
    def test_load_env_ignored_outside_actions(
        self, monkeypatch: pytest.MonkeyPatch, actions: str | None
    ) -> None:
        if actions is not None:
            monkeypatch.setenv("GITHUB_ACTIONS", actions)
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", _ACTIONS_VALUE)
        with pytest.raises(TrustedAuthorsNotConfiguredError):
            load_trusted_authors()


class TestRenderVmFile:
    def test_render_round_trips(self) -> None:
        authors = TrustedAuthors(
            human=("example-human",), agents=("example-agent", "example-x"), orgs=_ORG_CONFIGS
        )
        raw = tomllib.loads(render_vm_file(authors))
        assert parse_trusted_authors(raw, raw["orgs"]) == authors

    def test_vm_file_round_trips_orgs(self, tmp_path: Path) -> None:
        _write_vm_file(tmp_path, render_vm_file(_STANZA_AUTHORS))
        assert load_trusted_authors() == _STANZA_AUTHORS


class TestRenderActionsValue:
    def test_render_actions_value_has_every_org(self) -> None:
        value = render_actions_value(_STANZA_AUTHORS)
        assert " " not in value
        assert json.loads(value) == {
            "human": ["example-human"],
            "agents": ["example-agent", "example-audit-agent"],
            "orgs": {
                "example-org": {"automation_app": "example-org-automation"},
                "example-other-org": {"automation_app": "example-other-automation"},
            },
        }

    def test_render_actions_value_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("VRG_TRUSTED_AUTHORS", render_actions_value(_STANZA_AUTHORS))
        assert load_trusted_authors() == _STANZA_AUTHORS


class TestCli:
    def test_cli_prints_logins(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert main([]) == 0
        assert capsys.readouterr().out == (
            "human: example-human\nagent: example-agent\nagent: example-audit-agent\n"
            "org: example-org automation: example-org-automation\n"
            "org: example-other-org automation: example-other-automation\n"
        )

    def test_cli_prints_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert main(["--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "human": ["example-human"],
            "agents": ["example-agent", "example-audit-agent"],
            "orgs": {
                "example-org": {"automation_app": "example-org-automation"},
                "example-other-org": {"automation_app": "example-other-automation"},
            },
        }

    def test_cli_exits_1_when_not_configured(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "not configured" in captured.err

    def test_cli_exits_1_when_invalid(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_vm_file(tmp_path, 'human = ["example-human"]\nagents = ["app/example-agent"]\n')
        assert main(["--json"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "'app/' prefix" in captured.err


def test_old_helpers_removed() -> None:
    """The login-only readers are gone: every trust decision goes through the trust layer."""
    for module, name in (
        (trusted_authors, "trusted_comment_bodies"),
        (trusted_authors, "trusted_comment_lines"),
        (trusted_authors, "bare_login"),
        (epic_size, "trusted_logins"),
        (comments, "fetch_comments"),
        (comments, "trusted"),
        (epics, "issue_author"),
        (tracking, "_human_logins"),
    ):
        assert not hasattr(module, name), f"{module.__name__}.{name} still exists"
