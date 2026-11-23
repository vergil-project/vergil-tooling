"""Tests for vergil_tooling.lib.automation_identity (epic vergil-project/.github#1064)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.automation_identity import (
    AUTOMATION_ENV,
    ORG_VARIABLE,
    AutomationIdentityError,
    automation_app_id,
    automation_app_slug,
    env_slug,
    verify_token_is_app,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from pathlib import Path

_SLUG = "example-org-automation"
# Invented orgs and logins only.
_IDENTITIES = """\
[trusted_authors]
human = ["example-human"]
agents = ["example-agent"]

[orgs.example-org]
automation_app = "example-org-automation"

[orgs.example-other-org]
automation_app = "example-other-automation"
"""
_READ_JSON = "vergil_tooling.lib.github.read_json"
_GRAPHQL = "vergil_tooling.lib.github.graphql"


def _api_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api"), "", stderr)


def test_names() -> None:
    assert AUTOMATION_ENV == "VRG_AUTOMATION_APP"
    assert ORG_VARIABLE == "VRG_AUTOMATION_APP"


class TestEnvSlug:
    def test_unset_is_none(self) -> None:
        assert env_slug() is None

    def test_value_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTOMATION_ENV, f"  {_SLUG}\n")
        assert env_slug() == _SLUG

    @pytest.mark.parametrize("value", ["", " \t"])
    def test_set_but_empty_raises(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(AUTOMATION_ENV, value)
        with pytest.raises(AutomationIdentityError, match="set but empty"):
            env_slug()


class TestAutomationAppSlug:
    """The slug: ``$VRG_AUTOMATION_APP`` for the job's own org, else the configuration."""

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("VRG_TRUSTED_AUTHORS", raising=False)
        monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)

    @staticmethod
    def _write_config(tmp_path: Path) -> None:
        d = tmp_path / ".config" / "vergil"
        d.mkdir(parents=True)
        (d / "identities.toml").write_text(_IDENTITIES)

    def test_automation_app_slug_env_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTOMATION_ENV, _SLUG)
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "Example-Org")
        with patch(_READ_JSON) as read:
            assert automation_app_slug("example-org") == _SLUG
        read.assert_not_called()

    def test_own_org_empty_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTOMATION_ENV, "")
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example-org")
        with pytest.raises(AutomationIdentityError, match="set but empty"):
            automation_app_slug("example-org")

    def test_automation_app_slug_other_org_ignores_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_config(tmp_path)
        monkeypatch.setenv(AUTOMATION_ENV, _SLUG)
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example-org")
        with patch(_READ_JSON) as read:
            assert automation_app_slug("example-other-org") == "example-other-automation"
        read.assert_not_called()

    def test_env_without_repository_owner_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A process outside Actions that sets the variable by hand names no
        # org of its own: the configuration decides.
        self._write_config(tmp_path)
        monkeypatch.setenv(AUTOMATION_ENV, "example-hand-set")
        assert automation_app_slug("example-org") == _SLUG

    def test_automation_app_slug_reads_config(self, tmp_path: Path) -> None:
        self._write_config(tmp_path)
        with patch(_READ_JSON) as read:
            assert automation_app_slug("example-org") == _SLUG
            assert automation_app_slug("EXAMPLE-ORG") == _SLUG
        read.assert_not_called()

    def test_automation_app_slug_unlisted_org_raises(self, tmp_path: Path) -> None:
        self._write_config(tmp_path)
        with pytest.raises(AutomationIdentityError, match=r"\[orgs\.example-unlisted\]"):
            automation_app_slug("example-unlisted")

    def test_own_org_without_env_reads_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_config(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example-org")
        assert automation_app_slug("example-org") == _SLUG

    def test_unconfigured_raises(self) -> None:
        with pytest.raises(AutomationIdentityError, match="not configured") as excinfo:
            automation_app_slug("example-org")
        assert isinstance(excinfo.value.__cause__, TrustedAuthorsNotConfiguredError)

    def test_invalid_config_raises(self, tmp_path: Path) -> None:
        d = tmp_path / ".config" / "vergil"
        d.mkdir(parents=True)
        (d / "identities.toml").write_text(
            '[trusted_authors]\nhuman = ["example-human"]\nagents = ["example-agent"]\n'
        )
        with pytest.raises(AutomationIdentityError, match=r"no \[orgs\.<org>\] stanza"):
            automation_app_slug("example-org")


class TestAutomationAppId:
    def test_automation_app_id_parses_id(self) -> None:
        payload = {"id": 1234567, "slug": _SLUG, "owner": {"login": "vergil-project"}}
        with patch(_READ_JSON, return_value=payload) as read:
            assert automation_app_id(_SLUG) == 1234567
        # Authenticated: through the run's own token, never an anonymous request.
        read.assert_called_once_with("api", f"apps/{_SLUG}")

    @pytest.mark.parametrize("payload", [{}, {"id": "1234567"}, {"id": True}, {"id": None}, []])
    def test_no_numeric_id_raises(self, payload: object) -> None:
        with (
            patch(_READ_JSON, return_value=payload),
            pytest.raises(AutomationIdentityError, match="no numeric id"),
        ):
            automation_app_id(_SLUG)

    def test_lookup_failure_raises(self) -> None:
        with (
            patch(_READ_JSON, side_effect=_api_error("gh: Not Found (HTTP 404)")),
            pytest.raises(AutomationIdentityError, match="could not look up") as excinfo,
        ):
            automation_app_id(_SLUG)
        assert "HTTP 404" in str(excinfo.value)


class TestVerifyTokenIsApp:
    def test_matching_login_passes(self) -> None:
        with patch(_GRAPHQL, return_value={"viewer": {"login": f"{_SLUG}[bot]"}}):
            verify_token_is_app(_SLUG)

    @pytest.mark.parametrize(
        "login", [_SLUG, "vergil-project-user[bot]", "github-actions[bot]", "wphillipmoore"]
    )
    def test_other_login_raises_naming_both(self, login: str) -> None:
        with (
            patch(_GRAPHQL, return_value={"viewer": {"login": login}}),
            pytest.raises(AutomationIdentityError) as excinfo,
        ):
            verify_token_is_app(_SLUG)
        assert f"belongs to {login!r}" in str(excinfo.value)
        assert f"not '{_SLUG}[bot]'" in str(excinfo.value)

    @pytest.mark.parametrize("data", [{}, {"viewer": None}, {"viewer": {}}])
    def test_missing_login_raises(self, data: dict[str, object]) -> None:
        with (
            patch(_GRAPHQL, return_value=data),
            pytest.raises(AutomationIdentityError, match="belongs to None"),
        ):
            verify_token_is_app(_SLUG)

    def test_graphql_error_raises(self) -> None:
        with (
            patch(_GRAPHQL, side_effect=RuntimeError("GraphQL errors: [boom]")),
            pytest.raises(AutomationIdentityError, match="could not be read.*boom"),
        ):
            verify_token_is_app(_SLUG)
