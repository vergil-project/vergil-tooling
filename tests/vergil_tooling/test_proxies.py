"""Tests for vergil_tooling.lib.proxies: the proxy epic's link to its primary."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import proxies
from vergil_tooling.lib.epics import IssueRef
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
