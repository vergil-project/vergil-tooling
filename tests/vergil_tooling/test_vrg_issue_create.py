"""Tests for vergil_tooling.bin.vrg_issue_create."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_issue_create import _render_scaffold_body, main, parse_args
from vergil_tooling.lib import epic_chain, epics, github

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_issue_create"

EPIC = epics.IssueRef("org", "repo", 710)
TASK = epics.IssueRef("org", "repo", 123)
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
def graph(label_gh: MagicMock, monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """The sub-issue graph the chain layer reads and links in (.github#1609).

    It holds the open epic ``EPIC`` and the issue ``TASK`` a patched
    ``create_issue`` reports (``_URL``). ``github.run`` stays ``label_gh``,
    so the label tests see the label writes; the chain layer's plain link
    makes no ``github.run`` call.
    """
    fake = FakeSubIssueGraph().install(monkeypatch)
    monkeypatch.setattr(github, "run", label_gh)
    fake.add_issue(EPIC, title="Epic", labels=["epic"])
    fake.add_issue(TASK, title="T")
    return fake


def _assert_linked(
    graph: FakeSubIssueGraph, container: epics.IssueRef, task: epics.IssueRef
) -> None:
    """*task* is linked under *container*, by one link and no other write."""
    assert graph.parent_of(task) == container
    assert graph.mutations == [("add", container, task)]


@pytest.fixture(autouse=True)
def dep_kind() -> Iterator[MagicMock]:
    """Every --blocked-by dependency is a plain task unless a test says otherwise."""
    with patch(f"{_MOD}.epics.non_task_kind", return_value=None) as mock_kind:
        yield mock_kind


@pytest.mark.parametrize("kind", ["epic", "archive"])
@pytest.mark.parametrize("issue_kind", ["task", "validation"])
def test_blocked_by_epic_or_archive_creates_nothing(
    dep_kind: MagicMock,
    graph: FakeSubIssueGraph,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    issue_kind: str,
) -> None:
    # #2842: a dependency names the task this one waits on, never an umbrella.
    dep_kind.side_effect = lambda ref: kind if ref.number == 6 else None
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
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
    assert graph.mutations == []
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
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--kind", "validation", "--label", "bug"])
    assert rc == 0
    assert order == ["label:bug@org/repo", "label:validation@org/repo", "create"]


def test_main_without_labels_makes_no_label_calls(label_gh: MagicMock) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 0
    label_gh.assert_not_called()


def test_main_unknown_label_creates_nothing(
    label_gh: MagicMock, graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "no-such-label"])
    assert rc == 1
    mock_create.assert_not_called()
    assert graph.mutations == []
    label_gh.assert_not_called()
    assert "vrg-issue-create: unknown label 'no-such-label'" in capsys.readouterr().err


def test_main_label_create_failure_creates_nothing(
    label_gh: MagicMock, graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    label_gh.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: forbidden")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "bug"])
    assert rc == 1
    mock_create.assert_not_called()
    assert graph.mutations == []
    err = capsys.readouterr().err
    assert "vrg-issue-create: could not ensure label(s) bug exist in org/repo" in err
    assert "HTTP 403: forbidden" in err


def test_parse_args_requires_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--title", "T"])  # missing --epic


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "adhoc"])  # missing --title


def test_issue_create_closed_epic_refused_before_create(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # Closed is closed (.github#1609): the chain layer's capacity check refuses
    # it before the issue is created, so the refused link never orphans a new issue.
    graph.close(EPIC)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(
            f"{_MOD}.epic_chain.check_capacity", wraps=epic_chain.check_capacity
        ) as mock_capacity,
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "#710", "--title", "T"])
    assert rc == 1
    mock_capacity.assert_called_once_with(EPIC)
    mock_create.assert_not_called()
    assert graph.mutations == []
    assert graph.events == []
    assert capsys.readouterr().err == (
        "vrg-issue-create: epic org/repo#710 is closed; closed epics are not reopened "
        "by the tooling\n"
    )


def _deepen(graph: FakeSubIssueGraph, head: epics.IssueRef) -> None:
    """Put *head* at GitHub's nesting limit: seven ancestors above it."""
    above = [epics.IssueRef("org", "repo", 800 + i) for i in range(1, 8)]
    for ref in above:
        graph.add_issue(ref, title=f"Level {ref.number - 799}", labels=["epic"])
    for parent, child in zip(above, [*above[1:], head], strict=True):
        graph.link(parent, child)
    graph.clear_log()


def test_issue_create_depth_refusal_creates_nothing(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # An epic at the nesting limit has no level left for its items (.github#1609):
    # refused by the capacity check before the issue is created.
    _deepen(graph, EPIC)
    before = set(graph.issues)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch.object(github, "create_issue", wraps=graph.create_issue) as mock_create,
    ):
        rc = main(["--epic", "#710", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
    assert set(graph.issues) == before
    assert graph.events == []
    message = str(epic_chain.ChainDepthError(EPIC, items=0, containers=1))
    assert "nesting limit" in message
    assert capsys.readouterr().err == f"vrg-issue-create: {message}\n"


def _fill(graph: FakeSubIssueGraph, head: epics.IssueRef, count: int) -> None:
    """Link *count* new issues under *head*, numbered from 1."""
    for number in range(1, count + 1):
        ref = graph.add_issue(epics.IssueRef("org", "repo", number), title=f"Item {number}")
        graph.link(head, ref)
    graph.clear_log()


def test_issue_create_lands_in_continuation_names_it(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A head holding its 99 items rolls the new issue into a
    # continuation, and the closing line names the epic by its head and the container.
    monkeypatch.setattr(github, "run", graph.run)  # the rollover's label and announce writes
    _fill(graph, EPIC, 99)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
    ):
        rc = main(["--epic", "#710", "--title", "New"])
    assert rc == 0
    created = [event[1] for event in graph.events if event[0] == "create"]
    assert len(created) == 2  # the task, then its continuation
    task, continuation = created
    assert graph.issues[task].title == "New"
    assert epics.CONTINUATION_LABEL in graph.issues[continuation].labels
    assert graph.parent_of(continuation) == EPIC
    assert graph.parent_of(task) == continuation
    assert len(graph.children_of(EPIC)) == 100
    out = capsys.readouterr().out
    assert out.endswith(
        f"Created https://github.com/org/repo/issues/{task.number}, linked under epic "
        f"{EPIC.slug} (in continuation {continuation.slug}).\n"
    )


def test_main_creates_issue_and_links_under_epic(graph: FakeSubIssueGraph) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--body", "B", "--label", "bug"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo="org/repo")
    assert mock_create.call_args.kwargs["title"] == "T"
    assert mock_create.call_args.kwargs["labels"] == ["bug"]
    _assert_linked(graph, EPIC, epics.IssueRef("org", "repo", 123))


def test_kind_validation_applies_label_and_scaffold() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
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
    # The check for an update is the human's update exiting 0; an agent's diff is
    # informational only and its exit 2 is expected (#4327, epic .github#1064).
    prose = " ".join(body.split())
    assert "the check is the human's `vrg-github-repo-config update` exiting 0" in prose
    assert "`vrg-github-repo-config diff` the agent runs afterwards is informational only" in prose
    assert "Never treat `diff`'s exit code as the verdict on an update." in prose
    assert "verify with:" not in prose
    assert "release" in body.lower()  # release-as-precondition boundary
    assert "## Results" in body
    assert kwargs["body_file"] is None


def test_deployment_scaffold_names_check_command() -> None:
    """A blocked-by task's release is confirmed by its record (spec §4.10, epic .github#1440)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(
            ["--epic", "adhoc", "--kind", "deployment", "--title", "Deploy it"]
            + ["--blocked-by", "org/repo#5"]
        )
    assert rc == 0
    preconditions = mock_create.call_args.kwargs["body"].split("## Preconditions", 1)[1]
    prose = " ".join(preconditions.split("## Deploy steps", 1)[0].split())
    assert (
        "depends on a blocked-by development task having been released, confirm that"
        " release with `vrg-release-record check <owner>/<repo>#<N>`: exit 0 (`pass`)"
        " confirms it, and exit 3 (`fail`) is an unmet precondition."
    ) in prose
    assert "Exit 4 (`none`) means the task carries no release record" in prose
    assert "Exit 1 is a read error, never a pass" in prose
    assert "Every other precondition is checked as before." in prose


def test_kind_retrospective_applies_label_and_scaffold() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
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
            assert "Never treat `diff`'s exit code as the verdict on an update." in body
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
    ):
        rc = main(["--epic", "adhoc", "--title", "T", "--label", "bug"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["bug"]  # no validation label added


def test_plain_task_stamps_blocked_by_reflink_from_body() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
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


def test_main_adhoc_sentinel_skips_cross_org_guard(graph: FakeSubIssueGraph) -> None:
    # 'adhoc' resolves within the repo's org (.github), so it must skip the
    # explicit-ref cross-org guard and link.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo="org/repo")
    _assert_linked(graph, EPIC, epics.IssueRef("org", "repo", 123))


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


def test_main_link_failure_reports_created_issue(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # An ordinary link failure: GitHub refuses the link because the issue
    # already has a parent, so vrg-epic-move is the recovery.
    other = graph.add_issue(epics.IssueRef("org", "repo", 900), title="Other", labels=["epic"])
    graph.link(other, TASK)
    graph.clear_log()
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    assert graph.mutations == []
    assert graph.parent_of(TASK) == other
    err = capsys.readouterr().err
    assert _URL in err  # orphan-safe: the created issue is surfaced
    assert "Sub issue may only have one parent" in err
    # The fix names both refs in full, single-quoted for the shell (#3966).
    assert err.rstrip().endswith(
        "Link it with: vrg-epic-move --task 'org/repo#123' --epic 'org/repo#710'"
    )


def test_main_chain_refused_link_reports_created_issue_unlinked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A link the chain layer refuses after the create (here the depth limit,
    # met by a race past the capacity check) would refuse vrg-epic-move the
    # same way, so the move is not suggested.
    refusal = epic_chain.ChainDepthError(EPIC, items=3, containers=1, task=TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epic_chain.link_into", side_effect=refusal),
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err == (
        f"vrg-issue-create: created {_URL} but failed to link it under epic org/repo#710: "
        f"{refusal}. org/repo#123 is left unlinked, and vrg-epic-move would be refused the "
        "same way until that is resolved\n"
    )
    assert "vrg-epic-move --task" not in err


def test_main_scopes_token_to_repo_owner(graph: FakeSubIssueGraph) -> None:
    """Issue creation + linking run under the --repo owner installation (#2070)."""
    task = graph.add_issue(epics.IssueRef("other-org", "repo", 123), title="T")
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.github.current_repo") as mock_cur,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--repo", "other-org/repo", "--epic", "adhoc", "--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")
    # 'adhoc' resolves against --repo, never the cwd repo (#3409).
    mock_resolve.assert_called_once_with("adhoc", repo="other-org/repo")
    mock_cur.assert_not_called()
    _assert_linked(graph, EPIC, task)


def test_main_bare_epic_resolves_against_target_epic_home(graph: FakeSubIssueGraph) -> None:
    """A bare '--epic #N' reads against --repo's epic home, not the cwd repo (#3411).

    The home's existence check runs under the --repo owner's App token.
    """
    home_epic = graph.add_issue(epics.IssueRef("org", ".github", 407), title="E", labels=["epic"])
    task = graph.add_issue(epics.IssueRef("org", "lab", 9), title="T")
    seen: list[tuple[str, str, str | None]] = []

    def exists(owner: str, name: str) -> bool:
        seen.append((owner, name, github._target_org.get()))
        return True

    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}),
        patch(f"{_MOD}.github.create_issue", return_value="https://github.com/org/lab/issues/9"),
    ):
        rc = main(["--repo", "org/lab", "--epic", "#407", "--title", "T"])
    assert rc == 0
    _assert_linked(graph, home_epic, task)
    assert seen == [("org", ".github", "org")]


def _dotgithub_probe(error: str) -> object:
    """A ``_repo_probe`` fake whose ``owner/.github`` lookup fails with *error*."""

    def probe(nwo: str) -> str:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, ["gh", "repo", "view"], "", error)
        return nwo

    return probe


def test_main_repo_without_dotgithub_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """A repo whose owner has no .github fails up front, creating nothing (#3643)."""
    probe = _dotgithub_probe("GraphQL: Could not resolve to a Repository")
    with (
        patch("vergil_tooling.lib.github._repo_probe", side_effect=probe),
        patch(f"{_MOD}.github.create_issue") as mock_create,
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--repo", "owner/site", "--epic", "#4", "--title", "T"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "vrg-issue-create: the epics and queues of owner/site live in owner/.github, "
        "which does not exist; create the owner/.github repository and try again\n"
    )
    mock_create.assert_not_called()


def test_main_epic_home_api_error_is_a_clean_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Any other failed home check exits 1 with a one-line message, creating nothing."""
    probe = _dotgithub_probe("HTTP 502: Bad Gateway")
    with (
        patch("vergil_tooling.lib.github._repo_probe", side_effect=probe),
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


def test_main_explicit_same_org_epic_proceeds(graph: FakeSubIssueGraph) -> None:
    """An explicit epic ref in the issue's own org passes the guard and links."""
    with (
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--repo", "org/repo", "--epic", "org/.github#5", "--title", "T"])
    assert rc == 0
    _assert_linked(graph, EPIC, epics.IssueRef("org", "repo", 123))


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


@pytest.mark.parametrize("epic_sentinel", ["adhoc", "intake", "planning", "operations"])
def test_main_accepts_every_standing_sentinel(epic_sentinel: str, graph: FakeSubIssueGraph) -> None:
    """Every standing sentinel passes the cross-org guard and resolves (#3514)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", epic_sentinel, "--title", "T"])
    assert rc == 0
    mock_resolve.assert_called_once_with(epic_sentinel, repo="org/repo")
    _assert_linked(graph, EPIC, epics.IssueRef("org", "repo", 123))


def test_main_planning_ensures_in_progress_label_in_target_repo(
    label_gh: MagicMock, graph: FakeSubIssueGraph
) -> None:
    """Creating an issue into planning ensures 'in-progress' in its repo (#3520)."""
    planning = graph.add_issue(
        epics.IssueRef("org", ".github", 439), title="Planning", labels=["epic"]
    )
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._find_epic_by_title", return_value=planning),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", "planning", "--title", "T"])
    assert rc == 0
    ensured = [(c.args[2], c.args[4]) for c in label_gh.call_args_list]
    assert ("in-progress", "org/.github") in ensured
    assert ("in-progress", "org/repo") in ensured
    _assert_linked(graph, planning, epics.IssueRef("org", "repo", 123))


def test_epic_help_names_every_standing_sentinel(capsys: pytest.CaptureFixture[str]) -> None:
    """The --epic help lists adhoc, intake and planning (#3512)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for epic_sentinel in ("adhoc", "intake", "planning"):
        assert f"'{epic_sentinel}'" in out


def test_epic_help_lists_hoshin(capsys: pytest.CaptureFixture[str]) -> None:
    """The --epic help lists the hoshin sentinel (epic vergil-project/.github#1417)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "'hoshin'" in out


def test_epic_operations_sentinel_accepted(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    """--epic operations is listed and resolves the org's operations epic (#1494)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    assert "'operations'" in " ".join(capsys.readouterr().out.split())
    ops = graph.add_issue(epics.IssueRef("org", ".github", 31), title="Operations", labels=["epic"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.lib.epics.ensure_operations_epic", return_value=ops) as mock_ensure,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--epic", "operations", "--title", "T"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/repo")
    _assert_linked(graph, ops, epics.IssueRef("org", "repo", 123))


# -- Deferred queue: entered only through vrg-intake-route (#3673) -----------


def test_issue_create_refuses_deferred_epic(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "deferred", "--title", "T"])
    assert rc == 1
    assert "vrg-intake-route" in capsys.readouterr().err
    mock_resolve.assert_not_called()  # the deferred epic is never created
    mock_create.assert_not_called()
    assert graph.mutations == []


def test_issue_create_refuses_deferred_epic_number(
    queue_kind: MagicMock, graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.return_value = "deferred"
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "org/repo#710", "--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#710" in err
    queue_kind.assert_called_once_with(EPIC)
    mock_create.assert_not_called()
    assert graph.mutations == []
