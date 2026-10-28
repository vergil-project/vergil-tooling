"""Tests for the trusted-authors configuration and the vrg-trusted-authors CLI.

Every login here is invented (``example-*``); no real login appears in a fixture.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin.vrg_trusted_authors import main
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
    parse_trusted_authors,
    render_vm_file,
)

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITIES_STANZA = """\
[trusted_authors]
human = ["example-human"]
agents = ["example-agent", "example-audit-agent"]

[identities.vergil-user]
vm_instance = "vergil-user"
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at tmp_path so the real ~/.config/vergil is never read."""
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
            {"human": ["example-human"], "agents": ["example-agent", "example-audit-agent"]}
        )
        assert authors == TrustedAuthors(
            human=("example-human",), agents=("example-agent", "example-audit-agent")
        )

    @pytest.mark.parametrize(
        "raw", [{"agents": ["example-agent"]}, {"human": [], "agents": ["example-agent"]}]
    )
    def test_parse_trusted_authors_requires_a_human(self, raw: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="'human'"):
            parse_trusted_authors(raw)

    @pytest.mark.parametrize(
        "raw", [{"human": ["example-human"]}, {"human": ["example-human"], "agents": []}]
    )
    def test_parse_trusted_authors_requires_an_agent(self, raw: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="'agents'"):
            parse_trusted_authors(raw)

    def test_parse_trusted_authors_rejects_login_in_both_lists(self) -> None:
        with pytest.raises(ValueError, match="both 'human' and 'agents': example-human"):
            parse_trusted_authors(
                {"human": ["example-human"], "agents": ["example-agent", "example-human"]}
            )

    def test_parse_trusted_authors_rejects_app_prefixed_login(self) -> None:
        with pytest.raises(ValueError, match="'app/' prefix"):
            parse_trusted_authors({"human": ["example-human"], "agents": ["app/example-agent"]})

    @pytest.mark.parametrize("entry", ["", "   ", 7, None, " example-agent"])
    def test_parse_trusted_authors_rejects_bad_entry(self, entry: object) -> None:
        with pytest.raises(ValueError, match="'agents'"):
            parse_trusted_authors({"human": ["example-human"], "agents": [entry]})

    def test_parse_trusted_authors_rejects_non_list(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_trusted_authors({"human": "example-human", "agents": ["example-agent"]})


class TestLoad:
    def test_load_reads_identities_stanza(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert load_trusted_authors() == TrustedAuthors(
            human=("example-human",), agents=("example-agent", "example-audit-agent")
        )

    def test_load_prefers_vm_file_over_identities(self, tmp_path: Path) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        _write_vm_file(tmp_path, 'human = ["example-vm-human"]\nagents = ["example-vm-agent"]\n')
        assert load_trusted_authors() == TrustedAuthors(
            human=("example-vm-human",), agents=("example-vm-agent",)
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


class TestRenderVmFile:
    def test_render_round_trips(self) -> None:
        authors = TrustedAuthors(human=("example-human",), agents=("example-agent", "example[bot]"))
        assert parse_trusted_authors(tomllib.loads(render_vm_file(authors))) == authors


class TestCli:
    def test_cli_prints_logins(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert main([]) == 0
        assert capsys.readouterr().out == (
            "human: example-human\nagent: example-agent\nagent: example-audit-agent\n"
        )

    def test_cli_prints_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_identities(tmp_path, _IDENTITIES_STANZA)
        assert main(["--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "human": ["example-human"],
            "agents": ["example-agent", "example-audit-agent"],
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
