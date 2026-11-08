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
            "checks": [{"context": "app / check", "app_id": None}],
        },
    },
}
_RULES = [
    {"type": "pull_request", "parameters": {}},
    {"type": "required_status_checks", "parameters": None},
    {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [
                {"context": "test / evidence", "integration_id": 15368},
                {"context": "quality / evidence"},
            ],
        },
    },
]


def _rules_with(checks: object) -> list[object]:
    return [{"type": "required_status_checks", "parameters": {"required_status_checks": checks}}]


@pytest.mark.parametrize(
    ("protection", "rules"),
    [
        ({"protected": True, "protection": "on"}, []),
        ({"protection": {"required_status_checks": ["x"]}}, []),
        ({"protection": {"required_status_checks": {"contexts": "legacy / ci"}}}, []),
        ({"protection": {"required_status_checks": {"contexts": [None]}}}, []),
        ({"protection": {"required_status_checks": {"checks": {"context": "x"}}}}, []),
        ({"protection": {"required_status_checks": {"checks": [{"app_id": 3}]}}}, []),
        ({"protected": False}, ["not-a-rule"]),
        ({"protected": False}, [{"type": "required_status_checks", "parameters": []}]),
        ({"protected": False}, _rules_with("test / evidence")),
        ({"protected": False}, _rules_with(["junk"])),
        ({"protected": False}, _rules_with([{"context": ""}])),
    ],
)
def test_required_check_names_malformed_shape_warns_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    protection: dict[str, Any],
    rules: list[object],
) -> None:
    # A field of the wrong type is never read as "none required" (#4058): it
    # takes the visible lookup-failure path instead.
    responses = {
        "pr": _PR_VIEW,
        "repos/o/r/branches/develop": protection,
        "repos/o/r/rules/branches/develop": rules,
    }
    monkeypatch.setattr(github, "read_json", _api(responses))
    assert github.required_check_names("12") is None
    assert "unexpected response shape" in capsys.readouterr().err


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


def test_required_check_names_reads_the_prs_own_repo_not_the_cwds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4013: a PR URL from another repo is looked up in that repo, not the cwd's."""
    responses = {
        "pr": {"baseRefName": "main", "url": "https://github.com/acme/repo/pull/5"},
        "repos/acme/repo/branches/main": {"protection": {}},
        "repos/acme/repo/rules/branches/main": [_RULES[-1]],
    }
    monkeypatch.setattr(github, "read_json", _api(responses))
    monkeypatch.setattr(github, "current_repo", lambda: "cwd-org/cwd-repo")
    assert github.required_check_names("https://github.com/acme/repo/pull/5") == {
        "test / evidence",
        "quality / evidence",
    }


@pytest.mark.parametrize(
    "url", ["", None, "https://example.com/acme/repo/pull/5", "https://github.com/acme/repo"]
)
def test_required_check_names_unparseable_pr_url_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str | None
) -> None:
    """#4013: no silent fall back to the cwd's repo, and no downgrade to a warning."""
    calls: list[tuple[str, ...]] = []
    fake = _api({"pr": {"baseRefName": "main", "url": url}})

    def recording(*args: str) -> Any:
        calls.append(args)
        return fake(*args)

    monkeypatch.setattr(github, "read_json", recording)
    monkeypatch.setattr(github, "current_repo", lambda: pytest.fail("no fallback to the cwd"))
    with pytest.raises(github.GitHubAPIError, match="cannot read the repository of PR 5"):
        github.required_check_names("5")
    assert [call[0] for call in calls] == ["pr"]
    assert "Warning" not in capsys.readouterr().err


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


@pytest.mark.parametrize("failing", ["pr", "repos/o/r/branches/develop"])
def test_required_check_names_missing_token_propagates(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    exc = github.MissingGitHubTokenError(1, "gh", stderr="set the GH_TOKEN environment variable")
    monkeypatch.setattr(github, "read_json", _api({"pr": _PR_VIEW, failing: exc}))
    with pytest.raises(github.MissingGitHubTokenError):
        github.required_check_names("12")
