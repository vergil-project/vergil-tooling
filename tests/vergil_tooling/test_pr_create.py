"""Tests for vergil_tooling.lib.pr_create: create a PR, adopting one a failed create opened."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github, pr_create, retry, trust
from vergil_tooling.lib.pr_create import CreatedPR, PRCreateError, PRSpec, create_or_adopt
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.pr_create"
_URL = "https://github.com/o/r/pull/21"
_SPEC = PRSpec(head="automation/job/1", base="develop", title="T", head_oid="h0")


def _gh_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "pr", "create"), output="", stderr=stderr)


_TRANSIENT = _gh_error("HTTP 504: Gateway Timeout")
_EXISTS = _gh_error(f'a pull request for branch "automation/job/1" already exists:\n{_URL}')


def _open_pr(**overrides: Any) -> trust.PRRef:
    pr = trust.PRRef(
        number=21,
        url=_URL,
        title="T",
        head="automation/job/1",
        head_oid="h0",
        base="develop",
        author_role=trust.Role.AUTOMATION,
        state="OPEN",
    )
    return dataclasses.replace(pr, **overrides)


@pytest.fixture
def sleep() -> Iterator[MagicMock]:
    with patch(_MOD + ".time.sleep") as mock:
        yield mock


def _run(
    create_effect: list[object], open_prs: list[object], spec: PRSpec = _SPEC
) -> tuple[CreatedPR, MagicMock, MagicMock]:
    create = MagicMock(side_effect=create_effect)
    with patch(_MOD + ".open_trusted_prs", side_effect=open_prs) as lookup:
        created = create_or_adopt(create, spec, opener="this run", rerun="run again")
    return created, create, lookup


def test_create_that_succeeds_is_not_looked_up(sleep: MagicMock) -> None:
    created, create, lookup = _run([_URL], [])
    assert created == CreatedPR(url=_URL, adopted=False)
    create.assert_called_once_with()
    lookup.assert_not_called()
    sleep.assert_not_called()


def test_create_that_landed_is_adopted(
    sleep: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    created, create, lookup = _run([_TRANSIENT], [[_open_pr()]])
    assert created == CreatedPR(url=_URL, adopted=True)
    create.assert_called_once()
    lookup.assert_called_once_with(_SPEC)
    sleep.assert_not_called()
    assert f"adopting {_URL}" in capsys.readouterr().out


def test_already_exists_on_retry_is_adopted(sleep: MagicMock) -> None:
    created, create, lookup = _run([_TRANSIENT, _EXISTS], [[], [_open_pr()]])
    assert created == CreatedPR(url=_URL, adopted=True)
    assert create.call_count == 2
    assert lookup.call_count == 2
    sleep.assert_called_once()


@pytest.mark.parametrize(
    ("other", "reason"),
    [
        (_open_pr(head_oid="ffff"), "its head commit is 'ffff', not 'h0'"),
        (_open_pr(title="Other"), "its title is 'Other', not 'T'"),
    ],
)
def test_other_open_pr_is_refused_by_name(
    other: trust.PRRef, reason: str, sleep: MagicMock
) -> None:
    with pytest.raises(PRCreateError) as exc:
        _run([_EXISTS], [[other]])
    message = str(exc.value)
    assert _URL in message
    assert reason in message
    assert "not the PR this run opened" in message
    assert "it was not adopted" in message
    assert message.endswith("close it and run again.")
    assert "already exists" in message
    sleep.assert_not_called()


def test_several_open_prs_are_refused(sleep: MagicMock) -> None:
    second = _open_pr(url="https://github.com/o/r/pull/22")
    with pytest.raises(PRCreateError, match="several PRs are open") as exc:
        _run([_EXISTS], [[_open_pr(), second]])
    assert _URL in str(exc.value)
    assert "pull/22" in str(exc.value)
    assert str(exc.value).endswith("should not be open and run again.")
    sleep.assert_not_called()


def test_failed_lookup_names_both_errors(sleep: MagicMock) -> None:
    with pytest.raises(PRCreateError) as exc:
        _run([_TRANSIENT], [_gh_error("HTTP 401: Bad credentials (lookup)")])
    assert "Gateway Timeout" in str(exc.value)
    assert "Bad credentials (lookup)" in str(exc.value)
    sleep.assert_not_called()


def test_create_that_really_failed_is_retried(sleep: MagicMock) -> None:
    url = "https://github.com/o/r/pull/30"
    created, create, lookup = _run([_TRANSIENT, url], [[]])
    assert created == CreatedPR(url=url, adopted=False)
    assert create.call_count == 2
    lookup.assert_called_once()
    sleep.assert_called_once()


def test_non_transient_failure_is_raised_as_it_came(sleep: MagicMock) -> None:
    refusal = _gh_error("pull request create failed: GraphQL: No commits between develop and x")
    with pytest.raises(github.GitHubAPIError) as exc:
        _run([refusal], [[]])
    assert exc.value is refusal
    sleep.assert_not_called()


def test_transient_failure_gives_up_after_the_retries(sleep: MagicMock) -> None:
    attempts = retry.MAX_RETRIES + 1
    with pytest.raises(github.GitHubAPIError, match="Gateway Timeout"):
        _run([_TRANSIENT] * attempts, [[]] * attempts)
    assert sleep.call_count == retry.MAX_RETRIES


def test_adoptable_with_none_open_is_none() -> None:
    with patch(_MOD + ".open_trusted_prs", return_value=[]):
        assert pr_create.adoptable(_SPEC, _TRANSIENT, opener="x", rerun="y") is None


@pytest.mark.parametrize(
    "exc",
    [
        trust.TrustReadError("prs_for_branch o/r automation/job/1", "no repository"),
        TrustedAuthorsNotConfiguredError("no trusted authors"),
    ],
)
def test_trust_failure_in_lookup_is_one_error(exc: Exception) -> None:
    with (
        patch(_MOD + ".open_trusted_prs", side_effect=exc),
        pytest.raises(PRCreateError, match="looking for an open PR") as raised,
    ):
        pr_create.adoptable(_SPEC, _TRANSIENT, opener="x", rerun="y")
    assert "Gateway Timeout" in str(raised.value)
    assert str(exc) in str(raised.value)


_FORK = trust.Skipped(
    "o/r", 22, "pr", "mallory", "2026-01-01T00:00:00Z", "head", trust.CROSS_REPOSITORY
)


def test_open_trusted_prs_reads_the_spec_repo_through_the_layer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only same-repo trusted PRs into the spec's base come back; a fork is reported."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    other_base = _open_pr(url="https://github.com/o/r/pull/23", number=23, base="main")
    spec = dataclasses.replace(_SPEC, repo="o/r")
    with (
        patch(
            _MOD + ".trust.prs_for_branch", return_value=([_open_pr(), other_base], [_FORK])
        ) as lookup,
        patch(_MOD + ".github.current_repo") as current,
    ):
        assert pr_create.open_trusted_prs(spec) == [_open_pr()]
    lookup.assert_called_once_with("o/r", "automation/job/1", state="open")
    current.assert_not_called()
    assert "skipped untrusted content" in capsys.readouterr().err


def test_open_trusted_prs_reads_the_cwd_repo_by_default() -> None:
    with (
        patch(_MOD + ".trust.prs_for_branch", return_value=([], [])) as lookup,
        patch(_MOD + ".github.current_repo", return_value="o/cwd"),
    ):
        assert pr_create.open_trusted_prs(_SPEC) == []
    lookup.assert_called_once_with("o/cwd", "automation/job/1", state="open")


def test_a_fork_pr_on_the_branch_is_never_adopted(sleep: MagicMock) -> None:
    """With only a fork PR open on the branch, nothing is adopted and the error stands."""
    refusal = _gh_error("pull request create failed: GraphQL: No commits between develop and x")
    create = MagicMock(side_effect=[refusal])
    with (
        patch(_MOD + ".trust.prs_for_branch", return_value=([], [_FORK])),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        pytest.raises(github.GitHubAPIError) as exc,
    ):
        create_or_adopt(create, _SPEC, opener="this run", rerun="run again")
    assert exc.value is refusal
    sleep.assert_not_called()
