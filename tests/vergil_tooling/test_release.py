"""Tests for vergil_tooling.lib.release."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.release import is_release_branch, is_release_tracking_issue

if TYPE_CHECKING:
    from tests.vergil_tooling.trust_stubs import FakeItems


@pytest.mark.parametrize(
    "branch",
    [
        "release/1.4.9",
        "release/2.0.0",
        "release/0.1.0",
        "release/bump-version-1.4.10",
        "release/bump-version-0.1.1",
        "release/42-next-cycle-deps-1.4.10",
        "release/99-next-cycle-deps-2.0.1",
        "chore/bump-version-1.4.10",
        "chore/bump-version-0.1.1",
        "chore/42-next-cycle-deps-1.4.10",
        "chore/99-next-cycle-deps-2.0.1",
    ],
)
def test_release_branch_allowed(branch: str) -> None:
    assert is_release_branch(branch) is True


@pytest.mark.parametrize(
    "branch",
    [
        "feature/42-foo",
        "bugfix/99-bar",
        "chore/update-deps",
        "hotfix/critical",
        "main",
        "develop",
        "release",
        "",
    ],
)
def test_non_release_branch_denied(branch: str) -> None:
    assert is_release_branch(branch) is False


_MARKER = "<!-- vrg-release:progress -->"
_REPO = "org/repo"


@pytest.fixture(autouse=True)
def layer(monkeypatch: pytest.MonkeyPatch) -> FakeItems:
    """The trust layer's reads, strict: a test names every issue it expects read."""
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    return fake


def test_release_issue_detected_by_body_marker(layer: FakeItems) -> None:
    # The checklist marker is authoritative even if the title was hand-edited.
    body = f"intro\n{_MARKER}\n- [ ] x"
    layer.items[(_REPO, 5)] = trust_stubs.item(body=body)
    assert is_release_tracking_issue(_REPO, 5, title="anything at all", body=body)
    assert layer.reads == [(_REPO, 5)]


def test_release_issue_detected_by_title_when_body_absent(layer: FakeItems) -> None:
    # Secondary signal: a caller with only the title still classifies it.
    layer.items[(_REPO, 5)] = trust_stubs.item(body="", author=trust_stubs.AGENT)
    assert is_release_tracking_issue(_REPO, 5, title="release: 2.1.4", body=None)
    assert is_release_tracking_issue(_REPO, 5, title="release:   0.10.0", body="")


def test_marker_counts_only_in_the_trusted_body(layer: FakeItems) -> None:
    # The caller's copy carried the marker; the body the layer trusts does not.
    layer.items[(_REPO, 5)] = trust_stubs.item(body="no marker now")
    assert not is_release_tracking_issue(_REPO, 5, title="x", body=_MARKER)


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login", "cause", "at"),
    [
        (trust_stubs.OUTSIDER, None, "issue", "drive-by", "untrusted", trust_stubs.CREATED),
        (
            trust_stubs.HUMAN,
            trust_stubs.OUTSIDER,
            "edit",
            "drive-by",
            "untrusted",
            trust_stubs.EDITED,
        ),
        (trust_stubs.IMPOSTOR, None, "issue", "vergil-user", "wrong-type", trust_stubs.CREATED),
        (None, None, "issue", "a deleted account", "deleted", trust_stubs.CREATED),
    ],
)
@pytest.mark.parametrize(
    ("title", "marker"), [("release: 2.1.4", False), ("anything", True), ("release: 2.1.4", True)]
)
def test_release_marker_untrusted_body_not_tracking(
    layer: FakeItems,
    capsys: pytest.CaptureFixture[str],
    author: dict[str, str] | None,
    editor: dict[str, str] | None,
    kind: str,
    login: str,
    cause: str,
    at: str,
    title: str,
    marker: bool,
) -> None:
    # A forged title or marker in a body an outsider wrote or edited is not release bookkeeping.
    body = f"{trust_stubs.SPAM_TEXT}\n{_MARKER if marker else ''}\n"
    layer.items[(_REPO, 5)] = trust_stubs.item(
        body=body,
        author=author,
        editor=editor,
        nodes=[trust_stubs.comment(9, trust_stubs.SPAM_TEXT, trust_stubs.OUTSIDER)],
    )
    assert is_release_tracking_issue(_REPO, 5, title=title, body=body) is False
    err = capsys.readouterr().err
    # Only the body is reported, metadata only: the comment is not this reader's.
    assert err == f"skipped untrusted content: {_REPO}#5 {kind} {login} at {at} on body ({cause})\n"
    assert "SPAM-MARKER-TEXT" not in err


def test_untrusted_body_reported_as_counts_under_actions(
    layer: FakeItems, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    layer.items[(_REPO, 5)] = trust_stubs.item(body=_MARKER, author=trust_stubs.OUTSIDER)
    assert not is_release_tracking_issue(_REPO, 5, title="x", body=_MARKER)
    assert capsys.readouterr().err == "skipped 1 untrusted item(s) (untrusted: 1)\n"


def test_release_marker_read_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail loudly: an unreadable body is never "not a tracking issue".
    def fail(_query: str, **_variables: object) -> object:
        raise github.GitHubAPIError(1, "gh api graphql", stderr="HTTP 502")

    monkeypatch.setattr(github, "graphql", fail)
    with pytest.raises(trust.TrustReadError, match="HTTP 502"):
        is_release_tracking_issue(_REPO, 5, title="release: 2.1.4", body="")


@pytest.mark.parametrize(
    "title",
    [
        "feat(release): overhaul the release pipeline",
        "release: overhaul the pipeline",  # no version -> not a tracking issue
        "chore: release notes",
        "fix(reporting): exclude release-tracking issues",
        "",
    ],
)
def test_non_release_titles_are_not_tracking_issues(layer: FakeItems, title: str) -> None:
    assert is_release_tracking_issue(_REPO, 5, title=title, body="") is False
    # Neither signal matched, so nothing was read.
    assert layer.reads == []


def test_no_signals_is_false(layer: FakeItems) -> None:
    assert is_release_tracking_issue(_REPO, 5) is False
    assert is_release_tracking_issue(_REPO, 5, title=None, body=None) is False
    assert layer.reads == []
