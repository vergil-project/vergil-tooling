"""Tests for vergil_tooling.lib.operational_close."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import epics, github, operational_close, trust

_REF = epics.IssueRef("org", "repo", 7)


def _stub(
    monkeypatch: pytest.MonkeyPatch, kind: str | None, bodies: list[str] | Exception
) -> list[str | None]:
    """Stub the label and trusted-comment reads; return the orgs each read ran under."""
    orgs: list[str | None] = []

    def operational_kind(ref: epics.IssueRef) -> str | None:
        assert ref == _REF
        orgs.append(github._target_org.get())
        return kind

    def trusted_bodies(ref: epics.IssueRef) -> list[str]:
        assert ref == _REF
        orgs.append(github._target_org.get())
        if isinstance(bodies, Exception):
            raise bodies
        return bodies

    monkeypatch.setattr(operational_close.epics, "operational_kind", operational_kind)
    monkeypatch.setattr(operational_close.trusted_comments, "bodies", trusted_bodies)
    return orgs


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("- Outcome: SUCCESS", True),
        ("ran it\nOutcome: success\n", True),
        ("* Outcome: PASS", True),
        ("- Outcome: SUCCESS / FAILURE", False),
        ("- Outcome: FAILURE", False),
        ("Deployed: see the SUCCESS result above.", False),
    ],
)
def test_records_success(body: str, expected: bool) -> None:
    assert operational_close.records_success(["first", body]) is expected


def test_non_operational_issue_may_close(monkeypatch: pytest.MonkeyPatch) -> None:
    orgs = _stub(monkeypatch, None, AssertionError("comments must not be read"))
    assert operational_close.refusal(_REF) is None
    assert orgs == ["org"]


def test_operational_task_with_success_may_close(monkeypatch: pytest.MonkeyPatch) -> None:
    orgs = _stub(monkeypatch, "deployment", ["started", "- Outcome: SUCCESS"])
    assert operational_close.refusal(_REF) is None
    assert orgs == ["org", "org"]


@pytest.mark.parametrize("bodies", [[], ["- Outcome: FAILURE"], ["- Outcome: SUCCESS / FAILURE"]])
def test_operational_task_without_success_is_refused(
    monkeypatch: pytest.MonkeyPatch, bodies: list[str]
) -> None:
    _stub(monkeypatch, "validation", bodies)
    message = operational_close.refusal(_REF)
    assert message is not None
    assert "org/repo#7 is a validation task" in message
    assert "Outcome: SUCCESS" in message


def test_failed_comment_read_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "validation", trust.TrustReadError("test", "read failed"))
    with pytest.raises(trust.TrustReadError):
        operational_close.refusal(_REF)
