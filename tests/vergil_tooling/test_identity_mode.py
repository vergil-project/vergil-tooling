"""Tests for vergil_tooling.lib.identity_mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.identity_mode import (
    IdentityMode,
    current_mode,
    is_agent,
    is_human,
)

if TYPE_CHECKING:
    import pytest


class TestCurrentMode:
    def test_user_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert current_mode() == IdentityMode.USER

    def test_audit_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert current_mode() == IdentityMode.AUDIT

    def test_human_when_no_env_and_no_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert current_mode() == IdentityMode.HUMAN

    def test_fallback_to_user_with_app_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
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
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert current_mode() == IdentityMode.HUMAN


class TestIsAgent:
    def test_user_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_agent() is True

    def test_audit_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert is_agent() is True

    def test_human_is_not_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert is_agent() is False


class TestIsHuman:
    def test_human_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert is_human() is True

    def test_agent_is_not_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_human() is False
