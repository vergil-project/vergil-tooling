"""Tests for vergil_tooling.lib.branch_sweep (issue #3333)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from vergil_tooling.lib import branch_sweep, github
from vergil_tooling.lib.branch_sweep import (
    Action,
    Outcome,
    PullRequest,
    RemoteBranch,
    decide,
    format_outcomes,
    newest_pr,
    summarize,
)

REPO = "acme/widgets"
TIP = "a" * 40
OTHER = "b" * 40


def _branch(name: str, tip: str = TIP, *, protected: bool = False) -> RemoteBranch:
    return RemoteBranch(name=name, tip=tip, protected=protected)


def _pr(number: int, state: str, head: str = TIP) -> PullRequest:
    return PullRequest(number=number, state=state, head_oid=head)


# ---------------------------------------------------------------------------
# decide: the keep/delete rules
# ---------------------------------------------------------------------------


def test_merged_pr_at_tip_is_deleted() -> None:
    d = decide(_branch("feature/1-x"), [_pr(5, "MERGED")], default_branch="develop")
    assert d.delete is True
    assert d.reason == "PR #5 merged"
    assert d.pr == _pr(5, "MERGED")


def test_closed_unmerged_pr_at_tip_is_deleted() -> None:
    d = decide(_branch("feature/1-x"), [_pr(5, "CLOSED")], default_branch="develop")
    assert d.delete is True
    assert d.reason == "PR #5 closed"


def test_open_pr_is_kept() -> None:
    d = decide(_branch("feature/1-x"), [_pr(5, "OPEN")], default_branch="develop")
    assert d.delete is False
    assert d.reason == "open PR #5"


def test_older_open_pr_keeps_branch_even_if_newest_is_merged() -> None:
    prs = [_pr(9, "MERGED"), _pr(4, "OPEN")]
    d = decide(_branch("feature/1-x"), prs, default_branch="develop")
    assert d.delete is False
    assert d.reason == "open PR #4"
    assert d.pr is not None and d.pr.number == 9


def test_no_pr_is_kept() -> None:
    d = decide(_branch("feature/1-x"), [], default_branch="develop")
    assert d.delete is False
    assert d.reason == "no PR"
    assert d.pr is None


def test_reused_tip_is_kept() -> None:
    d = decide(_branch("feature/1-x", OTHER), [_pr(5, "MERGED")], default_branch="develop")
    assert d.delete is False
    assert d.reason.startswith("reused branch name")
    assert OTHER[:12] in d.reason
    assert TIP[:12] in d.reason


def test_newest_pr_decides_the_tip_comparison() -> None:
    # Older PR matches the tip, newest does not: the name was reused after #3.
    prs = [_pr(3, "MERGED", OTHER), _pr(8, "CLOSED", TIP)]
    d = decide(_branch("feature/1-x", OTHER), prs, default_branch="develop")
    assert d.delete is False
    assert d.pr is not None and d.pr.number == 8


def test_protected_branch_is_kept() -> None:
    branch = _branch("release/1.0", protected=True)
    d = decide(branch, [_pr(5, "MERGED")], default_branch="develop")
    assert d.delete is False
    assert d.reason == "protected branch"


def test_default_branch_is_kept() -> None:
    d = decide(_branch("trunk"), [_pr(5, "MERGED")], default_branch="trunk")
    assert d.delete is False
    assert d.reason == "default branch"


@pytest.mark.parametrize("name", ["develop", "main", "gh-pages"])
def test_reserved_branches_are_kept(name: str) -> None:
    d = decide(_branch(name), [_pr(5, "MERGED")], default_branch="trunk")
    assert d.delete is False
    assert d.reason == "reserved branch"


def test_unexpected_pr_state_raises() -> None:
    with pytest.raises(ValueError, match="unexpected state 'WEIRD'"):
        decide(_branch("feature/1-x"), [_pr(5, "WEIRD")], default_branch="develop")


def test_newest_pr_is_highest_number() -> None:
    assert newest_pr([]) is None
    newest = newest_pr([_pr(2, "MERGED"), _pr(7, "CLOSED"), _pr(5, "MERGED")])
    assert newest is not None and newest.number == 7


# ---------------------------------------------------------------------------
# Fake GitHub boundary
# ---------------------------------------------------------------------------


def _err(cmd: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, cmd, "", "HTTP 502: boom")


@dataclass
class FakeGitHub:
    """Stands in for lib.github's read_json / read_json_paginated / delete."""

    default: object = field(default_factory=lambda: {"default_branch": "develop"})
    branches: list[object] = field(default_factory=list)
    prs: dict[str, object] = field(default_factory=dict)
    rules: dict[str, list[object]] = field(default_factory=dict)
    refs: dict[str, object] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    deleted: list[str] = field(default_factory=list)

    def read_json(self, *args: str) -> object:
        if args[0] == "pr":
            branch = args[args.index("--head") + 1]
            if f"prs:{branch}" in self.fail:
                raise _err("gh pr list")
            return self.prs.get(branch, [])
        endpoint = args[1]
        if endpoint == f"repos/{REPO}":
            return self.default
        prefix = f"repos/{REPO}/git/ref/heads/"
        assert endpoint.startswith(prefix), endpoint
        branch = endpoint.removeprefix(prefix)
        if f"ref:{branch}" in self.fail:
            raise _err("gh api ref")
        return self.refs[branch]

    def read_json_paginated(self, endpoint: str, key: str | None = None) -> list[object]:
        assert key is None
        if endpoint == f"repos/{REPO}/branches":
            if "branches" in self.fail:
                raise _err("gh api branches")
            return self.branches
        prefix = f"repos/{REPO}/rules/branches/"
        assert endpoint.startswith(prefix), endpoint
        branch = endpoint.removeprefix(prefix)
        if f"rules:{branch}" in self.fail:
            raise _err("gh api rules")
        return self.rules.get(branch, [])

    def delete(self, endpoint: str) -> None:
        if f"delete:{endpoint}" in self.fail:
            raise _err("gh api delete")
        self.deleted.append(endpoint)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub()
    monkeypatch.setattr(github, "read_json", gh.read_json)
    monkeypatch.setattr(github, "read_json_paginated", gh.read_json_paginated)
    monkeypatch.setattr(github, "delete", gh.delete)
    return gh


def _api_branch(name: str, tip: str = TIP, *, protected: bool = False) -> dict[str, object]:
    return {"name": name, "commit": {"sha": tip}, "protected": protected}


def _api_pr(number: int, state: str, head: str = TIP, *, fork: bool = False) -> dict[str, object]:
    return {"number": number, "state": state, "headRefOid": head, "isCrossRepository": fork}


def _by_branch(outcomes: list[Outcome]) -> dict[str, Outcome]:
    return {o.branch: o for o in outcomes}


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _populate(fake: FakeGitHub) -> None:
    fake.branches = [
        _api_branch("develop"),
        _api_branch("main", protected=True),
        _api_branch("feature/1-merged"),
        _api_branch("feature/2-closed"),
        _api_branch("feature/3-open"),
        _api_branch("feature/4-no-pr"),
        _api_branch("feature/5-reused", OTHER),
        _api_branch("release/6-protected", protected=True),
    ]
    fake.prs = {
        "feature/1-merged": [_api_pr(10, "MERGED")],
        "feature/2-closed": [_api_pr(11, "CLOSED")],
        "feature/3-open": [_api_pr(12, "OPEN")],
        "feature/5-reused": [_api_pr(13, "MERGED")],
    }


def test_sweep_dry_run_classifies_every_branch(fake: FakeGitHub) -> None:
    _populate(fake)
    got = _by_branch(branch_sweep.sweep(REPO, apply=False))
    assert [o.branch for o in branch_sweep.sweep(REPO, apply=False)] == sorted(got)
    assert got["feature/1-merged"].action is Action.WOULD_DELETE
    assert got["feature/2-closed"].action is Action.WOULD_DELETE
    assert got["feature/3-open"].action is Action.KEPT
    assert got["feature/4-no-pr"].reason == "no PR"
    assert got["feature/5-reused"].reason.startswith("reused branch name")
    assert got["release/6-protected"].reason == "protected branch"
    assert got["develop"].reason == "default branch"
    assert got["main"].reason == "reserved branch"
    assert fake.deleted == []


def test_sweep_apply_deletes_only_finished_branches(fake: FakeGitHub) -> None:
    _populate(fake)
    fake.refs = {
        "feature/1-merged": {"object": {"sha": TIP}},
        "feature/2-closed": {"object": {"sha": TIP}},
    }
    got = _by_branch(branch_sweep.sweep(REPO, apply=True))
    assert got["feature/1-merged"].action is Action.DELETED
    assert got["feature/2-closed"].action is Action.DELETED
    assert fake.deleted == [
        f"repos/{REPO}/git/refs/heads/feature/1-merged",
        f"repos/{REPO}/git/refs/heads/feature/2-closed",
    ]


def test_sweep_ignores_fork_prs(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/7-x")]
    fake.prs = {"feature/7-x": [_api_pr(20, "MERGED", fork=True)]}
    (outcome,) = branch_sweep.sweep(REPO, apply=False)
    assert outcome.action is Action.KEPT
    assert outcome.reason == "no PR"


def test_sweep_keeps_branch_targeted_by_ruleset(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/1-merged")]
    fake.prs = {"feature/1-merged": [_api_pr(10, "MERGED")]}
    fake.rules = {"feature/1-merged": [{"type": "deletion"}, {"type": "deletion"}]}
    (outcome,) = branch_sweep.sweep(REPO, apply=True)
    assert outcome.action is Action.KEPT
    assert outcome.reason == "protected by ruleset (deletion)"
    assert fake.deleted == []


def test_sweep_keeps_branch_whose_tip_moved_before_delete(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/1-merged")]
    fake.prs = {"feature/1-merged": [_api_pr(10, "MERGED")]}
    fake.refs = {"feature/1-merged": {"object": {"sha": OTHER}}}
    (outcome,) = branch_sweep.sweep(REPO, apply=True)
    assert outcome.action is Action.KEPT
    assert outcome.reason.startswith("tip moved during sweep")
    assert fake.deleted == []


def test_sweep_escapes_branch_names_in_paths(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/1 odd#name")]
    fake.prs = {"feature/1 odd#name": [_api_pr(10, "CLOSED")]}
    fake.refs = {"feature/1%20odd%23name": {"object": {"sha": TIP}}}
    (outcome,) = branch_sweep.sweep(REPO, apply=True)
    assert outcome.action is Action.DELETED
    assert fake.deleted == [f"repos/{REPO}/git/refs/heads/feature/1%20odd%23name"]


@pytest.mark.parametrize(
    "failure",
    ["prs:feature/1-merged", "rules:feature/1-merged", "ref:feature/1-merged"],
)
def test_sweep_records_error_and_continues(fake: FakeGitHub, failure: str) -> None:
    fake.branches = [_api_branch("feature/1-merged"), _api_branch("feature/2-closed")]
    fake.prs = {
        "feature/1-merged": [_api_pr(10, "MERGED")],
        "feature/2-closed": [_api_pr(11, "CLOSED")],
    }
    fake.refs = {
        "feature/1-merged": {"object": {"sha": TIP}},
        "feature/2-closed": {"object": {"sha": TIP}},
    }
    fake.fail = {failure}
    got = _by_branch(branch_sweep.sweep(REPO, apply=True))
    assert got["feature/1-merged"].action is Action.ERROR
    assert "HTTP 502: boom" in got["feature/1-merged"].reason
    assert "\n" not in got["feature/1-merged"].reason
    assert got["feature/2-closed"].action is Action.DELETED


def test_sweep_records_delete_failure(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/1-merged")]
    fake.prs = {"feature/1-merged": [_api_pr(10, "MERGED")]}
    fake.refs = {"feature/1-merged": {"object": {"sha": TIP}}}
    fake.fail = {f"delete:repos/{REPO}/git/refs/heads/feature/1-merged"}
    (outcome,) = branch_sweep.sweep(REPO, apply=True)
    assert outcome.action is Action.ERROR
    assert fake.deleted == []


def test_sweep_records_malformed_pr_data(fake: FakeGitHub) -> None:
    fake.branches = [_api_branch("feature/1-x")]
    fake.prs = {"feature/1-x": [{"number": "10", "state": "MERGED", "headRefOid": TIP}]}
    (outcome,) = branch_sweep.sweep(REPO, apply=False)
    assert outcome.action is Action.ERROR
    assert "PR number" in outcome.reason


def test_sweep_propagates_missing_token(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.branches = [_api_branch("feature/1-x")]

    def boom(*_args: str) -> object:
        raise github.MissingGitHubTokenError(1, "gh", "", "set GH_TOKEN")

    monkeypatch.setattr(github, "read_json", boom)
    monkeypatch.setattr(branch_sweep, "default_branch", lambda _repo: "develop")
    with pytest.raises(github.MissingGitHubTokenError):
        branch_sweep.sweep(REPO, apply=False)


def test_sweep_propagates_branch_listing_failure(fake: FakeGitHub) -> None:
    fake.fail = {"branches"}
    with pytest.raises(subprocess.CalledProcessError):
        branch_sweep.sweep(REPO, apply=False)


# ---------------------------------------------------------------------------
# API response validation
# ---------------------------------------------------------------------------


def test_default_branch_rejects_malformed(fake: FakeGitHub) -> None:
    fake.default = {"default_branch": ""}
    with pytest.raises(ValueError, match="default_branch"):
        branch_sweep.default_branch(REPO)
    fake.default = ["not", "a", "dict"]
    with pytest.raises(ValueError, match="unexpected API response"):
        branch_sweep.default_branch(REPO)


def test_list_branches_rejects_non_bool_protected(fake: FakeGitHub) -> None:
    fake.branches = [{"name": "x", "commit": {"sha": TIP}, "protected": "yes"}]
    with pytest.raises(ValueError, match="protected"):
        branch_sweep.list_branches(REPO)


def test_list_prs_rejects_non_list(fake: FakeGitHub) -> None:
    fake.prs = {"x": {"oops": 1}}
    with pytest.raises(ValueError, match="PRs of x"):
        branch_sweep.list_prs(REPO, "x")


def test_list_prs_refuses_truncated_list(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(branch_sweep, "PR_LIST_LIMIT", 2)
    fake.prs = {"x": [_api_pr(1, "CLOSED"), _api_pr(2, "CLOSED")]}
    with pytest.raises(ValueError, match="truncated"):
        branch_sweep.list_prs(REPO, "x")


def test_list_prs_rejects_bool_number(fake: FakeGitHub) -> None:
    fake.prs = {"x": [{"number": True, "state": "MERGED", "headRefOid": TIP}]}
    with pytest.raises(ValueError, match="PR number"):
        branch_sweep.list_prs(REPO, "x")


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_outcomes_aligns_columns() -> None:
    lines = format_outcomes(
        [
            Outcome("feature/1-merged", _pr(10, "MERGED"), Action.WOULD_DELETE, "PR #10 merged"),
            Outcome("dev", None, Action.KEPT, "no PR"),
        ]
    )
    assert lines == [
        "feature/1-merged  #10     MERGED  would delete (PR #10 merged)",
        "dev               -       -       kept (no PR)",
    ]


def test_format_outcomes_empty() -> None:
    assert format_outcomes([]) == []


def test_summarize_dry_run_and_apply() -> None:
    outcomes = [
        Outcome("a", None, Action.WOULD_DELETE, "r"),
        Outcome("b", None, Action.KEPT, "r"),
        Outcome("c", None, Action.ERROR, "r"),
    ]
    dry = summarize(REPO, outcomes, apply=False)
    assert dry == f"{REPO}: 3 branches, 1 would delete, 1 kept, 1 errors" + (
        " (dry run; pass --apply to delete)"
    )
    applied = [Outcome("a", None, Action.DELETED, "r")]
    assert summarize(REPO, applied, apply=True) == (
        f"{REPO}: 1 branches, 1 deleted, 0 kept, 0 errors"
    )
