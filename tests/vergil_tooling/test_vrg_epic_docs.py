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
    assert "o/.github#1: the edit failed" in err
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
