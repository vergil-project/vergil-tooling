"""Tests for vergil_tooling.lib.identity_mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from vergil_tooling.lib.identity_mode import (
    IdentityMode,
    current_mode,
    is_agent,
    is_human,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate HOME so real ~/.config/vergil files can't leak into tests."""
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


class TestCurrentMode:
    def test_user_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert current_mode() == IdentityMode.USER

    def test_audit_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert current_mode() == IdentityMode.AUDIT

    def test_human_when_no_env_and_no_app(self) -> None:
        assert current_mode() == IdentityMode.HUMAN

    def test_fallback_to_user_with_app_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_APP_ID", "12345")
        assert current_mode() == IdentityMode.USER

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "USER")
        assert current_mode() == IdentityMode.USER

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "  audit  ")
        assert current_mode() == IdentityMode.AUDIT

    def test_invalid_mode_with_app_creds_falls_back_to_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "invalid")
        monkeypatch.setenv("VRG_APP_ID", "12345")
        assert current_mode() == IdentityMode.USER

    def test_invalid_mode_without_app_creds_falls_back_to_human(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "invalid")
        assert current_mode() == IdentityMode.HUMAN


class TestCurrentModeFileFallback:
    def test_user_from_mode_file(self, tmp_path: Path) -> None:
        _write_mode_file(tmp_path, "user\n")
        assert current_mode() == IdentityMode.USER

    def test_audit_from_mode_file(self, tmp_path: Path) -> None:
        _write_mode_file(tmp_path, "audit\n")
        assert current_mode() == IdentityMode.AUDIT

    def test_env_wins_over_mode_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_mode_file(tmp_path, "user\n")
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert current_mode() == IdentityMode.AUDIT

    def test_invalid_mode_file_without_app_key_is_human(self, tmp_path: Path) -> None:
        _write_mode_file(tmp_path, "garbage\n")
        assert current_mode() == IdentityMode.HUMAN

    def test_invalid_mode_file_with_app_key_is_user(self, tmp_path: Path) -> None:
        _write_mode_file(tmp_path, "garbage\n")
        _write_app_key(tmp_path)
        assert current_mode() == IdentityMode.USER

    def test_app_key_presence_means_user(self, tmp_path: Path) -> None:
        _write_app_key(tmp_path)
        assert current_mode() == IdentityMode.USER

    def test_mode_file_wins_over_app_key(self, tmp_path: Path) -> None:
        _write_mode_file(tmp_path, "audit\n")
        _write_app_key(tmp_path)
        assert current_mode() == IdentityMode.AUDIT


class TestIsAgent:
    def test_user_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_agent() is True

    def test_audit_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert is_agent() is True

    def test_human_is_not_agent(self) -> None:
        assert is_agent() is False

    def test_provisioned_vm_is_agent(self, tmp_path: Path) -> None:
        _write_app_key(tmp_path)
        assert is_agent() is True


class TestIsHuman:
    def test_human_when_no_env(self) -> None:
        assert is_human() is True

    def test_agent_is_not_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_human() is False
