"""Tests for vergil_tooling.bin.vrg_intake_create."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_intake_create import main, parse_args
from vergil_tooling.lib import github
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_intake_create"
_URL = "https://github.com/org/.github/issues/321"
_INTAKE = IssueRef(owner="org", repo=".github", number=7)
_RETIRED_KINDS = {"triage", "idea", "research"}


@pytest.fixture(autouse=True)
def label_gh() -> Iterator[MagicMock]:
    """Stub the gh boundary under the real ensure_labels (#3373)."""
    with patch("vergil_tooling.lib.labels.github.run") as mock_run:
        yield mock_run


@pytest.fixture
def cwd_public() -> Iterator[dict[str, MagicMock]]:
    """A cwd in a public repo of ``org``: its queue home is ``org/.github``."""
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org") as detect,
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling") as current,
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")) as home,
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=_INTAKE) as ensure,
        patch(f"{_MOD}.epics.add_child") as add_child,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as create,
    ):
        yield {
            "detect": detect,
            "current": current,
            "home": home,
            "ensure": ensure,
            "add_child": add_child,
            "create": create,
        }


def test_intake_create_files_in_org_dotgithub_and_links_under_intake(
    cwd_public: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--title", "T", "--body", "B"])
    assert rc == 0
    cwd_public["home"].assert_called_once_with("org/tooling")
    create = cwd_public["create"]
    assert create.call_args.kwargs["repo"] == "org/.github"
    assert create.call_args.kwargs["labels"] == []
    assert create.call_args.kwargs["title"] == "T"
    assert create.call_args.kwargs["body"] == "B"
    cwd_public["ensure"].assert_called_once_with("org/.github")
    cwd_public["add_child"].assert_called_once_with(
        _INTAKE, IssueRef(owner="org", repo=".github", number=321)
    )
    assert f"Created {_URL} (intake, under {_INTAKE.slug})." in capsys.readouterr().out


def test_intake_create_private_repo_files_in_repo(capsys: pytest.CaptureFixture[str]) -> None:
    # A private repo self-homes: the capture lands in the repo itself and is
    # linked under that repo's own intake epic ("Epic (intake): secret"),
    # never in .github (Review Focus 2).
    secret_intake = IssueRef(owner="org", repo="secret", number=4)
    url = "https://github.com/org/secret/issues/12"
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.current_repo", return_value="org/secret"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/secret", "secret")),
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=secret_intake) as ensure,
        patch(f"{_MOD}.epics.add_child") as add_child,
        patch(f"{_MOD}.github.create_issue", return_value=url) as create,
    ):
        rc = main(["--title", "T"])
    assert rc == 0
    assert create.call_args.kwargs["repo"] == "org/secret"
    ensure.assert_called_once_with("org/secret")
    add_child.assert_called_once_with(
        secret_intake, IssueRef(owner="org", repo="secret", number=12)
    )
    assert "under org/secret#4" in capsys.readouterr().out


def test_intake_create_private_repo_links_under_the_named_intake_epic() -> None:
    # End to end through the real queue_home/ensure_intake_epic: the private
    # repo's standing epic is looked up as "Epic (intake): secret" in itself.
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.current_repo", return_value="org/secret"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/secret"),
        patch(
            "vergil_tooling.lib.epics._find_epic_by_title",
            return_value=IssueRef(owner="org", repo="secret", number=4),
        ) as find,
        patch(f"{_MOD}.epics.add_child"),
        patch(
            f"{_MOD}.github.create_issue",
            return_value="https://github.com/org/secret/issues/12",
        ),
    ):
        rc = main(["--title", "T"])
    assert rc == 0
    assert find.call_args.args[:2] == ("org/secret", "Epic (intake): secret")


def test_intake_create_applies_no_kind_label(cwd_public: dict[str, MagicMock]) -> None:
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 0
    labels = cwd_public["create"].call_args.kwargs["labels"]
    assert labels == ["bug"]
    assert not _RETIRED_KINDS & set(labels)


def test_intake_create_rejects_kind_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--title", "T", "--kind", "idea"])


def test_intake_create_link_failure_is_loud(
    cwd_public: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    cwd_public["add_child"].side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502: boom")
    rc = main(["--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert _URL in err
    assert "not linked" in err
    assert _INTAKE.slug in err
    assert "HTTP 502: boom" in err


def test_intake_epic_failure_creates_nothing(
    cwd_public: dict[str, MagicMock],
) -> None:
    # The intake epic is resolved before the issue is created, so a failure
    # there raises without leaving an unlinked capture behind.
    cwd_public["ensure"].side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 500")
    with pytest.raises(github.GitHubAPIError):
        main(["--title", "T"])
    cwd_public["create"].assert_not_called()


def test_main_ensures_extra_labels_in_target_repo_before_create(
    label_gh: MagicMock, cwd_public: dict[str, MagicMock]
) -> None:
    order: list[str] = []
    label_gh.side_effect = lambda *a: order.append(f"label:{a[2]}@{a[4]}")
    cwd_public["create"].side_effect = lambda **kw: order.append("create") or _URL
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 0
    assert order == ["label:bug@org/.github", "create"]


def test_main_no_labels_ensures_none(label_gh: MagicMock, cwd_public: dict[str, MagicMock]) -> None:
    assert main(["--title", "T"]) == 0
    label_gh.assert_not_called()


def test_main_unknown_label_creates_nothing(
    label_gh: MagicMock, cwd_public: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--title", "T", "--label", "no-such-label"])
    assert rc == 1
    cwd_public["create"].assert_not_called()
    label_gh.assert_not_called()
    assert "vrg-intake-create: unknown label 'no-such-label'" in capsys.readouterr().err


def test_main_label_create_failure_creates_nothing(
    label_gh: MagicMock, cwd_public: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    label_gh.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: forbidden")
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 1
    cwd_public["create"].assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-intake-create: could not ensure label(s) bug exist in org/.github" in err
    assert "HTTP 403: forbidden" in err


def test_main_dedupes_extra_labels(cwd_public: dict[str, MagicMock]) -> None:
    main(["--title", "T", "--label", "bug", "--label", "bug"])
    assert cwd_public["create"].call_args.kwargs["labels"] == ["bug"]


def test_main_passes_body_file_and_assignees(cwd_public: dict[str, MagicMock]) -> None:
    main(["--title", "T", "--body-file", "b.md", "--assignee", "me"])
    kwargs = cwd_public["create"].call_args.kwargs
    assert kwargs["body_file"] == "b.md"
    assert kwargs["assignees"] == ["me"]


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_scopes_token_to_repo_owner() -> None:
    """Issue creation runs under the --repo owner's App installation (#2722)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(
            f"{_MOD}.epics.ensure_intake_epic",
            return_value=IssueRef(owner="other-org", repo=".github", number=1),
        ),
        patch(f"{_MOD}.epics.add_child"),
        patch(
            f"{_MOD}.github.create_issue",
            return_value="https://github.com/other-org/repo/issues/5",
        ),
    ):
        rc = main(["--title", "T", "--repo", "other-org/repo"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_scopes_token_to_detected_org(cwd_public: dict[str, MagicMock]) -> None:
    """The default route is scoped to the cwd org's token (#2722)."""
    with patch(f"{_MOD}.github.target_org") as mock_scope:
        rc = main(["--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("org")


def test_main_repo_override_files_there_and_links_under_its_intake() -> None:
    tooling_url = "https://github.com/org/tooling/issues/9"
    with (
        patch(f"{_MOD}.github.detect_org") as mock_detect,
        patch(f"{_MOD}.github.current_repo") as mock_current,
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=_INTAKE) as ensure,
        patch(f"{_MOD}.epics.add_child") as add_child,
        patch(f"{_MOD}.github.create_issue", return_value=tooling_url) as mock_create,
    ):
        rc = main(["--title", "T", "--repo", "org/tooling"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/tooling"
    ensure.assert_called_once_with("org/tooling")
    add_child.assert_called_once_with(_INTAKE, IssueRef(owner="org", repo="tooling", number=9))
    mock_detect.assert_not_called()
    mock_current.assert_not_called()


def test_main_repo_without_owner_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.create_issue") as mock_create:
        rc = main(["--title", "T", "--repo", "tooling"])
    assert rc == 1
    mock_create.assert_not_called()
    assert "vrg-intake-create: --repo must be owner/name" in capsys.readouterr().err


def test_main_undetectable_org_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value=None),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
    assert "could not determine the GitHub org" in capsys.readouterr().err


def test_main_no_installation_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.current_repo", side_effect=github.NoInstallationError("org", [])),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
    assert capsys.readouterr().err.startswith("vrg-intake-create: ")
