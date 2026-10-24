"""Tests for vergil_tooling.bin.vrg_issue_create."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_issue_create import main, parse_args
from vergil_tooling.lib import epics, github

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_issue_create"

EPIC = epics.IssueRef("org", "repo", 710)
_URL = "https://github.com/org/repo/issues/123"


@pytest.fixture(autouse=True)
def label_gh() -> Iterator[MagicMock]:
    """Stub the gh boundary under the real ensure_labels (#3373)."""
    with patch("vergil_tooling.lib.labels.github.run") as mock_run:
        yield mock_run


def test_main_ensures_labels_in_target_repo_before_create(label_gh: MagicMock) -> None:
    # The kind label and caller labels are created-if-absent in the target repo
    # before the issue is created (no 'validation' not found).
    order: list[str] = []
    label_gh.side_effect = lambda *a: order.append(f"label:{a[2]}@{a[4]}")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(
            f"{_MOD}.github.create_issue",
            side_effect=lambda **kw: order.append("create") or _URL,
        ),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--kind", "validation", "--label", "bug"])
    assert rc == 0
    assert order == ["label:bug@org/repo", "label:validation@org/repo", "create"]


def test_main_without_labels_makes_no_label_calls(label_gh: MagicMock) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 0
    label_gh.assert_not_called()


def test_main_unknown_label_creates_nothing(
    label_gh: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "no-such-label"])
    assert rc == 1
    mock_create.assert_not_called()
    mock_add.assert_not_called()
    label_gh.assert_not_called()
    assert "vrg-issue-create: unknown label 'no-such-label'" in capsys.readouterr().err


def test_main_label_create_failure_creates_nothing(
    label_gh: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    label_gh.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: forbidden")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "bug"])
    assert rc == 1
    mock_create.assert_not_called()
    mock_add.assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-issue-create: could not ensure label(s) bug exist in org/repo" in err
    assert "HTTP 403: forbidden" in err


def test_parse_args_requires_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--title", "T"])  # missing --epic


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "adhoc"])  # missing --title


def test_main_creates_issue_and_links_under_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--body", "B", "--label", "bug"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo="org/repo")
    assert mock_create.call_args.kwargs["title"] == "T"
    assert mock_create.call_args.kwargs["labels"] == ["bug"]
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_kind_validation_applies_label_and_scaffold() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--kind",
                "validation",
                "--title",
                "Validate the thing",
                "--blocked-by",
                "org/repo#5",
            ]
        )
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert "validation" in kwargs["labels"]
    body = kwargs["body"]
    assert "Blocked-by: org/repo#5" in body  # machine-parseable reflink present
    assert "## Preconditions" in body
    assert "## Results" in body
    assert kwargs["body_file"] is None


def test_kind_deployment_applies_label_and_scaffold() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--kind",
                "deployment",
                "--title",
                "Deploy the thing",
                "--blocked-by",
                "org/repo#5",
            ]
        )
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert "deployment" in kwargs["labels"]
    body = kwargs["body"]
    assert "Blocked-by: org/repo#5" in body
    assert "## Deploy steps" in body  # deployment-specific scaffold section
    assert "release" in body.lower()  # release-as-precondition boundary
    assert "## Results" in body
    assert kwargs["body_file"] is None


def test_kind_retrospective_applies_label_and_scaffold() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(
            ["--epic", "adhoc", "--kind", "retrospective", "--title", "Retrospective: the epic"]
        )
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert "retrospective" in kwargs["labels"]
    body = kwargs["body"]
    assert "retrospective task" in body.lower()  # scaffold rendered
    assert "## What went well" in body
    assert "PR-workable" in body  # scaffold states it is PR-workable
    assert kwargs["body_file"] is None


def test_kind_retrospective_is_not_operational() -> None:
    # The critical distinction: retrospective is a labelled kind, but unlike
    # validation/deployment it is PR-workable, so PR tooling (vrg-submit-pr /
    # vrg-pr-workflow report-ready) must NOT refuse it. The single source of
    # truth is epics._OPERATIONAL_LABELS — retrospective must be excluded.
    assert "retrospective" not in epics.operational_labels()
    assert "validation" in epics.operational_labels()  # sanity: set is populated


def test_kind_retrospective_rejects_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "b.md"
    body_file.write_text("x")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--kind",
                "retrospective",
                "--title",
                "R",
                "--body-file",
                str(body_file),
            ]
        )
    assert rc == 1
    mock_create.assert_not_called()


def test_kind_retrospective_rejects_blocked_by() -> None:
    # --blocked-by encodes an operational merge-first dependency; a retrospective
    # is an ordinary PR-workable task and must not accept it.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--kind",
                "retrospective",
                "--title",
                "R",
                "--blocked-by",
                "org/repo#5",
            ]
        )
    assert rc == 1
    mock_create.assert_not_called()


def test_kind_validation_without_blockers_has_no_dependency_reflink() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--kind", "validation", "--title", "V"])
    assert rc == 0
    body = mock_create.call_args.kwargs["body"]
    assert "Blocked-by:" not in body
    assert "## Preconditions" in body


def test_kind_validation_rejects_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "b.md"
    body_file.write_text("x")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--kind",
                "validation",
                "--title",
                "V",
                "--body-file",
                str(body_file),
            ]
        )
    assert rc == 1
    mock_create.assert_not_called()


def test_kind_validation_rejects_invalid_blocked_by() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(
            ["--epic", "adhoc", "--kind", "validation", "--title", "V", "--blocked-by", "not-a-ref"]
        )
    assert rc == 1
    mock_create.assert_not_called()


def test_kind_defaults_to_task_without_validation_label() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "bug"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["bug"]  # no validation label added


def test_plain_task_stamps_blocked_by_reflink_from_body() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--title",
                "T",
                "--body",
                "Do the thing.",
                "--blocked-by",
                "org/repo#5",
                "--blocked-by",
                "org/repo#6",
            ]
        )
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert kwargs["body_file"] is None  # inlined so the appended reflinks survive
    body = kwargs["body"]
    assert body.startswith("Do the thing.")  # caller's body preserved
    assert "Blocked-by: org/repo#5" in body
    assert "Blocked-by: org/repo#6" in body


def test_plain_task_blocked_by_with_empty_body_is_just_the_section() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--blocked-by", "org/repo#5"])
    assert rc == 0
    body = mock_create.call_args.kwargs["body"]
    assert body.startswith("## Dependencies")
    assert "Blocked-by: org/repo#5" in body


def test_plain_task_blocked_by_appends_to_body_file(tmp_path: Path) -> None:
    body_file = tmp_path / "b.md"
    body_file.write_text("From a file.\n")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--title",
                "T",
                "--body-file",
                str(body_file),
                "--blocked-by",
                "org/repo#5",
            ]
        )
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert kwargs["body_file"] is None  # file contents were read and inlined
    body = kwargs["body"]
    assert body.startswith("From a file.")
    assert "Blocked-by: org/repo#5" in body


def test_plain_task_without_blocked_by_leaves_body_file_passthrough(tmp_path: Path) -> None:
    body_file = tmp_path / "b.md"
    body_file.write_text("From a file.\n")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--body-file", str(body_file)])
    assert rc == 0
    # No --blocked-by: the file is handed straight to gh, unchanged.
    assert mock_create.call_args.kwargs["body_file"] == str(body_file)


def test_plain_task_rejects_invalid_blocked_by() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--blocked-by", "not-a-ref"])
    assert rc == 1
    mock_create.assert_not_called()


def test_plain_task_blocked_by_reports_unreadable_body_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.md"
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--title",
                "T",
                "--body-file",
                str(missing),
                "--blocked-by",
                "org/repo#5",
            ]
        )
    assert rc == 1
    mock_create.assert_not_called()
    assert "cannot read --body-file" in capsys.readouterr().err


def test_main_adhoc_sentinel_skips_cross_org_guard() -> None:
    # 'adhoc' resolves within the repo's org (.github), so it must skip the
    # explicit-ref cross-org guard and link.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo="org/repo")
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_epic_resolution_failure_creates_nothing() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.resolve_epic_ref",
            side_effect=ValueError("multiple ad-hoc epics in org/repo — pass an explicit --epic"),
        ),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


def test_main_link_failure_reports_created_issue(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child", side_effect=RuntimeError("link failed")),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "123" in err  # orphan-safe: the created issue is surfaced


def test_main_scopes_token_to_repo_owner() -> None:
    """Issue creation + linking run under the --repo owner installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.github.current_repo") as mock_cur,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--repo", "other-org/repo", "--epic", "adhoc", "--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")
    # 'adhoc' resolves against --repo, never the cwd repo (#3409).
    mock_resolve.assert_called_once_with("adhoc", repo="other-org/repo")
    mock_cur.assert_not_called()


@pytest.mark.parametrize(
    ("repo", "public", "expected_epic"),
    [
        # Public target: its epics home in <org>/.github.
        ("org/tooling", {"org/tooling", "org/.github"}, epics.IssueRef("org", ".github", 407)),
        # Private target with a public .github: its epics home in the repo itself.
        ("org/lab", {"org/.github"}, epics.IssueRef("org", "lab", 407)),
    ],
)
def test_main_bare_epic_resolves_against_target_epic_home(
    repo: str, public: set[str], expected_epic: epics.IssueRef
) -> None:
    """A bare '--epic #N' reads against --repo's epic home, not the cwd repo (#3411).

    The visibility probes run under the --repo owner's App token.
    """
    seen_orgs: list[str | None] = []

    def is_public(nwo: str) -> bool:
        seen_orgs.append(github._target_org.get())
        return nwo in public

    owner, name = repo.split("/", 1)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.is_public", side_effect=is_public),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch(f"{_MOD}.github.create_issue", return_value=f"https://github.com/{repo}/issues/9"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--repo", repo, "--epic", "#407", "--title", "T"])
    assert rc == 0
    mock_add.assert_called_once_with(expected_epic, epics.IssueRef(owner, name, 9))
    assert seen_orgs
    assert set(seen_orgs) == {"org"}


def test_main_explicit_same_org_epic_proceeds() -> None:
    """An explicit epic ref in the issue's own org passes the guard and links."""
    with (
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--repo", "org/repo", "--epic", "org/.github#5", "--title", "T"])
    assert rc == 0
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_rejects_bad_epic_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "not-a-ref", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


def test_main_rejects_cross_org_epic() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--repo", "org-a/repo", "--epic", "org-b/repo#5", "--title", "T"])
    assert rc == 1
    mock_resolve.assert_not_called()
    mock_create.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.resolve_epic_ref",
            side_effect=github.NoInstallationError("org", []),
        ),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


@pytest.mark.parametrize("sentinel", ["adhoc", "intake", "planning"])
def test_main_accepts_every_standing_sentinel(sentinel: str) -> None:
    """Every standing sentinel passes the cross-org guard and resolves (#3514)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", sentinel, "--title", "T"])
    assert rc == 0
    mock_resolve.assert_called_once_with(sentinel, repo="org/repo")
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_planning_ensures_in_progress_label_in_target_repo(label_gh: MagicMock) -> None:
    """Creating an issue into planning ensures 'in-progress' in its repo (#3520)."""
    planning = epics.IssueRef("org", ".github", 439)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._find_epic_by_title", return_value=planning),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "planning", "--title", "T"])
    assert rc == 0
    ensured = [(c.args[2], c.args[4]) for c in label_gh.call_args_list]
    assert ("in-progress", "org/.github") in ensured
    assert ("in-progress", "org/repo") in ensured
    mock_add.assert_called_once_with(planning, epics.IssueRef("org", "repo", 123))


def test_epic_help_names_every_standing_sentinel(capsys: pytest.CaptureFixture[str]) -> None:
    """The --epic help lists adhoc, intake and planning (#3512)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for sentinel in ("adhoc", "intake", "planning"):
        assert f"'{sentinel}'" in out
