"""Tests for vergil_tooling.lib.auto_merge.

The GitHub API is a fake standing in for the three ``github`` calls the helper
makes: ``read_json`` (``gh pr view``), ``graphql`` (the mutation) and ``merge``
(``gh pr merge``). A refusal is raised as ``gh`` raises it: a
:class:`~vergil_tooling.lib.github.GitHubAPIError` carrying GitHub's message on
stderr, or, for a 200 response with errors, a ``RuntimeError``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from vergil_tooling.lib import auto_merge, github
from vergil_tooling.lib.auto_merge import (
    CLEAN_STATUS_REFUSAL,
    AutoMergeError,
    AutoMergeResult,
    MergeMethod,
    auto_merge_enabled,
    enable_auto_merge,
)

PR = "https://github.com/o/r/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"

# gh's own rendering of GitHub's refusal for a PR that can already merge.
CLEAN_STDERR = (
    "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)\n"
)


def _gh_refusal(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api", "graphql"), output="", stderr=stderr)


@dataclass
class FakeGitHub:
    """The PR as GitHub holds it, and every call the helper made."""

    auto_merge: dict[str, object] | None = None
    state: str = "OPEN"
    refusal: BaseException | None = None
    confirm: bool = True
    merge_failure: BaseException | None = None
    merge_leaves_state: str = "MERGED"
    head_oid: str = HEAD
    # A write that lands on GitHub and still returns its error (a lost
    # response followed by a retry GitHub refuses, #4696).
    lands_then_fails: bool = False
    mutations: list[dict[str, object]] = field(default_factory=list)
    merges: list[tuple[str, str, str | None]] = field(default_factory=list)

    def read_json(self, *args: str) -> object:
        assert args[:3] == ("pr", "view", PR), args
        fields = args[args.index("--json") + 1]
        if fields == "id":
            return {"id": "PR_kw7"}
        if fields == "state":
            return {"state": self.state}
        if fields == "state,headRefOid":
            return {"state": self.state, "headRefOid": self.head_oid}
        assert fields == "autoMergeRequest", fields
        return {"autoMergeRequest": self.auto_merge}

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert "enablePullRequestAutoMerge" in query
        self.mutations.append(variables)
        if self.refusal is not None:
            if self.lands_then_fails:
                self.auto_merge = {"mergeMethod": variables["method"]}
            raise self.refusal
        if self.confirm:
            self.auto_merge = {"mergeMethod": variables["method"]}
        return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}

    def merge(self, pr: str, *, strategy: str, match_head: str | None = None) -> None:
        self.merges.append((pr, strategy, match_head))
        if self.merge_failure is not None:
            if self.lands_then_fails:
                self.state = "MERGED"
            raise self.merge_failure
        self.state = self.merge_leaves_state


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub()
    for name in ("read_json", "graphql", "merge"):
        monkeypatch.setattr(github, name, getattr(api, name))
    return api


@pytest.mark.parametrize("method", ["SQUASH", "REBASE"])
def test_enables_and_reads_back(fake: FakeGitHub, method: MergeMethod) -> None:
    result = enable_auto_merge(PR, head=HEAD, method=method)

    assert result == AutoMergeResult(merged_now=False)
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": method}]
    assert fake.merges == []
    assert auto_merge_enabled(PR)


def test_mutation_pins_method_and_head_as_variables() -> None:
    query = auto_merge._ENABLE_AUTO_MERGE_MUTATION  # noqa: SLF001 - the mutation sent
    assert "mergeMethod: $method" in query
    assert "expectedHeadOid: $head" in query


@pytest.mark.parametrize(("method", "strategy"), [("REBASE", "rebase"), ("SQUASH", "squash")])
def test_already_mergeable_merges_directly_with_method_and_head(
    fake: FakeGitHub, method: MergeMethod, strategy: str
) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)

    result = enable_auto_merge(PR, head=HEAD, method=method)

    assert result == AutoMergeResult(merged_now=True)
    assert fake.merges == [(PR, strategy, HEAD)]
    assert fake.state == "MERGED"


def test_already_mergeable_in_a_200_with_errors_merges_directly(fake: FakeGitHub) -> None:
    fake.refusal = RuntimeError(
        "GraphQL errors: [{'message': 'Pull request Pull request is in clean status'}]"
    )

    assert enable_auto_merge(PR, head=HEAD, method="REBASE").merged_now
    assert fake.merges == [(PR, "rebase", HEAD)]


def test_clean_status_constant_is_in_the_refusal() -> None:
    assert CLEAN_STATUS_REFUSAL in CLEAN_STDERR


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL: Pull request Pull request is in unstable status (enablePullRequestAutoMerge)",
        "GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)",
        "GraphQL: Head sha didn't match expected head sha (enablePullRequestAutoMerge)",
    ],
)
def test_other_refusal_raises(fake: FakeGitHub, stderr: str) -> None:
    fake.refusal = _gh_refusal(stderr)

    with pytest.raises(AutoMergeError, match=f"{PR}: enabling auto-merge failed") as caught:
        enable_auto_merge(PR, head=HEAD, method="SQUASH")

    assert stderr.split(": ", 1)[1] in str(caught.value)
    assert fake.merges == []


def test_other_refusal_in_a_200_with_errors_raises(fake: FakeGitHub) -> None:
    fake.refusal = RuntimeError("GraphQL errors: [{'message': 'something else'}]")

    with pytest.raises(AutoMergeError, match="something else"):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")
    assert fake.merges == []


def test_readback_mismatch_raises(fake: FakeGitHub) -> None:
    fake.confirm = False

    with pytest.raises(AutoMergeError, match=f"{PR}: auto-merge is not confirmed with SQUASH"):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")


def test_readback_other_method_raises(fake: FakeGitHub) -> None:
    fake.confirm = False
    fake.auto_merge = {"mergeMethod": "SQUASH"}

    with pytest.raises(AutoMergeError, match="not confirmed with REBASE"):
        enable_auto_merge(PR, head=HEAD, method="REBASE")


def test_direct_merge_failure_raises(fake: FakeGitHub) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)
    fake.merge_failure = _gh_refusal("GraphQL: Head branch was modified (mergePullRequest)")

    with pytest.raises(
        AutoMergeError, match="(?s)the direct merge failed.*Head branch was modified"
    ):
        enable_auto_merge(PR, head=HEAD, method="REBASE")


def test_direct_merge_that_leaves_pr_open_raises(fake: FakeGitHub) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)
    fake.merge_leaves_state = "OPEN"

    with pytest.raises(AutoMergeError, match="state reads 'OPEN'"):
        enable_auto_merge(PR, head=HEAD, method="REBASE")


def test_unknown_method_raises_before_any_call(fake: FakeGitHub) -> None:
    with pytest.raises(AutoMergeError, match="merge method must be one of SQUASH, REBASE"):
        enable_auto_merge(PR, head=HEAD, method=cast("Any", "MERGE"))
    assert fake.mutations == []


def test_missing_node_id_raises(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *_args: {"id": ""})

    with pytest.raises(AutoMergeError, match=f"{PR} has no node ID"):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")
    assert fake.mutations == []


def test_pr_view_failure_raises_naming_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: str) -> object:
        raise subprocess.CalledProcessError(1, ("gh", *args), output="", stderr="boom")

    monkeypatch.setattr(github, "read_json", fail)

    with pytest.raises(AutoMergeError, match=f"(?s){PR}: reading auto-merge failed.*boom"):
        auto_merge_enabled(PR)


def test_pr_view_non_object_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *_args: [])

    with pytest.raises(AutoMergeError, match="gh pr view returned"):
        auto_merge_enabled(PR)


def test_auto_merge_enabled_false_when_off(fake: FakeGitHub) -> None:
    assert not auto_merge_enabled(PR)


def test_auto_merge_enabled_rejects_malformed_request(fake: FakeGitHub) -> None:
    fake.auto_merge = cast("Any", "SQUASH")

    with pytest.raises(AutoMergeError, match="neither an object nor null"):
        auto_merge_enabled(PR)


# A retried write whose first attempt landed: the retry fails, the PR shows it landed (#4696).
_RETRY_REFUSAL = "GraphQL: Something went wrong while executing your query (retried)"


def test_enable_error_with_auto_merge_on_is_success(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.refusal = _gh_refusal(_RETRY_REFUSAL)
    fake.lands_then_fails = True

    assert enable_auto_merge(PR, head=HEAD, method="REBASE") == AutoMergeResult(merged_now=False)
    assert "returned an error, but it is on" in capsys.readouterr().out
    assert fake.merges == []


def test_enable_error_with_auto_merge_on_other_method_raises(fake: FakeGitHub) -> None:
    fake.refusal = _gh_refusal(_RETRY_REFUSAL)
    fake.auto_merge = {"mergeMethod": "SQUASH"}

    with pytest.raises(AutoMergeError, match=f"{PR}: enabling auto-merge failed"):
        enable_auto_merge(PR, head=HEAD, method="REBASE")


def test_enable_error_with_failed_readback_names_both(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.refusal = _gh_refusal(_RETRY_REFUSAL)

    def read_json(*args: str) -> object:
        if args[args.index("--json") + 1] == "id":
            return {"id": "PR_kw7"}
        raise subprocess.CalledProcessError(1, ("gh", *args), output="", stderr="boom")

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(
        AutoMergeError, match="(?s)enabling auto-merge failed.*Something went wrong.*boom"
    ):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")


def test_direct_merge_error_with_pr_merged_at_head_is_success(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)
    fake.merge_failure = _gh_refusal("GraphQL: Pull request is already merged (retried)")
    fake.lands_then_fails = True

    assert enable_auto_merge(PR, head=HEAD, method="SQUASH") == AutoMergeResult(merged_now=True)
    assert "the direct merge returned an error, but it merged" in capsys.readouterr().out


def test_direct_merge_error_with_pr_merged_at_other_head_raises(fake: FakeGitHub) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)
    fake.merge_failure = _gh_refusal("GraphQL: Pull request is already merged")
    fake.lands_then_fails = True
    fake.head_oid = "f" * 40

    with pytest.raises(AutoMergeError, match="the direct merge failed"):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")


def test_direct_merge_error_with_failed_readback_names_both(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.refusal = _gh_refusal(CLEAN_STDERR)
    fake.merge_failure = _gh_refusal("GraphQL: Head branch was modified (mergePullRequest)")
    real_read_json = fake.read_json

    def read_json(*args: str) -> object:
        if args[args.index("--json") + 1] == "state,headRefOid":
            raise subprocess.CalledProcessError(1, ("gh", *args), output="", stderr="boom")
        return real_read_json(*args)

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(AutoMergeError, match="(?s)direct merge failed.*Head branch.*boom"):
        enable_auto_merge(PR, head=HEAD, method="SQUASH")
