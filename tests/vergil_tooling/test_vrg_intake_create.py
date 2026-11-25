"""Tests for vergil_tooling.bin.vrg_intake_create."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_intake_create import main, parse_args
from vergil_tooling.lib import epic_chain, epics, github
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_intake_create"
_URL = "https://github.com/org/.github/issues/321"
_INTAKE = IssueRef(owner="org", repo=".github", number=7)
_CREATED = IssueRef(owner="org", repo=".github", number=321)
_INTAKE_LABELS = ["epic", "intake", "epic-management"]
_RETIRED_KINDS = {"triage", "idea", "research"}


@pytest.fixture(autouse=True)
def label_gh() -> Iterator[MagicMock]:
    """Stub the gh boundary under the real ensure_labels (#3373)."""
    with patch("vergil_tooling.lib.labels.github.run") as mock_run:
        yield mock_run


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch, label_gh: MagicMock) -> FakeSubIssueGraph:
    """The sub-issue graph, with ``github.run`` left to :func:`label_gh`.

    ``install`` patches ``github.run`` module-wide, which would hide the
    label stub the label tests drive, so the stub is put back.
    """
    fake = FakeSubIssueGraph().install(monkeypatch)
    monkeypatch.setattr(github, "run", label_gh)
    return fake


def _intake_queue(graph: FakeSubIssueGraph, ref: IssueRef = _INTAKE, *, org: str = "org") -> None:
    """Add *ref*, an open intake queue epic of *org*, labelled as ensure_intake_epic makes it."""
    graph.add_issue(ref, title=f"Epic (intake): {org}", labels=_INTAKE_LABELS)


def _fill(graph: FakeSubIssueGraph, head: IssueRef, count: int) -> None:
    """Link *count* new captures under *head*."""
    start = 1000 + len(graph.issues)
    for number in range(start, start + count):
        graph.link(head, graph.add_issue(IssueRef("org", "repo-a", number), title="a capture"))


@pytest.fixture
def cwd_public(graph: FakeSubIssueGraph) -> Iterator[dict[str, Any]]:
    """A cwd in a repo of ``org``: its queue home is ``org/.github``.

    The intake epic is ``org/.github#7`` in the graph, and the create (the
    fake's, spied on) lands as ``org/.github#321``.
    """
    _intake_queue(graph)
    graph.add_issue(IssueRef("org", ".github", 320), title="an earlier issue")
    graph.clear_log()
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org") as detect,
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling") as current,
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")) as home,
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=_INTAKE) as ensure,
        patch(f"{_MOD}.github.create_issue", side_effect=graph.create_issue) as create,
    ):
        yield {
            "graph": graph,
            "detect": detect,
            "current": current,
            "home": home,
            "ensure": ensure,
            "create": create,
        }


def _only_parent(graph: FakeSubIssueGraph, ref: IssueRef) -> IssueRef | None:
    """*ref*'s parent, checking that no more than one container holds it."""
    holders = [issue.ref for issue in graph.issues.values() if ref in issue.children]
    assert len(holders) <= 1, holders
    assert graph.parent_of(ref) == (holders[0] if holders else None)
    return graph.parent_of(ref)


def test_intake_create_files_in_org_dotgithub_and_links_under_intake(
    cwd_public: dict[str, Any], capsys: pytest.CaptureFixture[str]
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
    graph = cwd_public["graph"]
    assert _only_parent(graph, _CREATED) == _INTAKE
    assert graph.mutations == [("add", _INTAKE, _CREATED)]
    assert f"Created {_URL} (intake, under {_INTAKE.slug})." in capsys.readouterr().out


def test_intake_create_links_under_the_org_intake_epic(graph: FakeSubIssueGraph) -> None:
    # End to end through the real queue_home/ensure_intake_epic: the standing
    # epic is looked up as "Epic (intake): org" in the org's .github.
    intake = IssueRef(owner="org", repo=".github", number=4)
    _intake_queue(graph, intake)
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.epics.github.repo_exists", return_value=True),
        patch("vergil_tooling.lib.epics._find_epic_by_title", return_value=intake) as find,
        patch(f"{_MOD}.github.create_issue", side_effect=graph.create_issue) as create,
    ):
        rc = main(["--title", "T"])
    assert rc == 0
    assert create.call_args.kwargs["repo"] == "org/.github"
    assert find.call_args.args[:2] == ("org/.github", "Epic (intake): org")
    assert _only_parent(graph, IssueRef("org", ".github", 5)) == intake


def test_intake_create_applies_no_kind_label(cwd_public: dict[str, Any]) -> None:
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 0
    labels = cwd_public["create"].call_args.kwargs["labels"]
    assert labels == ["bug"]
    assert not _RETIRED_KINDS & set(labels)


def test_intake_create_rejects_kind_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--title", "T", "--kind", "idea"])


def test_intake_create_link_failure_is_loud(
    cwd_public: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An ordinary failure (GitHub refusing the link, not a chain refusal)
    # names the created issue and still suggests the vrg-epic-move fix.
    graph = cwd_public["graph"]
    answer = graph.graphql

    def failing_link(query: str, **variables: Any) -> dict[str, Any]:
        if query == epics._ADD_SUBISSUE:
            raise github.GitHubAPIError(1, ["gh"], "", "HTTP 502: boom")
        return answer(query, **variables)

    monkeypatch.setattr(github, "graphql", failing_link)
    rc = main(["--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert _URL in err
    assert "not linked" in err
    assert _INTAKE.slug in err
    assert "HTTP 502: boom" in err
    # The fix names both refs single-quoted for the shell (#3966).
    assert err.rstrip().endswith(
        "Link it with: vrg-epic-move --task 'org/.github#321' --epic 'org/.github#7'"
    )
    assert _only_parent(graph, _CREATED) is None
    assert graph.mutations == []


def test_intake_create_link_refusal_names_no_move(
    cwd_public: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    # A racing writer fills the queue between the capacity check and the link,
    # with rollover off: the chain layer refuses the link, so the hint says the
    # capture is left unlinked and vrg-epic-move would be refused the same way.
    graph = cwd_public["graph"]

    def create_then_race(**kwargs: Any) -> str:
        url = graph.create_issue(**kwargs)
        _fill(graph, _INTAKE, epic_chain.SUBISSUE_LIMIT)
        graph.clear_log()
        return url

    cwd_public["create"].side_effect = create_then_race
    rc = main(["--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"created {_URL} but it is not linked under intake epic {_INTAKE.slug}" in err
    assert str(epic_chain.RolloverDisabledError(_INTAKE)) in err
    assert err.rstrip().endswith(
        "org/.github#321 is left unlinked, and vrg-epic-move would be refused the same way "
        "until that is resolved"
    )
    assert "Link it with" not in err
    assert _only_parent(graph, _CREATED) is None
    assert graph.mutations == []


def test_intake_create_rolls_over_full_queue(
    cwd_public: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = cwd_public["graph"]
    _fill(graph, _INTAKE, epic_chain.ITEM_CAP)
    graph.clear_log()
    rc = main(["--title", "T"])
    assert rc == 0
    created = [event[1] for event in graph.events if event[0] == "create"]
    assert created[0] == _CREATED
    (c2,) = created[1:]
    assert graph.issues[c2].labels == ["epic", epics.CONTINUATION_LABEL]
    assert _only_parent(graph, c2) == _INTAKE
    assert graph.children_of(_INTAKE)[-1] == c2
    assert len(graph.children_of(_INTAKE)) == epic_chain.SUBISSUE_LIMIT
    assert _only_parent(graph, _CREATED) == c2
    assert graph.children_of(c2) == [_CREATED]
    out = capsys.readouterr().out
    assert (
        f"queue {_INTAKE.slug} rolled over into continuation {c2.slug}; run epic-review intake\n"
        in out
    )
    assert out.endswith(
        f"Created {_URL} (intake, under {_INTAKE.slug} (in continuation {c2.slug})).\n"
    )


@pytest.mark.parametrize("case", ["depth", "full"])
def test_intake_create_refusal_creates_nothing(
    cwd_public: dict[str, Any], capsys: pytest.CaptureFixture[str], case: str
) -> None:
    # (depth) the capacity check refuses at the nesting limit; (full) rollover
    # is off by default, so a queue at GitHub's limit is refused by name. Either
    # way the refusal comes before the create, so nothing is left behind.
    graph = cwd_public["graph"]
    refusal: epics.EpicRefusalError
    if case == "depth":
        refusal = epic_chain.ChainDepthError(_INTAKE, items=693, containers=7)
        check = patch(f"{_MOD}.epic_chain.check_capacity", side_effect=refusal)
    else:
        _fill(graph, _INTAKE, epic_chain.SUBISSUE_LIMIT)
        graph.clear_log()
        refusal = epic_chain.RolloverDisabledError(_INTAKE)
        check = patch(f"{_MOD}.epic_chain.check_capacity", wraps=epic_chain.check_capacity)
    with check as mock_check:
        rc = main(["--title", "T"])
    assert rc == 1
    mock_check.assert_called_once_with(_INTAKE)
    assert capsys.readouterr().err == f"vrg-intake-create: {refusal}\n"
    cwd_public["create"].assert_not_called()
    assert graph.events == []


def test_intake_epic_failure_creates_nothing(
    cwd_public: dict[str, Any],
) -> None:
    # The intake epic is resolved before the issue is created, so a failure
    # there raises without leaving an unlinked capture behind.
    cwd_public["ensure"].side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 500")
    with pytest.raises(github.GitHubAPIError):
        main(["--title", "T"])
    cwd_public["create"].assert_not_called()
    assert cwd_public["graph"].events == []


def test_main_ensures_extra_labels_in_target_repo_before_create(
    label_gh: MagicMock, cwd_public: dict[str, Any]
) -> None:
    order: list[str] = []
    label_gh.side_effect = lambda *a: order.append(f"label:{a[2]}@{a[4]}")
    graph = cwd_public["graph"]

    def create(**kwargs: Any) -> str:
        order.append("create")
        return graph.create_issue(**kwargs)

    cwd_public["create"].side_effect = create
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 0
    assert order == ["label:bug@org/.github", "create"]


def test_main_no_labels_ensures_none(label_gh: MagicMock, cwd_public: dict[str, Any]) -> None:
    assert main(["--title", "T"]) == 0
    label_gh.assert_not_called()


def test_main_unknown_label_creates_nothing(
    label_gh: MagicMock, cwd_public: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--title", "T", "--label", "no-such-label"])
    assert rc == 1
    cwd_public["create"].assert_not_called()
    label_gh.assert_not_called()
    assert "vrg-intake-create: unknown label 'no-such-label'" in capsys.readouterr().err
    assert cwd_public["graph"].events == []


def test_main_label_create_failure_creates_nothing(
    label_gh: MagicMock, cwd_public: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    label_gh.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: forbidden")
    rc = main(["--title", "T", "--label", "bug"])
    assert rc == 1
    cwd_public["create"].assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-intake-create: could not ensure label(s) bug exist in org/.github" in err
    assert "HTTP 403: forbidden" in err
    assert cwd_public["graph"].events == []


def test_main_dedupes_extra_labels(cwd_public: dict[str, Any]) -> None:
    main(["--title", "T", "--label", "bug", "--label", "bug"])
    assert cwd_public["create"].call_args.kwargs["labels"] == ["bug"]


def test_main_passes_body_file_and_assignees(cwd_public: dict[str, Any], tmp_path: Path) -> None:
    body_file = tmp_path / "b.md"
    body_file.write_text("from a file\n", encoding="utf-8")
    main(["--title", "T", "--body-file", str(body_file), "--assignee", "me"])
    kwargs = cwd_public["create"].call_args.kwargs
    assert kwargs["body_file"] == str(body_file)
    assert kwargs["assignees"] == ["me"]


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_scopes_token_to_repo_owner(graph: FakeSubIssueGraph) -> None:
    """Issue creation runs under the --repo owner's App installation (#2722)."""
    intake = IssueRef(owner="other-org", repo=".github", number=1)
    _intake_queue(graph, intake, org="other-org")
    graph.add_issue(IssueRef("other-org", "repo", 4), title="an earlier issue")
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=intake),
    ):
        rc = main(["--title", "T", "--repo", "other-org/repo"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")
    assert _only_parent(graph, IssueRef("other-org", "repo", 5)) == intake


def test_main_scopes_token_to_detected_org(cwd_public: dict[str, Any]) -> None:
    """The default route is scoped to the cwd org's token (#2722)."""
    with patch(f"{_MOD}.github.target_org") as mock_scope:
        rc = main(["--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("org")


def test_main_repo_override_files_there_and_links_under_its_intake(
    graph: FakeSubIssueGraph,
) -> None:
    _intake_queue(graph)
    graph.add_issue(IssueRef("org", "tooling", 8), title="an earlier issue")
    created = IssueRef(owner="org", repo="tooling", number=9)
    with (
        patch(f"{_MOD}.github.detect_org") as mock_detect,
        patch(f"{_MOD}.github.current_repo") as mock_current,
        patch(f"{_MOD}.epics.ensure_intake_epic", return_value=_INTAKE) as ensure,
        patch(f"{_MOD}.github.create_issue", side_effect=graph.create_issue) as mock_create,
    ):
        rc = main(["--title", "T", "--repo", "org/tooling"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/tooling"
    ensure.assert_called_once_with("org/tooling")
    assert _only_parent(graph, created) == _INTAKE
    assert graph.children_of(_INTAKE) == [created]
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
