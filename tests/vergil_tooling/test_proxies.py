"""Tests for vergil_tooling.lib.proxies: the proxy epic's link to its primary."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.lib import epics, proxies, trust
from vergil_tooling.lib.epics import EpicListTruncatedError, IssueRef
from vergil_tooling.lib.proxies import ProxyLinkError
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

PROXY = IssueRef("logical-minds-foundry", ".github", 320)
PRIMARY = IssueRef("vergil-project", ".github", 1417)
PRIMARY_KEY = ("vergil-project/.github", 1417)


@pytest.fixture(autouse=True)
def _no_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The skip reports these tests check are the per-item lines, not Actions counts.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _comment(actor: Any, body: str, *, editor: Any = None) -> dict[str, Any]:
    """An ``IssueComment`` node by *actor* (a GraphQL actor, ``None`` a deleted account)."""
    return trust_stubs.comment(1, body, actor, editor=editor)


def _numbered(*nodes: dict[str, Any]) -> list[dict[str, Any]]:
    """*nodes* renumbered from 1, as GitHub gives each comment its own id."""
    return [
        {**node, "id": f"IC_{i}", "fullDatabaseId": str(i)} for i, node in enumerate(nodes, start=1)
    ]


def _views(views: dict[IssueRef, dict[str, Any]]) -> Any:
    """A ``github.read_json`` fake answering ``gh issue view`` from *views*."""

    def fake(*args: str) -> object:
        assert args[:2] == ("issue", "view")
        ref = IssueRef(*args[4].split("/", 1), int(args[2]))
        return views[ref]

    return fake


def _primary_of(
    body: str,
    primary_labels: tuple[str, ...] = ("epic",),
    *,
    author: Any = trust_stubs.HUMAN,
    editor: Any = None,
) -> IssueRef:
    """:func:`proxies.primary_of` of PROXY, its body read through the real trust layer."""
    views = {PRIMARY: {"labels": [{"name": name} for name in primary_labels]}}
    proxy_item = trust_stubs.item(body=body, author=author, editor=editor)
    items = trust_stubs.FakeItems({("logical-minds-foundry/.github", 320): proxy_item}, strict=True)
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_views(views)),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        return proxies.primary_of(PROXY)


def _proxies_of(comments: list[dict[str, Any]]) -> tuple[IssueRef, ...]:
    """:func:`proxies.proxies_of` of PRIMARY, its comments read through the real trust layer."""
    items = trust_stubs.FakeItems(
        {PRIMARY_KEY: trust_stubs.item(nodes=_numbered(*comments))}, strict=True
    )
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=AssertionError("no gh read")),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        return proxies.proxies_of(PRIMARY)


# --- the lines ------------------------------------------------------------------


def test_primary_line_and_proxy_line() -> None:
    assert proxies.primary_line(PRIMARY) == "Primary: vergil-project/.github#1417"
    assert proxies.proxy_line(PROXY) == "Proxy: logical-minds-foundry/.github#320"


# --- primary_of -----------------------------------------------------------------


def test_primary_of_reads_line() -> None:
    body = (
        "Primary: vergil-project/.github#1417\n"
        "Holds this org's tasks of the primary epic; the work is described there.\n"
    )
    assert _primary_of(body) == PRIMARY


def test_primary_of_reads_indented_line() -> None:
    assert _primary_of("intro\n  Primary: vergil-project/.github#1417  \n") == PRIMARY


def test_primary_of_missing_line() -> None:
    with pytest.raises(ProxyLinkError, match="no 'Primary:' line"):
        _primary_of("Holds this org's tasks.\n")


def test_primary_of_two_lines() -> None:
    body = "Primary: vergil-project/.github#1417\nPrimary: vergil-project/.github#1418\n"
    with pytest.raises(ProxyLinkError, match="2 'Primary:' lines"):
        _primary_of(body)


def test_primary_of_two_identical_lines() -> None:
    body = "Primary: vergil-project/.github#1417\nPrimary: vergil-project/.github#1417\n"
    with pytest.raises(ProxyLinkError, match="exactly one"):
        _primary_of(body)


@pytest.mark.parametrize(
    "line",
    [
        "Primary: #1417",
        "Primary: vergil-project/.github",
        "Primary: vergil-project/.github#1417 (the epic)",
        "Primary:vergil-project/.github#1417",
    ],
)
def test_primary_of_malformed_line(line: str) -> None:
    with pytest.raises(ProxyLinkError, match="malformed"):
        _primary_of(f"{line}\n")


def test_primary_of_non_epic() -> None:
    with pytest.raises(ProxyLinkError, match="not an epic"):
        _primary_of("Primary: vergil-project/.github#1417\n", primary_labels=("bug",))


def test_primary_of_unreadable_proxy_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=RuntimeError("gh failed")),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(trust.TrustReadError, match="read_issue logical-minds-foundry/.github#320"),
    ):
        proxies.primary_of(PROXY)


def test_primary_of_malformed_proxy_read_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": None}),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(trust.TrustReadError),
    ):
        proxies.primary_of(PROXY)


_SPAM_PRIMARY = f"Primary: vergil-project/.github#1417\n{trust_stubs.SPAM_TEXT}\n"


@pytest.mark.parametrize(
    ("author", "editor", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "untrusted"),
        (trust_stubs.IMPOSTOR, None, "wrong-type"),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, "untrusted"),
    ],
    ids=["outsider-body", "user-named-like-app", "edited-by-outsider"],
)
def test_primary_of_untrusted_body_is_a_stop(author: Any, editor: Any, cause: str) -> None:
    # The Primary: line counts only from a trusted body: an untrusted one is a
    # stop, metadata only, never a link.
    with pytest.raises(trust.UntrustedContentError) as excinfo:
        _primary_of(_SPAM_PRIMARY, author=author, editor=editor)
    assert excinfo.value.item.field == "body"
    assert excinfo.value.item.cause == cause
    assert "SPAM-MARKER-TEXT" not in str(excinfo.value)


# --- proxies_of -----------------------------------------------------------------


def test_proxies_of_union_of_trusted_comments() -> None:
    comments = [
        _comment(trust_stubs.AGENT, "Proxy: wphillipmoore/.github#9"),
        _comment(trust_stubs.HUMAN, "Created.\n\nProxy: logical-minds-foundry/.github#320\n"),
        _comment(trust_stubs.AGENT, "Proxy: wphillipmoore/.github#9"),
    ]
    assert _proxies_of(comments) == (PROXY, IssueRef("wphillipmoore", ".github", 9))


def test_proxies_of_none() -> None:
    assert _proxies_of([_comment(trust_stubs.HUMAN, "LGTM")]) == ()


def test_proxies_of_ignores_untrusted_comment(capsys: pytest.CaptureFixture[str]) -> None:
    comments = [
        _comment(trust_stubs.OUTSIDER, "Proxy: mallory-org/.github#1"),
        _comment(None, "Proxy: ghost-org/.github#2"),
        _comment(trust_stubs.HUMAN, "Proxy: logical-minds-foundry/.github#320"),
    ]
    assert _proxies_of(comments) == (PROXY,)
    out, err = capsys.readouterr()
    at = trust_stubs.CREATED
    assert err == (
        f"skipped untrusted content: vergil-project/.github#1417 comment drive-by at {at}"
        " on comment 1 (untrusted)\n"
        "skipped untrusted content: vergil-project/.github#1417 comment a deleted account"
        f" at {at} on comment 2 (deleted)\n"
    )
    assert "mallory-org" not in out + err
    assert "ghost-org" not in out + err


def test_proxies_of_ignores_untrusted_malformed_line(capsys: pytest.CaptureFixture[str]) -> None:
    # A forged malformed line must not be able to break the primary's readers.
    comments = [_comment(trust_stubs.OUTSIDER, f"Proxy: garbage\n{trust_stubs.SPAM_TEXT}")]
    assert _proxies_of(comments) == ()
    out, err = capsys.readouterr()
    assert "on comment 1 (untrusted)" in err
    assert "garbage" not in out + err
    assert "SPAM-MARKER-TEXT" not in out + err


@pytest.mark.parametrize("login", ["vergil-user", "app/vergil-user", "vergil-user[bot]"])
def test_proxies_of_strips_app_prefix(login: str) -> None:
    agent = {"__typename": "Bot", "login": login}
    comments = [_comment(agent, "Proxy: logical-minds-foundry/.github#320")]
    assert _proxies_of(comments) == (PROXY,)


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login", "at", "cause"),
    [
        (trust_stubs.IMPOSTOR, None, "comment", "vergil-user", trust_stubs.CREATED, "wrong-type"),
        (
            trust_stubs.AGENT,
            trust_stubs.OUTSIDER,
            "edit",
            "drive-by",
            trust_stubs.EDITED,
            "untrusted",
        ),
    ],
    ids=["user-named-like-app", "trusted-comment-edited-by-outsider"],
)
def test_proxy_line_from_user_named_like_app(
    capsys: pytest.CaptureFixture[str],
    author: Any,
    editor: Any,
    kind: str,
    login: str,
    at: str,
    cause: str,
) -> None:
    # A Proxy: line counts only from a comment the layer trusts: a User account
    # named like the agent App, or an outsider's edit of a trusted comment,
    # makes it count for nothing, reported metadata only.
    forged = f"{trust_stubs.SPAM_TEXT}\nProxy: mallory-org/.github#666\n"
    comments = [
        _comment(author, forged, editor=editor),
        _comment(trust_stubs.HUMAN, "Proxy: logical-minds-foundry/.github#320"),
    ]
    assert _proxies_of(comments) == (PROXY,)
    out, err = capsys.readouterr()
    assert err == (
        f"skipped untrusted content: vergil-project/.github#1417 {kind} {login} at {at}"
        f" on comment 1 ({cause})\n"
    )
    assert "SPAM-MARKER-TEXT" not in out + err
    assert "mallory-org" not in out + err


def test_proxies_of_trusted_load_failure_raises() -> None:
    items = trust_stubs.FakeItems({PRIMARY_KEY: trust_stubs.item()}, strict=True)
    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
        patch(
            "vergil_tooling.lib.trust.load_trusted_authors",
            side_effect=TrustedAuthorsNotConfiguredError("not configured"),
        ),
        pytest.raises(TrustedAuthorsNotConfiguredError),
    ):
        proxies.proxies_of(PRIMARY)


def test_proxies_of_line_not_alone_ignored() -> None:
    comments = [
        _comment(trust_stubs.HUMAN, "The Proxy: logical-minds-foundry/.github#320 line is below."),
        _comment(trust_stubs.HUMAN, "see Proxy: wphillipmoore/.github#9"),
    ]
    assert _proxies_of(comments) == ()


def test_proxies_of_malformed_trusted_line_raises() -> None:
    comments = [_comment(trust_stubs.HUMAN, "Proxy: logical-minds-foundry/.github#320 (new)")]
    with pytest.raises(ProxyLinkError, match="malformed"):
        _proxies_of(comments)


def test_proxies_of_outside_dotgithub_raises() -> None:
    comments = [_comment(trust_stubs.HUMAN, "Proxy: logical-minds-foundry/some-repo#3")]
    with pytest.raises(ProxyLinkError, match="not in an <owner>/.github home"):
        _proxies_of(comments)


def test_proxies_of_unreadable_primary_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=RuntimeError("gh failed")),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(trust.TrustReadError, match="read_issue vergil-project/.github#1417"),
    ):
        proxies.proxies_of(PRIMARY)


def test_proxies_of_malformed_primary_read_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": None}),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(trust.TrustReadError, match="read_issue vergil-project/.github#1417"),
    ):
        proxies.proxies_of(PRIMARY)


# --- is_proxy_ref ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("epic", "proxy"), True),
        (("epic",), False),
        (("proxy",), False),
        (("epic", "ad-hoc"), False),
    ],
)
def test_is_proxy_ref(labels: tuple[str, ...], expected: bool) -> None:
    views = {PROXY: {"labels": [{"name": name} for name in labels]}}
    with patch("vergil_tooling.lib.github.read_json", side_effect=_views(views)):
        assert proxies.is_proxy_ref(PROXY) is expected


def test_is_proxy_ref_non_object_view_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=["not", "a view"]),
        pytest.raises(proxies.ProxyLinkError, match="unexpected 'gh issue view' response"),
    ):
        proxies.is_proxy_ref(PROXY)


# --- create_proxy ---------------------------------------------------------------

TARGET = "logical-minds-foundry/some-repo"
HOME = "logical-minds-foundry/.github"
PRIMARY_REPO = "vergil-project/.github"
PRIMARY_TITLE = "Epic: keep ad-hoc queues standalone"
WRITES = ("create", "reopen", "retitle", "comment", "ensure_labels")
DERIVED_TITLE = "Epic (proxy): vergil-project/.github#1417 — keep ad-hoc queues standalone"


class FakeGitHub:
    """The GitHub reads and writes ``create_proxy`` makes, recorded with their org.

    Each call records the org of the :func:`github.target_org` it ran under; an
    org in *no_install* has no App installation, so any call under it raises
    :class:`github.NoInstallationError`, as minting its token would.
    """

    def __init__(
        self,
        *,
        primary_labels: tuple[str, ...] = ("epic",),
        primary_state: str = "OPEN",
        primary_title: str = PRIMARY_TITLE,
        comments: list[dict[str, Any]] | None = None,
        proxies_in_home: dict[int, dict[str, Any]] | None = None,
        unlisted: frozenset[int] = frozenset(),
        no_install: frozenset[str] = frozenset(),
    ) -> None:
        self.primary: dict[str, Any] = {
            "labels": [{"name": name} for name in primary_labels],
            "state": primary_state,
            "title": primary_title,
        }
        # The primary's comment nodes, as the trust layer reads them.
        self.comments: list[dict[str, Any]] = _numbered(*(comments or []))
        self.home = dict(proxies_in_home or {})
        self.unlisted = unlisted
        self.listing: object = None
        self.no_install = no_install
        self.calls: list[tuple[str, str | None, Any]] = []
        self.next_number = 400
        # Who wrote and last edited each home proxy's body; the trusted human unless named.
        self.body_actors: dict[int, tuple[Any, Any]] = {}

    def _org(self) -> str | None:
        from vergil_tooling.lib import github

        org = github._target_org.get()
        if org in self.no_install:
            raise github.NoInstallationError(org, ["vergil-project"])
        return org

    def read_json(self, *args: str) -> object:
        org = self._org()
        self.calls.append(("read", org, args))
        if args[:2] == ("issue", "list"):
            assert args[3] == HOME
            if self.listing is not None:
                return self.listing
            return [
                {
                    "number": number,
                    "state": proxy["state"],
                    "title": proxy["title"],
                }
                for number, proxy in sorted(self.home.items())
                if number not in self.unlisted
            ]
        assert args[:2] == ("issue", "view")
        number, repo = int(args[2]), args[4]
        if (repo, number) == (PRIMARY_REPO, PRIMARY.number):
            return self.primary
        assert repo == HOME
        proxy = self.home[number]
        return {
            "state": proxy["state"],
            "title": proxy["title"],
            "labels": [{"name": name} for name in proxy["labels"]],
        }

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        """The trust layer's item read: the primary's comments, or a home proxy's body."""
        assert query == trust._ITEM_QUERY, "only the item read is faked"
        org = self._org()
        repo, number = f"{variables['owner']}/{variables['name']}", int(variables["number"])
        self.calls.append(("graphql", org, (repo, number)))
        if (repo, number) == PRIMARY_KEY:
            return trust_stubs.item(nodes=self.comments)
        assert repo == HOME
        proxy = self.home[number]
        author, editor = self.body_actors.get(number, (trust_stubs.HUMAN, None))
        return trust_stubs.item(body=proxy["body"], author=author, editor=editor)

    def ensure_labels(self, repo: str, names: list[str]) -> None:
        self.calls.append(("ensure_labels", self._org(), (repo, tuple(names))))

    def create_issue(self, **kwargs: Any) -> str:
        self.calls.append(("create", self._org(), kwargs))
        number = self.next_number
        self.home[number] = {
            "state": "OPEN",
            "title": kwargs["title"],
            "body": kwargs["body"],
            "labels": kwargs["labels"],
        }
        self.body_actors[number] = (trust_stubs.AGENT, None)
        return f"https://github.com/{kwargs['repo']}/issues/{number}"

    def run(self, *args: str) -> None:
        org = self._org()
        if args[:2] == ("issue", "reopen"):
            assert args[4] == HOME
            self.calls.append(("reopen", org, args))
            self.home[int(args[2])]["state"] = "OPEN"
            return
        if args[:2] == ("issue", "edit"):
            assert args[4] == HOME
            assert args[5] == "--title"
            self.calls.append(("retitle", org, args))
            self.home[int(args[2])]["title"] = args[6]
            return
        assert args[:2] == ("issue", "comment")
        assert (args[4], int(args[2])) == (PRIMARY_REPO, PRIMARY.number)
        self.calls.append(("comment", org, args[6]))
        self.comments = _numbered(*self.comments, _comment(trust_stubs.AGENT, args[6]))

    def writes(self) -> list[tuple[str, str | None, Any]]:
        return [call for call in self.calls if call[0] in WRITES]

    def create(self, target: str = TARGET) -> proxies.ProxyResult:
        with (
            patch("vergil_tooling.lib.github.read_json", side_effect=self.read_json),
            patch("vergil_tooling.lib.github.run", side_effect=self.run),
            patch("vergil_tooling.lib.github.create_issue", side_effect=self.create_issue),
            patch("vergil_tooling.lib.proxies.ensure_labels", side_effect=self.ensure_labels),
            patch("vergil_tooling.lib.github.graphql", side_effect=self.graphql),
            # The primary is a top-level epic, so it is never a continuation.
            patch("vergil_tooling.lib.epics._container_parent", return_value=None),
            patch(
                "vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS
            ),
        ):
            return proxies.create_proxy(PRIMARY, target)


def _home_proxy(
    state: str = "OPEN", primary: IssueRef = PRIMARY, title: str = DERIVED_TITLE
) -> dict[str, Any]:
    return {
        "state": state,
        "title": title,
        "body": proxies.proxy_body(primary, "logical-minds-foundry"),
        "labels": ["epic", "proxy"],
    }


def _recorded(proxy: IssueRef = PROXY, actor: Any = trust_stubs.AGENT) -> dict[str, Any]:
    return _comment(actor, proxies.proxy_comment(proxy))


def test_proxy_for_creates_and_comments() -> None:
    fake = FakeGitHub()
    result = fake.create()
    created = IssueRef("logical-minds-foundry", ".github", 400)
    assert result == proxies.ProxyResult(created, created=True, retitled=False, commented=True)
    (_, create_org, kwargs), (_, comment_org, comment) = fake.writes()[1:]
    assert create_org == "logical-minds-foundry"
    assert kwargs["repo"] == HOME
    assert kwargs["title"] == DERIVED_TITLE
    assert kwargs["labels"] == ["epic", "proxy"]
    assert "Primary: vergil-project/.github#1417" in kwargs["body"].splitlines()
    assert comment_org == "vergil-project"
    assert "Proxy: logical-minds-foundry/.github#400" in comment.splitlines()
    # The link reads back both ways.
    assert proxies._primary_in_body(created, kwargs["body"]) == PRIMARY


def test_proxy_for_created_link_reads_back_through_proxies_of() -> None:
    fake = FakeGitHub()
    fake.create()
    assert fake.create() == proxies.ProxyResult(
        IssueRef("logical-minds-foundry", ".github", 400),
        created=False,
        retitled=False,
        commented=False,
    )


def test_proxy_title_names_the_primary_ref() -> None:
    assert (
        proxies.proxy_title(PRIMARY, "Epic: X") == "Epic (proxy): vergil-project/.github#1417 — X"
    )
    assert (
        proxies.proxy_title(PRIMARY, "Something")
        == "Epic (proxy): vergil-project/.github#1417 — Something"
    )


def test_proxy_title_matches_the_hand_retitled_proxy() -> None:
    # vergil-project/.github#1443 was retitled by hand to this; a re-run must not change it.
    primary = IssueRef("logical-minds-foundry", ".github", 319)
    title = (
        "Epic: M3 — the lab consumes published component packages"
        " (extract mq-resiliency-observability)"
    )
    assert proxies.proxy_title(primary, title) == (
        "Epic (proxy): logical-minds-foundry/.github#319 — M3 — the lab consumes published"
        " component packages (extract mq-resiliency-observability)"
    )


@pytest.mark.parametrize("target", ["vergil-project/vergil-tooling", "Vergil-Project/docs"])
def test_proxy_for_same_org_refused(target: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(proxies.ProxyRefusedError, match="same org"):
        fake.create(target)
    assert fake.calls == []


def test_proxy_for_malformed_target_refused() -> None:
    fake = FakeGitHub()
    with pytest.raises(proxies.ProxyRefusedError, match="owner/repo"):
        fake.create("noslash")
    assert fake.calls == []


@pytest.mark.parametrize(
    ("labels", "state", "title", "problem"),
    [
        (("epic", "ad-hoc"), "OPEN", "Epic (ad hoc): x", "a standing epic"),
        (("epic", "intake"), "OPEN", "Epic (intake): x", "a standing epic"),
        (("epic", "proxy"), "OPEN", "Epic (proxy): x", "a proxy epic"),
        (("epic",), "CLOSED", PRIMARY_TITLE, "closed"),
        (("bug",), "OPEN", PRIMARY_TITLE, "not an epic"),
        (("epic", "archive"), "OPEN", "Archive (ad hoc): x — 2026-Q3", "an archive"),
    ],
)
def test_proxy_for_primary_not_open_dev_epic_refused(
    labels: tuple[str, ...], state: str, title: str, problem: str
) -> None:
    fake = FakeGitHub(primary_labels=labels, primary_state=state, primary_title=title)
    with pytest.raises(proxies.ProxyRefusedError, match=problem):
        fake.create()
    assert fake.writes() == []


def test_proxy_ensure_homes_the_proxy_in_org_dotgithub() -> None:
    # Every repo is public (epic vergil-project/.github#1476): the proxy's
    # home is the org's .github.
    result = FakeGitHub().create()
    assert result.proxy == IssueRef("logical-minds-foundry", ".github", 400)
    assert result.created


def test_proxy_for_existing_open_proxy_reused() -> None:
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result.proxy == PROXY
    assert not result.created


def test_create_proxy_refuses_closed_proxy() -> None:
    # Closed is closed (.github#1609): a closed proxy naming the primary is
    # refused by name before any write, and never reopened.
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy("CLOSED")})
    with pytest.raises(epics.ClosedEpicError) as excinfo:
        fake.create()
    assert excinfo.value.epic == PROXY
    assert str(excinfo.value) == (
        "epic logical-minds-foundry/.github#320 is closed; closed epics are not reopened "
        "by the tooling; a person may reopen it by hand, and a re-run then reuses it"
    )
    assert fake.writes() == []
    assert fake.home[320]["state"] == "CLOSED"


def test_create_proxy_refuses_naming_lowest_closed_when_several() -> None:
    fake = FakeGitHub(
        proxies_in_home={322: _home_proxy("CLOSED"), 320: _home_proxy("CLOSED")},
        comments=[_recorded(), _recorded(IssueRef("logical-minds-foundry", ".github", 322))],
    )
    with pytest.raises(epics.ClosedEpicError) as excinfo:
        fake.create()
    assert excinfo.value.epic == PROXY
    assert fake.writes() == []


def test_create_proxy_reuses_open_beside_closed() -> None:
    fake = FakeGitHub(
        proxies_in_home={320: _home_proxy("CLOSED"), 322: _home_proxy()},
        comments=[_recorded(), _recorded(IssueRef("logical-minds-foundry", ".github", 322))],
    )
    assert fake.create().proxy == IssueRef("logical-minds-foundry", ".github", 322)
    assert fake.writes() == []


def test_proxy_for_rerun_posts_missing_comment() -> None:
    fake = FakeGitHub(proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, retitled=False, commented=True)
    ((kind, org, body),) = fake.writes()
    assert (kind, org) == ("comment", "vergil-project")
    assert "Proxy: logical-minds-foundry/.github#320" in body.splitlines()


def test_proxy_for_rerun_is_noop() -> None:
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, retitled=False, commented=False)
    assert fake.writes() == []


def test_proxy_for_stale_title_retitled() -> None:
    fake = FakeGitHub(
        comments=[_recorded()],
        proxies_in_home={320: _home_proxy(title="Epic (proxy): keep ad-hoc queues standalone")},
    )
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, retitled=True, commented=False)
    ((kind, org, args),) = fake.writes()
    assert (kind, org) == ("retitle", "logical-minds-foundry")
    assert args == ("issue", "edit", "320", "--repo", HOME, "--title", DERIVED_TITLE)
    assert fake.home[320]["title"] == DERIVED_TITLE
    # A second run finds the title current and writes nothing.
    assert not fake.create().retitled
    assert len(fake.writes()) == 1


def test_proxy_for_unlisted_stale_proxy_retitled() -> None:
    fake = FakeGitHub(
        comments=[_recorded()],
        proxies_in_home={320: _home_proxy(title="old")},
        unlisted=frozenset({320}),
    )
    assert fake.create().retitled
    assert fake.home[320]["title"] == DERIVED_TITLE


@pytest.mark.parametrize(
    ("actor", "editor", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "untrusted"),
        (trust_stubs.IMPOSTOR, None, "wrong-type"),
        (trust_stubs.AGENT, trust_stubs.OUTSIDER, "untrusted"),
    ],
    ids=["outsider", "user-named-like-app", "edited-by-outsider"],
)
def test_proxy_for_untrusted_comment_does_not_count(
    capsys: pytest.CaptureFixture[str], actor: Any, editor: Any, cause: str
) -> None:
    recorded = _comment(actor, proxies.proxy_comment(PROXY), editor=editor)
    fake = FakeGitHub(comments=[recorded], proxies_in_home={320: _home_proxy()})
    assert fake.create().commented
    err = capsys.readouterr().err
    assert err.startswith("skipped untrusted content: vergil-project/.github#1417 ")
    assert err.endswith(f" on comment 1 ({cause})\n")


@pytest.mark.parametrize(
    ("author", "editor", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "untrusted"),
        (trust_stubs.IMPOSTOR, None, "wrong-type"),
        (trust_stubs.AGENT, trust_stubs.OUTSIDER, "untrusted"),
    ],
    ids=["outsider-body", "user-named-like-app", "edited-by-outsider"],
)
@pytest.mark.parametrize("unlisted", [False, True], ids=["listed", "unlisted"])
def test_proxy_for_untrusted_proxy_body_in_home_stops(
    capsys: pytest.CaptureFixture[str], author: Any, editor: Any, cause: str, unlisted: bool
) -> None:
    # A home proxy's Primary: line counts only from a body the layer trusts:
    # an untrusted one stops create_proxy before any write, metadata only,
    # whether the listing or the primary's comment found it.
    spam = {**_home_proxy(), "body": f"{trust_stubs.SPAM_TEXT}\n{proxies.primary_line(PRIMARY)}\n"}
    fake = FakeGitHub(
        comments=[_recorded()],
        proxies_in_home={320: spam},
        unlisted=frozenset({320}) if unlisted else frozenset(),
    )
    fake.body_actors[320] = (author, editor)
    with pytest.raises(trust.UntrustedContentError) as excinfo:
        fake.create()
    assert excinfo.value.item.repo == HOME
    assert excinfo.value.item.number == 320
    assert excinfo.value.item.field == "body"
    assert excinfo.value.item.cause == cause
    assert fake.writes() == []
    out, err = capsys.readouterr()
    assert "SPAM-MARKER-TEXT" not in str(excinfo.value) + out + err


def test_proxy_for_other_primarys_proxy_ignored() -> None:
    other = IssueRef("vergil-project", ".github", 9)
    fake = FakeGitHub(proxies_in_home={320: _home_proxy(primary=other)})
    result = fake.create()
    assert result.created
    assert result.proxy.number == 400


def test_proxy_for_unlisted_proxy_found_through_comment() -> None:
    # A just-created proxy the listing has not caught up with is still found.
    fake = FakeGitHub(
        comments=[_recorded()], proxies_in_home={320: _home_proxy()}, unlisted=frozenset({320})
    )
    assert fake.create() == proxies.ProxyResult(
        PROXY, created=False, retitled=False, commented=False
    )


def test_proxy_for_comment_naming_non_proxy_raises() -> None:
    other = IssueRef("vergil-project", ".github", 9)
    fake = FakeGitHub(
        comments=[_recorded()],
        proxies_in_home={320: _home_proxy(primary=other)},
        unlisted=frozenset({320}),
    )
    with pytest.raises(ProxyLinkError, match="not a proxy naming"):
        fake.create()
    assert fake.writes() == []


def test_proxy_for_two_open_proxies_raises() -> None:
    fake = FakeGitHub(proxies_in_home={320: _home_proxy(), 321: _home_proxy()})
    with pytest.raises(ProxyLinkError, match="2 open proxies"):
        fake.create()
    assert fake.writes() == []


def test_proxy_for_malformed_primary_line_in_home_raises() -> None:
    bad = {
        "state": "OPEN",
        "title": DERIVED_TITLE,
        "body": "Primary: garbage\n",
        "labels": ["epic", "proxy"],
    }
    fake = FakeGitHub(proxies_in_home={320: bad})
    with pytest.raises(ProxyLinkError, match="malformed"):
        fake.create()
    assert fake.writes() == []


@pytest.mark.parametrize("org", ["vergil-project", "logical-minds-foundry"])
def test_proxy_for_token_missing_refuses(org: str) -> None:
    from vergil_tooling.lib import github

    fake = FakeGitHub(no_install=frozenset({org}))
    with pytest.raises(github.NoInstallationError):
        fake.create()
    assert fake.writes() == []


def test_proxy_for_ensures_labels_before_create() -> None:
    fake = FakeGitHub()
    fake.create()
    kinds = [(kind, org) for kind, org, _ in fake.writes()]
    assert kinds[:2] == [
        ("ensure_labels", "logical-minds-foundry"),
        ("create", "logical-minds-foundry"),
    ]
    assert fake.writes()[0][2] == (HOME, ("epic", "proxy"))


def test_proxy_for_truncated_listing_raises() -> None:
    fake = FakeGitHub(proxies_in_home={n: _home_proxy("CLOSED") for n in range(1, 1001)})
    with pytest.raises(EpicListTruncatedError, match="limit"):
        fake.create()
    assert fake.writes() == []


def test_proxy_for_non_list_listing_raises() -> None:
    fake = FakeGitHub()
    fake.listing = {"message": "odd"}
    with pytest.raises(ProxyLinkError, match="unexpected 'gh issue list'"):
        fake.create()
    assert fake.writes() == []


# -- a continuation's ref means its head (epic vergil-project/.github#1609) ------

_HEAD = IssueRef("vergil-project", ".github", 10)
_C2 = IssueRef("vergil-project", ".github", 20)


def _chain_graph(monkeypatch: pytest.MonkeyPatch, *head_labels: str) -> FakeSubIssueGraph:
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic", *head_labels])
    graph.add_continuation(_C2, _HEAD, _HEAD)
    return graph


def test_require_open_development_epic_maps_continuation_to_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _chain_graph(monkeypatch, "epic-development")
    assert proxies._require_open_development_epic(_C2) == (_HEAD, "Epic: big")
    assert (
        "vergil-project/.github#20 is a continuation of vergil-project/.github#10; "
        "using vergil-project/.github#10" in capsys.readouterr().err
    )


def test_require_open_development_epic_checks_the_heads_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The continuation's own labels mark no kind; its head is a standing epic.
    _chain_graph(monkeypatch, "intake")
    with pytest.raises(proxies.ProxyRefusedError, match=r"\.github#10 is a standing epic"):
        proxies._require_open_development_epic(_C2)
