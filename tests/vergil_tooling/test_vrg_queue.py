"""Tests for vergil_tooling.bin.vrg_queue."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_queue import main, parse_args
from vergil_tooling.lib import github, queue_view
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.queue_view import QueueItem

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_queue"


@pytest.fixture(autouse=True)
def dotgithub_exists() -> Iterator[MagicMock]:
    # The org-wide default probes <owner>/.github; it exists unless a test says not.
    with patch(f"{_MOD}.epics.github.repo_exists", return_value=True) as exists:
        yield exists


_ITEM = QueueItem(
    ref=IssueRef("org", ".github", 5),
    title="plan the thing",
    created_at="2026-10-01T00:00:00Z",
    state="ready-to-close",
    note="Tasks filed: yes",
    epic=IssueRef("org", ".github", 50),
)


def test_parse_args_requires_known_queue() -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["triage"])


def test_vrg_queue_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")) as home,
        patch.dict(f"{_MOD}._VIEWS", {"planning": lambda _h, _n: [_ITEM]}),
    ):
        rc = main(["planning", "--json"])
    assert rc == 0
    # Default home: the current org's .github pair, not the cwd repo.
    home.assert_called_once_with("org/.github")
    assert json.loads(capsys.readouterr().out) == [
        {
            "ref": {"owner": "org", "repo": ".github", "number": 5},
            "title": "plan the thing",
            "created_at": "2026-10-01T00:00:00Z",
            "state": "ready-to-close",
            "note": "Tasks filed: yes",
            "epic": {"owner": "org", "repo": ".github", "number": 50},
        }
    ]


def test_vrg_queue_repo_flag_selects_private_home(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[tuple[str, str, str | None]] = []

    def _intake(home: str, name: str) -> list[QueueItem]:
        seen.append((home, name, github._target_org.get()))
        return []

    with (
        patch(f"{_MOD}.github.current_repo") as cur,
        patch(f"{_MOD}.epics.queue_home", return_value=("other/secret", "secret")) as home,
        patch.dict(f"{_MOD}._VIEWS", {"intake": _intake}),
    ):
        rc = main(["intake", "--repo", "other/secret"])
    assert rc == 0
    cur.assert_not_called()
    home.assert_called_once_with("other/secret")
    # The token is scoped to the --repo owner.
    assert seen == [("other/secret", "secret", "other")]
    assert capsys.readouterr().out == "intake queue for secret in other/secret: 0 open item(s)\n"


def test_vrg_queue_text_output(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"planning": lambda h, n: [_ITEM]}),
    ):
        rc = main(["planning"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "planning queue for org in org/.github: 1 open item(s)"
    assert lines[1].startswith("org/.github#5  ")
    assert lines[1].endswith("d  ready-to-close  plan the thing  — Tasks filed: yes")


def test_vrg_queue_rejects_bare_repo(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.epics.queue_home") as home:
        rc = main(["intake", "--repo", "secret"])
    assert rc == 1
    home.assert_not_called()
    assert "--repo must be 'owner/repo'" in capsys.readouterr().err


def test_vrg_queue_missing_queue_epic_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    def _missing(home: str, name: str) -> list[QueueItem]:
        msg = f"no open 'Epic (intake): {name}' intake epic in {home}"
        raise queue_view.QueueNotFoundError(msg)

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"intake": _missing}),
    ):
        rc = main(["intake"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-queue: no open 'Epic (intake): org' intake epic" in captured.err


def test_vrg_queue_org_wide_probes_dotgithub(dotgithub_exists: MagicMock) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"intake": lambda _h, _n: []}),
    ):
        assert main(["intake"]) == 0
    dotgithub_exists.assert_called_once_with("org", ".github")


def test_vrg_queue_repo_flag_skips_dotgithub_probe(dotgithub_exists: MagicMock) -> None:
    # --repo reaches a self-homed repo's own queues, .github or not.
    with (
        patch(f"{_MOD}.epics.queue_home", return_value=("owner/notes", "notes")),
        patch.dict(f"{_MOD}._VIEWS", {"intake": lambda _h, _n: []}),
    ):
        assert main(["intake", "--repo", "owner/notes"]) == 0
    dotgithub_exists.assert_not_called()


def test_vrg_queue_org_wide_without_dotgithub_fails_clearly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An owner with no .github has no org-wide queue: one clear line, exit 1 (#3613).
    with (
        patch(f"{_MOD}.github.current_repo", return_value="owner/notes"),
        patch(f"{_MOD}.epics.github.repo_exists", return_value=False),
        patch(f"{_MOD}.epics.queue_home") as home,
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["intake"])
    assert excinfo.value.code == 1
    home.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "vrg-queue: owner has no .github repository, so there is no org-wide "
        "queue or epic home; pass --repo owner/<repo> to reach a self-homed "
        "repo's own queues and epics\n"
    )


def test_views_wire_to_queue_view() -> None:
    from vergil_tooling.bin import vrg_queue

    assert vrg_queue._VIEWS["intake"] is queue_view.intake_items
    assert vrg_queue._VIEWS["planning"] is queue_view.planning_items
    assert set(vrg_queue._VIEWS) == {"intake", "planning"}
