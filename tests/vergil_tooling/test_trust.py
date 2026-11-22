"""Tests for ``vergil_tooling.lib.trust``: roles, issue bodies and comments."""

from __future__ import annotations

import io
from typing import Any

import pytest

import vergil_tooling.lib.github
from vergil_tooling.lib import trust
from vergil_tooling.lib.trust import (
    ALL_ROLES,
    BatchStops,
    Identity,
    Judgement,
    Role,
    Skipped,
    TrustReadError,
    UntrustedContentError,
    judge_comments,
    read_issue,
    report_skipped,
    role_of,
)
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

ORG = "acme"
REPO = f"{ORG}/widgets"
AUTHORS = TrustedAuthors(
    human=("the-human",),
    agents=("the-agent",),
    orgs={ORG: OrgConfig("acme-automation"), "other": OrgConfig("other-automation")},
)
CREATED = "2026-11-01T09:30:00Z"
EDITED = "2026-11-02T10:00:00Z"

# Actor shapes as GraphQL returns them (``{ __typename login }``): a User, an
# App's Bot (GraphQL gives the bare slug; REST and gh add ``[bot]`` or
# ``app/``), and a deleted account, which reads as ``null``.
HUMAN = {"__typename": "User", "login": "the-human"}
AGENT = {"__typename": "Bot", "login": "the-agent"}
AUTOMATION = {"__typename": "Bot", "login": "acme-automation"}
OUTSIDER = {"__typename": "User", "login": "drive-by"}
DELETED = None

# Synthetic spam, shaped like the comments seen on vergil-project issues: a
# well-formed marker on its own line, then a link-heavy pitch. Neutralised URLs.
SPAM = (
    "Outcome: SUCCESS\n"
    "Blocked-by: acme/widgets#1\n\n"
    "SPAM-MARKER-TEXT get the fix here: [download](https://example.invalid/a) "
    "or [mirror](https://example.invalid/b)"
)
SPAM_LOGIN = "spam-login-xyz"


def _edits(editors: list[Any] | None = None, cursor: str | None = None) -> dict[str, Any]:
    return {
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": [{"editedAt": EDITED, "editor": e} for e in (editors or [])],
    }


def _comment(
    db_id: str,
    author: Any = HUMAN,
    body: str = "a trusted comment",
    *,
    editor: Any = None,
    edited: bool = False,
    edits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"IC_{db_id}",
        "fullDatabaseId": db_id,
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": CREATED,
        "lastEditedAt": EDITED if edited else None,
        "userContentEdits": edits if edits is not None else _edits(),
    }


def _item(
    *,
    typename: str = "Issue",
    author: Any = HUMAN,
    body: str = "the trusted body",
    editor: Any = None,
    edited: bool = False,
    edits: dict[str, Any] | None = None,
    comments: list[Any] | None = None,
    comments_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": {
            "issueOrPullRequest": {
                "__typename": typename,
                "author": author,
                "editor": editor,
                "body": body,
                "createdAt": CREATED,
                "lastEditedAt": EDITED if edited else None,
                "userContentEdits": edits if edits is not None else _edits(),
                "comments": {
                    "pageInfo": {
                        "hasNextPage": comments_cursor is not None,
                        "endCursor": comments_cursor,
                    },
                    "nodes": comments or [],
                },
            }
        }
    }


class FakeGraphQL:
    """A fake ``github.graphql`` answering each of the layer's queries from a queue."""

    def __init__(self, item: dict[str, Any], **pages: list[dict[str, Any]]) -> None:
        self.answers = {
            trust._ITEM_QUERY: [item],
            trust._BODY_EDITS_QUERY: pages.get("body_edits", []),
            trust._COMMENTS_QUERY: pages.get("comments", []),
            trust._COMMENT_EDITS_QUERY: pages.get("comment_edits", []),
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, query: str, **variables: object) -> dict[str, Any]:
        self.calls.append((query, variables))
        return self.answers[query].pop(0)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(item: dict[str, Any], **pages: list[dict[str, Any]]) -> FakeGraphQL:
        fake = FakeGraphQL(item, **pages)
        monkeypatch.setattr(vergil_tooling.lib.github, "graphql", fake)
        return fake

    return install


@pytest.fixture(autouse=True)
def _not_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _read(**kwargs: Any) -> trust.IssueContent:
    return read_issue(REPO, 7, authors=AUTHORS, **kwargs)


def _role(login: str | None, typename: str | None, org: str = ORG) -> Judgement:
    return role_of(Identity(login, typename), org=org, authors=AUTHORS)


# --- role_of -----------------------------------------------------------------


def test_role_of_human_user() -> None:
    assert _role("the-human", "User") == Judgement(Role.HUMAN, None)


def test_role_of_app_login_forms() -> None:
    for login in ("app/the-agent", "the-agent", "the-agent[bot]"):
        assert _role(login, "Bot") == Judgement(Role.AGENTS, None)
    assert _role("acme-automation[bot]", "Bot") == Judgement(Role.AUTOMATION, None)


def test_role_of_user_matching_app_slug_is_wrong_type() -> None:
    assert _role("the-agent", "User") == Judgement(None, trust.WRONG_TYPE)
    assert _role("acme-automation", "User") == Judgement(None, trust.WRONG_TYPE)
    assert _role("the-human", "Bot") == Judgement(None, trust.WRONG_TYPE)


def test_role_of_login_without_typename_is_wrong_type() -> None:
    assert _role("the-human", None) == Judgement(None, trust.WRONG_TYPE)


def test_judgement_untrusted_role_is_none() -> None:
    for login, typename in (
        ("drive-by", "User"),
        (None, None),
        ("ghost", "User"),
        ("the-agent", "User"),
        ("acme-automation", "Bot"),
    ):
        judgement = _role(login, typename, org="other")
        assert not judgement.role
        assert judgement.cause in (trust.UNTRUSTED, trust.DELETED, trust.WRONG_TYPE)


def test_role_of_deleted_is_untrusted() -> None:
    assert _role(None, None) == Judgement(None, trust.DELETED)


def test_role_of_ghost_is_untrusted() -> None:
    assert _role("ghost", "User") == Judgement(None, trust.DELETED)


def test_automation_untrusted_in_other_org() -> None:
    assert _role("acme-automation", "Bot", org="other") == Judgement(None, trust.UNTRUSTED)
    assert _role("other-automation", "Bot", org="other") == Judgement(Role.AUTOMATION, None)


def test_unlisted_org_has_no_automation_role() -> None:
    assert _role("acme-automation", "Bot", org="stranger") == Judgement(None, trust.UNTRUSTED)
    # The global roles hold in any org.
    assert _role("the-human", "User", org="stranger") == Judgement(Role.HUMAN, None)


def test_role_of_org_matches_case_insensitively() -> None:
    assert _role("acme-automation", "Bot", org="ACME") == Judgement(Role.AUTOMATION, None)


# --- read_issue ----------------------------------------------------------------


def test_read_issue_trusted_body_and_comments(fake: Any) -> None:
    calls = fake(
        _item(comments=[_comment("1"), _comment("2", AGENT), _comment("3", AUTOMATION)])
    ).calls
    content = _read()
    assert content.body == "the trusted body"
    assert content.body_role is Role.HUMAN
    assert content.author_role is Role.HUMAN
    assert [(c.id, c.role, c.at) for c in content.comments] == [
        ("1", Role.HUMAN, CREATED),
        ("2", Role.AGENTS, CREATED),
        ("3", Role.AUTOMATION, CREATED),
    ]
    assert content.skipped == ()
    assert calls == [(trust._ITEM_QUERY, {"owner": ORG, "name": "widgets", "number": 7})]


def test_read_issue_loads_configured_authors(fake: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(_item())
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    assert read_issue(REPO, 7).body_role is Role.HUMAN


def test_read_issue_reads_pr(fake: Any) -> None:
    calls = fake(_item(typename="PullRequest", author=AGENT)).calls
    content = _read()
    assert content.body_role is Role.AGENTS
    assert "issueOrPullRequest(number: $number)" in calls[0][0]
    assert "... on PullRequest" in calls[0][0]


def test_read_issue_pr_untrusted_body_is_kind_pr(fake: Any) -> None:
    fake(_item(typename="PullRequest", author=OUTSIDER))
    assert _read().skipped[0].kind == "pr"


def test_read_issue_comment_from_user_named_like_app_is_skipped(fake: Any) -> None:
    impostor = {"__typename": "User", "login": "the-agent"}
    fake(_item(comments=[_comment("9", impostor, SPAM)]))
    content = _read()
    assert content.comments == ()
    assert content.skipped == (
        Skipped(REPO, 7, "comment", "the-agent", CREATED, "comment 9", trust.WRONG_TYPE),
    )


def test_read_issue_body_from_user_named_like_app_require_body_raises(fake: Any) -> None:
    fake(_item(author={"__typename": "User", "login": "the-agent"}, body=SPAM))
    with pytest.raises(UntrustedContentError) as info:
        _read(require_body=True)
    assert info.value.item.cause == trust.WRONG_TYPE
    assert info.value.item.kind == "issue"
    assert "SPAM-MARKER-TEXT" not in str(info.value)


def test_read_issue_body_untrusted_after_outsider_edit(fake: Any) -> None:
    fake(_item(editor=OUTSIDER, edited=True, edits=_edits([OUTSIDER, HUMAN]), body=SPAM))
    content = _read()
    assert content.body is None
    assert content.body_role is None
    assert content.author_role is Role.HUMAN
    assert content.skipped == (
        Skipped(REPO, 7, "edit", "drive-by", EDITED, "body", trust.UNTRUSTED),
    )


def test_read_issue_last_editor_rule(fake: Any) -> None:
    # An outsider edited earlier; the human edited last.
    item = _item(editor=HUMAN, edited=True, edits=_edits([HUMAN, OUTSIDER]))
    fake(item)
    assert _read(editors="last").body == "the trusted body"
    fake(item)
    assert _read(editors="every").body is None


def test_read_issue_require_body_raises(fake: Any) -> None:
    fake(_item(author=OUTSIDER, body=SPAM))
    with pytest.raises(UntrustedContentError) as info:
        _read(require_body=True)
    assert info.value.item == Skipped(REPO, 7, "issue", "drive-by", CREATED, "body", "untrusted")
    assert info.value.reason == "the issue body is untrusted"


def test_read_issue_outsider_body_reported_without_require(fake: Any) -> None:
    fake(_item(author=OUTSIDER, body=SPAM))
    content = _read()
    assert (content.body, content.body_role, content.author_role) == (None, None, None)
    assert content.skipped[0].kind == "issue"


def test_read_issue_skips_spam_comment_and_reports(
    fake: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    spam = {"__typename": "User", "login": SPAM_LOGIN}
    fake(_item(comments=[_comment("1"), _comment("2", spam, SPAM), _comment("3", AGENT)]))
    content = _read()
    assert [c.id for c in content.comments] == ["1", "3"]
    assert all("SPAM-MARKER-TEXT" not in c.body for c in content.comments)
    report_skipped(content.skipped, stream=(stream := io.StringIO()))
    out = stream.getvalue()
    assert out == (
        f"skipped untrusted content: {REPO}#7 comment {SPAM_LOGIN} at {CREATED}"
        " on comment 2 (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in out
    assert "example.invalid" not in out


def test_read_issue_roles_parameter_narrows(fake: Any) -> None:
    fake(_item(author=AGENT, comments=[_comment("1"), _comment("2", AGENT)]))
    content = _read(roles=frozenset({Role.HUMAN}))
    assert content.body is None
    assert content.author_role is None
    assert [c.id for c in content.comments] == ["1"]
    assert [s.cause for s in content.skipped] == [trust.NOT_ACCEPTED, trust.NOT_ACCEPTED]


def test_read_issue_roles_parameter_applies_to_editors(fake: Any) -> None:
    fake(_item(editor=AGENT, edited=True, edits=_edits([AGENT])))
    content = _read(roles=frozenset({Role.HUMAN}))
    assert content.body is None
    assert content.skipped[0] == Skipped(
        REPO, 7, "edit", "the-agent", EDITED, "body", trust.NOT_ACCEPTED
    )


def test_read_issue_pages_all_comments(fake: Any) -> None:
    spam = {"__typename": "User", "login": SPAM_LOGIN}
    second = _item(comments=[_comment("2", spam, SPAM)], comments_cursor="c2")
    third = _item(comments=[_comment("3")])
    calls = fake(
        _item(comments=[_comment("1")], comments_cursor="c1"), comments=[second, third]
    ).calls
    content = _read()
    assert [c.id for c in content.comments] == ["1", "3"]
    assert [s.field for s in content.skipped] == ["comment 2"]
    assert [v.get("after") for q, v in calls if q == trust._COMMENTS_QUERY] == ["c1", "c2"]


def test_read_issue_pages_all_edits(fake: Any) -> None:
    # The body's history and a comment's history each run past one page,
    # and the outsider's edit is only on the last page.
    body_page = {"repository": {"issueOrPullRequest": {"userContentEdits": _edits([OUTSIDER])}}}
    comment = _comment("5", editor=HUMAN, edited=True, edits=_edits([HUMAN], cursor="e1"))
    comment_pages = [
        {"node": {"userContentEdits": _edits([HUMAN], cursor="e2")}},
        {"node": {"userContentEdits": _edits([DELETED])}},
    ]
    calls = fake(
        _item(editor=HUMAN, edited=True, edits=_edits([HUMAN], cursor="b1"), comments=[comment]),
        body_edits=[body_page],
        comment_edits=comment_pages,
    ).calls
    content = _read()
    assert content.body is None
    assert content.comments == ()
    assert [(s.kind, s.login, s.field, s.cause) for s in content.skipped] == [
        ("edit", "drive-by", "body", trust.UNTRUSTED),
        ("edit", None, "comment 5", trust.DELETED),
    ]
    assert [v for q, v in calls if q == trust._COMMENT_EDITS_QUERY] == [
        {"id": "IC_5", "after": "e1"},
        {"id": "IC_5", "after": "e2"},
    ]
    assert [v.get("after") for q, v in calls if q == trust._BODY_EDITS_QUERY] == ["b1"]


def test_read_issue_body_edited_by_deleted_account_is_untrusted(fake: Any) -> None:
    fake(_item(editor=DELETED, edited=True, edits=_edits([DELETED])))
    content = _read()
    assert content.body is None
    assert content.skipped == (Skipped(REPO, 7, "edit", None, EDITED, "body", trust.DELETED),)


def test_read_issue_comment_edited_by_deleted_account_is_skipped(fake: Any) -> None:
    fake(_item(comments=[_comment("4", editor=DELETED, edited=True, edits=_edits([DELETED]))]))
    content = _read(editors="last")
    assert content.comments == ()
    assert content.skipped == (Skipped(REPO, 7, "edit", None, EDITED, "comment 4", trust.DELETED),)


def test_read_issue_deleted_author_is_untrusted(fake: Any) -> None:
    fake(_item(author=DELETED, comments=[_comment("6", DELETED)]))
    content = _read()
    assert content.body is None
    assert [(s.kind, s.login, s.cause) for s in content.skipped] == [
        ("issue", None, trust.DELETED),
        ("comment", None, trust.DELETED),
    ]


def test_read_issue_empty_body_is_trusted(fake: Any) -> None:
    fake(_item(body=""))
    assert _read().body == ""


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d["repository"].pop("issueOrPullRequest"), id="no-item"),
        pytest.param(lambda d: d.pop("repository"), id="no-repository"),
        pytest.param(
            lambda d: d["repository"]["issueOrPullRequest"].update(__typename="Discussion"),
            id="other-type",
        ),
        pytest.param(lambda d: _node(d).pop("author"), id="no-author"),
        pytest.param(lambda d: _node(d).update(author={"login": "x"}), id="no-typename"),
        pytest.param(lambda d: _node(d).update(author={"__typename": "User"}), id="no-login"),
        pytest.param(lambda d: _node(d).update(author="the-human"), id="author-not-object"),
        pytest.param(lambda d: _node(d).update(body=None), id="body-null"),
        pytest.param(lambda d: _node(d).update(createdAt=""), id="created-empty"),
        pytest.param(lambda d: _node(d).update(lastEditedAt=3), id="edited-not-string"),
        pytest.param(lambda d: _node(d)["comments"].pop("pageInfo"), id="no-page-info"),
        pytest.param(lambda d: _node(d)["comments"].update(nodes=None), id="nodes-null"),
        pytest.param(
            lambda d: _node(d)["comments"]["pageInfo"].update(hasNextPage=None), id="has-next-null"
        ),
        pytest.param(
            lambda d: _node(d)["comments"]["pageInfo"].update(hasNextPage=True, endCursor=""),
            id="next-no-cursor",
        ),
        pytest.param(
            lambda d: _node(d)["userContentEdits"]["nodes"].append({"editor": HUMAN}),
            id="edit-no-time",
        ),
        pytest.param(
            lambda d: _node(d)["userContentEdits"]["nodes"].append(None), id="edit-not-object"
        ),
        pytest.param(
            lambda d: _node(d)["comments"]["nodes"].append({"fullDatabaseId": "8"}),
            id="comment-no-id",
        ),
        pytest.param(lambda d: _node(d)["comments"]["nodes"].append("x"), id="comment-not-object"),
    ],
)
def test_read_issue_malformed_page_raises(fake: Any, mutate: Any) -> None:
    item = _item(body=SPAM, comments=[_comment("1", OUTSIDER, SPAM)])
    mutate(item)
    fake(item)
    with pytest.raises(TrustReadError) as info:
        _read()
    assert info.value.call == f"read_issue {REPO}#7"
    assert "SPAM-MARKER-TEXT" not in str(info.value)


def _node(data: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = data["repository"]["issueOrPullRequest"]
    return node


def test_read_issue_malformed_later_page_raises(fake: Any) -> None:
    fake(
        _item(comments_cursor="c1"),
        comments=[{"repository": {"issueOrPullRequest": {}}}],
    )
    with pytest.raises(TrustReadError, match="has no comments"):
        _read()


def test_read_issue_malformed_comment_edits_page_raises(fake: Any) -> None:
    comment = _comment("5", edits=_edits([HUMAN], cursor="e1"))
    fake(_item(comments=[comment]), comment_edits=[{"node": None}])
    with pytest.raises(TrustReadError, match="comment 5 is missing"):
        _read()


def _api_error(stderr: str) -> vergil_tooling.lib.github.GitHubAPIError:
    return vergil_tooling.lib.github.GitHubAPIError(
        1, ["gh", "api", "graphql"], output=SPAM, stderr=stderr
    )


@pytest.mark.parametrize(
    ("exc", "detail"),
    [
        (
            _api_error("\nGraphQL: Could not resolve to an issue (repository)\n"),
            "the GraphQL call failed: GraphQL: Could not resolve to an issue (repository)",
        ),
        (_api_error(""), "the GraphQL call failed: no error output"),
        (
            RuntimeError(f"GraphQL response has no data object: {SPAM}"),
            "the GraphQL response carried errors or no data",
        ),
    ],
)
def test_read_issue_api_error_raises(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, detail: str
) -> None:
    def boom(query: str, **variables: object) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", boom)
    with pytest.raises(TrustReadError) as info:
        _read()
    assert info.value.detail == detail
    assert "SPAM-MARKER-TEXT" not in str(info.value)
    assert info.value.__cause__ is None


def test_read_issue_missing_token_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, **variables: object) -> dict[str, Any]:
        raise vergil_tooling.lib.github.MissingGitHubTokenError(1, ["gh"], stderr="no token")

    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", boom)
    with pytest.raises(vergil_tooling.lib.github.MissingGitHubTokenError):
        _read()


def test_read_issue_bad_repo_raises() -> None:
    for repo in ("widgets", "acme/", "/widgets", "acme/w/x"):
        with pytest.raises(ValueError, match="owner/name"):
            read_issue(repo, 7, authors=AUTHORS)


# --- judge_comments ---------------------------------------------------------------


def test_judge_comments_judges_complete_input() -> None:
    trusted, skipped = judge_comments(
        [_comment("1"), _comment("2", OUTSIDER, SPAM)], repo=REPO, number=7, authors=AUTHORS
    )
    assert [(c.id, c.body, c.role) for c in trusted] == [("1", "a trusted comment", Role.HUMAN)]
    assert [s.field for s in skipped] == ["comment 2"]


def test_judge_comments_unpaged_edits_raises() -> None:
    comment = _comment("1", edits=_edits([HUMAN], cursor="more"))
    with pytest.raises(TrustReadError, match="edits of comment 1 are not read in full"):
        judge_comments([comment], repo=REPO, number=7, authors=AUTHORS)


def test_judge_comments_missing_typename_raises() -> None:
    comment = _comment("1", {"login": "the-human"})
    with pytest.raises(TrustReadError, match="has no __typename") as info:
        judge_comments([comment], repo=REPO, number=7, authors=AUTHORS)
    assert info.value.call == f"judge_comments {REPO}#7"


def test_judge_comments_missing_field_raises() -> None:
    comment = _comment("1")
    del comment["userContentEdits"]
    with pytest.raises(TrustReadError, match="has no userContentEdits"):
        judge_comments([comment], repo=REPO, number=7, authors=AUTHORS)


def test_comment_fields_name_every_needed_field() -> None:
    for field in ("id", "fullDatabaseId", "__typename", "editedAt", "lastEditedAt", "hasNextPage"):
        assert field in trust.COMMENT_FIELDS


# --- errors and reports -----------------------------------------------------------


def test_errors_carry_no_untrusted_text(fake: Any) -> None:
    spam = {"__typename": "User", "login": SPAM_LOGIN}
    fake(_item(author=spam, body=SPAM))
    with pytest.raises(UntrustedContentError) as info:
        _read(require_body=True)
    message = str(info.value)
    assert message == (
        f"stopped before any write: the issue body is untrusted: {REPO}#7 issue"
        f" {SPAM_LOGIN} at {CREATED} on body (untrusted). The human decides next."
    )
    assert "SPAM-MARKER-TEXT" not in message
    assert "example.invalid" not in message
    error = TrustReadError("read_issue x/y#1", "the comments page has no pageInfo")
    assert str(error) == (
        "cannot read who wrote the content (read_issue x/y#1): the comments page has no pageInfo"
    )


def test_skipped_describes_deleted_account() -> None:
    item = Skipped(REPO, 7, "comment", None, CREATED, "comment 3", trust.DELETED)
    assert item.describe() == f"{REPO}#7 comment a deleted account at {CREATED} on comment 3"


def test_report_skipped_nothing_prints_nothing() -> None:
    report_skipped([], stream=(stream := io.StringIO()))
    assert stream.getvalue() == ""


def test_report_skipped_counts_only_under_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    skipped = [
        Skipped(REPO, 7, "comment", SPAM_LOGIN, CREATED, "comment 1", trust.UNTRUSTED),
        Skipped(REPO, 7, "comment", None, CREATED, "comment 2", trust.DELETED),
        Skipped(REPO, 8, "comment", SPAM_LOGIN, CREATED, "comment 3", trust.UNTRUSTED),
    ]
    report_skipped(skipped, stream=(stream := io.StringIO()))
    assert stream.getvalue() == "skipped 3 untrusted item(s) (deleted: 1, untrusted: 2)\n"


def _stops() -> BatchStops:
    stops = BatchStops()
    stops.add(Skipped(REPO, 7, "issue", SPAM_LOGIN, CREATED, "body", trust.UNTRUSTED), "untrusted")
    stops.add(Skipped(REPO, 9, "issue", None, "", "body", trust.READ_FAILURE), trust.READ_FAILURE)
    return stops


def test_batch_stops_exit_code() -> None:
    assert BatchStops().exit_code() == 0
    assert _stops().exit_code() == 1


def test_batch_summary_lists_stops_outside_actions() -> None:
    _stops().print_summary(stream := io.StringIO())
    assert stream.getvalue() == (
        f"stopped: {REPO}#7 issue {SPAM_LOGIN} at {CREATED} on body (untrusted)\n"
        f"stopped: {REPO}#9 issue a deleted account at  on body (read-failure)\n"
    )


def test_batch_summary_empty_prints_nothing() -> None:
    BatchStops().print_summary(stream := io.StringIO())
    assert stream.getvalue() == ""


def test_batch_summary_counts_only_under_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _stops().print_summary(stream := io.StringIO())
    out = stream.getvalue()
    assert out.startswith("stopped 2 item(s) (read-failure: 1, untrusted: 1)\n")
    assert SPAM_LOGIN not in out
    assert f"{REPO}#7" not in out


def test_batch_summary_lists_read_failures_under_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _stops().print_summary(stream := io.StringIO())
    assert stream.getvalue().splitlines()[1:] == [f"stopped on a read failure: {REPO}#9"]


def test_all_roles() -> None:
    assert ALL_ROLES == {Role.HUMAN, Role.AGENTS, Role.AUTOMATION}
