"""Tests for vergil_tooling.lib.automation_identity (epic vergil-project/.github#1064)."""

from __future__ import annotations

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

_SLUG = "vergil-project-automation"
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
    def test_env_wins_without_api_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTOMATION_ENV, _SLUG)
        with patch(_READ_JSON) as read:
            assert automation_app_slug("vergil-project") == _SLUG
        read.assert_not_called()

    def test_automation_app_slug_reads_org_variable_without_env(self) -> None:
        payload = {"name": ORG_VARIABLE, "value": f"{_SLUG}\n", "visibility": "all"}
        with patch(_READ_JSON, return_value=payload) as read:
            assert automation_app_slug("vergil-project") == _SLUG
        read.assert_called_once_with(
            "api", "orgs/vergil-project/actions/variables/VRG_AUTOMATION_APP"
        )

    def test_automation_app_slug_missing_raises(self) -> None:
        with (
            patch(_READ_JSON, side_effect=_api_error("gh: Not Found (HTTP 404)")),
            pytest.raises(AutomationIdentityError, match="is not set in vergil-project"),
        ):
            automation_app_slug("vergil-project")

    @pytest.mark.parametrize("payload", [{"value": ""}, {"value": "  "}, {}, {"value": 7}, []])
    def test_empty_org_variable_raises(self, payload: object) -> None:
        with (
            patch(_READ_JSON, return_value=payload),
            pytest.raises(AutomationIdentityError, match="is empty"),
        ):
            automation_app_slug("vergil-project")

    def test_other_read_failure_names_the_cause(self) -> None:
        with (
            patch(_READ_JSON, side_effect=_api_error("gh: Must have admin rights (HTTP 403)")),
            pytest.raises(AutomationIdentityError, match="HTTP 403") as excinfo,
        ):
            automation_app_slug("vergil-project")
        assert "could not read" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, github.GitHubAPIError)


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
