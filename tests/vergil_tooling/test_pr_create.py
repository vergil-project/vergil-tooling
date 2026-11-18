"""Tests for vergil_tooling.lib.pr_create: create a PR, adopting one a failed create opened."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github, pr_create, retry
from vergil_tooling.lib.pr_create import CreatedPR, PRCreateError, PRSpec, create_or_adopt

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.pr_create"
_URL = "https://github.com/o/r/pull/21"
_SPEC = PRSpec(head="automation/job/1", base="develop", title="T", head_oid="h0")


def _gh_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "pr", "create"), output="", stderr=stderr)


_TRANSIENT = _gh_error("HTTP 504: Gateway Timeout")
_EXISTS = _gh_error(f'a pull request for branch "automation/job/1" already exists:\n{_URL}')


def _open_pr(**overrides: Any) -> github.OpenPR:
    pr = github.OpenPR(
        url=_URL,
        title="T",
        head_ref="automation/job/1",
        base_ref="develop",
        head_oid="h0",
        cross_repo=False,
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
    with patch(_MOD + ".github.open_prs_for_head", side_effect=open_prs) as lookup:
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
    lookup.assert_called_once_with("automation/job/1", base="develop")
    sleep.assert_not_called()
    assert f"adopting {_URL}" in capsys.readouterr().out


def test_lookup_names_the_repo_when_the_spec_does(sleep: MagicMock) -> None:
    spec = dataclasses.replace(_SPEC, repo="o/r")
    created, _create, lookup = _run([_EXISTS], [[_open_pr()]], spec)
    assert created.adopted
    lookup.assert_called_once_with("automation/job/1", base="develop", repo="o/r")
    sleep.assert_not_called()


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
        (_open_pr(base_ref="main"), "its base is 'main', not 'develop'"),
        (_open_pr(head_ref="Automation/job/1"), "its head branch is 'Automation/job/1'"),
        (_open_pr(cross_repo=True), "its head is in another repository"),
    ],
)
def test_other_open_pr_is_refused_by_name(
    other: github.OpenPR, reason: str, sleep: MagicMock
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
    with patch(_MOD + ".github.open_prs_for_head", return_value=[]):
        assert pr_create.adoptable(_SPEC, _TRANSIENT, opener="x", rerun="y") is None
