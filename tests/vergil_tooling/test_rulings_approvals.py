"""Tests for the merge-review exemption request and its verifier (epic .github#1384).

Task 6a renders the request and reads the decision line; Task 6b verifies a
past exchange.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.approvals import (
    DECISION_LINES,
    REPLY_INSTRUCTION,
    Approval,
    parse_decision,
    render_exemption_request,
    verified_approvals,
)
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.marker import Marker, parse_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text
from vergil_tooling.lib.trusted_authors import TrustedAuthors

AGENT = "h-vergil-user"
FINGERPRINT = "c" * 64


def _marker(body: str) -> Marker | None:
    return parse_marker(IssueComment(1, AGENT, body, False), AGENT)


# --- the exemption request -------------------------------------------------------


def test_exemption_request_records_reason_and_fingerprint() -> None:
    body = render_exemption_request("a one-line typo fix", FINGERPRINT)
    lines = body.split("\n")
    assert lines[0].startswith("Merge-review exemption requested.")
    assert lines[1:3] == ["Reason: a one-line typo fix", f"Fingerprint: {FINGERPRINT}"]
    assert lines[3:] == [
        REPLY_INSTRUCTION,
        "",
        "<!-- vrg-ruling kind=request type=merge-review-exemption -->",
    ]
    assert _marker(body) == Marker("request", None, None, "merge-review-exemption")
    assert "protected-change" not in body
    check_plain_text(body.rpartition("\n")[0], what="the body")


@pytest.mark.parametrize(
    ("reason", "fingerprint", "message"),
    [
        ("", FINGERPRINT, "empty"),
        ("two\nlines", FINGERPRINT, "one line"),
        ("tab\there", FINGERPRINT, "not plain text"),
        ("café", FINGERPRINT, "not plain text"),
        ("   ", FINGERPRINT, "empty"),
        (" ok", FINGERPRINT, "whitespace"),
        ("ok ", FINGERPRINT, "whitespace"),
        ("ok", "C" * 64, "SHA-256"),
        ("ok", "c" * 63, "SHA-256"),
        ("ok", "", "SHA-256"),
    ],
)
def test_exemption_request_refuses_bad_input(reason: str, fingerprint: str, message: str) -> None:
    with pytest.raises(RulingError, match=message):
        render_exemption_request(reason, fingerprint)


# --- the decision line -----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "decision"),
    [
        ("Decision: approve", "approve"),
        ("Decision: refuse", "refuse"),
        ("Decision: approve\n", "approve"),
        ("Looked at the workflow diff.\nDecision: approve\nThanks.", "approve"),
        ("I decided: approve\nDecision: refuse", "refuse"),
    ],
)
def test_parse_decision_accepts_only_one_exact_line(body: str, decision: str) -> None:
    assert parse_decision(body) == decision


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "no decision line"),
        ("approve", "no decision line"),
        ("Looks good to me", "no decision line"),
        ("Decision: Approve", "not exactly"),
        ("decision: approve", "not exactly"),
        ("DECISION: refuse", "not exactly"),
        ("Decision: approved", "not exactly"),
        ("Decision:approve", "not exactly"),
        ("Decision:  approve", "not exactly"),
        ("Decision: approve ", "not exactly"),
        (" Decision: approve", "not exactly"),
        ("Decision: yes", "not exactly"),
        ("Decision: approve\nDecision: approve", "2 decision lines"),
        ("Decision: approve\nDecision: refuse", "2 decision lines"),
        ("Decision: approve\n  decision: maybe", "2 decision lines"),
    ],
)
def test_parse_decision_refuses_anything_else(body: str, message: str) -> None:
    with pytest.raises(RulingError, match=message):
        parse_decision(body)


def test_decision_lines_are_the_two_words() -> None:
    assert DECISION_LINES == {"Decision: approve": "approve", "Decision: refuse": "refuse"}


# --- verifying a past exchange (Task 6b) -----------------------------------------

AUTHORS = TrustedAuthors(human=("human",), agents=("human-vergil-audit", "human-vergil-user"))
HUMAN: dict[str, str] = {"__typename": "User", "login": "human"}
AGENT_ACTOR: dict[str, str] = {"__typename": "Bot", "login": "human-vergil-user"}
OTHER_AGENT: dict[str, str] = {"__typename": "Bot", "login": "human-vergil-audit"}
AUTOMATION: dict[str, str] = {"__typename": "Bot", "login": "o-automation"}
OUTSIDER: dict[str, str] = {"__typename": "User", "login": "drive-by"}
IMPOSTOR: dict[str, str] = {"__typename": "User", "login": "human-vergil-user"}
KEY = ("o/r", 7)
REF = "o/r#7"
EXEMPTION = render_exemption_request("a one-line typo fix", FINGERPRINT)
APPROVE = "Looked at the diff.\nDecision: approve"
REFUSE = "Decision: refuse"
ORDINARY_REQUEST = "Which way?\n\n<!-- vrg-ruling kind=request -->"


def _response(request_id: int, reply_id: int) -> str:
    return f"Done.\n\n<!-- vrg-ruling kind=response request={request_id} reply={reply_id} -->"


def _exemption(request_id: int) -> Approval:
    return Approval("merge-review-exemption", request_id, "approve", FINGERPRINT)


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> trust_stubs.FakeItems:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    return fake


def _on_issue(items: trust_stubs.FakeItems, *nodes: dict[str, Any]) -> None:
    items.items[KEY] = trust_stubs.item(nodes=list(nodes))


def test_verified_approvals_reads_every_approved_exemption(items: trust_stubs.FakeItems) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(11, APPROVE, HUMAN),
        trust_stubs.comment(12, _response(10, 11), AGENT_ACTOR),
        trust_stubs.comment(20, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(21, "Decision: approve", HUMAN),
        trust_stubs.comment(22, _response(20, 21), AGENT_ACTOR),
    )
    assert verified_approvals(REF, authors=AUTHORS) == [_exemption(10), _exemption(20)]


def test_verified_approvals_reads_the_whole_stream(
    items: trust_stubs.FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[trust.Role]] = []
    real = trust.read_comment_stream

    def spy(repo: str, number: int, **kwargs: Any) -> trust.CommentStream:
        seen.append(kwargs["roles"])
        return real(repo, number, **kwargs)

    monkeypatch.setattr(trust, "read_comment_stream", spy)
    _on_issue(items)
    assert verified_approvals(REF, authors=AUTHORS) == []
    assert seen == [frozenset({trust.Role.HUMAN, trust.Role.AGENTS})]


def test_past_approval_ignores_later_trusted_comments(items: trust_stubs.FakeItems) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(11, APPROVE, HUMAN),
        trust_stubs.comment(12, _response(10, 11), AGENT_ACTOR),
        trust_stubs.comment(13, "Merge review: done: eligible; accepted-risk 0", AGENT_ACTOR),
        trust_stubs.comment(14, "Decision: refuse", HUMAN),
        trust_stubs.comment(15, ORDINARY_REQUEST, AGENT_ACTOR),
        trust_stubs.comment(16, "Later note.", HUMAN, editor=HUMAN),
        trust_stubs.comment(17, "An unrelated remark.", OTHER_AGENT),
    )
    assert verified_approvals(REF, authors=AUTHORS) == [_exemption(10)]


@pytest.mark.parametrize(
    "nodes",
    [
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR, editor=AGENT_ACTOR),
                trust_stubs.comment(11, APPROVE, HUMAN),
            ],
            id="request-edited",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, APPROVE, HUMAN, editor=HUMAN),
            ],
            id="reply-edited",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, APPROVE, AGENT_ACTOR),
            ],
            id="reply-by-the-agent",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, APPROVE, OTHER_AGENT),
                trust_stubs.comment(12, APPROVE, HUMAN),
            ],
            id="reply-by-another-agent",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, OTHER_AGENT),
                trust_stubs.comment(11, APPROVE, HUMAN),
            ],
            id="request-by-another-agent",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, IMPOSTOR),
                trust_stubs.comment(11, APPROVE, HUMAN),
            ],
            id="request-by-an-impostor",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, HUMAN),
                trust_stubs.comment(11, APPROVE, HUMAN),
            ],
            id="request-by-the-human",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, REFUSE, HUMAN),
                trust_stubs.comment(12, APPROVE, HUMAN),
            ],
            id="second-human-reply",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, APPROVE, HUMAN, editor=HUMAN),
                trust_stubs.comment(12, APPROVE, HUMAN),
            ],
            id="second-reply-after-an-edited-one",
        ),
        pytest.param(
            [
                trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
                trust_stubs.comment(11, "Approved, café.\nDecision: approve", HUMAN),
            ],
            id="reply-not-plain-text",
        ),
    ],
)
def test_past_approval_rejects_edited_and_wrong_login(
    items: trust_stubs.FakeItems, nodes: list[dict[str, Any]]
) -> None:
    _on_issue(items, *nodes)
    assert verified_approvals(REF, authors=AUTHORS) == []


@pytest.mark.parametrize(
    "between",
    [
        trust_stubs.comment(11, "Decision: refuse", AUTOMATION),
        trust_stubs.comment(11, "Decision: refuse", OUTSIDER),
        trust_stubs.comment(11, "Decision: refuse", IMPOSTOR),
        # A trusted comment an outsider edited leaves the stream too.
        trust_stubs.comment(11, "Decision: refuse", HUMAN, editor=OUTSIDER),
    ],
)
def test_untrusted_comment_between_request_and_reply_is_skipped(
    items: trust_stubs.FakeItems, between: dict[str, Any]
) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
        between,
        trust_stubs.comment(12, APPROVE, HUMAN),
    )
    assert verified_approvals(REF, authors=AUTHORS) == [_exemption(10)]


def test_refuse_decision_is_not_an_approval(items: trust_stubs.FakeItems) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(11, REFUSE, HUMAN),
        trust_stubs.comment(12, _response(10, 11), AGENT_ACTOR),
        trust_stubs.comment(20, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(21, "Not this time.\nDecision: refuse", HUMAN),
    )
    assert verified_approvals(REF, authors=AUTHORS) == []


@pytest.mark.parametrize(
    "reply",
    [
        "Looks fine",
        "Decision: Approve",
        "Decision: approve\nDecision: approve",
        "Decision: approve\nDecision: refuse",
        "Decision: approveé",
    ],
)
def test_reply_without_one_exact_approve_is_not_an_approval(
    items: trust_stubs.FakeItems, reply: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
        trust_stubs.comment(11, reply, HUMAN),
    )
    assert verified_approvals(REF, authors=AUTHORS) == []
    assert "request 10" in capsys.readouterr().err


def test_unanswered_and_ordinary_requests_are_not_approvals(
    items: trust_stubs.FakeItems,
) -> None:
    _on_issue(
        items,
        trust_stubs.comment(1, ORDINARY_REQUEST, AGENT_ACTOR),
        trust_stubs.comment(2, "Decision: approve", HUMAN),
        trust_stubs.comment(3, _response(1, 2), AGENT_ACTOR),
        trust_stubs.comment(10, EXEMPTION, AGENT_ACTOR),
    )
    assert verified_approvals(REF, authors=AUTHORS) == []


@pytest.mark.parametrize(
    "body",
    [
        # A typed marker on a body that does not hold exactly what the kind records.
        EXEMPTION.replace(f"Fingerprint: {FINGERPRINT}", "Fingerprint: abc"),
        EXEMPTION.replace(f"Fingerprint: {FINGERPRINT}\n", ""),
        EXEMPTION.replace(
            f"Fingerprint: {FINGERPRINT}", f"Fingerprint: {FINGERPRINT}\nFingerprint: {FINGERPRINT}"
        ),
        EXEMPTION.replace("Reason: a one-line typo fix\n", ""),
        EXEMPTION.replace("Reason: a one-line typo fix", "Reason: a\nReason: b"),
        EXEMPTION.replace("Merge-review exemption requested.", "Please exempt."),
        EXEMPTION.replace(REPLY_INSTRUCTION, "Reply however you like."),
        "Merge-review exemption requested.\n\n"
        "<!-- vrg-ruling kind=request type=merge-review-exemption -->",
        # The exemption text as it read when a protected-change approval existed.
        EXEMPTION.replace(
            "fingerprint only.",
            "fingerprint only; it never waives a protected-change approval.",
        ),
    ],
)
def test_request_body_not_in_its_kinds_form_is_not_an_approval(
    items: trust_stubs.FakeItems, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _on_issue(
        items,
        trust_stubs.comment(10, body, AGENT_ACTOR),
        trust_stubs.comment(11, APPROVE, HUMAN),
    )
    assert verified_approvals(REF, authors=AUTHORS) == []
    assert "request 10" in capsys.readouterr().err


def test_removed_protected_change_request_is_not_an_approval(
    items: trust_stubs.FakeItems,
) -> None:
    """A ``type=protected-change`` request is now a malformed marker, never an approval."""
    legacy = EXEMPTION.replace("type=merge-review-exemption", "type=protected-change")
    _on_issue(
        items,
        trust_stubs.comment(10, legacy, AGENT_ACTOR),
        trust_stubs.comment(11, APPROVE, HUMAN),
        trust_stubs.comment(12, _response(10, 11), AGENT_ACTOR),
    )
    assert verified_approvals(REF, authors=AUTHORS) == []


@pytest.mark.parametrize("answer", [{}, {"repository": None}])
def test_read_failure_raises(items: trust_stubs.FakeItems, answer: dict[str, Any]) -> None:
    items.items[KEY] = answer
    with pytest.raises(trust.TrustReadError):
        verified_approvals(REF, authors=AUTHORS)


def test_gh_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_query: str, **_variables: Any) -> Any:
        raise github.GitHubAPIError(1, ("gh", "api"), None, "HTTP 502")

    monkeypatch.setattr(github, "graphql", boom)
    with pytest.raises(trust.TrustReadError, match="HTTP 502"):
        verified_approvals(REF, authors=AUTHORS)


@pytest.mark.parametrize(
    ("authors", "message"),
    [
        (TrustedAuthors(human=("human",), agents=("human-vergil-audit",)), "not on"),
        (TrustedAuthors(human=(), agents=("human-vergil-user",)), "exactly one"),
    ],
)
def test_misconfigured_logins_refuse(
    items: trust_stubs.FakeItems, authors: TrustedAuthors, message: str
) -> None:
    _on_issue(items)
    with pytest.raises(RulingError, match=message):
        verified_approvals(REF, authors=authors)


def test_bad_issue_ref_refuses() -> None:
    with pytest.raises(RulingError, match="owner/repo#N"):
        verified_approvals("o/r", authors=AUTHORS)
