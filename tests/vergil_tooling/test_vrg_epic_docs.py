"""Tests for vergil_tooling.bin.vrg_epic_docs."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_docs import main, parse_args
from vergil_tooling.lib import epic_docs, github
from vergil_tooling.lib.epic_docs import PENDING, DocLink

if TYPE_CHECKING:
    from collections.abc import Generator

_MOD = "vergil_tooling.bin.vrg_epic_docs"


class _Fake:
    """Mocks for read_body, write_body and default_branch, recording the target org."""

    def __init__(self, body: str, branches: dict[str, str] | None = None) -> None:
        self.body = body
        self.branches = branches or {}
        self.orgs: list[tuple[str, str | None]] = []
        self.read_body = MagicMock(side_effect=self._read)
        self.write_body = MagicMock(side_effect=self._write)
        self.default_branch = MagicMock(side_effect=self._branch)
        self.epic = True
        self.is_epic = MagicMock(side_effect=self._is_epic)

    def _is_epic(self, ref: object) -> bool:
        self.orgs.append(("epic", github._target_org.get()))
        return self.epic

    def _read(self, owner: str, repo: str, number: int) -> str:
        self.orgs.append(("read", github._target_org.get()))
        return self.body

    def _write(self, owner: str, repo: str, number: int, body: str) -> None:
        self.orgs.append(("write", github._target_org.get()))
        self.body = body

    def _branch(self, owner: str, repo: str) -> str:
        self.orgs.append(("branch", github._target_org.get()))
        return self.branches.get(repo, "develop")


@pytest.fixture
def fake() -> Generator[_Fake]:
    state = _Fake("Epic description.\n\nPlanning: #1\n")
    with (
        patch(f"{_MOD}.epic_docs.read_body", state.read_body),
        patch(f"{_MOD}.epic_docs.write_body", state.write_body),
        patch(f"{_MOD}.epic_docs.default_branch", state.default_branch),
        patch(f"{_MOD}.epics.is_epic", state.is_epic),
    ):
        yield state


def test_set_seeds_pending_section(fake: _Fake) -> None:
    assert main(["set", "--epic", "o/.github#1"]) == 0
    fake.write_body.assert_called_once()
    owner, repo, number, body = fake.write_body.call_args.args
    assert (owner, repo, number) == ("o", ".github", 1)
    assert epic_docs.parse(body) == dict.fromkeys(epic_docs.DOCS, PENDING)
    assert body.endswith("\n\nEpic description.\n\nPlanning: #1\n")
    fake.default_branch.assert_not_called()


def test_set_updates_named_entries_only(fake: _Fake) -> None:
    retro = DocLink("o", ".github", "epics/1-x/retrospective.md", "develop")
    fake.body = epic_docs.render(
        {"spec": PENDING, "plan": epic_docs.PREDATES_SPEC_PLAN, "retrospective": retro},
        ".github",
    )
    assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 0
    assert epic_docs.parse(fake.body) == {
        "spec": DocLink("o", ".github", "epics/1-x/spec.md", "develop"),
        "plan": epic_docs.PREDATES_SPEC_PLAN,
        "retrospective": retro,
    }
    fake.default_branch.assert_called_once_with("o", ".github")


def test_set_other_repo_path(fake: _Fake) -> None:
    fake.branches = {"vergil-vm": "main"}
    argv = ["set", "--epic", "o/.github#1", "--spec", "vergil-vm:docs/specs/a.md"]
    argv += ["--plan", "vergil-vm:docs/plans/a.md", "--retrospective", ".github:epics/1/r.md"]
    assert main(argv) == 0
    entries = epic_docs.parse(fake.body)
    assert entries is not None
    assert entries["spec"] == DocLink("o", "vergil-vm", "docs/specs/a.md", "main")
    assert entries["plan"] == DocLink("o", "vergil-vm", "docs/plans/a.md", "main")
    assert entries["retrospective"] == DocLink("o", ".github", "epics/1/r.md", "develop")
    # One branch query per repo.
    assert [c.args for c in fake.default_branch.call_args_list] == [
        ("o", "vergil-vm"),
        ("o", ".github"),
    ]
    assert "[`vergil-vm/docs/specs/a.md`](" in fake.body


def test_set_idempotent_no_write(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]
    assert main(argv) == 0
    assert fake.write_body.call_count == 1
    assert main(argv) == 0
    assert main(["set", "--epic", "o/.github#1"]) == 0
    assert fake.write_body.call_count == 1
    assert "o/.github#1: no change" in capsys.readouterr().out


def test_set_removes_legacy_lines(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = "Spec: `epics/1-x/spec.md` (published by the documentation task).\n\nText.\n"
    assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 0
    assert fake.body.endswith(f"{epic_docs.END}\n\nText.\n")
    out = capsys.readouterr().out
    assert "removed legacy line: Spec: `epics/1-x/spec.md`" in out
    assert "added the Documents section" in out


def test_set_does_not_check_existence(fake: _Fake) -> None:
    # Every gh call path ends in subprocess.run, so patching it catches a probe
    # made through any github wrapper.
    with (
        patch("vergil_tooling.lib.github.read_json") as mock_read,
        patch("vergil_tooling.lib.github.read_output") as mock_output,
        patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
    ):
        assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 0
    mock_read.assert_not_called()
    mock_output.assert_not_called()
    mock_run.assert_not_called()


def test_set_runs_in_target_org(fake: _Fake) -> None:
    assert main(["set", "--epic", "other-org/.github#5", "--spec", "epics/5-x/spec.md"]) == 0
    # The branch lookups come before the read, keeping the read and the write together.
    assert [kind for kind, _ in fake.orgs] == ["epic", "branch", "read", "write"]
    assert {org for _, org in fake.orgs} == {"other-org"}


def test_set_keeps_other_repo_link_on_branch_ending_in_repo(fake: _Fake) -> None:
    plan = DocLink("o", "vergil-vm", "plan.md", "x/vergil-vm")
    fake.body = epic_docs.render(
        {"spec": PENDING, "plan": plan, "retrospective": PENDING}, ".github"
    )
    assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 0
    entries = epic_docs.parse(fake.body, ".github")
    assert entries is not None
    assert entries["plan"] == plan


def test_set_malformed_section_edits_nothing(
    fake: _Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.body = f"{epic_docs.BEGIN}\n**Documents**\n\nHand edit lost the end marker.\n"
    assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 1
    fake.write_body.assert_not_called()
    assert "nothing was edited" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["set", "--epic", "#1"], "cannot resolve repo"),
        (["set", "--epic", "o/.github#1", "--spec", "/abs.md"], "bad document path"),
        (["set", "--epic", "o/.github#1", "--plan", ":a.md"], "empty repo name"),
        (["set", "--epic", "o/.github#1", "--plan", "vm:"], "bad document path"),
        (["set", "--epic", "o/.github#1", "--plan", "x?y:a.md"], "bad repo name"),
        (["set", "--epic", "o/.github#1", "--plan", "vm#1:a.md"], "bad repo name"),
        (["set", "--epic", "o/.github#1", "--plan", "my repo:a.md"], "bad repo name"),
        (["set", "--epic", "o/.github#1", "--plan", "..:a.md"], "bad repo name"),
        (["set", "--epic", "o/.github#1", "--spec", "o/vergil-vm:docs/a.md"], "names the owner"),
        (["set", "--epic", "O/.github#1", "--spec", "o/vergil-vm:docs/a.md"], "names the owner"),
    ],
)
def test_set_rejects_bad_arguments(
    fake: _Fake, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert main(argv) == 1
    assert message in capsys.readouterr().err
    fake.read_body.assert_not_called()
    fake.default_branch.assert_not_called()


def test_set_no_installation(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.read_body.side_effect = github.NoInstallationError("o", ["x"])
    assert main(["set", "--epic", "o/.github#1"]) == 1
    assert "no installation for owner 'o'" in capsys.readouterr().err
    fake.write_body.assert_not_called()


def test_set_refuses_an_issue_that_is_not_an_epic(
    fake: _Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.epic = False
    assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 1
    assert "o/.github#1 is not an epic" in capsys.readouterr().err
    fake.read_body.assert_not_called()
    fake.default_branch.assert_not_called()
    fake.write_body.assert_not_called()


def _api_error() -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api"], output="", stderr="HTTP 404: Not Found")


@pytest.mark.parametrize(
    "where",
    ["read_body", "default_branch"],
)
def test_set_api_error_before_the_write(
    fake: _Fake, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    getattr(fake, where).side_effect = _api_error()
    assert main(["set", "--epic", "o/.github#1", "--plan", "typo-repo:a.md"]) == 1
    err = capsys.readouterr().err
    assert "o/.github#1:" in err
    assert "nothing was edited" in err
    fake.write_body.assert_not_called()


def test_set_malformed_api_answer_edits_nothing(
    fake: _Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.read_body.side_effect = ValueError("unexpected API response: no body field")
    assert main(["set", "--epic", "o/.github#1"]) == 1
    assert "no body field; nothing was edited" in capsys.readouterr().err


def test_set_failed_write(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.write_body.side_effect = _api_error()
    assert main(["set", "--epic", "o/.github#1"]) == 1
    err = capsys.readouterr().err
    assert "o/.github#1: the edit may not have been applied" in err
    assert "re-run to confirm" in err
    assert "nothing was edited" not in err


@pytest.mark.parametrize("where", ["read_body", "write_body"])
def test_set_missing_token_is_left_to_cli_main(
    fake: _Fake, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    getattr(fake, where).side_effect = github.MissingGitHubTokenError(
        4, ("gh", "api"), "", "no token"
    )
    with pytest.raises(SystemExit):
        main(["set", "--epic", "o/.github#1"])
    err = capsys.readouterr().err
    assert "no GitHub token available" in err
    assert "nothing was edited" not in err


def test_path_with_colon_after_slash_is_home_path(fake: _Fake) -> None:
    assert main(["set", "--epic", "o/.github#1", "--spec", "docs/a:b.md"]) == 0
    entries = epic_docs.parse(fake.body)
    assert entries is not None
    assert entries["spec"] == DocLink("o", ".github", "docs/a:b.md", "develop")


def test_parse_args_requires_subcommand_and_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["set"])


# --- backfill -----------------------------------------------------------------


def _record(number: int, body: str, **fields: object) -> epic_docs.EpicRecord:
    record: dict[str, object] = {
        "number": number,
        "title": f"Epic: {number}",
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "createdAt": "2026-08-01T00:00:00Z",
        "labels": ["epic"],
        "body": body,
    }
    record.update(fields)
    return cast("epic_docs.EpicRecord", record)


class _Home:
    """A fake epic home: its epics, its files, and the target org of every call."""

    def __init__(self, records: list[epic_docs.EpicRecord], files: set[tuple[str, str]]) -> None:
        self.records = records
        self.bodies = {r["number"]: r["body"] or "" for r in records}
        self.files = files
        self.calls: list[tuple[str, str | None]] = []
        self.list_epics = MagicMock(side_effect=self._list_epics)
        self.read_body = MagicMock(side_effect=self._read)
        self.write_body = MagicMock(side_effect=self._write)
        self.default_branch = MagicMock(side_effect=self._branch)
        self.list_dir = MagicMock(side_effect=self._list_dir)
        self.file_exists = MagicMock(side_effect=self._exists)

    def _note(self, kind: str) -> None:
        self.calls.append((kind, github._target_org.get()))

    def _list_epics(self, home: str) -> list[epic_docs.EpicRecord]:
        self._note("list_epics")
        self.home = home
        return self.records

    def _read(self, owner: str, repo: str, number: int) -> str:
        self._note("read")
        return self.bodies[number]

    def _write(self, owner: str, repo: str, number: int, body: str) -> None:
        self._note("write")
        self.bodies[number] = body

    def _branch(self, owner: str, repo: str) -> str:
        self._note("default_branch")
        return "main" if repo == "vergil-vm" else "develop"

    def _list_dir(self, owner: str, repo: str, path: str, branch: str) -> dict[str, str] | None:
        self._note("list_dir")
        prefix = f"{path}/"
        entries: dict[str, str] = {}
        for file_repo, file_path in self.files:
            if file_repo == repo and file_path.startswith(prefix):
                head, _, rest = file_path[len(prefix) :].partition("/")
                entries[head] = "dir" if rest else "file"
        return entries or None

    def _exists(self, owner: str, repo: str, path: str, branch: str) -> bool:
        self._note("file_exists")
        return (repo, path) in self.files

    def patches(self) -> list[Any]:
        return [
            patch.object(epic_docs, name, getattr(self, name))
            for name in (
                "list_epics",
                "read_body",
                "write_body",
                "default_branch",
                "list_dir",
                "file_exists",
            )
        ]

    def run(self, argv: list[str]) -> int:
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return main(argv)


_DIR_FILES = {
    ("o/.github", "epics/1-a/spec.md"),
    ("o/.github", "epics/1-a/plan.md"),
}


def _home_files(*files: tuple[str, str]) -> set[tuple[str, str]]:
    return {(repo.split("/")[-1], path) for repo, path in files}


def test_backfill_dry_run_no_writes(capsys: pytest.CaptureFixture[str]) -> None:
    settled = epic_docs.render(
        {
            "spec": DocLink("o", ".github", "epics/3-c/spec.md", "develop"),
            "plan": epic_docs.NOT_WRITTEN,
            "retrospective": epic_docs.NOT_WRITTEN,
        },
        ".github",
    )
    stale = settled.replace("- Plan: not written", "- Plan: pending")
    home = _Home(
        [
            _record(1, "Epic one.\n\nSpec: `epics/1-a/spec.md` (published by the task).\n"),
            _record(3, f"{stale}\n\nEpic two.\n"),
            _record(4, f"{settled}\n\nEpic four.\n"),
        ],
        _home_files(*_DIR_FILES),
    )
    assert home.run(["backfill", "--org", "o"]) == 0
    home.write_body.assert_not_called()
    home.read_body.assert_not_called()
    out = capsys.readouterr().out
    assert "o/.github#1: add\n" in out
    assert "o/.github#3: change\n" in out
    assert "o/.github#4: no change\n" in out
    url = "https://github.com/o/.github/blob/develop/epics/1-a/spec.md"
    assert f"- Spec: [`epics/1-a/spec.md`]({url})" in out
    assert "removed legacy line: Spec: `epics/1-a/spec.md` (published by the task)." in out
    assert "3 epics: 1 to add, 1 to change, 1 no change, 0 need a decision, 0 failed" in out
    assert home.home == "o/.github"


def test_backfill_apply_writes_changed_only(capsys: pytest.CaptureFixture[str]) -> None:
    done = epic_docs.render(
        dict.fromkeys(epic_docs.DOCS, epic_docs.PREDATES_SPEC_PLAN)
        | {"retrospective": epic_docs.PREDATES_RETRO},
        ".github",
    )
    home = _Home(
        [
            _record(1, "Epic one.\n"),
            _record(2, done, createdAt="2026-07-01T00:00:00Z"),
            _record(5, "Old.\n", createdAt="2026-07-01T00:00:00Z"),
        ],
        _home_files(*_DIR_FILES),
    )
    assert home.run(["backfill", "--org", "o", "--apply"]) == 0
    assert [c.args[2] for c in home.write_body.call_args_list] == [1, 5]
    assert epic_docs.parse(home.bodies[1], ".github") == {
        "spec": DocLink("o", ".github", "epics/1-a/spec.md", "develop"),
        "plan": DocLink("o", ".github", "epics/1-a/plan.md", "develop"),
        "retrospective": epic_docs.NOT_WRITTEN,
    }
    assert home.bodies[5] == f"{done}\n\nOld.\n"
    out = capsys.readouterr().out
    assert "o/.github#1: added the Documents section" in out
    assert "3 epics: 2 added, 0 changed, 1 no change, 0 need a decision, 0 failed" in out


def test_backfill_apply_reads_the_body_again() -> None:
    home = _Home([_record(6, "Listed body.\n", state="OPEN")], set())
    home.bodies[6] = "Edited since the listing.\n"
    assert home.run(["backfill", "--org", "o", "--apply"]) == 0
    assert home.bodies[6].endswith("\n\nEdited since the listing.\n")


def test_backfill_ambiguous_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    twice = (("o/.github", "epics/7-a/spec.md"), ("o/.github", "epics/7-b/spec.md"))
    files = _home_files(*_DIR_FILES, *twice)
    home = _Home([_record(7, "Seven.\n"), _record(1, "One.\n")], files)
    assert home.run(["backfill", "--org", "o", "--apply"]) == 1
    assert [c.args[2] for c in home.write_body.call_args_list] == [1]
    assert home.bodies[7] == "Seven.\n"
    out = capsys.readouterr().out
    assert "o/.github#7: needs a decision: 2 candidate directories: epics/7-a/, epics/7-b/" in out
    assert "1 need a decision" in out


def test_backfill_api_error_continues(capsys: pytest.CaptureFixture[str]) -> None:
    home = _Home([_record(8, "Eight.\n"), _record(1, "One.\n")], _home_files(*_DIR_FILES))

    def failing_list_dir(owner: str, repo: str, path: str, branch: str) -> dict[str, str] | None:
        if path == "epics" or path.startswith("epics/1-"):
            return home._list_dir(owner, repo, path, branch)
        raise github.GitHubAPIError(1, ["gh"], output="", stderr="HTTP 502: Bad Gateway")

    home.files.add((".github", "epics/8-h/spec.md"))
    home.list_dir.side_effect = failing_list_dir
    assert home.run(["backfill", "--org", "o", "--apply"]) == 1
    assert [c.args[2] for c in home.write_body.call_args_list] == [1]
    captured = capsys.readouterr()
    assert "o/.github#8:" in captured.err
    assert "Bad Gateway" in captured.err
    assert "left unchanged" in captured.err
    assert "1 added, 0 changed, 0 no change, 0 need a decision, 1 failed" in captured.out


def test_backfill_malformed_section_continues(capsys: pytest.CaptureFixture[str]) -> None:
    home = _Home([_record(9, f"{epic_docs.BEGIN}\nHalf.\n"), _record(1, "One.\n")], set())
    assert home.run(["backfill", "--org", "o"]) == 1
    assert "o/.github#9: malformed Documents section" in capsys.readouterr().err


def test_backfill_failed_write_continues(capsys: pytest.CaptureFixture[str]) -> None:
    home = _Home([_record(1, "One.\n"), _record(2, "Two.\n")], set())
    home.write_body.side_effect = [
        github.GitHubAPIError(1, ["gh"], output="", stderr="HTTP 502"),
        None,
    ]
    assert home.run(["backfill", "--org", "o", "--apply"]) == 1
    assert home.write_body.call_count == 2
    captured = capsys.readouterr()
    assert "o/.github#1: the edit may not have been applied" in captured.err
    assert "re-run to confirm" in captured.err
    assert "1 added, 0 changed, 0 no change, 0 need a decision, 1 failed" in captured.out


def test_backfill_repo_flag() -> None:
    home = _Home([], set())
    assert home.run(["backfill", "--repo", "o/private"]) == 0
    assert home.home == "o/private"
    assert home.default_branch.call_args.args == ("o", "private")


def test_backfill_keeps_existing_link() -> None:
    spec = DocLink("o", "vergil-vm", "docs/specs/hand-set.md", "main")
    body = epic_docs.render(
        {"spec": spec, "plan": epic_docs.PENDING, "retrospective": epic_docs.PENDING}, ".github"
    )
    home = _Home([_record(1, body)], _home_files(("o/.github", "epics/1-a/plan.md")))
    assert home.run(["backfill", "--org", "o", "--apply"]) == 0
    assert epic_docs.parse(home.bodies[1], ".github") == {
        "spec": spec,
        "plan": DocLink("o", ".github", "epics/1-a/plan.md", "develop"),
        "retrospective": epic_docs.NOT_WRITTEN,
    }


def test_backfill_set_decided_not_reflagged(capsys: pytest.CaptureFixture[str]) -> None:
    links = {
        doc: DocLink("o", ".github", f"epics/7-a/{doc}.md", "develop") for doc in epic_docs.DOCS
    }
    files = _home_files(("o/.github", "epics/7-a/spec.md"), ("o/.github", "epics/7-b/spec.md"))
    home = _Home([_record(7, epic_docs.render(links, ".github"))], files)
    assert home.run(["backfill", "--org", "o"]) == 0
    assert "o/.github#7: no change" in capsys.readouterr().out
    home.list_dir.assert_called_once()  # the home's epics/ listing only


def test_backfill_partly_linked_epic_not_reflagged(capsys: pytest.CaptureFixture[str]) -> None:
    # Settled with set: spec and plan linked, the retrospective still a note.
    entries: dict[str, epic_docs.Entry] = {
        doc: DocLink("o", ".github", f"epics/7-a/{doc}.md", "develop") for doc in ("spec", "plan")
    }
    entries["retrospective"] = epic_docs.NOT_WRITTEN
    body = f"{epic_docs.render(entries, '.github')}\n\nSee also docs/specs/old.md.\n"
    files = _home_files(
        ("o/.github", "epics/7-a/spec.md"),
        ("o/.github", "epics/7-a/plan.md"),
        ("o/.github", "epics/7-b/spec.md"),
        ("o/.github", "docs/specs/old.md"),
    )
    home = _Home([_record(7, body), _record(8, body.replace("7-a", "8-a"))], files)
    home.files |= _home_files(
        ("o/.github", "epics/8-a/spec.md"), ("o/.github", "epics/8-a/plan.md")
    )
    assert home.run(["backfill", "--org", "o"]) == 0
    out = capsys.readouterr().out
    assert "o/.github#7: no change" in out
    assert "o/.github#8: no change" in out
    assert "0 need a decision" in out


def test_backfill_reads_in_target_org() -> None:
    files = _home_files(("o/.github", "epics/1-a/spec.md"))
    files.add(("vergil-vm", "docs/specs/a.md"))
    records = [_record(1, "One.\n"), _record(51, "Spec: `vergil-vm/docs/specs/a.md`\n")]
    home = _Home(records, files)
    assert home.run(["backfill", "--org", "other-org", "--apply"]) == 0
    kinds = {kind for kind, _ in home.calls}
    assert {"list_epics", "default_branch", "list_dir", "file_exists", "read", "write"} <= kinds
    assert {org for _, org in home.calls} == {"other-org"}


def test_backfill_listing_failure_edits_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    home = _Home([], set())
    home.list_epics.side_effect = ValueError("unexpected epic in the o/.github issue listing")
    assert home.run(["backfill", "--org", "o"]) == 1
    assert "o/.github: unexpected epic" in capsys.readouterr().err
    home.list_epics.side_effect = github.NoInstallationError("o", ["x"])
    assert home.run(["backfill", "--org", "o"]) == 1
    assert "no installation for owner 'o'" in capsys.readouterr().err


def test_backfill_missing_token_is_left_to_cli_main(capsys: pytest.CaptureFixture[str]) -> None:
    home = _Home([_record(1, "One.\n")], set())
    home.write_body.side_effect = github.MissingGitHubTokenError(4, ("gh",), "", "no token")
    with pytest.raises(SystemExit):
        home.run(["backfill", "--org", "o", "--apply"])
    assert "no GitHub token available" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["backfill"],
        ["backfill", "--org", "o", "--repo", "o/x"],
        ["backfill", "--org", "o/x"],
        ["backfill", "--org", "-o"],
        ["backfill", "--repo", "o"],
        ["backfill", "--repo", "o/.."],
        ["backfill", "--repo", "o/a b"],
    ],
)
def test_backfill_rejects_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)
