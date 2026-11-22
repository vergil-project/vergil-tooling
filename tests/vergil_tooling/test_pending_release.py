"""Tests for vergil_tooling.lib.pending_release."""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.pending_release import (
    ChangeKind,
    PendingRelease,
    PendingReleaseError,
    ReleaseTags,
    classify,
    first_parent_chain,
    gather_pending_release,
    gather_release_contents,
    parse_release_tags,
    redact_userinfo,
    trusted_closing_refs,
)
from vergil_tooling.lib.release.state import IssueEntry
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError

_URL = "https://github.com/o/r.git"
_MAIN = "a" * 40
_DEV_TIP = "d" * 40
_BASE_DATE = "2026-11-12T08:00:00Z"
_ALIAS_RE = re.compile(r'c(\d+): object\(oid: "([0-9a-f]{40})"\)')


def _sha(n: int) -> str:
    return f"{n:040x}"


def _ts(iso: str) -> float:
    return datetime.datetime.fromisoformat(iso).timestamp()


def _commit(
    sha: str,
    parents: list[str],
    subject: str = "a commit",
    date: str = "2026-11-12T09:00:00Z",
) -> dict[str, Any]:
    return {
        "sha": sha,
        "parents": [{"sha": p} for p in parents],
        "commit": {"message": f"{subject}\n\nbody", "committer": {"date": date}},
    }


def _linear(shas: list[str], **subjects: str) -> list[dict[str, Any]]:
    """Commits in a line: each one's parent is the one before (the first's is main)."""
    commits = []
    parent = _MAIN
    for sha in shas:
        commits.append(_commit(sha, [parent], subjects.get(sha, f"subject {sha[:7]}")))
        parent = sha
    return commits


def _pr(
    number: int,
    title: str = "feat: a thing",
    branch: str = "feature/1-x",
    *,
    merged_at: str = "2026-11-12T10:00:00Z",
    base: str = "develop",
    merged: bool = True,
    labels: tuple[str, ...] = (),
    label_total: int | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "mergedAt": merged_at,
        "baseRefName": base,
        "merged": merged,
        "labels": {
            "totalCount": len(labels) if label_total is None else label_total,
            "nodes": [{"name": name} for name in labels],
        },
    }


def _assoc(*prs: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    return {
        "associatedPullRequests": {
            "totalCount": len(prs) if total is None else total,
            "nodes": list(prs),
        }
    }


def _ls_remote(*, main: str | None = _MAIN, dev: str | None = None, tags: str = "") -> str:
    lines = []
    if main is not None:
        lines.append(f"{main}\trefs/heads/main")
    if dev is not None:
        lines.append(f"{dev}\trefs/heads/develop")
    return "\n".join(lines) + ("\n" + tags if tags else "")


def _page(commits: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "total_commits": total,
        "base_commit": _commit(_MAIN, [], "main tip", _BASE_DATE),
        "commits": commits,
    }


class _GraphQL:
    """A ``github.graphql`` stand-in answering each aliased commit from *nodes*."""

    def __init__(self, nodes: dict[str, Any]) -> None:
        self.nodes = nodes
        self.alias_counts: list[int] = []
        self.variables: list[dict[str, object]] = []
        self.queries: list[str] = []

    def __call__(self, query: str, **variables: object) -> dict[str, object]:
        self.queries.append(query)
        aliases = _ALIAS_RE.findall(query)
        self.alias_counts.append(len(aliases))
        self.variables.append(variables)
        return {"repository": {f"c{i}": self.nodes[sha] for i, sha in aliases}}


def _gather(
    commits: list[dict[str, Any]],
    nodes: dict[str, Any],
    *,
    dev: str | None = None,
    pages: list[dict[str, Any]] | None = None,
    tags: str = "",
    url: str = _URL,
) -> tuple[PendingRelease, MagicMock, _GraphQL]:
    head = dev if dev is not None else commits[-1]["sha"]
    graphql = _GraphQL(nodes)
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=url),
        patch(
            "vergil_tooling.lib.git.read_output",
            return_value=_ls_remote(dev=head, tags=tags),
        ),
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=pages if pages is not None else [_page(commits, len(commits))],
        ) as read_json,
        patch("vergil_tooling.lib.github.graphql", side_effect=graphql),
    ):
        result = gather_pending_release(integration="develop")
    return result, read_json, graphql


# --- parse_release_tags ---------------------------------------------------------


def _tag_lines(*tags: tuple[int, str]) -> str:
    return "\n".join(f"{_sha(n)}\trefs/tags/{name}" for n, name in tags)


def test_parse_release_tags_latest_and_rolling() -> None:
    out = _tag_lines((302, "v2.1.302"), (303, "v2.1.303"), (303, "v2.1"))
    assert parse_release_tags(out) == ReleaseTags("v2.1.303", "v2.1", "v2.1.303")


def test_parse_release_tags_rolling_lags() -> None:
    out = _tag_lines((302, "v2.1.302"), (303, "v2.1.303"), (302, "v2.1"))
    tags = parse_release_tags(out)
    assert tags.latest == "v2.1.303"
    assert tags.rolling_target == "v2.1.302"


def test_parse_release_tags_uses_peeled_commit() -> None:
    out = "\n".join(
        [
            f"{_sha(302)}\trefs/tags/v2.1.302",
            f"{_sha(303)}\trefs/tags/v2.1.303",
            f"{_sha(999)}\trefs/tags/v2.1",
            f"{_sha(303)}\trefs/tags/v2.1^{{}}",
        ]
    )
    assert parse_release_tags(out).rolling_target == "v2.1.303"


def test_parse_release_tags_peeled_release_tag() -> None:
    out = "\n".join(
        [
            f"{_sha(900)}\trefs/tags/v2.1.303",
            f"{_sha(303)}\trefs/tags/v2.1.303^{{}}",
            f"{_sha(303)}\trefs/tags/v2.1",
        ]
    )
    assert parse_release_tags(out).rolling_target == "v2.1.303"


def test_parse_release_tags_orders_numerically() -> None:
    out = "\n".join(
        [
            f"{_sha(1)}\trefs/tags/v2.1.99",
            f"{_sha(2)}\trefs/tags/v2.1.303",
            f"{_sha(3)}\trefs/tags/v2.9",
            f"{_sha(4)}\trefs/tags/v2.10",
        ]
    )
    tags = parse_release_tags(out)
    assert tags.latest == "v2.1.303"
    assert tags.rolling == "v2.10"


def test_parse_release_tags_ignores_develop_and_major() -> None:
    out = "\n".join(
        [
            f"{_sha(1)}\trefs/tags/v2.1.303",
            f"{_sha(1)}\trefs/tags/v2.1",
            f"{_sha(2)}\trefs/tags/develop-v2.1.304",
            f"{_sha(2)}\trefs/tags/v2",
            f"{_sha(2)}\trefs/tags/v3",
            f"{_MAIN}\trefs/heads/main",
        ]
    )
    assert parse_release_tags(out) == ReleaseTags("v2.1.303", "v2.1", "v2.1.303")


def test_parse_release_tags_none() -> None:
    assert parse_release_tags("") == ReleaseTags(None, None, None)
    untagged = f"{_sha(1)}\trefs/tags/v2.1.303\n{_sha(5)}\trefs/tags/v2.1"
    assert parse_release_tags(untagged) == ReleaseTags("v2.1.303", "v2.1", _sha(5)[:7])


def test_parse_release_tags_picks_highest_release_on_rolling_commit() -> None:
    out = "\n".join(
        [
            f"{_sha(1)}\trefs/tags/v2.1.9",
            f"{_sha(1)}\trefs/tags/v2.1.10",
            f"{_sha(1)}\trefs/tags/v2.1",
        ]
    )
    assert parse_release_tags(out).rolling_target == "v2.1.10"


# --- first_parent_chain ---------------------------------------------------------


def test_first_parent_chain_skips_side_commits() -> None:
    a, side1, side2, merge, b = _sha(1), _sha(2), _sha(3), _sha(4), _sha(5)
    commits = [
        _commit(a, [_MAIN]),
        _commit(side1, [_MAIN]),
        _commit(side2, [side1]),
        _commit(merge, [a, side2]),
        _commit(b, [merge]),
    ]
    chain = first_parent_chain(commits, b)
    assert [c["sha"] for c in chain] == [a, merge, b]


def test_first_parent_chain_head_absent_is_empty() -> None:
    assert first_parent_chain([], _DEV_TIP) == []


def test_first_parent_chain_root_commit() -> None:
    root = _commit(_sha(1), [])
    assert first_parent_chain([root], _sha(1)) == [root]


def test_first_parent_chain_parents_not_a_list_raises() -> None:
    bad: dict[str, object] = {"sha": _sha(1), "parents": "nope"}
    with pytest.raises(TypeError, match="parents is not a list"):
        first_parent_chain([bad], _sha(1))


# --- classify -------------------------------------------------------------------


def test_classify() -> None:
    assert classify(
        "chore(release): back-merge 2.1.303 and bump to 2.1.304", "release/post-2.1.303", []
    ) == (ChangeKind.RELEASE, "release")
    assert classify(
        "chore(deps): dependency update for 2.1.304",
        "deps/update-2.1.304",
        ["dependency-update"],
    ) == (ChangeKind.DEPS, "deps")
    assert classify("feat(validate): refuse ubuntu-latest runner labels", "feature/4249-x", []) == (
        ChangeKind.OTHER,
        "feat",
    )
    assert classify("Tidy things", "feature/1-x", []) == (ChangeKind.OTHER, "-")


def test_classify_deps_by_label_or_branch_alone() -> None:
    assert classify("chore: bump", "feature/1-x", ["dependency-update"]) == (
        ChangeKind.DEPS,
        "deps",
    )
    assert classify("chore: bump", "deps/update-1", []) == (ChangeKind.DEPS, "deps")
    assert classify("fix!: break", None, []) == (ChangeKind.OTHER, "fix")


def test_classify_automation_branch() -> None:
    # An automation PR is its own type, apart from development PRs, whatever
    # its title's conventional type or its labels (epic .github#1064).
    assert classify(
        "chore(repo-config): sync managed files", "automation/repo-config-sync/123", []
    ) == (ChangeKind.AUTOMATION, "automation")
    assert classify("chore: bump", "automation/other-job/9", ["dependency-update"]) == (
        ChangeKind.AUTOMATION,
        "automation",
    )
    # Only the branch prefix counts: a lookalike name is not automation.
    assert classify("feat: x", "feature/1-automation", []) == (ChangeKind.OTHER, "feat")
    assert classify("feat: x", "automationx/1", []) == (ChangeKind.OTHER, "feat")


# --- gather_pending_release: mapping --------------------------------------------


def test_batch_pr_commits_group_into_one_row() -> None:
    shas = [_sha(1), _sha(2), _sha(3)]
    commits = _linear(
        shas,
        **{
            _sha(1): "fix: one (#11)",
            _sha(2): "fix: two (#12)",
            _sha(3): "fix: three (#13)",
        },
    )
    batch = _pr(20, "fix: a batch", "feature/11-batch-x")
    release_pr = _pr(30, "chore(release): 1.0.0", "release/1.0.0", base="main")
    nodes = {sha: _assoc(batch, release_pr) for sha in shas}
    result, _, _ = _gather(commits, nodes)
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.pr_number == 20
    assert change.shas == tuple(shas)
    assert change.title == "fix: a batch"
    assert change.branch == "feature/11-batch-x"
    assert change.kind is ChangeKind.OTHER
    assert change.type_label == "fix"
    assert change.merged_ts == _ts("2026-11-12T10:00:00Z")


def test_merge_order_oldest_first() -> None:
    shas = [_sha(1), _sha(2), _sha(3)]
    commits = _linear(shas)
    nodes = {
        shas[0]: _assoc(_pr(1, "chore(release): back-merge", "release/post-1.0.0")),
        shas[1]: _assoc(
            _pr(2, "chore(deps): update", "deps/update-1.0.1", labels=("dependency-update",))
        ),
        shas[2]: _assoc(_pr(3, "feat: new", "feature/3-new")),
    }
    result, _, _ = _gather(commits, nodes)
    assert [(c.pr_number, c.kind, c.type_label) for c in result.changes] == [
        (1, ChangeKind.RELEASE, "release"),
        (2, ChangeKind.DEPS, "deps"),
        (3, ChangeKind.OTHER, "feat"),
    ]
    assert result.main_moved_ts == _ts(_BASE_DATE)


def test_same_pr_split_by_another_is_two_changes() -> None:
    shas = [_sha(1), _sha(2), _sha(3)]
    nodes = {
        shas[0]: _assoc(_pr(1)),
        shas[1]: _assoc(_pr(2)),
        shas[2]: _assoc(_pr(1)),
    }
    result, _, _ = _gather(_linear(shas), nodes)
    assert [c.pr_number for c in result.changes] == [1, 2, 1]


def test_commit_without_pr_is_no_pr_change() -> None:
    sha = _sha(1)
    commits = [_commit(sha, [_MAIN], "sneaky direct push", "2026-11-12T07:30:00Z")]
    result, _, _ = _gather(commits, {sha: _assoc()})
    (change,) = result.changes
    assert change.kind is ChangeKind.NO_PR
    assert change.pr_number is None
    assert change.branch is None
    assert change.title == "sneaky direct push"
    assert change.type_label == "-"
    assert change.merged_ts == _ts("2026-11-12T07:30:00Z")
    assert change.shas == (sha,)


def test_consecutive_no_pr_commits_are_separate_changes() -> None:
    shas = [_sha(1), _sha(2)]
    result, _, _ = _gather(_linear(shas), {sha: _assoc() for sha in shas})
    assert [c.shas for c in result.changes] == [(shas[0],), (shas[1],)]


def test_unmerged_or_other_base_pr_ignored() -> None:
    sha = _sha(1)
    nodes = {sha: _assoc(_pr(5, base="main"), _pr(6, merged=False))}
    result, _, _ = _gather(_linear([sha]), nodes)
    (change,) = result.changes
    assert change.kind is ChangeKind.NO_PR


def test_nothing_pending() -> None:
    result, _, graphql = _gather([], {}, dev=_MAIN, pages=[_page([], 0)])
    assert result.changes == ()
    assert graphql.alias_counts == []


def test_tags_and_repo_variables_passed_through() -> None:
    sha = _sha(1)
    tags = f"{_sha(9)}\trefs/tags/v1.0.0\n{_sha(9)}\trefs/tags/v1.0"
    result, read_json, graphql = _gather(_linear([sha]), {sha: _assoc(_pr(1))}, tags=tags)
    assert result.tags == ReleaseTags("v1.0.0", "v1.0", "v1.0.0")
    assert graphql.variables == [{"owner": "o", "name": "r"}]
    read_json.assert_called_once_with(
        "api", f"repos/o/r/compare/{_MAIN}...{sha}?per_page=100&page=1"
    )


def test_ls_remote_reads_branches_and_tags_only() -> None:
    sha = _sha(1)
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch(
            "vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=sha)
        ) as read_output,
        patch(
            "vergil_tooling.lib.github.read_json",
            return_value=_page(_linear([sha]), 1),
        ),
        patch("vergil_tooling.lib.github.graphql", side_effect=_GraphQL({sha: _assoc(_pr(1))})),
    ):
        gather_pending_release(integration="develop", remote="upstream")
    read_output.assert_called_once_with(
        "ls-remote", "upstream", "refs/heads/main", "refs/heads/develop", "refs/tags/v*"
    )


# --- gather_pending_release: paging and chunking --------------------------------


def test_multi_page_compare_collects_all() -> None:
    shas = [_sha(n) for n in range(1, 151)]
    commits = _linear(shas)
    pages = [_page(commits[:100], 150), _page(commits[100:], 150)]
    nodes = {sha: _assoc(_pr(1)) for sha in shas}
    result, read_json, _ = _gather(commits, nodes, pages=pages)
    assert read_json.call_count == 2
    assert read_json.call_args_list[1].args[1].endswith("per_page=100&page=2")
    (change,) = result.changes
    assert change.shas == tuple(shas)


def test_graphql_chunks_over_fifty_shas() -> None:
    shas = [_sha(n) for n in range(1, 121)]
    commits = _linear(shas)
    nodes = {sha: _assoc(_pr(n)) for n, sha in enumerate(shas, start=1)}
    pages = [_page(commits[:100], 120), _page(commits[100:], 120)]
    result, _, graphql = _gather(commits, nodes, pages=pages)
    assert graphql.alias_counts == [50, 50, 20]
    assert [c.pr_number for c in result.changes] == list(range(1, 121))


# --- gather_pending_release: errors ---------------------------------------------


def test_incomplete_compare_raises() -> None:
    shas = [_sha(n) for n in range(1, 251)]
    commits = _linear(shas)
    pages = [_page(commits, 300), _page([], 300)]
    with pytest.raises(PendingReleaseError, match="250 of 300"):
        _gather(commits, {}, pages=pages)


def test_overfull_compare_raises() -> None:
    commits = _linear([_sha(1), _sha(2)])
    with pytest.raises(PendingReleaseError, match="2 of 1 commits"):
        _gather(commits, {}, pages=[_page(commits, 1)])


def test_missing_branch_raises() -> None:
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch(
            "vergil_tooling.lib.git.read_output",
            return_value=_ls_remote(main=None, dev=_DEV_TIP),
        ),
        patch("vergil_tooling.lib.github.read_json") as read_json,
        pytest.raises(PendingReleaseError, match="main or develop not found on origin"),
    ):
        gather_pending_release(integration="develop")
    read_json.assert_not_called()


def test_api_failure_raises_pending_release_error() -> None:
    sha = _sha(1)
    boom = RuntimeError("GraphQL errors: [{'message': 'nope'}]")
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=sha)),
        patch("vergil_tooling.lib.github.read_json", return_value=_page(_linear([sha]), 1)),
        patch("vergil_tooling.lib.github.graphql", side_effect=boom),
        pytest.raises(PendingReleaseError, match="GraphQL errors") as info,
    ):
        gather_pending_release(integration="develop")
    assert info.value.__cause__ is boom


def test_process_failure_uses_stderr_then_command() -> None:
    with_stderr = subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 404: Not Found")
    bare = subprocess.CalledProcessError(2, ["gh", "api", "x"], stderr=b"  ")
    for exc, expected in (
        (with_stderr, "HTTP 404: Not Found"),
        (bare, "Command 'gh api x' returned non-zero exit status 2."),
    ):
        with (
            patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
            patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=_DEV_TIP)),
            patch("vergil_tooling.lib.github.read_json", side_effect=exc),
            pytest.raises(PendingReleaseError) as info,
        ):
            gather_pending_release(integration="develop")
        assert str(info.value) == expected
        assert info.value.__cause__ is exc


def test_bytes_stderr_is_decoded() -> None:
    exc = subprocess.CalledProcessError(1, ["git"], stderr=b"fatal: bad\n")
    with (
        patch("vergil_tooling.lib.git.remote_url", side_effect=exc),
        pytest.raises(PendingReleaseError, match="^fatal: bad$"),
    ):
        gather_pending_release(integration="develop")


def test_malformed_response_raises() -> None:
    sha = _sha(1)
    page = _page(_linear([sha]), 1)
    del page["base_commit"]
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=sha)),
        patch("vergil_tooling.lib.github.read_json", return_value=page),
        pytest.raises(PendingReleaseError, match="unexpected GitHub response: KeyError") as info,
    ):
        gather_pending_release(integration="develop")
    assert isinstance(info.value.__cause__, KeyError)


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ([], "compare response is not an object: list"),
        ({"total_commits": "3"}, "total_commits is not an integer: str"),
        ({"total_commits": True}, "total_commits is not an integer: bool"),
        (
            {"total_commits": 1, "base_commit": {"commit": {"committer": {"date": 5}}}},
            "base date is not a string: int",
        ),
        (
            {
                "total_commits": 1,
                "base_commit": _commit(_MAIN, []),
                "commits": {},
            },
            "commits is not a list: dict",
        ),
    ],
)
def test_response_shape_errors(page: object, message: str) -> None:
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=_DEV_TIP)),
        patch("vergil_tooling.lib.github.read_json", return_value=page),
        pytest.raises(PendingReleaseError, match=f"TypeError: {re.escape(message)}"),
    ):
        gather_pending_release(integration="develop")


def test_unknown_commit_on_github_raises() -> None:
    sha = _sha(1)
    with pytest.raises(PendingReleaseError, match=f"commit {sha[:7]} not found on GitHub"):
        _gather(_linear([sha]), {sha: None})


def test_malformed_sha_raises_before_query() -> None:
    bad = "Z" * 40
    commits = [_commit(bad, [_MAIN])]
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=bad)),
        patch("vergil_tooling.lib.github.read_json", return_value=_page(commits, 1)),
        patch("vergil_tooling.lib.github.graphql") as graphql,
        pytest.raises(PendingReleaseError, match="malformed commit SHA"),
    ):
        gather_pending_release(integration="develop")
    graphql.assert_not_called()


def test_multiple_prs_for_one_commit_raises() -> None:
    sha = _sha(1)
    nodes = {sha: _assoc(_pr(7), _pr(8))}
    with pytest.raises(PendingReleaseError, match="2 merged PRs into develop: #7, #8"):
        _gather(_linear([sha]), nodes)


def test_truncated_pr_list_raises() -> None:
    sha = _sha(1)
    nodes = {sha: _assoc(*[_pr(n, base="main") for n in range(5)], total=6)}
    with pytest.raises(
        PendingReleaseError, match=f"associatedPullRequests for {sha[:7]} truncated"
    ):
        _gather(_linear([sha]), nodes)


def test_truncated_label_list_raises() -> None:
    sha = _sha(1)
    labels = tuple(f"l{n}" for n in range(20))
    nodes = {sha: _assoc(_pr(4, labels=labels, label_total=21))}
    with pytest.raises(PendingReleaseError, match=f"labels of #4 for {sha[:7]} truncated"):
        _gather(_linear([sha]), nodes)


def test_auth_errors_use_standard_messages() -> None:
    missing = github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN environment variable")
    app = github.AppAuthError("o", "123", Path("/keys/app.pem"), "key rejected")
    no_install = github.NoInstallationError("o", ["other"])
    for exc, expected in (
        (missing, github.missing_token_message("vrg-repo-status")),
        (app, github.app_auth_message(app)),
        (no_install, github.no_installation_message(no_install)),
    ):
        with (
            patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
            patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=_DEV_TIP)),
            patch("vergil_tooling.lib.github.read_json", side_effect=exc),
            pytest.raises(PendingReleaseError) as info,
        ):
            gather_pending_release(integration="develop")
        assert str(info.value) == expected
        assert info.value.__cause__ is exc


def test_error_reason_strips_url_userinfo() -> None:
    exc = subprocess.CalledProcessError(
        128,
        ["git", "ls-remote"],
        stderr="fatal: unable to access 'https://user:tok123@github.com/o/r.git/': 403",
    )
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", side_effect=exc),
        pytest.raises(PendingReleaseError) as info,
    ):
        gather_pending_release(integration="develop")
    message = str(info.value)
    assert "https://github.com/o/r.git/" in message
    assert "tok123" not in message
    assert "user:" not in message


def test_reason_strips_every_userinfo_form() -> None:
    text = "a ssh://git@github.com/o/r and HTTPS://x-access-token:s3cr3t@github.com/o/r b"
    assert redact_userinfo(text) == "a ssh://github.com/o/r and HTTPS://github.com/o/r b"
    assert redact_userinfo("no urls here: user@example.com") == "no urls here: user@example.com"
    assert redact_userinfo("'https://u:p@ss@github.com/o/r'") == "'https://github.com/o/r'"
    assert redact_userinfo("https://github.com/o/r?x=a@b") == "https://github.com/o/r?x=a@b"


def test_non_github_remote_raises() -> None:
    url = "https://user:secret@gitlab.example/o/r.git"
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=url),
        patch("vergil_tooling.lib.git.read_output") as read_output,
        pytest.raises(PendingReleaseError) as info,
    ):
        gather_pending_release(integration="develop")
    message = str(info.value)
    assert message == "origin is not a github.com remote"
    assert "gitlab" not in message
    assert "secret" not in message
    read_output.assert_not_called()


# --- gather_pending_release: pinned head --------------------------------------


def test_head_pins_the_range_to_the_cut() -> None:
    """A release reads main..<cut>, not the integration tip (epic .github#1440 §4.1)."""
    cut, later = _sha(1), _sha(2)
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=later)),
        patch(
            "vergil_tooling.lib.github.read_json", return_value=_page(_linear([cut]), 1)
        ) as read_json,
        patch("vergil_tooling.lib.github.graphql", side_effect=_GraphQL({cut: _assoc(_pr(1))})),
    ):
        result = gather_pending_release(integration="develop", head=cut)
    assert [c.shas for c in result.changes] == [(cut,)]
    read_json.assert_called_once_with(
        "api", f"repos/o/r/compare/{_MAIN}...{cut}?per_page=100&page=1"
    )


def test_head_that_is_not_a_full_sha_raises() -> None:
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=_DEV_TIP)),
        patch("vergil_tooling.lib.github.read_json") as read_json,
        pytest.raises(PendingReleaseError, match="not a full SHA"),
    ):
        gather_pending_release(integration="develop", head="HEAD")
    read_json.assert_not_called()


# --- gather_release_contents: the issue list ------------------------------------

_TRUSTED = TrustedAuthors(human=("human",), agents=("agent",))


def _actor(login: str | None) -> dict[str, str] | None:
    return None if login is None else {"login": login}


def _issue_node(number: int, repo: str = "o/r") -> dict[str, Any]:
    return {"number": number, "repository": {"nameWithOwner": repo}}


# Timeline items of other types on each PR: the unfiltered totalCount is above
# the filteredCount by this much.
_UNFILTERED = 5


def _feature_pr(
    number: int,
    *closes: int | tuple[str, int],
    branch: str | None = None,
    body: str | None = None,
    author: str | None = "human",
    editor: str | None = None,
    edits: tuple[str | None, ...] = (),
    connected: tuple[tuple[str | None, int], ...] = (),
    closing_total: int | None = None,
) -> dict[str, Any]:
    """A PR node with the issue-list fields; each of *closes* is in the body by default."""
    refs = [_issue_node(c) if isinstance(c, int) else _issue_node(c[1], c[0]) for c in closes]
    if body is None:
        lines = [f"Closes #{c}" if isinstance(c, int) else f"Closes {c[0]}#{c[1]}" for c in closes]
        body = "Summary.\n\n" + "\n".join(lines)
    node = _pr(number, branch=branch or f"feature/{number}-x")
    node.update(
        {
            "repository": {"nameWithOwner": "o/r"},
            "body": body,
            "author": _actor(author),
            "editor": _actor(editor),
            "userContentEdits": {
                "totalCount": len(edits),
                "nodes": [{"editor": _actor(e)} for e in edits],
            },
            "closingIssuesReferences": {
                "totalCount": len(refs) if closing_total is None else closing_total,
                "nodes": refs,
            },
            # GitHub's totalCount here counts every timeline item, whatever
            # the type filter; filteredCount counts the connected events.
            "timelineItems": {
                "filteredCount": len(connected),
                "totalCount": len(connected) + _UNFILTERED,
                "nodes": [
                    {"actor": _actor(actor), "subject": _issue_node(issue), "source": {}}
                    for actor, issue in connected
                ],
            },
        }
    )
    return node


def _contents(
    commits: list[dict[str, Any]],
    nodes: dict[str, Any],
    *,
    graphql: Any = None,
) -> tuple[PendingRelease, tuple[IssueEntry, ...], _GraphQL]:
    cut = commits[-1]["sha"]
    fake = _GraphQL(nodes)
    with (
        patch("vergil_tooling.lib.pending_release.load_trusted_authors", return_value=_TRUSTED),
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.git.read_output", return_value=_ls_remote(dev=_DEV_TIP)),
        patch("vergil_tooling.lib.github.read_json", return_value=_page(commits, len(commits))),
        patch("vergil_tooling.lib.github.graphql", side_effect=graphql or fake),
    ):
        pending, issues = gather_release_contents(cut=cut)
    return pending, issues, fake


def test_issue_list_from_feature_prs() -> None:
    s1, s2 = _sha(1), _sha(2)
    nodes = {
        s1: _assoc(_feature_pr(1, 11)),
        s2: _assoc(_feature_pr(2, 10, author="agent", editor="agent", edits=("agent",))),
    }
    pending, issues, graphql = _contents(_linear([s1, s2]), nodes)
    assert [c.pr_number for c in pending.changes] == [1, 2]
    assert issues == (IssueEntry("o/r#10", (2,)), IssueEntry("o/r#11", (1,)))
    assert graphql.alias_counts == [2]


def test_issue_list_query_reads_closing_refs_and_history() -> None:
    s1 = _sha(1)
    _, _, graphql = _contents(_linear([s1]), {s1: _assoc(_feature_pr(1, 10))})
    query = graphql.queries[0]
    assert "closingIssuesReferences(first: 20, userLinkedOnly: false)" in query
    assert "userContentEdits(first: 100)" in query
    assert "timelineItems(itemTypes: [CONNECTED_EVENT], first: 100)" in query
    timeline = query[query.index("timelineItems(") : query.index("... on ConnectedEvent")]
    assert "filteredCount" in timeline
    assert "totalCount" not in timeline
    assert "author { login }" in query


def test_pending_release_query_reads_no_closing_refs() -> None:
    _, _, graphql = _gather(_linear([_sha(1)]), {_sha(1): _assoc(_pr(1))})
    assert "closingIssuesReferences" not in graphql.queries[0]


def test_issue_list_skips_deps_release_automation() -> None:
    s = [_sha(n) for n in range(1, 5)]
    nodes = {
        s[0]: _assoc(_feature_pr(1, 10, branch="deps/update-2026-11-12")),
        s[1]: _assoc(_feature_pr(2, 11, branch="release/post-2.1.1")),
        s[2]: _assoc(_feature_pr(3, 12, branch="automation/refresh/99")),
        s[3]: _assoc(),
    }
    pending, issues, _ = _contents(_linear(s), nodes)
    assert len(pending.changes) == 4
    assert issues == ()


def test_issue_list_ignores_other_repo_refs(capsys: pytest.CaptureFixture[str]) -> None:
    s1 = _sha(1)
    nodes = {s1: _assoc(_feature_pr(1, ("other/x", 5), 10))}
    _, issues, _ = _contents(_linear([s1]), nodes)
    assert issues == (IssueEntry("o/r#10", (1,)),)
    assert "#1: closing reference other/x#5 is in another repo" in capsys.readouterr().err


def test_issue_list_dedupes_issue_closed_twice() -> None:
    """An issue closed by two feature/ PRs is listed once, with both (Review Focus 2)."""
    s = [_sha(n) for n in range(1, 5)]
    batch = _feature_pr(3, 10, 12, branch="feature/3-batch-x")
    nodes = {
        s[0]: _assoc(_feature_pr(1, 10)),
        s[1]: _assoc(_feature_pr(2, 10)),
        s[2]: _assoc(batch),
        s[3]: _assoc(batch),
    }
    _, issues, _ = _contents(_linear(s), nodes)
    assert issues == (IssueEntry("o/r#10", (1, 2, 3)), IssueEntry("o/r#12", (3,)))


def test_issue_list_excludes_pr_edited_by_untrusted_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s1, s2 = _sha(1), _sha(2)
    nodes = {
        s1: _assoc(_feature_pr(1, 10, editor="mallory", edits=("human", "mallory"))),
        s2: _assoc(_feature_pr(2, 11)),
    }
    _, issues, _ = _contents(_linear([s1, s2]), nodes)
    assert issues == (IssueEntry("o/r#11", (2,)),)
    err = capsys.readouterr().err
    assert "WARNING: #1: closing reference #10 not counted" in err
    assert "untrusted mallory" in err


def test_issue_list_excludes_earlier_untrusted_edit(capsys: pytest.CaptureFixture[str]) -> None:
    """An untrusted edit stays in the history after a trusted one replaced it."""
    s1 = _sha(1)
    nodes = {s1: _assoc(_feature_pr(1, 10, editor="human", edits=("mallory", "human")))}
    _, issues, _ = _contents(_linear([s1]), nodes)
    assert issues == ()
    assert "untrusted mallory" in capsys.readouterr().err


def test_issue_list_excludes_untrusted_or_deleted_author(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s1, s2 = _sha(1), _sha(2)
    nodes = {
        s1: _assoc(_feature_pr(1, 10, author="mallory")),
        s2: _assoc(_feature_pr(2, 11, author=None)),
    }
    _, issues, _ = _contents(_linear([s1, s2]), nodes)
    assert issues == ()
    err = capsys.readouterr().err
    assert "untrusted mallory" in err
    assert "untrusted (no login)" in err


def test_issue_list_excludes_untrusted_manual_link(capsys: pytest.CaptureFixture[str]) -> None:
    s1, s2, s3 = _sha(1), _sha(2), _sha(3)
    nodes = {
        s1: _assoc(_feature_pr(1, 10, body="No closing line.", connected=(("mallory", 10),))),
        s2: _assoc(_feature_pr(2, 11, body="No closing line.", connected=(("human", 11),))),
        s3: _assoc(
            _feature_pr(3, 12, body="Linked twice.", connected=(("agent", 12), ("mallory", 12)))
        ),
    }
    _, issues, _ = _contents(_linear([s1, s2, s3]), nodes)
    assert issues == (IssueEntry("o/r#11", (2,)),)
    err = capsys.readouterr().err
    assert (
        "#1: closing reference #10 not counted: it was linked by hand by untrusted mallory" in err
    )
    assert "#3: closing reference #12 not counted" in err


def test_issue_list_reads_filtered_count_of_connected_events() -> None:
    """A PR whose timeline holds other items still counts: filteredCount, not totalCount."""
    s1, s2 = _sha(1), _sha(2)
    linked = _feature_pr(1, 10, body="No closing line.", connected=(("human", 10),))
    plain = _feature_pr(2, 11)
    for node in (linked, plain):
        timeline = node["timelineItems"]
        assert timeline["totalCount"] > timeline["filteredCount"] == len(timeline["nodes"])
    _, issues, _ = _contents(_linear([s1, s2]), {s1: _assoc(linked), s2: _assoc(plain)})
    assert issues == (IssueEntry("o/r#10", (1,)), IssueEntry("o/r#11", (2,)))


def test_issue_list_trusted_link_counts_despite_untrusted_body() -> None:
    s1 = _sha(1)
    nodes = {s1: _assoc(_feature_pr(1, 10, editor="mallory", connected=(("human", 10),)))}
    _, issues, _ = _contents(_linear([s1]), nodes)
    assert issues == (IssueEntry("o/r#10", (1,)),)


def test_issue_list_counts_closed_keyword(capsys: pytest.CaptureFixture[str]) -> None:
    s1 = _sha(1)
    body = "Closed #10\n- close o/r#11\nFixes #12\nRef #13\nsee Closes #14 inline"
    nodes = {s1: _assoc(_feature_pr(1, 10, 11, 12, 13, 14, body=body))}
    _, issues, _ = _contents(_linear([s1]), nodes)
    assert issues == (IssueEntry("o/r#10", (1,)), IssueEntry("o/r#11", (1,)))
    err = capsys.readouterr().err
    for issue in (12, 13, 14):
        assert f"#1: closing reference #{issue} not counted: no closing line" in err


def test_issue_list_empty_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    s1 = _sha(1)
    _, issues, _ = _contents(_linear([s1]), {s1: _assoc(_feature_pr(1))})
    assert issues == ()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("connection", "count", "message"),
    [
        ("closingIssuesReferences", "totalCount", "closingIssuesReferences of #1 truncated"),
        ("userContentEdits", "totalCount", "userContentEdits of #1 truncated"),
        ("timelineItems", "filteredCount", "connected events of #1 truncated"),
    ],
)
def test_closing_refs_truncated_raises(connection: str, count: str, message: str) -> None:
    s1 = _sha(1)
    node = _feature_pr(1, 10)
    node[connection][count] = 500
    with pytest.raises(PendingReleaseError, match=message):
        _contents(_linear([s1]), {s1: _assoc(node)})


def test_closing_refs_failure_raises() -> None:
    """A failed GraphQL read raises, never passes as no issues (Review Focus 5)."""
    failure = subprocess.CalledProcessError(1, ["gh", "api", "graphql"], stderr="HTTP 502")
    with pytest.raises(PendingReleaseError, match="HTTP 502"):
        _contents(_linear([_sha(1)]), {}, graphql=MagicMock(side_effect=failure))


def test_closing_refs_malformed_node_raises() -> None:
    s1 = _sha(1)
    node = _feature_pr(1, 10)
    node["body"] = None
    with pytest.raises(PendingReleaseError, match="body of #1 is not a string"):
        _contents(_linear([s1]), {s1: _assoc(node)})


def test_release_contents_trusted_authors_failure_raises() -> None:
    with (
        patch(
            "vergil_tooling.lib.pending_release.load_trusted_authors",
            side_effect=TrustedAuthorsNotConfiguredError("trusted authors are not configured"),
        ),
        patch("vergil_tooling.lib.github.graphql") as graphql,
        pytest.raises(PendingReleaseError, match="could not load the trusted authors"),
    ):
        gather_release_contents(cut=_sha(1))
    graphql.assert_not_called()


def test_trusted_closing_refs_ignores_pr_on_a_connected_side() -> None:
    node = _feature_pr(1, 10, body="", connected=(("human", 10),))
    events = cast("dict[str, Any]", node["timelineItems"])["nodes"]
    events[0]["source"] = {}
    events.append({"actor": _actor("mallory"), "subject": {}, "source": None})
    cast("dict[str, Any]", node["timelineItems"])["filteredCount"] = 2
    assert trusted_closing_refs(node, {"human"}) == ((10,), [])
