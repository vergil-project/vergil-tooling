"""Tests for vergil_tooling.bin.vrg_queue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_queue import main, parse_args
from vergil_tooling.lib import github, in_flight, live_branches, queue_view, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.queue_view import AdhocItem, QueueItem

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


def test_parse_args_queue_optional_but_known() -> None:
    assert parse_args([]).queue is None
    assert parse_args(["deferred"]).queue == "deferred"
    with pytest.raises(SystemExit):
        parse_args(["triage"])


_DEFERRED_ITEM = QueueItem(
    ref=IssueRef("org", "tool", 7),
    title="park the thing",
    created_at="2026-09-01T00:00:00Z",
    state="waiting",
    note="Deferred 2026-10-20: waits on X | Revisit when: X ships",
    epic=None,
)


def _missing(kind: str) -> object:
    def view(home: str, name: str) -> list[QueueItem]:
        msg = f"no open 'Epic ({kind}): {name}' {kind} epic in {home}"
        raise queue_view.QueueNotFoundError(msg)

    return view


def test_vrg_queue_deferred_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"deferred": lambda _h, _n: [_DEFERRED_ITEM]}),
    ):
        assert main(["deferred"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert main(["deferred", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
    assert lines[0] == "deferred queue for org in org/.github: 1 open item(s)"
    assert lines[1].startswith("org/tool#7  ")
    assert lines[1].endswith(
        "d  waiting  park the thing  — Deferred 2026-10-20: waits on X | Revisit when: X ships"
    )
    assert len(lines) == 2
    assert data == [
        {
            "ref": {"owner": "org", "repo": "tool", "number": 7},
            "title": "park the thing",
            "created_at": "2026-09-01T00:00:00Z",
            "state": "waiting",
            "note": "Deferred 2026-10-20: waits on X | Revisit when: X ships",
            "epic": None,
        }
    ]


def test_vrg_queue_named_deferred_missing_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"deferred": _missing("deferred")}),
    ):
        rc = main(["deferred"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-queue: no open 'Epic (deferred): org' deferred epic" in captured.err


def test_vrg_queue_bare_lists_all_three_and_tolerates_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    views = {
        "intake": lambda _h, _n: [_ITEM],
        "planning": _missing("planning"),
        "deferred": lambda _h, _n: [_DEFERRED_ITEM],
    }
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")) as home,
        patch.dict(f"{_MOD}._VIEWS", views),
    ):
        assert main([]) == 0
        captured = capsys.readouterr()
        assert main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
    assert home.call_count == 2
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0] == "intake queue for org in org/.github: 1 open item(s)"
    assert lines[1].startswith("org/.github#5  ")
    assert lines[2] == ""
    assert lines[3] == "planning queue for org in org/.github: no queue yet"
    assert lines[4] == ""
    assert lines[5] == "deferred queue for org in org/.github: 1 open item(s)"
    assert lines[6].startswith("org/tool#7  ")
    assert len(lines) == 7
    # JSON: one key per queue, in order; a missing queue is null.
    assert list(data) == ["intake", "planning", "deferred"]
    assert data["intake"] == queue_view.to_json([_ITEM])
    assert data["planning"] is None
    assert data["deferred"] == queue_view.to_json([_DEFERRED_ITEM])


def _failing(exc: Exception) -> object:
    def view(_home: str, _name: str) -> list[QueueItem]:
        raise exc

    return view


_PRIVATE_HOME = security_records.PrivateHomeError(
    "vrg-security-record: org/.github is not public; security records need a "
    "public org .github (private orgs are unsupported)"
)


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (_PRIVATE_HOME, "org/.github is not public"),
        (github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 403: Forbidden"), "HTTP 403"),
        (ValueError("advisory list row lacks a ghsa_id or state"), "lacks a ghsa_id"),
    ],
)
def test_vrg_queue_named_queue_read_failure_is_reported(
    capsys: pytest.CaptureFixture[str], exc: Exception, reason: str
) -> None:
    # An advisory-lookup failure is one clear line and exit 1, never a traceback (#3826).
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"planning": _failing(exc)}),
    ):
        rc = main(["planning"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "vrg-queue: planning queue for org in org/.github could not be read: "
    )
    assert reason in captured.err


@pytest.mark.parametrize("argv", [["planning"], []])
def test_vrg_queue_missing_token_keeps_the_guided_message(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    exc = github.MissingGitHubTokenError(1, ["gh", "api"], "", "no token")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", {"planning": _failing(exc)}, clear=True),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(argv)
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-queue: no GitHub token available.")
    assert "could not be read" not in err


def test_vrg_queue_bare_text_keeps_other_queues_when_one_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    views = {
        "intake": lambda _h, _n: [_ITEM],
        "planning": _failing(_PRIVATE_HOME),
        "deferred": lambda _h, _n: [_DEFERRED_ITEM],
    }
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", views),
    ):
        rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == "intake queue for org in org/.github: 1 open item(s)"
    assert lines[1].startswith("org/.github#5  ")
    assert lines[3] == "planning queue for org in org/.github: not shown (failed; see stderr)"
    assert lines[5] == "deferred queue for org in org/.github: 1 open item(s)"
    assert lines[6].startswith("org/tool#7  ")
    assert len(lines) == 7
    assert captured.err == (
        f"vrg-queue: planning queue for org in org/.github could not be read: {_PRIVATE_HOME}\n"
    )


def test_vrg_queue_bare_json_prints_no_partial_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    views = {
        "intake": lambda _h, _n: [_ITEM],
        "planning": _failing(_PRIVATE_HOME),
        "deferred": _failing(ValueError("bad row")),
    }
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", views),
    ):
        rc = main(["--json"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"vrg-queue: planning queue for org in org/.github could not be read: {_PRIVATE_HOME}",
        "vrg-queue: deferred queue for org in org/.github could not be read: bad row",
    ]


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


def test_vrg_queue_public_repo_without_dotgithub_fails_clearly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --repo on a public repo whose owner has no .github: one clear line, exit 1 (#3643).
    def is_public(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, "cmd", "", "Could not resolve to a Repository")
        return True

    with (
        patch(f"{_MOD}.epics.github.is_public", side_effect=is_public),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["intake", "--repo", "owner/site"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "vrg-queue: owner/site is public, so its epics and queues live in "
        "owner/.github, which does not exist; create the owner/.github repository "
        "and try again\n"
    )


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
        "queue or epic home; create the owner/.github repository to give it one, "
        "or pass --repo owner/<repo> to reach a private, self-homed "
        "repo's own queues and epics\n"
    )


def test_views_wire_to_queue_view() -> None:
    from vergil_tooling.bin import vrg_queue

    assert vrg_queue._VIEWS["intake"] is queue_view.intake_items
    assert vrg_queue._VIEWS["planning"] is queue_view.planning_items
    assert vrg_queue._VIEWS["deferred"] is queue_view.deferred_items
    # Bare vrg-queue lists the queues in this order; adhoc is never among them.
    assert list(vrg_queue._VIEWS) == ["intake", "planning", "deferred"]


# -- vrg-queue adhoc --------------------------------------------------------------

_ADHOC_ITEM = AdhocItem(
    ref=IssueRef("org", "tool", 8),
    title="fix the thing",
    created_at="2026-09-01T00:00:00Z",
    kind="deployment",
    blocked_by=(IssueRef("org", "tool", 6),),
    state="blocked",
    marks=(),
    disposition=None,
)


def test_parse_args_accepts_adhoc() -> None:
    assert parse_args(["adhoc"]).queue == "adhoc"


def test_vrg_queue_adhoc_text_and_json(
    capsys: pytest.CaptureFixture[str], dotgithub_exists: MagicMock
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.queue_view.adhoc_items", return_value=[_ADHOC_ITEM]) as items,
    ):
        assert main(["adhoc"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert main(["adhoc", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
    # Org-wide: the org's .github, checked to exist, and no narrowing.
    items.assert_called_with("org/.github", repo=None, roots=None)
    dotgithub_exists.assert_called_with("org", ".github")
    assert lines[0] == "adhoc queue for org in org/.github: 1 open task(s)"
    assert lines[1].startswith("org/tool#8  ")
    assert lines[1].endswith("d  blocked  deployment  fix the thing  — blocked by org/tool#6")
    assert len(lines) == 2
    assert data == [
        {
            "ref": {"owner": "org", "repo": "tool", "number": 8},
            "title": "fix the thing",
            "created_at": "2026-09-01T00:00:00Z",
            "kind": "deployment",
            "blocked_by": [{"owner": "org", "repo": "tool", "number": 6}],
            "state": "blocked",
            "marks": [],
            "disposition": None,
        }
    ]


def test_vrg_queue_bare_does_not_list_adhoc(capsys: pytest.CaptureFixture[str]) -> None:
    views = {
        "intake": lambda _h, _n: [],
        "planning": lambda _h, _n: [],
        "deferred": lambda _h, _n: [],
    }
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.epics.queue_home", return_value=("org/.github", "org")),
        patch.dict(f"{_MOD}._VIEWS", views),
        patch(f"{_MOD}.queue_view.adhoc_items") as items,
    ):
        assert main([]) == 0
        text = capsys.readouterr().out
        assert main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
    items.assert_not_called()
    assert "adhoc" not in text
    assert list(data) == ["intake", "planning", "deferred"]


def test_vrg_queue_adhoc_repo_narrows_a_public_repo(
    capsys: pytest.CaptureFixture[str], dotgithub_exists: MagicMock
) -> None:
    seen: list[str | None] = []

    def _items(home: str, *, repo: str | None = None, roots: Any = None) -> list[AdhocItem]:
        seen.append(github._target_org.get())
        return []

    with (
        patch(f"{_MOD}.github.current_repo") as cur,
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github") as home,
        patch(f"{_MOD}.queue_view.adhoc_items", side_effect=_items) as items,
    ):
        rc = main(["adhoc", "--repo", "org/no-adhoc-yet"])
    # No ad-hoc epic for the repo is an empty list and exit 0, not an error.
    assert rc == 0
    cur.assert_not_called()
    dotgithub_exists.assert_not_called()
    home.assert_called_once_with("org", "no-adhoc-yet")
    items.assert_called_once_with("org/.github", repo="org/no-adhoc-yet", roots=None)
    assert seen == ["org"]
    assert capsys.readouterr().out == (
        "adhoc queue for no-adhoc-yet in org/.github: 0 open task(s)\n"
    )


def test_vrg_queue_adhoc_repo_selects_a_private_home(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="other/secret"),
        patch(f"{_MOD}.queue_view.adhoc_items", return_value=[]) as items,
    ):
        assert main(["adhoc", "--repo", "other/secret"]) == 0
    items.assert_called_once_with("other/secret", repo="other/secret", roots=None)
    assert capsys.readouterr().out == "adhoc queue for secret in other/secret: 0 open task(s)\n"


@pytest.mark.parametrize(
    "exc",
    [
        github.GitHubAPIError(1, ["gh", "api", "graphql"], "", "HTTP 502"),
        ValueError("the open epic + ad-hoc issues of org/.github reached its 1000-row limit"),
    ],
)
def test_vrg_queue_adhoc_read_failure_exits_1(
    capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.queue_view.adhoc_items", side_effect=exc),
    ):
        rc = main(["adhoc"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"vrg-queue: adhoc queue for org in org/.github could not be read: {exc}\n"
    )


def test_vrg_queue_adhoc_missing_token_keeps_the_guided_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exc = github.MissingGitHubTokenError(1, ["gh", "api"], "", "no token")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.queue_view.adhoc_items", side_effect=exc),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["adhoc"])
    assert excinfo.value.code == 1
    assert capsys.readouterr().err.startswith("vrg-queue: no GitHub token available.")


def test_vrg_queue_adhoc_passes_roots() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.queue_view.adhoc_items", return_value=[]) as items,
    ):
        assert main(["adhoc", "--root", "/a", "--root", "/b"]) == 0
    items.assert_called_once_with("org/.github", repo=None, roots=[Path("/a"), Path("/b")])


def test_vrg_queue_root_only_applies_to_adhoc(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/tool"):
        assert main(["intake", "--root", "/a"]) == 1
    assert capsys.readouterr().err == "vrg-queue: --root applies only to adhoc\n"


def test_vrg_queue_adhoc_no_repo_roots_fails(capsys: pytest.CaptureFixture[str]) -> None:
    # No --root on a platform with no default: the RepoSetError message, exit 1.
    msg = "no default repo set on the physical host. Pass --root DIR for each projects directory."
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(
            "vergil_tooling.lib.queue_view.adhoc_items",
            side_effect=live_branches.RepoSetError(msg),
        ),
    ):
        assert main(["adhoc"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err == f"vrg-queue: adhoc queue for org in org/.github could not be read: {msg}\n"
    )


def test_vrg_queue_adhoc_repo_without_clone_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    exc = in_flight.InFlightError("org/tool has open ad-hoc tasks but no main clone under /r")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tool"),
        patch(f"{_MOD}.queue_view.adhoc_items", side_effect=exc),
    ):
        assert main(["adhoc", "--root", "/r"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no main clone under /r" in captured.err
