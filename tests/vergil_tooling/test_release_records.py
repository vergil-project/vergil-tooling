"""The release-records stage (spec §4.9 of epic vergil-project/.github#1440)."""

from __future__ import annotations

import argparse
import ast
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github, progress, trust
from vergil_tooling.lib.release import records
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import close_tracking
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.records import (
    counting_record,
    marker,
    post_records,
    record_body,
    record_comments,
)
from vergil_tooling.lib.release.state import IssueEntry, StateBlock, initial_state
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release.records"
_ORCH = "vergil_tooling.lib.release.orchestrator"
_FINALIZE = "vergil_tooling.lib.release.finalize"
_REPO = "o/r"
_VERSION = "2.1.2"
_HUMAN = "wphillipmoore"
_AGENT = "wphillipmoore-vergil-user"
_SHAS = {7: "a" * 40, 8: "b" * 40, 9: "c" * 40}
_AUTHORS = TrustedAuthors(human=(_HUMAN,), agents=(_AGENT,))
_CREATED = "2026-11-20T09:00:00Z"
_EDITED = "2026-11-20T10:00:00Z"
_GHOST = "ghost"
# Each test login's account type, as GraphQL returns it; "ghost" is a deleted account.
_TYPES = {_HUMAN: "User", _AGENT: "Bot", "drive-by": "User"}


@dataclass(frozen=True)
class Comment:
    """A comment as a test writes it: logins (an actor dict for an odd account), then body.

    *edits* are the earlier editors in its history; *editor* is its last editor.
    """

    author: str | dict[str, str] | None
    editor: str | dict[str, str] | None
    body: str
    edits: tuple[str, ...] = ()


def _actor(who: str | dict[str, str] | None) -> dict[str, str] | None:
    if isinstance(who, dict):
        return who
    if who is None or who == _GHOST:
        return None
    return {"__typename": _TYPES[who], "login": who}


def _node(index: int, comment: Comment) -> dict[str, object]:
    """The layer's IssueComment node for *comment*, in a recorded response's shape."""
    history = [*comment.edits, *([] if comment.editor is None else [comment.editor])]
    return {
        "id": f"IC_{index}",
        "fullDatabaseId": str(index),
        "author": _actor(comment.author),
        "editor": _actor(comment.editor),
        "body": comment.body,
        "createdAt": _CREATED,
        "lastEditedAt": None if comment.editor is None else _EDITED,
        "userContentEdits": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"editedAt": _EDITED, "editor": _actor(e)} for e in history],
        },
    }


class _FakeGitHub:
    """GitHub as records sees it: issue comments, PR merge commits, the Release's date."""

    def __init__(self, comments: dict[str, list[Comment]] | None = None) -> None:
        self.comments: dict[str, list[Comment]] = comments or {}
        self.posts: list[str] = []
        self.fail_post: set[str] = set()
        self.fail_read: dict[str, int] = {}
        self.reads: list[tuple[str, str | None]] = []

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        if query in (trust._ITEM_QUERY, trust._COMMENTS_QUERY):  # noqa: SLF001
            return self._comments_page(query, variables)
        if "mergeCommit" in query:
            number = variables["number"]
            assert isinstance(number, int)
            return {"repository": {"pullRequest": {"mergeCommit": {"oid": _SHAS[number]}}}}
        if "release(tagName" in query:
            return {"repository": {"release": {"publishedAt": "2026-11-20T10:00:00Z"}}}
        raise AssertionError(query)

    def _comments_page(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        ref = f"{variables['owner']}/{variables['name']}#{variables['number']}"
        after = variables.get("after")
        assert after is None or isinstance(after, str)
        self.reads.append((ref, after))
        start = int(after) if after else 0
        if ref in self.fail_read and start // 100 == self.fail_read[ref]:
            raise github.GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
        everything = self.comments.get(ref, [])
        page = everything[start : start + 100]
        end = start + len(page)
        nodes = [_node(start + i + 1, c) for i, c in enumerate(page)]
        info = {"hasNextPage": end < len(everything), "endCursor": str(end) if page else None}
        item: dict[str, object] = {"comments": {"pageInfo": info, "nodes": nodes}}
        if query == trust._ITEM_QUERY:  # noqa: SLF001 - the first page carries the issue body
            item |= _node(0, Comment(_AGENT, None, "the task body"))
            item["__typename"] = "Issue"
        return {"repository": {"issueOrPullRequest": item}}

    def post(self, endpoint: str, body: dict[str, object]) -> dict[str, Any]:
        _, repo_owner, repo_name, _, number, _ = endpoint.split("/")
        ref = f"{repo_owner}/{repo_name}#{number}"
        if ref in self.fail_post:
            raise github.GitHubAPIError(1, ("gh", "api", endpoint), stderr="HTTP 403")
        text = body["body"]
        assert isinstance(text, str)
        self.comments.setdefault(ref, []).append(Comment(_HUMAN, None, text))
        self.posts.append(ref)
        return {"id": len(self.posts)}

    def records_on(self, ref: str) -> int:
        expected = marker(_REPO, _VERSION)
        return sum(expected in c.body.splitlines() for c in self.comments.get(ref, []))


def _issues(*numbers: int) -> tuple[IssueEntry, ...]:
    return tuple(IssueEntry(ref=f"{_REPO}#{n}", prs=(n + 6,)) for n in numbers)


def _state(issues: tuple[IssueEntry, ...], **changes: Any) -> StateBlock:
    base = initial_state(version=_VERSION, cut="c" * 40, issues=issues, without_deps=False)
    fields: dict[str, Any] = {"done": ("promote", "install"), "install_done": True, **changes}
    return replace(base, **fields)


def _ctx(state: StateBlock) -> ReleaseContext:
    root = Path("/tmp/r")  # noqa: S108
    ctx = ReleaseContext(repo=_REPO, version=_VERSION, repo_root=root, version_override=None)
    ctx.issue_number = 42
    ctx.tag = f"v{_VERSION}"
    ctx.release_url = f"https://github.com/{_REPO}/releases/tag/v{_VERSION}"
    ctx.state = state
    return ctx


@pytest.fixture
def fake() -> Iterator[_FakeGitHub]:
    gh = _FakeGitHub()

    def write(ctx: ReleaseContext, new: StateBlock) -> None:
        ctx.state = new

    with (
        patch(_MOD + ".github.graphql", side_effect=gh.graphql),
        patch(_MOD + ".github.post_json_once", side_effect=gh.post),
        patch(_MOD + ".write_state", side_effect=write),
        patch(_MOD + ".trusted_authors.load_trusted_authors", return_value=_AUTHORS),
    ):
        yield gh


def _record(
    author: str | dict[str, str] | None = _HUMAN,
    editor: str | dict[str, str] | None = None,
    edits: tuple[str, ...] = (),
) -> Comment:
    body = record_body(
        repo=_REPO,
        version=_VERSION,
        date="2026-11-20",
        release_url="https://x",
        prs=[(7, "a" * 40)],
        install="ran",
        supersedes=None,
    )
    return Comment(author, editor, body, edits)


def _trusted(body: str, index: int = 1) -> trust.TrustedComment:
    return trust.TrustedComment(str(index), body, trust.Role.HUMAN, _CREATED)


def test_record_body_matches_spec() -> None:
    body = record_body(
        repo="vergil-project/vergil-tooling",
        version="2.1.340",
        date="2026-11-20",
        release_url="https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340",
        prs=[(4840, "1a2b3c4d5e6f")],
        install="ran",
        supersedes=None,
    )
    assert body == (
        "<!-- vrg-release:released:vergil-project/vergil-tooling:v2.1.340 -->\n"
        "Released: vergil-project/vergil-tooling v2.1.340 on 2026-11-20\n"
        "\n"
        "- Release: https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340\n"
        "- Closed by: #4840 (merge commit 1a2b3c4)\n"
        "- Install: ran (`[publish].install`)"
    )


def test_record_body_none_configured_supersedes_and_two_prs() -> None:
    body = record_body(
        repo=_REPO,
        version=_VERSION,
        date="2026-11-20",
        release_url="https://x",
        prs=[(7, "a" * 40), (8, "b" * 40)],
        install="none configured",
        supersedes="2.1.1",
    )
    lines = body.splitlines()
    assert "- Closed by: #7 (merge commit aaaaaaa), #8 (merge commit bbbbbbb)" in lines
    assert "- Install: none configured" in lines
    assert lines[-1] == "- Supersedes: v2.1.1"


def test_records_posted_on_closed_and_reopened_issues(fake: _FakeGitHub) -> None:
    """The issue's state is never read: every issue in the list gets a record."""
    ctx = _ctx(_state(_issues(1, 2, 3)))
    post_records(ctx)
    assert fake.posts == ["o/r#1", "o/r#2", "o/r#3"]
    assert ctx.state is not None
    assert ctx.state.records_posted == ("o/r#1", "o/r#2", "o/r#3")
    first = fake.comments["o/r#1"][0].body
    assert first.splitlines()[0] == marker(_REPO, _VERSION)
    assert "- Closed by: #7 (merge commit aaaaaaa)" in first
    assert "Released: o/r v2.1.2 on 2026-11-20" in first


def test_records_name_each_issue_once(
    fake: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line per recorded issue, naming it and what happened (vergil-tooling#5221)."""
    fake.comments["o/r#2"] = [_record()]
    ctx = _ctx(_state(_issues(1, 2)))
    post_records(ctx)
    lines = capsys.readouterr().out.splitlines()
    assert "posted: https://github.com/o/r/issues/1" in lines
    assert "found existing: https://github.com/o/r/issues/2" in lines


def test_records_say_none_configured_without_an_install(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1), install_done=False))
    post_records(ctx)
    assert "- Install: none configured" in fake.comments["o/r#1"][0].body


def test_records_name_the_reported_install(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1), install_version=_VERSION, install_commit="f" * 40))
    post_records(ctx)
    assert "- Install: ran — installed v2.1.2 (commit fffffff)" in (
        fake.comments["o/r#1"][0].body.splitlines()
    )


def test_records_keep_ran_when_the_install_reported_nothing(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1)))
    post_records(ctx)
    assert "- Install: ran (`[publish].install`)" in fake.comments["o/r#1"][0].body.splitlines()


def test_records_refuse_when_install_not_done(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1), done=("promote",)))
    with pytest.raises(ReleaseError, match="does not record install done"):
        post_records(ctx)
    assert fake.posts == []


def test_records_resume_posts_only_missing(fake: _FakeGitHub) -> None:
    """Review focus 1: a crash partway through release-records. Only the issues
    without a counting record get one; none gets two."""
    ctx = _ctx(_state(_issues(1, 2, 3)))
    writes = 0

    def crash_on_second_write(c: ReleaseContext, new: StateBlock) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:  # noqa: PLR2004 - after #2's record is posted, before it is recorded
            raise KeyboardInterrupt
        c.state = new

    with (
        patch(_MOD + ".write_state", side_effect=crash_on_second_write),
        pytest.raises(KeyboardInterrupt),
    ):
        post_records(ctx)
    assert fake.posts == ["o/r#1", "o/r#2"]
    assert ctx.state is not None
    assert ctx.state.records_posted == ("o/r#1",)

    resumed = _ctx(ctx.state)
    fake.reads.clear()
    post_records(resumed)
    assert fake.posts == ["o/r#1", "o/r#2", "o/r#3"]
    assert [ref for ref, _ in fake.reads] == ["o/r#2", "o/r#3"]
    for ref in ("o/r#1", "o/r#2", "o/r#3"):
        assert fake.records_on(ref) == 1
    assert resumed.state is not None
    assert resumed.state.records_posted == ("o/r#1", "o/r#2", "o/r#3")
    assert (resumed.records_new, resumed.records_found) == (1, 1)


def test_foreign_marker_ignored_with_warning(
    fake: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.comments["o/r#1"] = [_record(author=_AGENT)]
    ctx = _ctx(_state(_issues(1)))
    post_records(ctx)
    assert fake.posts == ["o/r#1"]
    err = capsys.readouterr().err
    assert f"skipped untrusted content: o/r#1 comment {_AGENT} at {_CREATED} on comment 1" in err
    assert "(not-accepted)" in err
    assert "the task body" not in err


def test_edited_by_agent_record_does_not_count(
    fake: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.comments["o/r#1"] = [_record(editor=_AGENT)]
    post_records(_ctx(_state(_issues(1))))
    assert fake.posts == ["o/r#1"]
    err = capsys.readouterr().err
    assert f"o/r#1 edit {_AGENT} at {_EDITED} on comment 1 (not-accepted)" in err


def test_record_last_editor_rule_kept(fake: _FakeGitHub) -> None:
    """Only the last editor is checked: an earlier edit by an agent does not stop a record."""
    fake.comments["o/r#1"] = [_record(editor=_HUMAN, edits=(_AGENT,))]
    ctx = _ctx(_state(_issues(1)))
    post_records(ctx)
    assert fake.posts == []
    assert ctx.records_found == 1


def test_record_from_user_named_like_app_or_outsider_does_not_count(
    fake: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    impostor = {"__typename": "Bot", "login": _HUMAN}
    spam = Comment("drive-by", None, f"{marker(_REPO, _VERSION)}\nSPAM-MARKER-TEXT")
    fake.comments["o/r#1"] = [_record(author=impostor), spam]
    post_records(_ctx(_state(_issues(1))))
    assert fake.posts == ["o/r#1"]
    err = capsys.readouterr().err
    assert f"o/r#1 comment {_HUMAN} at {_CREATED} on comment 1 (wrong-type)" in err
    assert f"o/r#1 comment drive-by at {_CREATED} on comment 2 (untrusted)" in err
    assert "SPAM-MARKER-TEXT" not in err


def test_skip_report_counts_only_under_actions(
    fake: _FakeGitHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake.comments["o/r#1"] = [_record(author=_AGENT), Comment("drive-by", None, "x")]
    post_records(_ctx(_state(_issues(1))))
    err = capsys.readouterr().err
    assert "skipped 2 untrusted item(s) (not-accepted: 1, untrusted: 1)" in err
    assert "drive-by" not in err


def test_counting_record_found_and_marker_must_be_alone_on_its_line() -> None:
    own = _trusted(_record().body, 3)
    inline = _trusted(f"see {marker(_REPO, _VERSION)} above", 1)
    other_version = _trusted(marker(_REPO, "2.1.1"), 2)
    assert counting_record([inline, other_version, own], _REPO, _VERSION) == own
    assert counting_record([inline, other_version], _REPO, _VERSION) is None


def test_deleted_author_record_does_not_count(fake: _FakeGitHub) -> None:
    fake.comments["o/r#1"] = [_record(author=None), _record(editor=_GHOST)]
    assert record_comments("o/r#1", authors=_AUTHORS) == ()


def test_records_failure_names_each_issue_and_blocks_close(fake: _FakeGitHub) -> None:
    fake.fail_post = {"o/r#1", "o/r#3"}
    ctx = _ctx(_state(_issues(1, 2, 3)))
    with pytest.raises(ReleaseError) as excinfo:
        post_records(ctx)
    message = str(excinfo.value)
    assert "2 release record(s) could not be posted or confirmed" in message
    assert "o/r#1:" in message
    assert "o/r#3:" in message
    assert "o/r#2" not in message
    assert "vrg-release --resume 2.1.2" in message
    assert ctx.state is not None
    assert ctx.state.records_posted == ("o/r#2",)
    with (
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
        pytest.raises(ReleaseError, match=r"no record on o/r#1, o/r#3"),
    ):
        close_tracking(ctx)
    m_close.assert_not_called()


def test_close_tracking_only_after_all_records(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1)))
    with (
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
        pytest.raises(ReleaseError, match="no record on o/r#1"),
    ):
        close_tracking(ctx)
    m_close.assert_not_called()

    post_records(ctx)
    with (
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
        pytest.raises(ReleaseError, match="release-records is not done"),
    ):
        close_tracking(ctx)
    m_close.assert_not_called()

    assert ctx.state is not None
    ctx.state = replace(ctx.state, done=(*ctx.state.done, "release-records"))
    with patch(_FINALIZE + ".close_tracking_issue") as m_close:
        close_tracking(ctx)
    m_close.assert_called_once()
    summary = m_close.call_args.args[1]
    assert "### Released: records\n- o/r#1" in summary


def _run_tail(
    state: StateBlock, *, deps_fail: bool = False
) -> tuple[int, ReleaseContext, list[str], list[object]]:
    """Run update-deps, release-records and close-tracking through the pipeline."""
    rs = ReleaseState(version_override=None, repo_root=Path("/tmp/r"))  # noqa: S108
    rs.ctx = _ctx(state)
    names = ("update-deps", "release-records", "close-tracking")
    failure = ReleaseError(phase="update-deps", command="vrg-update-deps", message="failed")
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch(_ORCH + ".comment_phase_complete"),
        patch(_ORCH + ".comment_phase_failed"),
        patch(_ORCH + ".ensure_checklist"),
        patch(_ORCH + ".tick_stage"),
        patch(_ORCH + ".write_state", side_effect=lambda c, new: setattr(c, "state", new)),
        patch(_ORCH + ".update_deps", side_effect=failure if deps_fail else None),
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
    ):
        stages = [s for s in build_stages() if s.name in names]
        assert [s.name for s in stages] == list(names)
        rc = progress.run_pipeline(
            rs, stages, command="vrg-release", label="vrg-release", args=args, repo_root=Path()
        )
    return rc, rs.ctx, list(rs.ctx.deferred_failures), list(m_close.call_args_list)


def test_update_deps_failure_holds_records(fake: _FakeGitHub) -> None:
    rc, ctx, failed, closes = _run_tail(_state(_issues(1)), deps_fail=True)
    assert rc == 1
    assert failed == ["update-deps"]
    assert fake.posts == []
    assert closes == []
    assert ctx.state is not None
    assert "release-records" not in ctx.state.done


def test_clean_tail_posts_records_then_closes(fake: _FakeGitHub) -> None:
    rc, ctx, failed, closes = _run_tail(_state(_issues(1, 2)))
    assert rc == 0
    assert failed == []
    assert fake.posts == ["o/r#1", "o/r#2"]
    assert len(closes) == 1
    assert ctx.state is not None
    assert ctx.state.done[-2:] == ("release-records", "close-tracking")


def test_records_failure_holds_close_tracking_in_the_pipeline(fake: _FakeGitHub) -> None:
    fake.fail_post = {"o/r#2"}
    rc, ctx, failed, closes = _run_tail(_state(_issues(1, 2)))
    assert rc == 1
    assert failed == ["release-records"]
    assert closes == []
    assert ctx.state is not None
    assert ctx.state.records_posted == ("o/r#1",)


def test_records_comment_read_paginates(fake: _FakeGitHub) -> None:
    """150 comments, the record on page 2: found, so no second record is posted."""
    filler = [Comment(_HUMAN, None, f"comment {i}") for i in range(149)]
    fake.comments["o/r#1"] = [*filler, _record()]
    ctx = _ctx(_state(_issues(1)))
    post_records(ctx)
    assert fake.reads == [("o/r#1", None), ("o/r#1", "100")]
    assert fake.posts == []
    assert ctx.records_found == 1
    assert ctx.state is not None
    assert ctx.state.records_posted == ("o/r#1",)


def test_records_comment_page_failure_raises(fake: _FakeGitHub) -> None:
    fake.comments["o/r#1"] = [Comment(_HUMAN, None, f"c{i}") for i in range(150)]
    fake.fail_read = {"o/r#1": 1}
    with pytest.raises(trust.TrustReadError, match="HTTP 502"):
        record_comments("o/r#1", authors=_AUTHORS)
    ctx = _ctx(_state(_issues(1)))
    with pytest.raises(ReleaseError, match=r"(?s)o/r#1: .*HTTP 502"):
        post_records(ctx)
    assert fake.posts == []


def test_records_refuse_failed_trusted_author_load(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1)))
    with (
        patch(_MOD + ".trusted_authors.load_trusted_authors", side_effect=ValueError("bad")),
        pytest.raises(ReleaseError, match="Cannot tell which release records count: bad"),
    ):
        post_records(ctx)
    assert fake.posts == []


def test_comment_read_refuses_an_actor_without_its_type(fake: _FakeGitHub) -> None:
    """An actor whose account type is missing is a read failure, never a guess."""
    fake.comments["o/r#1"] = [_record(author={"login": _HUMAN})]
    with pytest.raises(trust.TrustReadError, match="has no __typename"):
        record_comments("o/r#1", authors=_AUTHORS)
    with pytest.raises(ReleaseError, match=r"o/r#1: .*has no __typename"):
        post_records(_ctx(_state(_issues(1))))
    assert fake.posts == []


def test_comment_read_refuses_a_malformed_ref() -> None:
    with pytest.raises(ValueError, match="is not <owner>/<repo>#<N>"):
        record_comments("o/r/1", authors=_AUTHORS)


def test_merge_commit_and_release_date_refuse_a_missing_value() -> None:
    no_oid = {"repository": {"pullRequest": {"mergeCommit": {"oid": ""}}}}
    with (
        patch(_MOD + ".github.graphql", return_value=no_oid),
        pytest.raises(records.RecordReadError, match="mergeCommit has no oid"),
    ):
        records._merge_commit(_REPO, 7)  # noqa: SLF001
    unmerged = {"repository": {"pullRequest": {"mergeCommit": None}}}
    with (
        patch(_MOD + ".github.graphql", return_value=unmerged),
        pytest.raises(records.RecordReadError, match="mergeCommit is None"),
    ):
        records._merge_commit(_REPO, 7)  # noqa: SLF001
    draft = {"repository": {"release": {"publishedAt": None}}}
    with (
        patch(_MOD + ".github.graphql", return_value=draft),
        pytest.raises(records.RecordReadError, match="release has no publishedAt"),
    ):
        records._release_date(_REPO, "v2.1.2")  # noqa: SLF001


def test_counting_record_keeps_the_first_of_two() -> None:
    first, second = _trusted(_record().body, 1), _trusted(_record().body, 2)
    assert counting_record([first, second], _REPO, _VERSION) == first


def test_records_refuse_without_a_tag(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1)))
    ctx.release_url = None
    with pytest.raises(ReleaseError, match="no tag or GitHub Release is recorded"):
        post_records(ctx)
    assert fake.posts == []


def test_records_refuse_without_a_state_block() -> None:
    ctx = _ctx(_state(()))
    ctx.state = None
    with pytest.raises(ReleaseError, match="no release state block"):
        post_records(ctx)


def test_records_with_no_issues_post_nothing(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(()))
    ctx.tag = None
    post_records(ctx)
    assert fake.posts == []
    assert fake.reads == []


def test_merge_commit_read_once_per_pr(fake: _FakeGitHub) -> None:
    issues = (IssueEntry("o/r#1", (7,)), IssueEntry("o/r#2", (7, 8)))
    ctx = _ctx(_state(issues))
    with patch(_MOD + "._merge_commit", side_effect=lambda _r, n: _SHAS[n]) as m_merge:
        post_records(ctx)
    assert [c.args for c in m_merge.call_args_list] == [(_REPO, 7), (_REPO, 8)]


def test_post_is_never_retried() -> None:
    """A post goes through post_json_once: a retried post that had landed would
    give the issue a second record."""
    with patch(_MOD + ".github.post_json_once") as m_post:
        records._post("o/r#5", "body")  # noqa: SLF001
    m_post.assert_called_once_with("repos/o/r/issues/5/comments", {"body": "body"})


# --- test_no_deferred_vergil_tooling_imports ---------------------------------

_SRC = Path(records.__file__).resolve().parents[3]


def _module_path(module: str) -> Path | None:
    base = _SRC / module.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _load_time_imports(body: list[ast.stmt]) -> Iterator[str]:
    """The modules *body* imports when its module loads: not in a function or TYPE_CHECKING."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node):
            yield from _load_time_imports(node.orelse)
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)
        else:
            for field in ("body", "orelse", "handlers", "finalbody"):
                sub = getattr(node, field, None)
                if isinstance(sub, list):
                    yield from _load_time_imports(sub)


def _deferred_imports(tree: ast.Module) -> list[int]:
    """The lines of every ``vergil_tooling`` import inside a function of *tree*."""
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import) and any(
                a.name.split(".")[0] == "vergil_tooling" for a in inner.names
            ):
                lines.add(inner.lineno)
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").split(".")[0] == (
                "vergil_tooling"
            ):
                lines.add(inner.lineno)
    return sorted(lines)


def test_no_deferred_vergil_tooling_imports() -> None:
    """After install, the last stages start no vergil-tooling code that is not
    loaded yet (spec §4.4): every module reachable from release-records and
    close-tracking imports what it uses when it loads, never in a function."""
    seen: set[str] = set()
    pending = ["vergil_tooling.lib.release.records", "vergil_tooling.lib.release.finalize"]
    offenders: list[str] = []
    while pending:
        module = pending.pop()
        if module in seen or module.split(".")[0] != "vergil_tooling":
            continue
        path = _module_path(module)
        if path is None:
            continue
        seen.add(module)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(f"{module}:{line}" for line in _deferred_imports(tree))
        parts = module.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        pending.extend(_load_time_imports(tree.body))
    assert "vergil_tooling.lib.release.tracking" in seen
    assert "vergil_tooling.lib.github" in seen
    assert offenders == []


def test_deferred_import_walker_finds_a_function_level_import() -> None:
    tree = ast.parse("def f():\n    from vergil_tooling.lib import git\n")
    assert _deferred_imports(tree) == [2]


def test_records_stage_wraps_subprocess_errors_only(fake: _FakeGitHub) -> None:
    """A failure that is not a read or post error (a bug) is not collected per issue."""
    ctx = _ctx(_state(_issues(1)))
    with (
        patch(_MOD + ".record_comments", side_effect=KeyError("bug")),
        pytest.raises(KeyError),
    ):
        post_records(ctx)
    assert fake.posts == []


def test_records_stage_collects_a_failed_read_per_issue(fake: _FakeGitHub) -> None:
    ctx = _ctx(_state(_issues(1, 2)))
    error = subprocess.CalledProcessError(1, ("gh",), stderr="boom")
    real = record_comments

    def flaky(ref: str, *, authors: TrustedAuthors) -> tuple[trust.TrustedComment, ...]:
        if ref == "o/r#1":
            raise error
        return real(ref, authors=authors)

    with patch(_MOD + ".record_comments", side_effect=flaky), pytest.raises(ReleaseError):
        post_records(ctx)
    assert fake.posts == ["o/r#2"]
