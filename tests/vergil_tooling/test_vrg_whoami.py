"""Tests for the vrg-whoami CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from vergil_tooling.bin.vrg_whoami import main


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate HOME and env so real provisioning state can't leak in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
    monkeypatch.delenv("VRG_APP_ID", raising=False)


def _vergil_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "vergil"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_mode_file(tmp_path: Path, value: str) -> None:
    (_vergil_dir(tmp_path) / "identity-mode").write_text(value)


def _write_app_key(tmp_path: Path) -> None:
    (_vergil_dir(tmp_path) / "app.pem").write_text("fake-key\n")


class TestBareAndMode:
    def test_bare_prints_role_token(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert main([]) == 0
        assert capsys.readouterr().out.strip() == "user"

    def test_mode_flag_prints_role_token(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert main(["--mode"]) == 0
        assert capsys.readouterr().out.strip() == "audit"

    def test_mode_token_is_a_single_bare_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The provisioning contract: export VRG_IDENTITY_MODE="$(vrg-whoami --mode)".
        _write_mode_file(tmp_path, "user\n")
        assert main(["--mode"]) == 0
        out = capsys.readouterr().out
        assert out == "user\n"

    def test_default_human_when_no_signal(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 0
        assert capsys.readouterr().out.strip() == "human"

    def test_unset_env_resolves_via_mode_file_not_human(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bug class this tool eliminates: empty env != HUMAN.
        _write_mode_file(tmp_path, "user\n")
        assert main(["--mode"]) == 0
        assert capsys.readouterr().out.strip() == "user"


class TestExplain:
    def test_reports_role_and_resolving_signal(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert main(["--explain"]) == 0
        captured = capsys.readouterr()
        assert "role:          user" in captured.out
        assert "resolved from: environment variable" in captured.out

    def test_lists_every_signal_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_mode_file(tmp_path, "user\n")
        assert main(["--explain"]) == 0
        out = capsys.readouterr().out
        assert "environment variable ($VRG_IDENTITY_MODE): absent" in out
        assert "mode file (~/.config/vergil/identity-mode): user <-- resolved" in out
        assert "app credential (~/.config/vergil/app.pem): absent" in out
        assert "app id ($VRG_APP_ID): absent" in out

    def test_unrecognized_value_is_shown(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "garbage")
        assert main(["--explain"]) == 0
        out = capsys.readouterr().out
        assert "environment variable ($VRG_IDENTITY_MODE): present (unrecognized value)" in out
        assert "role:          human" in out

    def test_warns_on_disagreement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "human")
        _write_app_key(tmp_path)
        assert main(["--explain"]) == 0
        captured = capsys.readouterr()
        assert "WARNING: identity signals disagree" in captured.err
        # the resolved role still prints to stdout
        assert "role:          human" in captured.out

    def test_no_warning_when_signals_agree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_mode_file(tmp_path, "user\n")
        _write_app_key(tmp_path)
        assert main(["--explain"]) == 0
        assert "WARNING" not in capsys.readouterr().err


def test_mode_and_explain_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main(["--mode", "--explain"])
