"""Required-status-check awareness in vergil_tooling.lib.github (#3061)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from vergil_tooling.lib import github


@pytest.fixture(autouse=True)
def _no_credential_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)


def _check(name: str, bucket: str) -> dict[str, str]:
    return {"name": name, "bucket": bucket, "state": "", "link": ""}


_REQUIRED = frozenset({"test / evidence", "quality / evidence"})


# --- terminal / outstanding evaluation --------------------------------------


def test_all_checks_terminal_false_when_required_check_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The #1270 window: every *registered* check is done, but the required
    # aggregator checks do not exist yet.
    monkeypatch.setattr(github, "pr_checks", lambda pr: [_check("test / unit / 3.14", "pass")])
    assert github.all_checks_terminal("1", _REQUIRED) is False
    assert github.all_checks_terminal("1") is True  # the old, registered-only verdict


def test_all_checks_terminal_false_when_required_check_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "pr_checks",
        lambda pr: [_check("test / evidence", "pass"), _check("quality / evidence", "pending")],
    )
    assert github.all_checks_terminal("1", _REQUIRED) is False


def test_all_checks_terminal_true_when_required_checks_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "pr_checks",
        lambda pr: [
            _check("test / evidence", "pass"),
            _check("quality / evidence", "fail"),  # verdict is failed_check_names' job
            _check("extra", "skipping"),
        ],
    )
    assert github.all_checks_terminal("1", _REQUIRED) is True


def test_outstanding_required_checks_reports_reasons_sorted() -> None:
    checks = [
        _check("quality / evidence", "pending"),
        _check("test / evidence", "pending"),
        _check("test / evidence", "pass"),  # a non-pending occurrence counts
    ]
    required = {"quality / evidence", "test / evidence", "audit / evidence"}
    assert github.outstanding_required_checks(checks, required) == {
        "audit / evidence": "not registered",
        "quality / evidence": "pending",
    }


def test_outstanding_required_checks_empty_when_nothing_required() -> None:
    assert github.outstanding_required_checks([], ()) == {}


def test_describe_outstanding() -> None:
    text = github.describe_outstanding({"a": "not registered", "b": "pending"})
    assert text == "a (not registered), b (pending)"


def test_required_checks_outstanding_skips_api_when_nothing_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "pr_checks", lambda pr: pytest.fail("must not query checks"))
    assert github.required_checks_outstanding("1", frozenset()) == {}


def test_required_checks_outstanding_queries_current_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "pr_checks", lambda pr: [_check("test / evidence", "pass")])
    assert github.required_checks_outstanding("1", _REQUIRED) == {
        "quality / evidence": "not registered"
    }


# --- wait_for_checks ---------------------------------------------------------


def test_wait_for_checks_waits_for_unregistered_required_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _check("test / unit / 3.14", "pass")
    batches = iter(
        [
            [unit],  # evidence jobs not registered yet
            [unit],  # unchanged -> no repeated announcement
            [unit, _check("test / evidence", "pending"), _check("quality / evidence", "pending")],
            [unit, _check("test / evidence", "pass"), _check("quality / evidence", "pass")],
        ]
    )
    monkeypatch.setattr(github, "pr_checks", lambda pr: next(batches))
    sleeps: list[int] = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    github.wait_for_checks("1", poll_interval=3, poll_timeout=1000, required=_REQUIRED)
    assert sleeps == [3, 3, 3]
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("Awaiting")]
    assert lines == [
        "Awaiting required checks: quality / evidence (not registered), "
        "test / evidence (not registered)",
        "Awaiting required checks: quality / evidence (pending), test / evidence (pending)",
    ]


def test_wait_for_checks_resolves_required_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        github, "required_check_names", lambda pr: seen.append(pr) or frozenset({"gate"})
    )
    batches = iter([[_check("other", "pass")], [_check("other", "pass"), _check("gate", "pass")]])
    monkeypatch.setattr(github, "pr_checks", lambda pr: next(batches))
    monkeypatch.setattr(github.time, "sleep", lambda s: None)
    github.wait_for_checks("7", poll_interval=1, poll_timeout=1000)
    assert seen == ["7"]


def test_wait_for_checks_lookup_failure_falls_back_to_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "required_check_names", lambda pr: None)
    monkeypatch.setattr(github, "pr_checks", lambda pr: [_check("other", "pass")])
    monkeypatch.setattr(github.time, "sleep", lambda s: pytest.fail("must not wait"))
    github.wait_for_checks("7", poll_interval=1, poll_timeout=1000)


def test_wait_for_checks_timeout_names_outstanding_required_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "pr_checks", lambda pr: [_check("unit", "pass")])
    monkeypatch.setattr(github, "orphaned_check_names", lambda pr: [])
    with pytest.raises(
        github.GitHubAPIError,
        match=r"outstanding required checks: quality / evidence \(not registered\)",
    ):
        github.wait_for_checks("1", poll_interval=0, poll_timeout=0, required=_REQUIRED)


# --- resolving the required set ---------------------------------------------


def _api(responses: dict[str, Any]) -> Any:
    """A read_json stand-in that dispatches on the request's identifying arg."""

    def fake(*args: str) -> Any:
        key = args[1] if args[0] == "api" else args[0]
        value = responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    return fake


_PR_VIEW = {"baseRefName": "develop", "url": "https://github.com/o/r/pull/12"}
_PROTECTION = {
    "protected": True,
    "protection": {
        "enabled": True,
        "required_status_checks": {
            "enforcement_level": "non_admins",
            "contexts": ["legacy / ci"],
            "checks": [{"context": "app / check", "app_id": None}, {"app_id": 3}],
        },
    },
}
_RULES = [
    {"type": "pull_request", "parameters": {}},
    "not-a-rule",
    {"type": "required_status_checks", "parameters": None},
    {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [
                {"context": "test / evidence", "integration_id": 15368},
                {"context": "quality / evidence"},
                "junk",
            ],
        },
    },
]


def test_required_check_names_unions_protection_and_rulesets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    responses = {
        "pr": _PR_VIEW,
        "repos/o/r/branches/develop": _PROTECTION,
        "repos/o/r/rules/branches/develop": _RULES,
    }
    fake = _api(responses)

    def recording(*args: str) -> Any:
        calls.append(args)
        return fake(*args)

    monkeypatch.setattr(github, "read_json", recording)
    monkeypatch.setattr(github, "current_repo", lambda: pytest.fail("repo comes from the URL"))
    assert github.required_check_names("12") == {
        "legacy / ci",
        "app / check",
        "test / evidence",
        "quality / evidence",
    }
    assert ("api", "repos/o/r/branches/develop") in calls
    assert ("api", "repos/o/r/rules/branches/develop") in calls


def test_required_check_names_rulesets_only(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "pr": _PR_VIEW,
        "repos/o/r/branches/develop": {"protected": False},
        "repos/o/r/rules/branches/develop": _RULES,
    }
    monkeypatch.setattr(github, "read_json", _api(responses))
    assert github.required_check_names("12") == {"test / evidence", "quality / evidence"}


@pytest.mark.parametrize(
    "protection",
    [
        {"enabled": True},
        {"required_status_checks": {"enforcement_level": "off", "contexts": ["x"]}},
        {"required_status_checks": {"enforcement_level": "everyone"}},
    ],
)
def test_required_check_names_protection_without_checks(
    monkeypatch: pytest.MonkeyPatch, protection: dict[str, Any]
) -> None:
    responses = {
        "pr": _PR_VIEW,
        "repos/o/r/branches/develop": {"protected": True, "protection": protection},
        "repos/o/r/rules/branches/develop": [],
    }
    monkeypatch.setattr(github, "read_json", _api(responses))
    assert github.required_check_names("12") == frozenset()


def test_required_check_names_falls_back_to_current_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "pr": {"baseRefName": "main", "url": ""},
        "repos/x/y/branches/main": {"protection": {}},
        "repos/x/y/rules/branches/main": [_RULES[-1]],
    }
    monkeypatch.setattr(github, "read_json", _api(responses))
    monkeypatch.setattr(github, "current_repo", lambda: "x/y")
    assert github.required_check_names("5") == {"test / evidence", "quality / evidence"}


@pytest.mark.parametrize(
    ("responses", "detail"),
    [
        (
            {
                "pr": _PR_VIEW,
                "repos/o/r/branches/develop": _PROTECTION,
                "repos/o/r/rules/branches/develop": github.GitHubAPIError(
                    1, "gh api", stderr="HTTP 403: Resource not accessible by integration"
                ),
            },
            "HTTP 403",
        ),
        (
            {"pr": _PR_VIEW, "repos/o/r/branches/develop": ["unexpected"]},
            "unexpected response shape",
        ),
        (
            {
                "pr": _PR_VIEW,
                "repos/o/r/branches/develop": _PROTECTION,
                "repos/o/r/rules/branches/develop": {"message": "odd"},
            },
            "unexpected response shape",
        ),
        ({"pr": ["not", "a", "dict"]}, "no baseRefName"),
        ({"pr": {"url": "https://github.com/o/r/pull/1"}}, "no baseRefName"),
        ({"pr": json.JSONDecodeError("Expecting value", "", 0)}, "Expecting value"),
    ],
)
def test_required_check_names_lookup_failure_warns_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    responses: dict[str, Any],
    detail: str,
) -> None:
    monkeypatch.setattr(github, "read_json", _api(responses))
    assert github.required_check_names("12") is None
    err = capsys.readouterr().err
    assert "Warning: could not read the base branch's required status checks for PR 12" in err
    assert detail in err
    assert "Falling back to waiting on registered checks only" in err


def test_required_check_names_warning_with_empty_error_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Blank(github.GitHubAPIError):
        def __str__(self) -> str:
            return ""

    monkeypatch.setattr(github, "read_json", _api({"pr": _Blank(1, "gh")}))
    assert github.required_check_names("12") is None
    assert "(_Blank)" in capsys.readouterr().err


def test_required_check_names_missing_token_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = github.MissingGitHubTokenError(1, "gh", stderr="set the GH_TOKEN environment variable")
    monkeypatch.setattr(github, "read_json", _api({"pr": exc}))
    with pytest.raises(github.MissingGitHubTokenError):
        github.required_check_names("12")
