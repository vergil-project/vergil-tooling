"""vrg-release-record check (spec §4.10 of epic vergil-project/.github#1440).

GitHub is faked at ``github.graphql``; git runs for real, against a local
origin repository standing in for the task's repo on GitHub.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_release_record
from vergil_tooling.bin.vrg_release_record import main
from vergil_tooling.lib import github
from vergil_tooling.lib.release.records import marker, record_body
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_release_record"
_REPO = "o/r"
_TASK = f"{_REPO}#5"
_HUMAN = "wphillipmoore"
_AGENT = "wphillipmoore-vergil-user"
_AUTHORS = TrustedAuthors(human=(_HUMAN,), agents=(_AGENT,))


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class _Origin:
    """A local repository standing in for the task's repo on GitHub."""

    def __init__(self, root: Path) -> None:
        self.path = root / "origin"
        self.path.mkdir()
        _git(self.path, "init", "-q", "-b", "develop")
        _git(self.path, "config", "user.name", "t")
        _git(self.path, "config", "user.email", "t@example.com")
        # What GitHub allows: a filtered fetch and a fetch of any reachable SHA.
        _git(self.path, "config", "uploadpack.allowFilter", "true")
        _git(self.path, "config", "uploadpack.allowAnySHA1InWant", "true")

    def commit(self, message: str) -> str:
        (self.path / "f.txt").write_text(message)
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)
        return _git(self.path, "rev-parse", "HEAD")

    def tag(self, name: str) -> None:
        _git(self.path, "tag", "-a", name, "-m", name)

    def side_commit(self, branch: str, message: str) -> str:
        """A commit on a branch the tag does not contain."""
        _git(self.path, "checkout", "-q", "-b", branch)
        sha = self.commit(message)
        _git(self.path, "checkout", "-q", "develop")
        return sha


@pytest.fixture
def origin(tmp_path: Path) -> _Origin:
    return _Origin(tmp_path)


def _comment(body: str, author: str = _HUMAN, editor: str | None = None) -> dict[str, Any]:
    return {
        "author": {"login": author},
        "editor": None if editor is None else {"login": editor},
        "body": body,
    }


def _record(version: str, prs: tuple[tuple[int, str], ...] = ((7, "a" * 40),)) -> str:
    return record_body(
        repo=_REPO,
        version=version,
        date="2026-11-20",
        release_url=f"https://github.com/{_REPO}/releases/tag/v{version}",
        prs=prs,
        install="ran",
        supersedes=None,
    )


def _closing_pr(number: int, sha: str, *, author: str = _HUMAN, merged: bool = True) -> dict:
    """A merged PR node that closes #5 from its body, with the trust-check fields."""
    return {
        "number": number,
        "merged": merged,
        "mergeCommit": {"oid": sha} if merged else None,
        "repository": {"nameWithOwner": _REPO},
        "body": "Summary.\n\nCloses #5",
        "author": {"login": author},
        "editor": None,
        "userContentEdits": {"totalCount": 0, "nodes": []},
        "closingIssuesReferences": {
            "totalCount": 1,
            "nodes": [{"number": 5, "repository": {"nameWithOwner": _REPO}}],
        },
        "timelineItems": {"totalCount": 0, "nodes": []},
    }


class _FakeGitHub:
    def __init__(
        self,
        comments: list[dict[str, Any]],
        prs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.comments = comments
        self.prs = prs or []
        self.fail: str | None = None

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert variables["owner"] == "o"
        assert variables["name"] == "r"
        assert variables["number"] == 5
        if self.fail is not None and self.fail in query:
            raise github.GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
        if "comments(" in query:
            after = variables.get("after")
            start = int(after) if isinstance(after, str) else 0
            page = self.comments[start : start + 100]
            end = start + len(page)
            info = {"hasNextPage": end < len(self.comments), "endCursor": str(end)}
            conn = {"totalCount": len(self.comments), "pageInfo": info, "nodes": page}
            return {"repository": {"issue": {"comments": conn}}}
        if "closedByPullRequestsReferences" in query:
            conn = {"totalCount": len(self.prs), "nodes": self.prs}
            return {"repository": {"issue": {"closedByPullRequestsReferences": conn}}}
        raise AssertionError(query)


def _run(
    fake: _FakeGitHub, origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    with (
        patch(f"{_MOD}.github.graphql", side_effect=fake.graphql),
        patch("vergil_tooling.lib.release.records.github.graphql", side_effect=fake.graphql),
        patch(f"{_MOD}.load_trusted_authors", return_value=_AUTHORS),
        patch(f"{_MOD}._remote_url", return_value=f"file://{origin.path}"),
    ):
        rc = main(["check", _TASK])
    out = capsys.readouterr()
    return rc, (json.loads(out.out) if out.out else None), out.err


def test_check_pass(origin: _Origin, capsys: pytest.CaptureFixture[str]) -> None:
    origin.commit("base")
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    fake = _FakeGitHub(
        [_comment("unrelated"), _comment(_record("2.1.2"))], [_closing_pr(7, merged)]
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 0
    assert out == {
        "task": _TASK,
        "verdict": "pass",
        "record": {
            "version": "2.1.2",
            "tag": "v2.1.2",
            "url": "https://github.com/o/r/releases/tag/v2.1.2",
        },
        "ancestry": "pass",
        "warnings": [],
    }


def test_check_none_when_no_record_exits_4(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGitHub([_comment("no record here")])
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 4
    assert out == {
        "task": _TASK,
        "verdict": "none",
        "record": None,
        "ancestry": None,
        "warnings": [],
    }


def test_check_none_reads_every_comment_page(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    comments = [_comment(f"c{i}") for i in range(150)]
    comments[120] = _comment(_record("2.1.2"))
    fake = _FakeGitHub(comments, [_closing_pr(7, merged)])
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 0
    assert out is not None
    assert out["verdict"] == "pass"


def test_check_fail_when_ancestry_fails_exits_3(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    origin.commit("base")
    origin.tag("v2.1.2")
    later = origin.commit("feat: the change (#7), merged after the release")
    fake = _FakeGitHub([_comment(_record("2.1.2"))], [_closing_pr(7, later)])
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 3
    assert out is not None
    assert out["verdict"] == "fail"
    assert out["ancestry"] == "fail"
    assert out["record"]["tag"] == "v2.1.2"
    assert out["warnings"] == [f"#7 (merge commit {later[:7]}) is not an ancestor of v2.1.2"]


def test_check_two_closing_prs_both_must_be_ancestors(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    first = origin.commit("feat: part one (#7)")
    origin.tag("v2.1.2")
    second = origin.side_commit("part-two", "feat: part two (#8)")
    fake = _FakeGitHub(
        [_comment(_record("2.1.2"))], [_closing_pr(7, first), _closing_pr(8, second)]
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 3
    assert out is not None
    assert out["ancestry"] == "fail"
    assert out["warnings"] == [f"#8 (merge commit {second[:7]}) is not an ancestor of v2.1.2"]

    # With both merged before the tag, both are ancestors and the check passes.
    _git(origin.path, "merge", "-q", "--no-ff", "-m", "merge part two", "part-two")
    origin.tag("v2.1.3")
    fake = _FakeGitHub(
        [_comment(_record("2.1.3"))], [_closing_pr(7, first), _closing_pr(8, second)]
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 0
    assert out is not None
    assert out["ancestry"] == "pass"


def test_check_two_versions_uses_newest_lists_other(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.9")
    origin.commit("more")
    origin.tag("v2.1.10")
    # Versions compare as numbers: v2.1.10 is newer than v2.1.9.
    fake = _FakeGitHub(
        [_comment(_record("2.1.10")), _comment(_record("2.1.9"))], [_closing_pr(7, merged)]
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 0
    assert out is not None
    assert out["record"]["version"] == "2.1.10"
    assert out["warnings"] == [
        f"{_TASK} also carries a counting record of o/r v2.1.9; the newest, v2.1.10, is used"
    ]


def test_check_ignores_agent_record_with_warning(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    origin.commit("more")
    origin.tag("v2.1.3")
    fake = _FakeGitHub(
        [
            _comment(_record("2.1.3"), author=_AGENT),
            _comment(_record("2.1.2"), editor=f"app/{_AGENT}"),
        ],
        [_closing_pr(7, merged)],
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 4
    assert out is not None
    assert out["verdict"] == "none"
    assert out["warnings"] == [
        f"a record marker for o/r v2.1.3 in a comment by {_AGENT} does not count: "
        "only a human login may post a record",
        f"a record marker for o/r v2.1.2 in a comment last edited by {_AGENT} does not "
        "count: only a human login may edit a record",
    ]

    # Beside a human's record, the agent's is still only a warning.
    fake.comments.append(_comment(_record("2.1.2")))
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 0
    assert out is not None
    assert out["record"]["version"] == "2.1.2"
    assert len(out["warnings"]) == 2


def test_check_foreign_repo_marker_ignored(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGitHub([_comment(marker("x/y", "1.0.0"))])
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 4
    assert out is not None
    assert out["warnings"] == [
        f"a record marker for x/y v1.0.0 is on {_TASK}, which is in o/r; ignored"
    ]


def test_check_untrusted_closing_pr_does_not_count(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    fake = _FakeGitHub(
        [_comment(_record("2.1.2"))],
        [_closing_pr(7, merged, author="mallory"), _closing_pr(9, "f" * 40, merged=False)],
    )
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 3
    assert out is not None
    assert out["ancestry"] == "fail"
    assert out["warnings"] == [
        "closing PR #7 of o/r#5 does not count: its closing reference is not one the "
        "release's trust rule accepts, so the ancestry check skips it",
        f"no closing PR of {_TASK} counts, so nothing confirms that v2.1.2 contains it",
    ]


def test_check_missing_tag_fails(origin: _Origin, capsys: pytest.CaptureFixture[str]) -> None:
    merged = origin.commit("feat: the change (#7)")
    fake = _FakeGitHub([_comment(_record("2.1.2"))], [_closing_pr(7, merged)])
    rc, out, _ = _run(fake, origin, capsys)
    assert rc == 3
    assert out is not None
    assert out["warnings"] == ["o/r has no tag v2.1.2, so the record names no release"]


@pytest.mark.parametrize("failing", ["comments(", "closedByPullRequestsReferences"])
def test_check_read_failure_exits_1(
    origin: _Origin, capsys: pytest.CaptureFixture[str], failing: str
) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    fake = _FakeGitHub([_comment(_record("2.1.2"))], [_closing_pr(7, merged)])
    fake.fail = failing
    rc, out, err = _run(fake, origin, capsys)
    assert rc == 1
    assert out is None
    assert "HTTP 502" in err


def test_check_truncated_closing_prs_exits_1(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGitHub([_comment(_record("2.1.2"))], [_closing_pr(7, "a" * 40)])
    real = fake.graphql

    def graphql(query: str, **variables: object) -> dict[str, object]:
        data = real(query, **variables)
        if "closedByPullRequestsReferences" in query:
            data["repository"]["issue"]["closedByPullRequestsReferences"]["totalCount"] = 21  # type: ignore[index]
        return data

    fake.graphql = graphql  # type: ignore[method-assign]
    rc, out, err = _run(fake, origin, capsys)
    assert rc == 1
    assert out is None
    assert "lists 21 closing PRs but returned 1" in err


def test_check_git_failure_exits_1(origin: _Origin, capsys: pytest.CaptureFixture[str]) -> None:
    merged = origin.commit("feat: the change (#7)")
    origin.tag("v2.1.2")
    fake = _FakeGitHub([_comment(_record("2.1.2"))], [_closing_pr(7, merged)])
    origin.path = origin.path.parent / "missing"
    with patch("vergil_tooling.lib.git.time.sleep"):
        rc, out, err = _run(fake, origin, capsys)
    assert rc == 1
    assert out is None
    assert f"ERROR: {_TASK}: " in err


def test_check_trusted_authors_failure_exits_1(
    origin: _Origin, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(
        f"{_MOD}.load_trusted_authors",
        side_effect=vrg_release_record.TrustedAuthorsNotConfiguredError("not configured"),
    ):
        rc = main(["check", _TASK])
    assert rc == 1
    assert "not configured" in capsys.readouterr().err


@pytest.mark.parametrize("task", ["o/r", "#5", "o/r#0", "o#5"])
def test_check_rejects_a_malformed_task(task: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["check", task])
    assert exc.value.code == 2
