"""Tests for vergil_tooling.bin.vrg_issue_create."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_issue_create import _render_scaffold_body, main, parse_args
from vergil_tooling.lib import epics, github

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_issue_create"

EPIC = epics.IssueRef("org", "repo", 710)
_URL = "https://github.com/org/repo/issues/123"


@pytest.fixture(autouse=True)
def label_gh() -> Iterator[MagicMock]:
    """Stub the gh boundary under the real ensure_labels (#3373)."""
    with patch("vergil_tooling.lib.labels.github.run") as mock_run:
        yield mock_run


@pytest.fixture(autouse=True)
def queue_kind() -> Iterator[MagicMock]:
    """The resolved epic is a non-deferred epic unless a test says otherwise (#3673)."""
    with patch(f"{_MOD}.epics.queue_kind", return_value=None) as mock_kind:
        yield mock_kind


@pytest.fixture(autouse=True)
def dep_kind() -> Iterator[MagicMock]:
    """Every --blocked-by dependency is a plain task unless a test says otherwise."""
    with patch(f"{_MOD}.epics.non_task_kind", return_value=None) as mock_kind:
        yield mock_kind


@pytest.mark.parametrize("kind", ["epic", "archive"])
@pytest.mark.parametrize("issue_kind", ["task", "validation"])
def test_blocked_by_epic_or_archive_creates_nothing(
    dep_kind: MagicMock, capsys: pytest.CaptureFixture[str], kind: str, issue_kind: str
) -> None:
    # #2842: a dependency names the task this one waits on, never an umbrella.
    dep_kind.side_effect = lambda ref: kind if ref.number == 6 else None
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(
            [
                "--epic",
                "adhoc",
                "--title",
                "T",
                "--kind",
                issue_kind,
                "--blocked-by",
                "org/repo#5",
                "--blocked-by",
                "#6",
            ]
        )
    assert rc == 1
    mock_create.assert_not_called()
    mock_add.assert_not_called()
    assert f"vrg-issue-create: --blocked-by (org/repo#6) is an {kind}, not a task" in (
        capsys.readouterr().err
    )
    assert [c.args[0] for c in dep_kind.call_args_list] == [
        epics.IssueRef("org", "repo", 5),
        epics.IssueRef("org", "repo", 6),
    ]


def test_blocked_by_task_deps_are_checked_and_pass(dep_kind: MagicMock) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--blocked-by", "org/repo#5"])
    assert rc == 0
    dep_kind.assert_called_once_with(epics.IssueRef("org", "repo", 5))
    mock_create.assert_called_once()


def test_blocked_by_lookup_failure_creates_nothing(dep_kind: MagicMock) -> None:
    # Fail closed: an unreadable dependency is never stamped as though a task.
    dep_kind.side_effect = subprocess.CalledProcessError(1, ["gh"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        main(["--epic", "adhoc", "--title", "T", "--blocked-by", "org/repo#5"])
    mock_create.assert_not_called()


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
    # Steps the agent identity cannot run are marked human-run up front (#4176).
    assert "## Human-run steps" in body
    # The check for an apply is the human's apply exiting 0; an agent's diff is
    # informational only and its exit 2 is expected (#4327).
    prose = " ".join(body.split())
    assert "the check is the human's `vrg-github-repo-config apply` exiting 0" in prose
    assert "`vrg-github-repo-config diff` the agent runs afterwards is informational only" in prose
    assert "Never treat `diff`'s exit code as the verdict on an apply." in prose
    assert "verify with:" not in prose
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


def test_kind_validation_rejects_body_file_text_outside_a_section(tmp_path: Path) -> None:
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


# Operational body files (#4348): the kind's scaffold keeps its frame and a
# sectioned --body-file fills its Intro, Preconditions, Procedure, Acceptance
# and (deployment only) Human-run steps slots.
_FIXTURES = Path(__file__).parent / "fixtures" / "issue_create"

_SECTION_TEXT = {
    "Intro": "Release the fixture and confirm it contains the change.",
    "Preconditions": "- the change task org/repo#4 is closed (probe: `vrg-gh issue view 4`)",
    "Procedure": "```bash\n## not a heading inside a fence\nvrg-gh release view v1.2.3\n```",
    "Acceptance": "- release v1.2.3 exists and contains org/repo#4's merge",
    "Human-run steps": "- `vrg-release` on the fixture — check: the command exits 0",
}

# The template placeholders a body file replaces, per kind.
_SLOT_PLACEHOLDERS = {
    "validation": [
        "Post-merge validation task.",
        r"\<precondition — probe or human-attested\>",
        r"\<concrete validation command(s)\>",
        r"\<explicit pass/fail condition(s)\>",
    ],
    "deployment": [
        "Deployment task.",
        r"\<precondition — e.g.",
        r"\<install / sync / restart command(s) the agent identity can run\>",
        r"\<how you know the change is deployed and usable\>",
        r"\<command the human runs\>",
    ],
}

_REQUIRED = ["Intro", "Preconditions", "Procedure", "Acceptance"]


def _sectioned(tmp_path: Path, names: list[str], extra: str = "") -> Path:
    body_file = tmp_path / "body.md"
    parts = [f"## {name}\n\n{_SECTION_TEXT.get(name, 'text')}\n" for name in names]
    body_file.write_text("\n".join(parts) + extra, encoding="utf-8")
    return body_file


def _run_kind(kind: str, extra: list[str]) -> tuple[int, MagicMock]:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "adhoc", "--kind", kind, "--title", "T", *extra])
    return rc, mock_create


@pytest.mark.parametrize(
    ("kind", "names"),
    [
        ("validation", _REQUIRED),
        ("deployment", _REQUIRED),
        ("deployment", [*_REQUIRED, "Human-run steps"]),
    ],
)
def test_operational_body_file_fills_slots(kind: str, names: list[str], tmp_path: Path) -> None:
    body_file = _sectioned(tmp_path, names)
    rc, mock_create = _run_kind(kind, ["--body-file", str(body_file)])
    assert rc == 0
    kwargs = mock_create.call_args.kwargs
    assert kwargs["body_file"] is None
    assert kind in kwargs["labels"]
    body = kwargs["body"]
    assert body.startswith(_SECTION_TEXT["Intro"] + "\n")
    for name in names:
        assert _SECTION_TEXT[name] in body
    for placeholder in _SLOT_PLACEHOLDERS[kind]:
        assert placeholder not in body
    # The run-time placeholders the run skill fills stay in the frame.
    assert r"\<which\>" in body
    assert r"- Evidence: \<command output / observations\>" in body
    procedure_heading = (
        "## Commands to run" if kind == "validation" else "## Deploy steps (agent-safe; idempotent)"
    )
    assert f"{procedure_heading}\n\n{_SECTION_TEXT['Procedure']}\n" in body
    assert f"## Acceptance criteria\n\n{_SECTION_TEXT['Acceptance']}\n" in body
    if kind == "deployment":
        if "Human-run steps" in names:
            assert "## Human-run steps (need a human identity)" in body
            assert "Never treat `diff`'s exit code as the verdict on an apply." in body
            assert f"{_SECTION_TEXT['Human-run steps']}\n\n## Acceptance criteria" in body
        else:
            assert "## Human-run steps" not in body
            assert "need a human identity" not in body
            assert f"{_SECTION_TEXT['Procedure']}\n\n## Acceptance criteria" in body


def test_operational_body_file_refuses_human_steps_on_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body_file = _sectioned(tmp_path, [*_REQUIRED, "Human-run steps"])
    rc, mock_create = _run_kind("validation", ["--body-file", str(body_file)])
    assert rc == 1
    mock_create.assert_not_called()
    assert "## Human-run steps" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["validation", "deployment"])
def test_operational_body_file_keeps_frame(kind: str, tmp_path: Path) -> None:
    body_file = _sectioned(tmp_path, _REQUIRED)
    rc, mock_create = _run_kind(
        kind,
        ["--body-file", str(body_file), "--blocked-by", "org/repo#5", "--blocked-by", "org/repo#6"],
    )
    assert rc == 0
    body = mock_create.call_args.kwargs["body"]
    assert "## Preconditions (self-check — run first, do not fabricate)" in body
    assert "Never fabricate a result." in body
    assert "## Dependencies (merge-first)" in body
    assert "Blocked-by: org/repo#5" in body
    assert "Blocked-by: org/repo#6" in body
    assert "## Results" in body
    assert "- Outcome: SUCCESS / FAILURE" in body


def test_render_scaffold_body_refuses_sections_missing_a_required_slot() -> None:
    with pytest.raises(ValueError, match="'acceptance'"):
        _render_scaffold_body(
            kind="validation",
            intro="",
            deps=[],
            sections={"Intro": "i", "Preconditions": "p", "Procedure": "c"},
        )


def test_operational_body_file_heading_inside_fence_is_content(tmp_path: Path) -> None:
    # A '## ' line inside a fence is content, and a fence closes only on its own
    # marker: a ``` line inside a ~~~ fence leaves it open.
    procedure = "~~~markdown\n```\n## Acceptance\n```\n~~~"
    body_file = tmp_path / "body.md"
    body_file.write_text(
        "## Intro\n\ni\n\n## Preconditions\n\np\n\n"
        f"## Procedure\n\n{procedure}\n\n## Acceptance\n\na\n",
        encoding="utf-8",
    )
    rc, mock_create = _run_kind("validation", ["--body-file", str(body_file)])
    assert rc == 0
    body = mock_create.call_args.kwargs["body"]
    assert f"## Commands to run\n\n{procedure}\n\n## Acceptance criteria\n\na\n" in body


@pytest.mark.parametrize(
    ("names", "extra", "named"),
    [
        (["Intro", "Procedure", "Acceptance"], "", "## Preconditions"),
        ([*_REQUIRED, "Other"], "", "## Other"),
        (_REQUIRED, "\n## Procedure\n\nagain\n", "## Procedure"),
        (["Intro", "Preconditions", "Procedure"], "\n## Acceptance\n\n   \n", "## Acceptance"),
    ],
    ids=["missing", "unknown", "duplicate", "empty"],
)
@pytest.mark.parametrize("kind", ["validation", "deployment"])
def test_operational_body_file_rejects_missing_or_extra_section(
    kind: str,
    names: list[str],
    extra: str,
    named: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_file = _sectioned(tmp_path, names, extra)
    rc, mock_create = _run_kind(kind, ["--body-file", str(body_file)])
    assert rc == 1
    mock_create.assert_not_called()
    assert named in capsys.readouterr().err


def test_operational_body_file_rejects_text_before_first_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body_file = _sectioned(tmp_path, _REQUIRED)
    body_file.write_text("stray\n\n" + body_file.read_text(encoding="utf-8"), encoding="utf-8")
    rc, mock_create = _run_kind("deployment", ["--body-file", str(body_file)])
    assert rc == 1
    mock_create.assert_not_called()
    assert "before the first section" in capsys.readouterr().err


def test_operational_body_file_reports_unreadable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, mock_create = _run_kind("deployment", ["--body-file", str(tmp_path / "nope.md")])
    assert rc == 1
    mock_create.assert_not_called()
    assert "cannot read --body-file" in capsys.readouterr().err


def test_operational_body_file_refuses_body_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body_file = _sectioned(tmp_path, _REQUIRED)
    rc, mock_create = _run_kind("validation", ["--body-file", str(body_file), "--body", "intro"])
    assert rc == 1
    mock_create.assert_not_called()
    assert "--body" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["validation", "deployment"])
def test_operational_without_body_file_unchanged(kind: str) -> None:
    rc, mock_create = _run_kind(kind, ["--blocked-by", "org/repo#5"])
    assert rc == 0
    expected = (_FIXTURES / f"{kind}_scaffold.md").read_text(encoding="utf-8")
    assert mock_create.call_args.kwargs["body"] == expected


def test_retrospective_body_file_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body_file = _sectioned(tmp_path, _REQUIRED)
    rc, mock_create = _run_kind("retrospective", ["--body-file", str(body_file)])
    assert rc == 1
    mock_create.assert_not_called()
    assert "not compatible with --kind retrospective" in capsys.readouterr().err


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


def test_main_unconfirmed_standing_epic_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A standing epic created but not confirmed is a clean refusal, not a traceback.
    message = "created ad-hoc epic org/.github#10 but it is still not listed in org/.github"
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.resolve_epic_ref",
            side_effect=epics.StandingEpicNotVisibleError(message),
        ),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    assert capsys.readouterr().err == f"vrg-issue-create: {message}\n"
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
    assert _URL in err  # orphan-safe: the created issue is surfaced
    # The fix names both refs in full, single-quoted for the shell (#3966).
    assert err.rstrip().endswith(
        "Link it with: vrg-epic-move --task 'org/repo#123' --epic 'org/repo#710'"
    )


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


def _personal_account_probe(dotgithub_error: str) -> object:
    """A visibility probe for a private personal repo whose owner's .github fails."""

    def is_public(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, ["gh", "repo", "view"], "", dotgithub_error)
        return False

    return is_public


def test_main_personal_repo_without_dotgithub_self_homes() -> None:
    """A private repo whose owner has no .github links under its own epic (#3603)."""
    probe = _personal_account_probe("GraphQL: Could not resolve to a Repository")
    with (
        patch("vergil_tooling.lib.github.is_public", side_effect=probe),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch(
            f"{_MOD}.github.create_issue", return_value="https://github.com/owner/notes/issues/9"
        ),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--repo", "owner/notes", "--epic", "#4", "--title", "T"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("owner", "notes", 4), epics.IssueRef("owner", "notes", 9)
    )


def test_main_public_repo_without_dotgithub_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """A public repo whose owner has no .github fails up front, creating nothing (#3643)."""

    def is_public(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(
                1, ["gh", "repo", "view"], "", "GraphQL: Could not resolve to a Repository"
            )
        return True

    with (
        patch("vergil_tooling.lib.github.is_public", side_effect=is_public),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--repo", "owner/site", "--epic", "#4", "--title", "T"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "vrg-issue-create: owner/site is public, so its epics and queues live in "
        "owner/.github, which does not exist; create the owner/.github repository "
        "and try again\n"
    )
    mock_create.assert_not_called()


def test_main_epic_home_api_error_is_a_clean_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Any other probe failure exits 1 with a one-line message, creating nothing."""
    probe = _personal_account_probe("HTTP 502: Bad Gateway")
    with (
        patch("vergil_tooling.lib.github.is_public", side_effect=probe),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--repo", "owner/notes", "--epic", "#4", "--title", "T"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-issue-create: cannot resolve the epic home of owner/notes:")
    assert "Bad Gateway" in err
    assert "Traceback" not in err
    mock_create.assert_not_called()


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


# -- Deferred queue: entered only through vrg-intake-route (#3673) -----------


def test_issue_create_refuses_deferred_epic(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "deferred", "--title", "T"])
    assert rc == 1
    assert "vrg-intake-route" in capsys.readouterr().err
    mock_resolve.assert_not_called()  # the deferred epic is never created
    mock_create.assert_not_called()
    mock_add.assert_not_called()


def test_issue_create_refuses_deferred_epic_number(
    queue_kind: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.return_value = "deferred"
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/repo#710", "--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#710" in err
    queue_kind.assert_called_once_with(EPIC)
    mock_create.assert_not_called()
    mock_add.assert_not_called()
