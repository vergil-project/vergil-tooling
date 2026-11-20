"""Tests for vergil_tooling.lib.proxies: the proxy epic's link to its primary."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import proxies
from vergil_tooling.lib.epics import EpicListTruncatedError, IssueRef
from vergil_tooling.lib.proxies import ProxyLinkError
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

PROXY = IssueRef("logical-minds-foundry", ".github", 320)
PRIMARY = IssueRef("vergil-project", ".github", 1417)
TRUSTED = frozenset({"wphillipmoore", "wphillipmoore-vergil-user"})


def _comment(login: str, body: str) -> dict[str, Any]:
    return {"author": {"login": login}, "body": body}


def _views(views: dict[IssueRef, dict[str, Any]]) -> Any:
    """A ``github.read_json`` fake answering ``gh issue view`` from *views*."""

    def fake(*args: str) -> object:
        assert args[:2] == ("issue", "view")
        ref = IssueRef(*args[4].split("/", 1), int(args[2]))
        return views[ref]

    return fake


def _primary_of(body: str, primary_labels: tuple[str, ...] = ("epic",)) -> IssueRef:
    views = {
        PROXY: {"body": body},
        PRIMARY: {"labels": [{"name": name} for name in primary_labels]},
    }
    with patch("vergil_tooling.lib.github.read_json", side_effect=_views(views)):
        return proxies.primary_of(PROXY)


def _proxies_of(comments: list[dict[str, Any]]) -> tuple[IssueRef, ...]:
    views = {PRIMARY: {"comments": comments}}
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_views(views)),
        patch("vergil_tooling.lib.proxies.epic_size.trusted_logins", return_value=TRUSTED),
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
        patch("vergil_tooling.lib.github.read_json", side_effect=RuntimeError("gh failed")),
        pytest.raises(RuntimeError, match="gh failed"),
    ):
        proxies.primary_of(PROXY)


def test_primary_of_non_object_view_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        pytest.raises(ProxyLinkError, match="unexpected"),
    ):
        proxies.primary_of(PROXY)


# --- proxies_of -----------------------------------------------------------------


def test_proxies_of_union_of_trusted_comments() -> None:
    comments = [
        _comment("wphillipmoore-vergil-user", "Proxy: wphillipmoore/.github#9"),
        _comment("wphillipmoore", "Created.\n\nProxy: logical-minds-foundry/.github#320\n"),
        _comment("wphillipmoore-vergil-user", "Proxy: wphillipmoore/.github#9"),
    ]
    assert _proxies_of(comments) == (PROXY, IssueRef("wphillipmoore", ".github", 9))


def test_proxies_of_none() -> None:
    assert _proxies_of([_comment("wphillipmoore", "LGTM")]) == ()


def test_proxies_of_ignores_untrusted_comment() -> None:
    comments = [
        _comment("mallory", "Proxy: mallory-org/.github#1"),
        _comment("", "Proxy: ghost-org/.github#2"),
        _comment("wphillipmoore", "Proxy: logical-minds-foundry/.github#320"),
    ]
    assert _proxies_of(comments) == (PROXY,)


def test_proxies_of_ignores_untrusted_malformed_line() -> None:
    # A forged malformed line must not be able to break the primary's readers.
    comments = [_comment("mallory", "Proxy: garbage")]
    assert _proxies_of(comments) == ()


def test_proxies_of_strips_app_prefix() -> None:
    comments = [
        _comment("app/wphillipmoore-vergil-user", "Proxy: logical-minds-foundry/.github#320")
    ]
    assert _proxies_of(comments) == (PROXY,)


def test_proxies_of_trusted_load_failure_raises() -> None:
    views = {PRIMARY: {"comments": []}}
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_views(views)),
        patch(
            "vergil_tooling.lib.proxies.epic_size.trusted_logins",
            side_effect=TrustedAuthorsNotConfiguredError("not configured"),
        ),
        pytest.raises(TrustedAuthorsNotConfiguredError),
    ):
        proxies.proxies_of(PRIMARY)


def test_proxies_of_line_not_alone_ignored() -> None:
    comments = [
        _comment("wphillipmoore", "The Proxy: logical-minds-foundry/.github#320 line is below."),
        _comment("wphillipmoore", "see Proxy: wphillipmoore/.github#9"),
    ]
    assert _proxies_of(comments) == ()


def test_proxies_of_malformed_trusted_line_raises() -> None:
    comments = [_comment("wphillipmoore", "Proxy: logical-minds-foundry/.github#320 (new)")]
    with pytest.raises(ProxyLinkError, match="malformed"):
        _proxies_of(comments)


def test_proxies_of_outside_dotgithub_raises() -> None:
    comments = [_comment("wphillipmoore", "Proxy: logical-minds-foundry/some-repo#3")]
    with pytest.raises(ProxyLinkError, match="not in an <owner>/.github home"):
        _proxies_of(comments)


def test_proxies_of_unreadable_primary_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=RuntimeError("gh failed")),
        patch("vergil_tooling.lib.proxies.epic_size.trusted_logins", return_value=TRUSTED),
        pytest.raises(RuntimeError, match="gh failed"),
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


# --- create_proxy ---------------------------------------------------------------

TARGET = "logical-minds-foundry/some-repo"
HOME = "logical-minds-foundry/.github"
PRIMARY_REPO = "vergil-project/.github"
PRIMARY_TITLE = "Epic: keep ad-hoc queues standalone"
AGENT = "app/wphillipmoore-vergil-user"
WRITES = ("create", "reopen", "comment", "ensure_labels")


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
        private: frozenset[str] = frozenset(),
        no_install: frozenset[str] = frozenset(),
    ) -> None:
        self.primary: dict[str, Any] = {
            "labels": [{"name": name} for name in primary_labels],
            "state": primary_state,
            "title": primary_title,
            "comments": list(comments or []),
        }
        self.home = dict(proxies_in_home or {})
        self.unlisted = unlisted
        self.listing: object = None
        self.private = private
        self.no_install = no_install
        self.calls: list[tuple[str, str | None, Any]] = []
        self.next_number = 400

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
                {"number": number, "state": proxy["state"], "body": proxy["body"]}
                for number, proxy in sorted(self.home.items())
                if number not in self.unlisted
            ]
        assert args[:2] == ("issue", "view")
        number, repo = int(args[2]), args[4]
        if (repo, number) == (PRIMARY_REPO, PRIMARY.number):
            return self.primary
        assert repo == HOME
        proxy = self.home[number]
        return {**proxy, "labels": [{"name": name} for name in proxy["labels"]]}

    def is_public(self, repo: str) -> bool:
        self.calls.append(("is_public", self._org(), repo))
        return repo not in self.private

    def ensure_labels(self, repo: str, names: list[str]) -> None:
        self.calls.append(("ensure_labels", self._org(), (repo, tuple(names))))

    def create_issue(self, **kwargs: Any) -> str:
        self.calls.append(("create", self._org(), kwargs))
        number = self.next_number
        self.home[number] = {"state": "OPEN", "body": kwargs["body"], "labels": kwargs["labels"]}
        return f"https://github.com/{kwargs['repo']}/issues/{number}"

    def run(self, *args: str) -> None:
        org = self._org()
        if args[:2] == ("issue", "reopen"):
            assert args[4] == HOME
            self.calls.append(("reopen", org, args))
            self.home[int(args[2])]["state"] = "OPEN"
            return
        assert args[:2] == ("issue", "comment")
        assert (args[4], int(args[2])) == (PRIMARY_REPO, PRIMARY.number)
        self.calls.append(("comment", org, args[6]))
        self.primary["comments"].append(_comment(AGENT, args[6]))

    def writes(self) -> list[tuple[str, str | None, Any]]:
        return [call for call in self.calls if call[0] in WRITES]

    def create(self, target: str = TARGET) -> proxies.ProxyResult:
        with (
            patch("vergil_tooling.lib.github.read_json", side_effect=self.read_json),
            patch("vergil_tooling.lib.github.is_public", side_effect=self.is_public),
            patch("vergil_tooling.lib.github.run", side_effect=self.run),
            patch("vergil_tooling.lib.github.create_issue", side_effect=self.create_issue),
            patch("vergil_tooling.lib.proxies.ensure_labels", side_effect=self.ensure_labels),
            patch("vergil_tooling.lib.proxies.epic_size.trusted_logins", return_value=TRUSTED),
        ):
            return proxies.create_proxy(PRIMARY, target)


def _home_proxy(state: str = "OPEN", primary: IssueRef = PRIMARY) -> dict[str, Any]:
    return {
        "state": state,
        "body": proxies.proxy_body(primary, "logical-minds-foundry"),
        "labels": ["epic", "proxy"],
    }


def _recorded(proxy: IssueRef = PROXY, login: str = AGENT) -> dict[str, Any]:
    return _comment(login, proxies.proxy_comment(proxy))


def test_proxy_for_creates_and_comments() -> None:
    fake = FakeGitHub()
    result = fake.create()
    created = IssueRef("logical-minds-foundry", ".github", 400)
    assert result == proxies.ProxyResult(created, created=True, reopened=False, commented=True)
    (_, create_org, kwargs), (_, comment_org, comment) = fake.writes()[1:]
    assert create_org == "logical-minds-foundry"
    assert kwargs["repo"] == HOME
    assert kwargs["title"] == "Epic (proxy): keep ad-hoc queues standalone"
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
        reopened=False,
        commented=False,
    )


def test_proxy_title_without_epic_prefix() -> None:
    assert proxies.proxy_title("Epic: X") == "Epic (proxy): X"
    assert proxies.proxy_title("Something") == "Epic (proxy): Something"


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


def test_proxy_for_private_primary_refused() -> None:
    fake = FakeGitHub(private=frozenset({PRIMARY_REPO}))
    with pytest.raises(proxies.ProxyRefusedError) as excinfo:
        fake.create()
    assert "not public" in str(excinfo.value)
    assert "must not be filed in an ad-hoc epic" in str(excinfo.value)
    assert fake.writes() == []
    # Refused before the primary's title or comments are read.
    assert [call for call in fake.calls if call[0] == "read"] == []


@pytest.mark.parametrize("private", [TARGET, HOME])
def test_proxy_for_private_target_refused(private: str) -> None:
    fake = FakeGitHub(private=frozenset({private}))
    with pytest.raises(proxies.ProxyRefusedError) as excinfo:
        fake.create()
    assert "not public" in str(excinfo.value)
    assert "must not be filed in an ad-hoc epic" in str(excinfo.value)
    assert fake.writes() == []
    assert not any(call[0] == "read" and call[2][:2] == ("issue", "list") for call in fake.calls)


def test_proxy_for_existing_open_proxy_reused() -> None:
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result.proxy == PROXY
    assert not result.created
    assert not result.reopened


def test_proxy_for_closed_proxy_reopened() -> None:
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy("CLOSED")})
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, reopened=True, commented=False)
    assert [(kind, org) for kind, org, _ in fake.writes()] == [("reopen", "logical-minds-foundry")]
    assert fake.home[320]["state"] == "OPEN"


def test_proxy_for_closed_proxy_reopened_lowest_when_several() -> None:
    fake = FakeGitHub(
        proxies_in_home={322: _home_proxy("CLOSED"), 320: _home_proxy("CLOSED")},
        comments=[_recorded(), _recorded(IssueRef("logical-minds-foundry", ".github", 322))],
    )
    assert fake.create().proxy == PROXY


def test_proxy_for_rerun_posts_missing_comment() -> None:
    fake = FakeGitHub(proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, reopened=False, commented=True)
    ((kind, org, body),) = fake.writes()
    assert (kind, org) == ("comment", "vergil-project")
    assert "Proxy: logical-minds-foundry/.github#320" in body.splitlines()


def test_proxy_for_rerun_is_noop() -> None:
    fake = FakeGitHub(comments=[_recorded()], proxies_in_home={320: _home_proxy()})
    result = fake.create()
    assert result == proxies.ProxyResult(PROXY, created=False, reopened=False, commented=False)
    assert fake.writes() == []


def test_proxy_for_untrusted_comment_does_not_count() -> None:
    fake = FakeGitHub(comments=[_recorded(login="mallory")], proxies_in_home={320: _home_proxy()})
    assert fake.create().commented


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
        PROXY, created=False, reopened=False, commented=False
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
    bad = {"state": "OPEN", "body": "Primary: garbage\n", "labels": ["epic", "proxy"]}
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
