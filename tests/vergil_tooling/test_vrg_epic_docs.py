"""Tests for vergil_tooling.bin.vrg_epic_docs."""

from __future__ import annotations

from typing import TYPE_CHECKING
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
    with (
        patch("vergil_tooling.lib.github.read_json") as mock_read,
        patch("vergil_tooling.lib.github.read_output") as mock_output,
    ):
        assert main(["set", "--epic", "o/.github#1", "--spec", "epics/1-x/spec.md"]) == 0
    mock_read.assert_not_called()
    mock_output.assert_not_called()


def test_set_runs_in_target_org(fake: _Fake) -> None:
    assert main(["set", "--epic", "other-org/.github#5", "--spec", "epics/5-x/spec.md"]) == 0
    assert [kind for kind, _ in fake.orgs] == ["read", "branch", "write"]
    assert {org for _, org in fake.orgs} == {"other-org"}


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
    ],
)
def test_set_rejects_bad_arguments(
    fake: _Fake, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert main(argv) == 1
    assert message in capsys.readouterr().err
    fake.read_body.assert_not_called()


def test_set_no_installation(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.read_body.side_effect = github.NoInstallationError("o", ["x"])
    assert main(["set", "--epic", "o/.github#1"]) == 1
    assert "o" in capsys.readouterr().err


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
