"""Tests for ``vrg-release-group list``: the read, its output, its exit codes and its usage errors.

The log is stubbed at ``record.read_log``; the record's own reads are tested in
``test_release_group_record.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_release_group
from vergil_tooling.bin.vrg_release_group import main
from vergil_tooling.lib import git
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.release_groups import write
from vergil_tooling.lib.release_groups.record import Group, GroupLog, GroupRecordError

if TYPE_CHECKING:
    from collections.abc import Callable

_MOD = "vergil_tooling.lib.release_groups.record.read_log"
_CLONE = Path("/clones/repo")
EPIC = IssueRef("org", ".github", 40)


def _ref(number: int) -> IssueRef:
    return IssueRef("org", "repo", number)


def _group(gid: str, rev: int, *members: int, dissolved: bool = False) -> Group:
    return Group(
        epic=EPIC,
        group=gid,
        rev=rev,
        members=() if dissolved else tuple(_ref(n) for n in members),
        dissolved=dissolved,
        comment_id=str(rev),
    )


def _log(
    groups: list[Group],
    *,
    broken: dict[str, GroupRecordError] | None = None,
    epic_error: GroupRecordError | None = None,
) -> GroupLog:
    return GroupLog(
        epic=EPIC,
        groups={g.group: g for g in groups},
        broken=broken or {},
        epic_error=epic_error,
        ids_used=frozenset({g.group for g in groups} | set(broken or {})),
    )


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.fixture
def stub_log(monkeypatch: pytest.MonkeyPatch) -> Callable[[GroupLog | Exception], None]:
    def install(result: GroupLog | Exception) -> None:
        def fake(epic: IssueRef, **_: object) -> GroupLog:
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(_MOD, fake)

    return install


LISTED = [_group("G10", 2, 5), _group("G2", 3, 1, 2), _group("G3", 1, dissolved=True)]


def test_cli_list_plain_in_id_order(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(_log(LISTED))
    rc, out, err = _run(["list", "--epic", "org/.github#40"], capsys)
    assert rc == 0
    assert out == "G2 rev 3: org/repo#1 org/repo#2\nG3 rev 1: dissolved\nG10 rev 2: org/repo#5\n"
    assert err == ""


def test_cli_list_json_and_plain(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(_log(LISTED))
    rc, out, _ = _run(["list", "--epic", "org/.github#40", "--json"], capsys)
    assert rc == 0
    assert json.loads(out) == {
        "epic": "org/.github#40",
        "groups": [
            {
                "group": "G2",
                "rev": 3,
                "members": ["org/repo#1", "org/repo#2"],
                "dissolved": False,
            },
            {"group": "G3", "rev": 1, "members": [], "dissolved": True},
            {"group": "G10", "rev": 2, "members": ["org/repo#5"], "dissolved": False},
        ],
    }


def test_cli_list_group_filter(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(_log(LISTED))
    rc, out, _ = _run(["list", "--epic", "org/.github#40", "--group", "G10"], capsys)
    assert rc == 0
    assert out == "G10 rev 2: org/repo#5\n"


def test_cli_list_filter_on_an_unknown_group_is_an_empty_read(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(_log(LISTED))
    rc, out, _ = _run(["list", "--epic", "org/.github#40", "--group", "G7", "--json"], capsys)
    assert rc == 0
    assert json.loads(out) == {"epic": "org/.github#40", "groups": []}


def test_cli_list_read_failure_exits_1(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(RuntimeError("the comments could not be read"))
    rc, out, err = _run(["list", "--epic", "org/.github#40"], capsys)
    assert rc == 1
    assert out == ""
    assert "vrg-release-group: the comments could not be read" in err


def test_cli_list_epic_refusal_exits_1(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stub_log(_log(LISTED, epic_error=GroupRecordError("org/.github#40 cannot be read: x")))
    rc, out, err = _run(["list", "--epic", "org/.github#40"], capsys)
    assert rc == 1
    assert out == ""
    assert "org/.github#40 cannot be read: x" in err


def test_cli_list_broken_group_refuses_the_listing(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = {"G4": GroupRecordError("org/.github#40 group G4 cannot be read: comment 9")}
    stub_log(_log(LISTED, broken=broken))
    rc, out, err = _run(["list", "--epic", "org/.github#40"], capsys)
    assert rc == 1
    assert out == ""
    assert "group G4 cannot be read: comment 9" in err


def test_cli_list_of_one_group_reads_past_another_broken_group(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = {"G4": GroupRecordError("org/.github#40 group G4 cannot be read: comment 9")}
    stub_log(_log(LISTED, broken=broken))
    rc, out, _ = _run(["list", "--epic", "org/.github#40", "--group", "G10"], capsys)
    assert rc == 0
    assert out == "G10 rev 2: org/repo#5\n"


def test_cli_list_of_the_broken_group_exits_1(
    stub_log: Callable[[GroupLog | Exception], None], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = {"G4": GroupRecordError("org/.github#40 group G4 cannot be read: comment 9")}
    stub_log(_log(LISTED, broken=broken))
    rc, out, err = _run(["list", "--epic", "org/.github#40", "--group", "G4"], capsys)
    assert rc == 1
    assert out == ""
    assert "group G4 cannot be read" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["list", "--epic", "#5"],
        ["list", "--epic", "not-a-ref"],
        ["list", "--epic", "org/.github#40", "--group", "G01"],
        ["list", "--epic", "org/.github#40", "--group", "7"],
        ["show", "--epic", "org/.github#40"],
    ],
)
def test_cli_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err


# --- set and dissolve: the posts, their output and their exit codes ----------------------

_SET = ["set", "--epic", "org/.github#40", "--group", "G1"]
_DISSOLVE = ["dissolve", "--epic", "org/.github#40", "--group", "G1"]


@pytest.fixture
def main_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """This command runs from the main clone at ``_CLONE``."""
    monkeypatch.setattr(git, "is_main_worktree", lambda: True)
    monkeypatch.setattr(git, "main_worktree_root", lambda: _CLONE)


@pytest.fixture
def stub_change(
    monkeypatch: pytest.MonkeyPatch, main_clone: None
) -> Callable[[str, write.WriteResult | Exception], list[dict[str, object]]]:
    """Stub a write: its name, and what it returns or raises; the calls it gets are returned."""
    calls: list[dict[str, object]] = []

    def install(name: str, result: write.WriteResult | Exception) -> list[dict[str, object]]:
        def fake(epic: IssueRef, group: str, *rest: object, **kwargs: object) -> write.WriteResult:
            calls.append({"name": name, "epic": epic, "group": group, "rest": rest, **kwargs})
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(write, name, fake)
        return calls

    return install


def test_cli_set_posts_and_prints_posted(
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = stub_change("set_group", write.WriteResult(posted=True, rev=1))
    argv = [
        *_SET,
        "--member",
        "org/repo#1",
        "--member",
        "org/repo#2",
        "--reason",
        "Two tasks ship together",
        "--at-batch-stop",
    ]
    rc, out, err = _run(argv, capsys)
    assert rc == 0
    assert out == "G1 rev 1: posted\n"
    assert err == ""
    assert calls == [
        {
            "name": "set_group",
            "epic": EPIC,
            "group": "G1",
            "rest": ([_ref(1), _ref(2)],),
            "reason": "Two tasks ship together",
            "at_batch_stop": True,
            "main_root": _CLONE,
        }
    ]


def test_cli_set_already_recorded_prints_the_line(
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub_change("set_group", write.WriteResult(posted=False, rev=3))
    rc, out, err = _run([*_SET, "--member", "org/repo#1", "--member", "org/repo#2"], capsys)
    assert rc == 0
    assert out == "G1 rev 3: already recorded\n"
    assert err == ""


def test_cli_set_retry_needs_no_reason(
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = stub_change("set_group", write.WriteResult(posted=False, rev=3))
    rc, out, _ = _run([*_SET, "--member", "org/repo#1"], capsys)
    assert rc == 0
    assert out == "G1 rev 3: already recorded\n"
    assert calls[0]["reason"] is None


def test_cli_dissolve_posts_and_prints_posted(
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = stub_change("dissolve_group", write.WriteResult(posted=True, rev=2))
    rc, out, _ = _run([*_DISSOLVE, "--reason", "Shipped elsewhere"], capsys)
    assert rc == 0
    assert out == "G1 rev 2: posted\n"
    assert calls[0]["name"] == "dissolve_group"
    assert calls[0]["reason"] == "Shipped elsewhere"
    assert calls[0]["at_batch_stop"] is False


def test_cli_refusal_prints_each_message_and_exits_1(
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub_change("set_group", write.WriteRefusedError(["first problem", "second problem"]))
    rc, out, err = _run([*_SET, "--member", "org/repo#1", "--reason", "r"], capsys)
    assert rc == 1
    assert out == ""
    assert err == "vrg-release-group: first problem\nvrg-release-group: second problem\n"


def test_cli_outside_the_main_clone_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = stub_change("set_group", write.WriteResult(posted=True, rev=1))
    monkeypatch.setattr(git, "is_main_worktree", lambda: False)
    rc, out, err = _run([*_SET, "--member", "org/repo#1", "--reason", "r"], capsys)
    assert rc == 1
    assert out == ""
    assert "run this from the main clone" in err
    assert calls == []


def test_cli_unknown_checkout_refused(
    monkeypatch: pytest.MonkeyPatch,
    stub_change: Callable[[str, write.WriteResult | Exception], list[dict[str, object]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = stub_change("dissolve_group", write.WriteResult(posted=True, rev=2))

    def no_repo() -> bool:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr(git, "is_main_worktree", no_repo)
    rc, _, err = _run(_DISSOLVE, capsys)
    assert rc == 1
    assert "cannot tell whether this is the main clone" in err
    assert calls == []


@pytest.mark.parametrize(
    "argv",
    [
        [*_SET, "--reason", "r"],
        [*_SET, "--member", "#5", "--reason", "r"],
        ["set", "--epic", "org/.github#40", "--group", "G01", "--member", "org/repo#1"],
        ["set", "--group", "G1", "--member", "org/repo#1"],
        [*_DISSOLVE, "--member", "org/repo#1"],
        ["dissolve", "--epic", "org/.github#40"],
    ],
)
def test_cli_change_usage_errors_exit_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_cli_docstring_covers_the_writes() -> None:
    doc = vrg_release_group.__doc__ or ""
    assert "``set --epic" in doc
    assert "``dissolve --epic" in doc
    assert "--at-batch-stop" in doc
