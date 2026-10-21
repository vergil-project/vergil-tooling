"""Tests for vergil_tooling.bin.vrg_triage_create."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_triage_create import main, parse_args
from vergil_tooling.lib import github

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_triage_create"
_URL = "https://github.com/org/.github/issues/321"


@pytest.fixture(autouse=True)
def label_gh() -> Iterator[MagicMock]:
    """Stub the gh boundary under the real ensure_labels (#3373)."""
    with patch("vergil_tooling.lib.labels.github.run") as mock_run:
        yield mock_run


def test_main_ensures_labels_in_target_repo_before_create(label_gh: MagicMock) -> None:
    # Every applied label is created-if-absent in the target repo first, so a
    # repo that was never label-synced does not fail with 'triage' not found.
    order: list[str] = []
    label_gh.side_effect = lambda *a: order.append(f"label:{a[2]}@{a[4]}")
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(
            f"{_MOD}.github.create_issue",
            side_effect=lambda **kw: order.append("create") or _URL,
        ),
    ):
        rc = main(["--title", "T", "--label", "bug"])
    assert rc == 0
    assert order == ["label:triage@org/.github", "label:bug@org/.github", "create"]


def test_main_unknown_label_creates_nothing(
    label_gh: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T", "--label", "no-such-label"])
    assert rc == 1
    mock_create.assert_not_called()
    label_gh.assert_not_called()
    assert "vrg-triage-create: unknown label 'no-such-label'" in capsys.readouterr().err


def test_main_label_create_failure_creates_nothing(
    label_gh: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    label_gh.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: forbidden")
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-triage-create: could not ensure label(s) triage exist in org/.github" in err
    assert "HTTP 403: forbidden" in err


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_defaults_to_org_dotgithub() -> None:
    # Intake defaults to <org>/.github (supersedes the old current-repo default,
    # #2075) so the whole org-wide intake queue lives in one place.
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--body", "B"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["labels"] == ["triage"]


def test_main_kind_research() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--kind", "research"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["labels"] == ["research"]


def test_main_kind_idea() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--kind", "idea"])
    assert mock_create.call_args.kwargs["labels"] == ["idea"]


def test_main_scopes_token_to_repo_owner() -> None:
    """Issue creation runs under the --repo owner's App installation (#2722).

    Without target_org(owner) the App token is minted for the cwd org, so a
    cross-org create misroutes or misreports the issue's location.
    """
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--title", "T", "--repo", "other-org/repo"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_scopes_token_to_default_dotgithub_org() -> None:
    """The default <org>/.github route is also scoped to that org's token (#2722)."""
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("org")


def test_main_repo_override_skips_detect_org() -> None:
    with (
        patch(f"{_MOD}.github.detect_org") as mock_detect,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--repo", "org/tooling"])
    assert mock_create.call_args.kwargs["repo"] == "org/tooling"
    mock_detect.assert_not_called()


def test_main_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "bug"])
    assert mock_create.call_args.kwargs["labels"] == ["triage", "bug"]


def test_main_kind_dedupes_with_extra_label() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--kind", "research", "--label", "research"])
    assert mock_create.call_args.kwargs["labels"] == ["research"]


def test_main_undetectable_org_errors() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value=None),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
