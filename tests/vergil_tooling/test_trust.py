"""Tests for ``vergil_tooling.lib.trust``: roles, issue bodies and comments."""

from __future__ import annotations

import io
import re
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


# --- outsider_opened -----------------------------------------------------------


def test_outsider_opened_names_the_author(fake: Any) -> None:
    fake(_item(author=OUTSIDER, body=SPAM, comments=[_comment("4", OUTSIDER, SPAM)]))
    assert trust.outsider_opened(_read()) == Skipped(
        REPO, 7, "issue", "drive-by", CREATED, "body", trust.UNTRUSTED
    )


def test_outsider_opened_pr_and_impostor(fake: Any) -> None:
    fake(_item(typename="PullRequest", author={"__typename": "User", "login": "the-agent"}))
    opened = trust.outsider_opened(_read())
    assert opened is not None
    assert (opened.kind, opened.login, opened.cause) == ("pr", "the-agent", trust.WRONG_TYPE)


def test_outsider_opened_none_for_trusted_author_with_untrusted_edit_and_comment(
    fake: Any,
) -> None:
    # Only the author decides it: an outsider's edit or comment does not.
    fake(
        _item(
            editor=OUTSIDER,
            edited=True,
            edits=_edits([OUTSIDER]),
            comments=[_comment("5", OUTSIDER, SPAM)],
        )
    )
    content = _read()
    assert content.body is None
    assert trust.outsider_opened(content) is None


def test_outsider_opened_finds_the_body_record_among_others() -> None:
    comment = Skipped(REPO, 7, "comment", "drive-by", CREATED, "comment 4", trust.UNTRUSTED)
    body = Skipped(REPO, 7, "issue", "drive-by", CREATED, "body", trust.UNTRUSTED)
    content = trust.IssueContent(
        body=None, body_role=None, author_role=None, comments=(), skipped=(comment, body)
    )
    assert trust.outsider_opened(content) == body


def test_outsider_opened_without_a_skip_record_is_a_read_error() -> None:
    # An edit record is not the author's: with no body record, nothing is guessed.
    edit = Skipped(REPO, 7, "edit", "drive-by", EDITED, "body", trust.UNTRUSTED)
    for skipped in ((), (edit,)):
        content = trust.IssueContent(
            body=None, body_role=None, author_role=None, comments=(), skipped=skipped
        )
        with pytest.raises(trust.TrustReadError, match="outsider_opened"):
            trust.outsider_opened(content)


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
        f"stopped on a read failure: {REPO}#9 on body\n"
    )


_READ_FAILURE = Skipped(REPO, 9, "issue", None, "", "comments", trust.READ_FAILURE)


def test_batch_summary_read_failure_detail_outside_actions() -> None:
    stops = BatchStops()
    stops.add(_READ_FAILURE, trust.READ_FAILURE, detail="cannot read who wrote the content (x)")
    stops.print_summary(stream := io.StringIO())
    assert stream.getvalue() == (
        f"stopped on a read failure: {REPO}#9 on comments: cannot read who wrote the content (x)\n"
    )


def test_batch_summary_read_failure_detail_not_printed_under_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    stops = BatchStops()
    stops.add(_READ_FAILURE, trust.READ_FAILURE, detail="the detail")
    stops.print_summary(stream := io.StringIO())
    assert stream.getvalue() == (
        f"stopped 1 item(s) (read-failure: 1)\nstopped on a read failure: {REPO}#9\n"
    )


def test_batch_stopped_lists_items_in_order() -> None:
    assert BatchStops().stopped == ()
    assert [item.number for item in _stops().stopped] == [7, 9]


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
    assert {Role.HUMAN, Role.AGENTS, Role.AUTOMATION} == ALL_ROLES


# --- events, branch lookups and reviews: fixtures -----------------------------------


class Answers:
    """A fake ``github.graphql`` answering each query from its own queue."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, query: str, **variables: object) -> dict[str, Any]:
        self.calls.append((query, variables))
        return self.answers[query].pop(0)


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(answers: dict[str, list[dict[str, Any]]]) -> Answers:
        fake = Answers(answers)
        monkeypatch.setattr(vergil_tooling.lib.github, "graphql", fake)
        return fake

    return install


def _page(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


# Timeline items as GraphQL returns them: ``__typename``, the actor (``null`` for
# a deleted account) and the time.
def _event(typename: str, actor: Any = HUMAN, at: str = CREATED) -> dict[str, Any]:
    return {"__typename": typename, "actor": actor, "createdAt": at}


def _timeline(
    events: list[Any], cursor: str | None = None, typename: str = "Issue"
) -> dict[str, Any]:
    return {
        "repository": {
            "issueOrPullRequest": {"__typename": typename, "timelineItems": _page(events, cursor)}
        }
    }


# A PR node as ``pullRequests(headRefName:)`` returns it. A fork's head
# repository is the fork's; GraphQL flags it with isCrossRepository.
def _pr(
    number: int,
    author: Any = AGENT,
    *,
    branch: str = "release/1.2.3",
    fork: bool = False,
    title: str = "release 1.2.3",
    state: str = "OPEN",
) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "title": title,
        "headRefName": branch,
        "headRefOid": f"{number:040x}",
        "state": state,
        "createdAt": CREATED,
        "author": author,
        "isCrossRepository": fork,
        "headRepositoryOwner": {"login": "mallory" if fork else ORG},
        "headRepository": {"name": "widgets"},
    }


def _prs(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"repository": {"pullRequests": _page(nodes, cursor)}}


def _review(
    db_id: str, author: Any = HUMAN, body: str = "a trusted review", **content: Any
) -> dict[str, Any]:
    review = _comment(db_id, author, body, **content)
    review["id"] = f"PRR_{db_id}"
    return review


def _inline(comments: list[Any], cursor: str | None = None) -> dict[str, Any]:
    """A page of one review's inline comments, read through the review's node."""
    return {"node": {"comments": _page(comments, cursor)}}


def _review_answers(*reviews: tuple[dict[str, Any], list[Any]]) -> dict[str, Any]:
    """One page of *reviews*, each with its one page of inline comments."""
    return {
        trust._REVIEWS_QUERY: [_reviews([review for review, _ in reviews])],
        trust._REVIEW_COMMENTS_QUERY: [_inline(comments) for _, comments in reviews],
    }


def _review_comment(db_id: str, author: Any = HUMAN, body: str = "a nit", **content: Any) -> Any:
    review_comment = _comment(db_id, author, body, **content)
    review_comment["id"] = f"PRRC_{db_id}"
    return review_comment


def _reviews(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"repository": {"pullRequest": {"reviews": _page(nodes, cursor)}}}


# --- read_events and close_judgement ---------------------------------------------------


def test_read_events_reads_every_kind_in_order(answer: Any) -> None:
    kinds = [
        ("ClosedEvent", "closed"),
        ("ReopenedEvent", "reopened"),
        ("LabeledEvent", "labeled"),
        ("UnlabeledEvent", "unlabeled"),
        ("MentionedEvent", "mentioned"),
        ("CrossReferencedEvent", "cross-referenced"),
        ("SubscribedEvent", "subscribed"),
    ]
    calls = answer(
        {trust._EVENTS_QUERY: [_timeline([_event(t, OUTSIDER) for t, _ in kinds])]}
    ).calls
    events = trust.read_events(REPO, 7)
    assert [e.kind for e in events] == [kind for _, kind in kinds]
    assert events[0] == trust.IssueEvent("closed", Identity("drive-by", "User"), CREATED)
    assert calls == [(trust._EVENTS_QUERY, {"owner": ORG, "name": "widgets", "number": 7})]
    assert "itemTypes: [CLOSED_EVENT, REOPENED_EVENT," in calls[0][0]


def test_read_events_deleted_actor(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", DELETED)])]})
    assert trust.read_events(REPO, 7)[0].actor == Identity(None, None)


def test_read_events_reads_pr(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent")], typename="PullRequest")]})
    assert [e.kind for e in trust.read_events(REPO, 7)] == ["closed"]


def test_read_events_pages(answer: Any) -> None:
    calls = answer(
        {
            trust._EVENTS_QUERY: [
                _timeline([_event("ClosedEvent", OUTSIDER)], cursor="t1"),
                _timeline([_event("ReopenedEvent")]),
            ]
        }
    ).calls
    assert [e.kind for e in trust.read_events(REPO, 7)] == ["closed", "reopened"]
    assert calls[1][1]["after"] == "t1"


def test_read_events_truncated_raises(answer: Any) -> None:
    page = _timeline([_event("ClosedEvent")])
    page["repository"]["issueOrPullRequest"]["timelineItems"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": None,
    }
    answer({trust._EVENTS_QUERY: [page]})
    with pytest.raises(TrustReadError, match="the timeline has a next page but no cursor"):
        trust.read_events(REPO, 7)


@pytest.mark.parametrize(
    ("event", "match"),
    [
        pytest.param(_event("IssueComment"), "not asked for", id="unasked-type"),
        pytest.param({"actor": HUMAN, "createdAt": CREATED}, "has no __typename", id="no-type"),
        pytest.param(
            {"__typename": "ClosedEvent", "createdAt": CREATED}, "no actor", id="no-actor"
        ),
        pytest.param(_event("ClosedEvent", {"login": "x"}), "has no __typename", id="untyped"),
        pytest.param(_event("ClosedEvent", at=""), "no usable createdAt", id="no-time"),
        pytest.param("x", "not an object", id="not-object"),
    ],
)
def test_read_events_malformed_raises(answer: Any, event: Any, match: str) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([event])]})
    with pytest.raises(TrustReadError, match=match) as info:
        trust.read_events(REPO, 7)
    assert info.value.call == f"read_events {REPO}#7"


def test_read_events_not_an_item_raises(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [{"repository": {"issueOrPullRequest": {"__typename": "X"}}}]})
    with pytest.raises(TrustReadError, match="neither an issue nor a pull request"):
        trust.read_events(REPO, 7)


def _close(**kwargs: Any) -> Judgement:
    return trust.close_judgement(REPO, 7, authors=AUTHORS, **kwargs)


def test_close_judgement_human(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("LabeledEvent"), _event("ClosedEvent")])]})
    assert _close() == Judgement(Role.HUMAN, None)


def test_close_judgement_outsider_has_no_role(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", OUTSIDER)])]})
    assert _close() == Judgement(None, trust.UNTRUSTED)


def test_close_judgement_deleted_account_has_no_role(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", DELETED)])]})
    assert _close() == Judgement(None, trust.DELETED)


def test_close_judgement_user_named_like_app_is_wrong_type(answer: Any) -> None:
    impostor = {"__typename": "User", "login": "the-agent"}
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", impostor)])]})
    assert _close() == Judgement(None, trust.WRONG_TYPE)


def test_close_judgement_latest_close_counts(answer: Any) -> None:
    answer(
        {
            trust._EVENTS_QUERY: [
                _timeline(
                    [
                        _event("ClosedEvent", HUMAN),
                        _event("ReopenedEvent", OUTSIDER),
                        _event("ClosedEvent", OUTSIDER),
                    ]
                ),
                _timeline(
                    [
                        _event("ClosedEvent", OUTSIDER),
                        _event("ReopenedEvent", HUMAN),
                        _event("ClosedEvent", AGENT),
                    ]
                ),
            ]
        }
    )
    assert _close() == Judgement(None, trust.UNTRUSTED)
    assert _close() == Judgement(Role.AGENTS, None)


def test_close_judgement_roles_parameter_narrows(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", AGENT)])]})
    assert _close(roles=frozenset({Role.HUMAN})) == Judgement(None, trust.NOT_ACCEPTED)


def test_close_judgement_no_close_raises(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("LabeledEvent")])]})
    with pytest.raises(TrustReadError, match="has no close event") as info:
        _close()
    assert info.value.call == f"close_judgement {REPO}#7"


def test_require_trusted_close_gives_the_role(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", AGENT)])]})
    assert trust.require_trusted_close(REPO, 7, authors=AUTHORS) == Role.AGENTS


@pytest.mark.parametrize(
    ("actor", "login", "cause"),
    [(OUTSIDER, "drive-by", trust.UNTRUSTED), (DELETED, None, trust.DELETED)],
)
def test_require_trusted_close_untrusted_is_a_stop(
    answer: Any, actor: Any, login: str | None, cause: str
) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", actor, at=EDITED)])]})
    with pytest.raises(UntrustedContentError) as info:
        trust.require_trusted_close(REPO, 7, authors=AUTHORS)
    assert info.value.item == trust.Skipped(REPO, 7, "event", login, EDITED, "close", cause)
    assert str(info.value).startswith("stopped before any write: the close is untrusted:")


def test_require_trusted_close_roles_parameter_narrows(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent", AGENT)])]})
    with pytest.raises(UntrustedContentError) as info:
        trust.require_trusted_close(REPO, 7, roles=frozenset({Role.HUMAN}), authors=AUTHORS)
    assert info.value.item.cause == trust.NOT_ACCEPTED


def test_require_trusted_close_no_close_raises(answer: Any) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("LabeledEvent")])]})
    with pytest.raises(TrustReadError, match="has no close event") as info:
        trust.require_trusted_close(REPO, 7, authors=AUTHORS)
    assert info.value.call == f"require_trusted_close {REPO}#7"


def test_close_judgement_loads_configured_authors(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer({trust._EVENTS_QUERY: [_timeline([_event("ClosedEvent")])]})
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    assert trust.close_judgement(REPO, 7).role is Role.HUMAN


# --- is_same_repo and prs_for_branch --------------------------------------------------


def test_is_same_repo_checks_head_repository() -> None:
    assert trust.is_same_repo(REPO, _pr(1))
    assert trust.is_same_repo(REPO.upper(), _pr(1))
    assert not trust.is_same_repo(REPO, _pr(2, fork=True))
    # A fork whose head repository is gone reads as cross-repository, no head.
    assert not trust.is_same_repo(REPO, {**_pr(3, fork=True), "headRepository": None})
    contradicting = {**_pr(4), "headRepositoryOwner": {"login": "mallory"}}
    with pytest.raises(TrustReadError, match="PR 4 is not cross-repository, but its head is"):
        trust.is_same_repo(REPO, contradicting)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"isCrossRepository": None}, "PR 1 has no isCrossRepository flag"),
        ({"headRepository": None}, "PR 1 has no head repository"),
        ({"headRepositoryOwner": {}}, "PR 1 has no head repository"),
        ({"number": True, "headRepository": None}, "a PR has no head repository"),
    ],
)
def test_is_same_repo_malformed_raises(change: dict[str, Any], match: str) -> None:
    with pytest.raises(TrustReadError, match=match) as info:
        trust.is_same_repo(REPO, {**_pr(1), **change})
    assert info.value.call == f"is_same_repo {REPO}"


def _branch(**kwargs: Any) -> tuple[list[trust.PRRef], list[Skipped]]:
    kwargs.setdefault("state", "open")
    return trust.prs_for_branch(REPO, "release/1.2.3", authors=AUTHORS, **kwargs)


def test_prs_for_branch_returns_trusted_same_repo_pr(answer: Any) -> None:
    query = trust._BRANCH_QUERIES["open"]
    calls = answer({query: [_prs([_pr(5)])]}).calls
    found, skipped = _branch()
    assert found == [
        trust.PRRef(
            number=5,
            url=f"https://github.com/{REPO}/pull/5",
            title="release 1.2.3",
            head="release/1.2.3",
            head_oid=f"{5:040x}",
            author_role=Role.AGENTS,
            state="OPEN",
        )
    ]
    assert skipped == []
    assert calls == [(query, {"owner": ORG, "name": "widgets", "branch": "release/1.2.3"})]


def test_prs_for_branch_skips_fork_and_reports(answer: Any) -> None:
    fork = _pr(6, {"__typename": "User", "login": SPAM_LOGIN}, fork=True, title=SPAM)
    answer({trust._BRANCH_QUERIES["open"]: [_prs([fork, _pr(5)])]})
    found, skipped = _branch()
    assert [pr.number for pr in found] == [5]
    assert skipped == [Skipped(REPO, 6, "pr", SPAM_LOGIN, CREATED, "head", trust.CROSS_REPOSITORY)]
    report_skipped(skipped, stream=(stream := io.StringIO()))
    out = stream.getvalue()
    assert out == (
        f"skipped untrusted content: {REPO}#6 pr {SPAM_LOGIN} at {CREATED} on head"
        " (cross-repository)\n"
    )
    assert "SPAM-MARKER-TEXT" not in out


def test_prs_for_branch_only_fork_returns_empty_and_reports(answer: Any) -> None:
    # A fork PR whose author is even a trusted login never matches a branch.
    answer({trust._BRANCH_QUERIES["open"]: [_prs([_pr(6, HUMAN, fork=True, title=SPAM)])]})
    found, skipped = _branch()
    assert found == []
    assert [(s.number, s.cause) for s in skipped] == [(6, trust.CROSS_REPOSITORY)]


def test_prs_for_branch_untrusted_author_skipped(answer: Any) -> None:
    impostor = {"__typename": "User", "login": "the-agent"}
    answer(
        {
            trust._BRANCH_QUERIES["open"]: [
                _prs([_pr(5, OUTSIDER, title=SPAM), _pr(6, impostor), _pr(7, DELETED), _pr(8)])
            ]
        }
    )
    found, skipped = _branch()
    assert [pr.number for pr in found] == [8]
    assert [(s.number, s.kind, s.login, s.field, s.cause) for s in skipped] == [
        (5, "pr", "drive-by", "author", trust.UNTRUSTED),
        (6, "pr", "the-agent", "author", trust.WRONG_TYPE),
        (7, "pr", None, "author", trust.DELETED),
    ]


def test_prs_for_branch_roles_parameter_narrows(answer: Any) -> None:
    answer({trust._BRANCH_QUERIES["open"]: [_prs([_pr(5, AGENT), _pr(6, HUMAN)])]})
    found, skipped = _branch(roles=frozenset({Role.HUMAN}))
    assert [(pr.number, pr.author_role) for pr in found] == [(6, Role.HUMAN)]
    assert [(s.number, s.cause) for s in skipped] == [(5, trust.NOT_ACCEPTED)]


@pytest.mark.parametrize(
    ("state", "states"),
    [("open", "states: [OPEN], "), ("merged", "states: [MERGED], "), ("all", "")],
)
def test_prs_for_branch_state_filter(answer: Any, state: str, states: str) -> None:
    query = trust._BRANCH_QUERIES[state]
    answer({query: [_prs([_pr(5, state="MERGED")])]})
    found, _ = _branch(state=state)
    assert found[0].state == "MERGED"
    assert f"pullRequests(headRefName: $branch, {states}first: 100" in query


def test_prs_for_branch_pages(answer: Any) -> None:
    query = trust._BRANCH_QUERIES["all"]
    calls = answer({query: [_prs([_pr(5)], cursor="p1"), _prs([_pr(6, fork=True)])]}).calls
    found, skipped = _branch(state="all")
    assert [pr.number for pr in found] == [5]
    assert [s.number for s in skipped] == [6]
    assert calls[1][1]["after"] == "p1"


def test_prs_for_branch_loads_configured_authors(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer({trust._BRANCH_QUERIES["open"]: [_prs([_pr(5)])]})
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    assert trust.prs_for_branch(REPO, "release/1.2.3", state="open")[0][0].number == 5


@pytest.mark.parametrize(
    ("page", "match"),
    [
        pytest.param({"repository": None}, "the repository is missing", id="no-repository"),
        pytest.param({"repository": {}}, "has no pullRequests", id="no-connection"),
        pytest.param(_prs([{**_pr(5), "number": True}]), "no usable number", id="number-bool"),
        pytest.param(_prs([_pr(5, branch="other")]), "not on the branch", id="other-branch"),
        pytest.param(
            _prs([{**_pr(5), "isCrossRepository": None}]),
            "PR 5 has no isCrossRepository flag",
            id="no-flag",
        ),
        pytest.param(
            _prs([{**_pr(5), "headRepositoryOwner": {"login": "mallory"}}]),
            "PR 5 is not cross-repository",
            id="contradicting-head",
        ),
        pytest.param(_prs([{**_pr(5), "headRefOid": ""}]), "no usable headRefOid", id="no-oid"),
        pytest.param(_prs(["x"]), "not an object", id="not-object"),
    ],
)
def test_prs_for_branch_malformed_raises(answer: Any, page: Any, match: str) -> None:
    answer({trust._BRANCH_QUERIES["open"]: [page]})
    with pytest.raises(TrustReadError, match=match) as info:
        _branch()
    assert info.value.call == f"prs_for_branch {REPO} release/1.2.3"


# --- read_reviews -------------------------------------------------------------------


def _reviews_of(**kwargs: Any) -> trust.PRReviewContent:
    return trust.read_reviews(REPO, 7, authors=AUTHORS, **kwargs)


def test_read_reviews_trusted(answer: Any) -> None:
    calls = answer(
        _review_answers(
            (_review("1", HUMAN), [_review_comment("11", HUMAN)]),
            (_review("2", AGENT, body=""), []),
        )
    ).calls
    content = _reviews_of()
    assert [(r.id, r.body, r.role) for r in content.reviews] == [
        ("1", "a trusted review", Role.HUMAN),
        ("2", "", Role.AGENTS),
    ]
    assert [(c.id, c.role, c.at) for c in content.review_comments] == [("11", Role.HUMAN, CREATED)]
    assert content.skipped == ()
    assert calls == [
        (trust._REVIEWS_QUERY, {"owner": ORG, "name": "widgets", "number": 7}),
        (trust._REVIEW_COMMENTS_QUERY, {"id": "PRR_1"}),
        (trust._REVIEW_COMMENTS_QUERY, {"id": "PRR_2"}),
    ]


def test_read_reviews_skips_outsider_review(answer: Any) -> None:
    spam = {"__typename": "User", "login": SPAM_LOGIN}
    answer(
        _review_answers(
            (_review("1", spam, SPAM), [_review_comment("11", spam, SPAM)]),
            (_review("2", HUMAN), [_review_comment("21", DELETED, SPAM)]),
        )
    )
    content = _reviews_of()
    assert [r.id for r in content.reviews] == ["2"]
    assert content.review_comments == ()
    assert [(s.kind, s.login, s.field, s.cause) for s in content.skipped] == [
        ("review", SPAM_LOGIN, "review 1", trust.UNTRUSTED),
        ("review-comment", SPAM_LOGIN, "review comment 11", trust.UNTRUSTED),
        ("review-comment", None, "review comment 21", trust.DELETED),
    ]
    report_skipped(content.skipped, stream=(stream := io.StringIO()))
    out = stream.getvalue()
    assert f"{REPO}#7 review {SPAM_LOGIN} at {CREATED} on review 1 (untrusted)" in out
    assert "SPAM-MARKER-TEXT" not in out
    assert "example.invalid" not in out


def test_read_reviews_skips_edited_by_outsider(answer: Any) -> None:
    edited: dict[str, Any] = {"editor": OUTSIDER, "edited": True, "edits": _edits([OUTSIDER])}
    earlier: dict[str, Any] = {
        "editor": HUMAN,
        "edited": True,
        "edits": _edits([HUMAN, OUTSIDER]),
    }

    def install() -> None:
        answer(
            _review_answers(
                (_review("1", **edited), [_review_comment("11", **edited)]),
                (_review("2", **earlier), []),
            )
        )

    install()
    content = _reviews_of()
    assert content.reviews == ()
    assert content.review_comments == ()
    assert [(s.kind, s.login, s.field) for s in content.skipped] == [
        ("edit", "drive-by", "review 1"),
        ("edit", "drive-by", "review 2"),
        ("edit", "drive-by", "review comment 11"),
    ]
    # Under the last-editor rule, only review 2 (last edited by the human) passes.
    install()
    assert [r.id for r in _reviews_of(editors="last").reviews] == ["2"]


def test_read_reviews_roles_parameter_narrows(answer: Any) -> None:
    answer(_review_answers((_review("1", AGENT), []), (_review("2", HUMAN), [])))
    content = _reviews_of(roles=frozenset({Role.HUMAN}))
    assert [r.id for r in content.reviews] == ["2"]
    assert [s.cause for s in content.skipped] == [trust.NOT_ACCEPTED]


def test_read_reviews_pages_reviews_comments_and_edits(answer: Any) -> None:
    review = _review("1", edits=_edits([HUMAN], cursor="re1"))
    first = _review_comment("11", edits=_edits([HUMAN], cursor="ce1"))
    calls = answer(
        {
            trust._REVIEWS_QUERY: [_reviews([review], cursor="r1"), _reviews([_review("2")])],
            trust._REVIEW_COMMENTS_QUERY: [
                _inline([first], cursor="rc1"),
                _inline([_review_comment("12", OUTSIDER)]),
                _inline([]),
            ],
            trust._REVIEW_EDITS_QUERY: [{"node": {"userContentEdits": _edits([OUTSIDER])}}],
            trust._REVIEW_COMMENT_EDITS_QUERY: [{"node": {"userContentEdits": _edits([OUTSIDER])}}],
        }
    ).calls
    content = _reviews_of()
    assert [r.id for r in content.reviews] == ["2"]
    assert content.review_comments == ()
    assert [(s.kind, s.field) for s in content.skipped] == [
        ("edit", "review 1"),
        ("edit", "review comment 11"),
        ("review-comment", "review comment 12"),
    ]
    assert calls[1:] == [
        (trust._REVIEWS_QUERY, {"owner": ORG, "name": "widgets", "number": 7, "after": "r1"}),
        (trust._REVIEW_COMMENTS_QUERY, {"id": "PRR_1"}),
        (trust._REVIEW_COMMENTS_QUERY, {"id": "PRR_1", "after": "rc1"}),
        (trust._REVIEW_EDITS_QUERY, {"id": "PRR_1", "after": "re1"}),
        (trust._REVIEW_COMMENTS_QUERY, {"id": "PRR_2"}),
        (trust._REVIEW_COMMENT_EDITS_QUERY, {"id": "PRRC_11", "after": "ce1"}),
    ]


def test_read_reviews_loads_configured_authors(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer(_review_answers((_review("1"), [])))
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    assert [r.id for r in trust.read_reviews(REPO, 7).reviews] == ["1"]


@pytest.mark.parametrize(
    ("answers", "match"),
    [
        pytest.param(
            {trust._REVIEWS_QUERY: [{"repository": {}}]}, "the pull request is missing", id="no-pr"
        ),
        pytest.param(
            {trust._REVIEWS_QUERY: [{"repository": {"pullRequest": {}}}]},
            "has no reviews",
            id="no-reviews",
        ),
        pytest.param(
            _review_answers(({**_review("1"), "author": {"login": "x"}}, [])),
            "has no __typename",
            id="untyped",
        ),
        pytest.param(
            {trust._REVIEWS_QUERY: [_reviews([_review("1")])], trust._REVIEW_COMMENTS_QUERY: [{}]},
            "review 1 is missing",
            id="no-node",
        ),
        pytest.param(
            {
                trust._REVIEWS_QUERY: [_reviews([_review("1")])],
                trust._REVIEW_COMMENTS_QUERY: [{"node": {}}],
            },
            "review 1 has no comments",
            id="no-comments",
        ),
        pytest.param(
            _review_answers((_review("1"), [{"fullDatabaseId": "11"}])),
            "review comment 11 has no",
            id="comment-incomplete",
        ),
    ],
)
def test_read_reviews_malformed_raises(answer: Any, answers: Any, match: str) -> None:
    answer(answers)
    with pytest.raises(TrustReadError, match=match) as info:
        _reviews_of()
    assert info.value.call == f"read_reviews {REPO}#7"
    assert "SPAM-MARKER-TEXT" not in str(info.value)


# --- query cost ---------------------------------------------------------------------

# GitHub refuses a query whose worst case exceeds this many nodes: the product
# of the ``first:`` arguments down each nesting, summed over every connection.
_NODE_LIMIT = 500_000


def _worst_case_nodes(query: str) -> int:
    total = 0
    stack = [1]
    pending: int | None = None
    for token in re.findall(r"first: (\d+)|([{}])", query):
        first, brace = token
        if first:
            pending = int(first)
        elif brace == "{":
            if pending is not None:
                total += stack[-1] * pending
                stack.append(stack[-1] * pending)
                pending = None
            else:
                stack.append(stack[-1])
        else:
            stack.pop()
    return total


def test_worst_case_nodes_counts_nesting() -> None:
    query = "{ a(first: 10) { nodes { b(first: 5) { x } } } c(first: 2) { y } }"
    assert _worst_case_nodes(query) == 10 + 50 + 2


@pytest.mark.parametrize(
    "query",
    [
        trust._ITEM_QUERY,
        trust._BODY_EDITS_QUERY,
        trust._COMMENTS_QUERY,
        trust._COMMENT_EDITS_QUERY,
        trust._EVENTS_QUERY,
        *trust._BRANCH_QUERIES.values(),
        trust._REVIEWS_QUERY,
        trust._REVIEW_EDITS_QUERY,
        trust._REVIEW_COMMENTS_QUERY,
        trust._REVIEW_COMMENT_EDITS_QUERY,
        *trust._SEARCH_QUERIES.values(),
        *trust._LIST_QUERIES.values(),
        trust._RENAMES_QUERY,
    ],
)
def test_every_query_is_under_githubs_node_limit(query: str) -> None:
    assert _worst_case_nodes(query) <= _NODE_LIMIT


# --- search_items and list_items -------------------------------------------------


def _hit(
    number: int,
    author: Any = HUMAN,
    *,
    typename: str = "Issue",
    title: str = "Fixture run: a change",
    body: str = "the trusted body",
    editor: Any = None,
    edited: bool = False,
    edits: dict[str, Any] | None = None,
    renames: dict[str, Any] | None = None,
    labels: dict[str, Any] | None = None,
    state: str = "OPEN",
) -> dict[str, Any]:
    """A search or list result as GraphQL returns it."""
    path = "issues" if typename == "Issue" else "pull"
    return {
        "__typename": typename,
        "number": number,
        "title": title,
        "url": f"https://github.com/{REPO}/{path}/{number}",
        "state": state,
        "repository": {"nameWithOwner": REPO},
        "labels": labels if labels is not None else _page([{"name": "deployment"}]),
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": CREATED,
        "lastEditedAt": EDITED if edited else None,
        "userContentEdits": edits if edits is not None else _edits(),
        "timelineItems": renames if renames is not None else _page([]),
    }


def _rename(actor: Any, at: str = EDITED) -> dict[str, Any]:
    return {"__typename": "RenamedTitleEvent", "actor": actor, "createdAt": at}


def _search(
    nodes: list[Any], cursor: str | None = None, count: int | None = None
) -> dict[str, Any]:
    page = _page(nodes, cursor)
    return {"search": {"issueCount": len(nodes) if count is None else count, **page}}


def _listing(nodes: list[Any], cursor: str | None = None, key: str = "issues") -> dict[str, Any]:
    return {"repository": {key: _page(nodes, cursor)}}


def _search_issues(**kwargs: Any) -> tuple[list[trust.TrustedHit], list[Skipped]]:
    return trust.search_items(f"repo:{REPO} is:open", kind="issue", authors=AUTHORS, **kwargs)


def test_search_items_keeps_trusted_results(answer: Any) -> None:
    fake = answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1), _hit(2, AGENT)])]})
    hits, skipped = _search_issues()
    assert skipped == []
    assert [(h.number, h.body_role, h.kind) for h in hits] == [
        (1, Role.HUMAN, "issue"),
        (2, Role.AGENTS, "issue"),
    ]
    assert hits[0].labels == ("deployment",)
    assert hits[0].title == "Fixture run: a change"
    assert hits[0].body == "the trusted body"
    assert fake.calls[0][1]["q"] == f"repo:{REPO} is:open is:issue"


def test_search_items_skips_outsider_result_and_reports(answer: Any) -> None:
    spam = _hit(3, {"__typename": "User", "login": SPAM_LOGIN}, title="Fixture run", body=SPAM)
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1), spam])]})
    hits, skipped = _search_issues()
    assert [h.number for h in hits] == [1]
    assert skipped == [Skipped(REPO, 3, "issue", SPAM_LOGIN, CREATED, "body", trust.UNTRUSTED)]
    stream = io.StringIO()
    report_skipped(skipped, stream=stream)
    assert "SPAM-MARKER-TEXT" not in stream.getvalue()
    assert f"{REPO}#3 issue {SPAM_LOGIN}" in stream.getvalue()


def test_search_items_skips_body_edited_by_outsider(answer: Any) -> None:
    edited = _hit(4, edits=_edits([HUMAN, OUTSIDER]), edited=True, editor=HUMAN)
    answer({trust._SEARCH_QUERIES["issue"]: [_search([edited])]})
    hits, skipped = _search_issues()
    assert hits == []
    assert skipped == [Skipped(REPO, 4, "edit", "drive-by", EDITED, "body", trust.UNTRUSTED)]


def test_search_items_skips_title_renamed_by_outsider(answer: Any) -> None:
    renamed = _hit(5, renames=_page([_rename(HUMAN), _rename(OUTSIDER)]))
    answer({trust._SEARCH_QUERIES["issue"]: [_search([renamed])]})
    hits, skipped = _search_issues()
    assert hits == []
    assert skipped == [Skipped(REPO, 5, "event", "drive-by", EDITED, "title", trust.UNTRUSTED)]


def test_search_items_title_renamed_by_deleted_account_is_skipped(answer: Any) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(5, renames=_page([_rename(None)]))])]})
    _, skipped = _search_issues()
    assert [(s.kind, s.login, s.field, s.cause) for s in skipped] == [
        ("event", None, "title", trust.DELETED)
    ]


def test_search_items_last_editor_rule_checks_last_rename(answer: Any) -> None:
    renamed = _hit(5, renames=_page([_rename(OUTSIDER), _rename(HUMAN)]))
    answer({trust._SEARCH_QUERIES["issue"]: [_search([renamed])]})
    hits, skipped = _search_issues(editors="last")
    assert [h.number for h in hits] == [5]
    assert skipped == []


def test_search_items_roles_parameter_narrows(answer: Any) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1), _hit(2, AGENT)])]})
    hits, skipped = _search_issues(roles=frozenset({Role.HUMAN}))
    assert [h.number for h in hits] == [1]
    assert [(s.number, s.cause) for s in skipped] == [(2, trust.NOT_ACCEPTED)]


def test_search_items_roles_parameter_applies_to_renames(answer: Any) -> None:
    renamed = _hit(5, renames=_page([_rename(AGENT)]))
    answer({trust._SEARCH_QUERIES["issue"]: [_search([renamed])]})
    hits, skipped = _search_issues(roles=frozenset({Role.HUMAN}))
    assert hits == []
    assert [(s.kind, s.field, s.cause) for s in skipped] == [("event", "title", trust.NOT_ACCEPTED)]


def test_search_items_user_named_like_app_is_wrong_type(answer: Any) -> None:
    fake_agent = {"__typename": "User", "login": "the-agent"}
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1, fake_agent)])]})
    _, skipped = _search_issues()
    assert [s.cause for s in skipped] == [trust.WRONG_TYPE]


def test_search_items_pages_results_edits_and_renames(answer: Any) -> None:
    first = _hit(1, edits=_edits([HUMAN], cursor="E1"), renames=_page([_rename(HUMAN)], "R1"))
    fake = answer(
        {
            trust._SEARCH_QUERIES["issue"]: [_search([first], "S1", count=2), _search([_hit(2)])],
            trust._BODY_EDITS_QUERY: [
                {"repository": {"issueOrPullRequest": {"userContentEdits": _edits([OUTSIDER])}}}
            ],
            trust._RENAMES_QUERY: [
                {"repository": {"issueOrPullRequest": {"timelineItems": _page([_rename(HUMAN)])}}}
            ],
        }
    )
    hits, skipped = _search_issues()
    assert [h.number for h in hits] == [2]
    assert [(s.number, s.kind, s.login) for s in skipped] == [(1, "edit", "drive-by")]
    assert fake.calls[1][1]["after"] == "S1"
    variables = [v for _, v in fake.calls]
    assert {"owner": ORG, "name": "widgets", "number": 1, "after": "E1"} in variables
    assert {"owner": ORG, "name": "widgets", "number": 1, "after": "R1"} in variables


def test_search_items_over_the_result_cap_raises(answer: Any) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1)], "S1", count=1001)]})
    with pytest.raises(TrustReadError, match="1001 items, more than the 1000"):
        _search_issues()


def test_search_items_reads_prs(answer: Any) -> None:
    pr = _hit(9, AGENT, typename="PullRequest", state="MERGED")
    fake = answer({trust._SEARCH_QUERIES["pr"]: [_search([pr])]})
    hits, _ = trust.search_items(f"repo:{REPO} 12", kind="pr", authors=AUTHORS)
    assert [(h.number, h.kind, h.state) for h in hits] == [(9, "pr", "MERGED")]
    assert fake.calls[0][1]["q"] == f"repo:{REPO} 12 is:pr"
    assert "... on PullRequest {" in trust._SEARCH_QUERIES["pr"]
    assert "... on Issue" not in trust._SEARCH_QUERIES["pr"]


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"__typename": "PullRequest"}, "not of the kind asked for"),
        ({"number": "1"}, "no usable number"),
        ({"labels": _page([], "L1")}, "labels of acme/widgets#1 are not read in full"),
        ({"timelineItems": _page([{"__typename": "LabeledEvent"}])}, "not asked for"),
        ({"author": {"login": "the-human"}}, "has no __typename"),
        ({"userContentEdits": {"nodes": []}}, "has no pageInfo"),
        ({"repository": None}, "repo is missing"),
    ],
)
def test_search_items_malformed_result_raises(
    answer: Any, change: dict[str, Any], match: str
) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [_search([{**_hit(1), **change}])]})
    with pytest.raises(TrustReadError, match=match):
        _search_issues()


@pytest.mark.parametrize(
    ("page", "match"),
    [
        ({}, "the search is missing"),
        ({"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}, "has no issueCount"),
        ({"search": {"issueCount": True, "nodes": []}}, "no usable issueCount"),
        ({"search": {"issueCount": 0, "nodes": []}}, "has no pageInfo"),
    ],
)
def test_search_items_malformed_page_raises(answer: Any, page: Any, match: str) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [page]})
    with pytest.raises(TrustReadError, match=match):
        _search_issues()


def test_search_items_api_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(query: str, **variables: object) -> dict[str, Any]:
        raise _api_error("gh: Something went wrong\nHTTP 502")

    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", fail)
    with pytest.raises(TrustReadError, match="search_items issue") as info:
        _search_issues()
    assert "SPAM-MARKER-TEXT" not in str(info.value)


def test_search_items_loads_configured_authors(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer({trust._SEARCH_QUERIES["issue"]: [_search([_hit(1)])]})
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    hits, _ = trust.search_items("x", kind="issue")
    assert [h.number for h in hits] == [1]


def test_list_items_reads_the_repository(answer: Any) -> None:
    query = trust._LIST_QUERIES["pr", "open"]
    pr = _hit(3, AGENT, typename="PullRequest", title="Logbook: 2026-11-22")
    outsider = _hit(4, OUTSIDER, typename="PullRequest", title="Logbook: spam")
    fake = answer(
        {
            query: [
                _listing([pr], "P1", key="pullRequests"),
                _listing([outsider], key="pullRequests"),
            ]
        }
    )
    hits, skipped = trust.list_items(REPO, kind="pr", state="open", authors=AUTHORS)
    assert [(h.number, h.title) for h in hits] == [(3, "Logbook: 2026-11-22")]
    assert [(s.number, s.kind, s.cause) for s in skipped] == [(4, "pr", trust.UNTRUSTED)]
    assert fake.calls[0][1] == {"owner": ORG, "name": "widgets"}
    assert fake.calls[1][1] == {"owner": ORG, "name": "widgets", "after": "P1"}


@pytest.mark.parametrize(
    ("kind", "state", "selection"),
    [
        ("issue", "open", "issues(states: [OPEN]"),
        ("issue", "closed", "issues(states: [CLOSED]"),
        ("issue", "all", "issues(orderBy"),
        ("pr", "open", "pullRequests(states: [OPEN]"),
        ("pr", "closed", "pullRequests(states: [CLOSED, MERGED]"),
        ("pr", "merged", "pullRequests(states: [MERGED]"),
        ("pr", "all", "pullRequests(orderBy"),
    ],
)
def test_list_items_state_filter(kind: str, state: str, selection: str) -> None:
    assert selection in trust._LIST_QUERIES[kind, state]


def test_list_items_issue_has_no_merged_state() -> None:
    with pytest.raises(ValueError, match="no 'merged' state"):
        trust.list_items(REPO, kind="issue", state="merged", authors=AUTHORS)


def test_list_items_missing_connection_raises(answer: Any) -> None:
    answer({trust._LIST_QUERIES["issue", "open"]: [{"repository": {}}]})
    with pytest.raises(TrustReadError, match="has no issues"):
        trust.list_items(REPO, kind="issue", state="open", authors=AUTHORS)


def test_list_items_loads_configured_authors(answer: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    answer({trust._LIST_QUERIES["issue", "open"]: [_listing([_hit(1)])]})
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    hits, _ = trust.list_items(REPO, kind="issue", state="open")
    assert [h.number for h in hits] == [1]
