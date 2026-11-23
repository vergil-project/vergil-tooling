"""Tests for vergil_tooling.bin.vrg_checkpoint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin.vrg_checkpoint import main, parse_args
from vergil_tooling.lib import checkpoint, github, trust
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Generator

_MOD = "vergil_tooling.bin.vrg_checkpoint"
EC = "epic-create"
REF = "o/.github#7"
STAGES = checkpoint.WORKFLOWS[EC]
PRE = "Planning: o/.github#7\n\nSecurity advisory: none\n\nThe human's description.\n"


class _Fake:
    """Mocks for the trust-layer body read and the body write, recording the target org."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.orgs: list[tuple[str, str | None]] = []
        self.read_body = MagicMock(side_effect=self._read)
        self.write_body = MagicMock(side_effect=self._write)

    def _read(self, owner: str, repo: str, number: int) -> str:
        assert (owner, repo, number) == ("o", ".github", 7)
        self.orgs.append(("read", github._target_org.get()))
        return self.body

    def _write(self, owner: str, repo: str, number: int, body: str) -> None:
        assert (owner, repo, number) == ("o", ".github", 7)
        self.orgs.append(("write", github._target_org.get()))
        self.body = body


@pytest.fixture
def fake() -> Generator[_Fake]:
    state = _Fake(PRE)
    with (
        patch(f"{_MOD}.issue_body.read_body", state.read_body),
        patch(f"{_MOD}.issue_body.write_body", state.write_body),
    ):
        yield state


def _with_block(checked: tuple[str, ...] = ()) -> str:
    return PRE + "\n" + checkpoint.render(EC, checked) + "\n"


_MALFORMED = _with_block().replace("- [ ] plan\n", "- [ ] plan (by hand)\n")
_UNCLOSED = PRE + "\n" + checkpoint.begin(EC) + "\n- [ ] bootstrap\n"


def _status_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    return out


# --- init ---------------------------------------------------------------------


def test_init_writes_block_unticked(fake: _Fake) -> None:
    assert main(["init", REF, "--workflow", EC]) == 0
    fake.write_body.assert_called_once()
    assert fake.body == _with_block()
    assert checkpoint.parse(fake.body, EC) == [(s, False) for s in STAGES]


def test_init_with_tick_prefix(fake: _Fake) -> None:
    assert main(["init", REF, "--workflow", EC, "--tick", "bootstrap", "--tick", "brainstorm"]) == 0
    assert fake.body == _with_block(("bootstrap", "brainstorm"))


def test_init_tick_prefix_in_any_order(fake: _Fake) -> None:
    assert main(["init", REF, "--workflow", EC, "--tick", "brainstorm", "--tick", "bootstrap"]) == 0
    assert fake.body == _with_block(("bootstrap", "brainstorm"))


def test_init_refuses_existing_block(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["init", REF, "--workflow", EC]) == 1
    fake.write_body.assert_not_called()
    assert "already holds" in capsys.readouterr().err


def test_init_refuses_existing_skewed_block(fake: _Fake) -> None:
    fake.body = fake.body + "\n" + checkpoint.begin(EC) + "\n- [ ] old\n" + checkpoint.end(EC)
    assert main(["init", REF, "--workflow", EC]) == 1
    fake.write_body.assert_not_called()


@pytest.mark.parametrize("body", [_MALFORMED, _UNCLOSED])
def test_init_malformed_existing_block_exits_6(fake: _Fake, body: str) -> None:
    fake.body = body
    assert main(["init", REF, "--workflow", EC]) == 6
    fake.write_body.assert_not_called()


@pytest.mark.parametrize(
    "ticks",
    [["brainstorm"], ["bootstrap", "spec-pushback"], ["nope"]],
)
def test_init_tick_not_a_prefix_refused(
    fake: _Fake, capsys: pytest.CaptureFixture[str], ticks: list[str]
) -> None:
    argv = ["init", REF, "--workflow", EC]
    for stage in ticks:
        argv += ["--tick", stage]
    assert main(argv) == 1
    fake.read_body.assert_not_called()
    fake.write_body.assert_not_called()
    assert "--tick" in capsys.readouterr().err


# --- status -------------------------------------------------------------------


def test_status_json_ok(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["status", REF, "--workflow", EC, "--json"]) == 0
    assert _status_json(capsys) == {
        "issue": REF,
        "workflow": EC,
        "state": "ok",
        "stages": [{"name": s, "done": s == "bootstrap"} for s in STAGES],
        "next": "brainstorm",
        "skew": None,
    }
    fake.write_body.assert_not_called()


def test_status_text_ok(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["status", REF, "--workflow", EC]) == 0
    out = capsys.readouterr().out
    assert "- [x] bootstrap\n- [ ] brainstorm\n" in out
    assert out.endswith("next: brainstorm\n")


def test_status_missing_exits_4(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", REF, "--workflow", EC, "--json"]) == 4
    data = _status_json(capsys)
    assert (data["state"], data["stages"], data["next"], data["skew"]) == (
        "missing",
        [],
        None,
        None,
    )


def test_status_missing_text(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", REF, "--workflow", EC]) == 4
    assert "no epic-create checkpoint block" in capsys.readouterr().out


def test_status_skew_exits_5(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    lines = ["- [x] brainstorm", "- [x] bootstrap", "- [ ] old-stage"]
    fake.body = PRE + "\n" + "\n".join([checkpoint.begin(EC), *lines, checkpoint.end(EC)]) + "\n"
    assert main(["status", REF, "--workflow", EC, "--json"]) == 5
    data = _status_json(capsys)
    assert data["state"] == "skew"
    assert data["stages"] == [
        {"name": "brainstorm", "done": True},
        {"name": "bootstrap", "done": True},
        {"name": "old-stage", "done": False},
    ]
    assert data["next"] is None
    assert data["skew"] == {
        "recorded": ["brainstorm", "bootstrap", "old-stage"],
        "current": list(STAGES),
        "added": list(STAGES[2:]),
        "removed": ["old-stage"],
        "reordered": True,
    }


def test_status_skew_text(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block().replace("- [ ] alignment\n", "")
    assert main(["status", REF, "--workflow", EC]) == 5
    out = capsys.readouterr().out
    assert "added: alignment" in out
    assert "removed: (none)" in out
    assert "reordered: no" in out


@pytest.mark.parametrize("body", [_MALFORMED, _UNCLOSED])
def test_status_malformed_exits_6(
    fake: _Fake, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    fake.body = body
    assert main(["status", REF, "--workflow", EC, "--json"]) == 6
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert (data["state"], data["stages"], data["next"]) == ("malformed", [], None)
    assert "malformed" in captured.err


def test_status_all_ticked_next_null(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block(STAGES)
    assert main(["status", REF, "--workflow", EC, "--json"]) == 0
    assert _status_json(capsys)["next"] is None


def test_status_reads_hand_tick(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block().replace("- [ ] bootstrap", "- [X] bootstrap").replace("\n", "\r\n")
    assert main(["status", REF, "--workflow", EC, "--json"]) == 0
    assert _status_json(capsys)["next"] == "brainstorm"


# --- tick ---------------------------------------------------------------------


def test_tick_writes_once(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["tick", REF, "--workflow", EC, "brainstorm"]) == 0
    fake.write_body.assert_called_once()
    assert fake.body == _with_block(("bootstrap", "brainstorm"))
    assert "next: spec-pushback" in capsys.readouterr().out


def test_tick_already_ticked_writes_nothing(fake: _Fake) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["tick", REF, "--workflow", EC, "bootstrap"]) == 0
    fake.write_body.assert_not_called()


@pytest.mark.parametrize("stage", ["brainstorm", "nope"])
def test_tick_refused_exits_1(fake: _Fake, capsys: pytest.CaptureFixture[str], stage: str) -> None:
    fake.body = _with_block()
    assert main(["tick", REF, "--workflow", EC, stage]) == 1
    fake.write_body.assert_not_called()
    assert "nothing was written" in capsys.readouterr().err


@pytest.mark.parametrize("body", [_MALFORMED, _UNCLOSED])
def test_tick_malformed_exits_6_and_writes_nothing(fake: _Fake, body: str) -> None:
    fake.body = body
    assert main(["tick", REF, "--workflow", EC, "bootstrap"]) == 6
    fake.write_body.assert_not_called()


def test_tick_missing_exits_4(fake: _Fake) -> None:
    assert main(["tick", REF, "--workflow", EC, "bootstrap"]) == 4
    fake.write_body.assert_not_called()


def test_tick_skew_exits_5_and_writes_nothing(fake: _Fake) -> None:
    fake.body = _with_block().replace("- [ ] alignment\n", "")
    assert main(["tick", REF, "--workflow", EC, "bootstrap"]) == 5
    fake.write_body.assert_not_called()


# --- reset --------------------------------------------------------------------


def test_reset_removes_block_keeps_rest_of_body(fake: _Fake) -> None:
    post = "\nMore human text.\n"
    fake.body = _with_block(("bootstrap",)) + post
    assert main(["reset", REF, "--workflow", EC]) == 0
    fake.write_body.assert_called_once()
    assert fake.body == PRE + post


def test_reset_without_block_writes_nothing(fake: _Fake) -> None:
    assert main(["reset", REF, "--workflow", EC]) == 0
    fake.write_body.assert_not_called()


def test_reset_removes_malformed_block(fake: _Fake) -> None:
    fake.body = _MALFORMED
    assert main(["reset", REF, "--workflow", EC]) == 0
    assert fake.body == PRE


def test_reset_removes_duplicate_blocks(fake: _Fake) -> None:
    fake.body = _with_block() + "\n" + checkpoint.render(EC, ["bootstrap"]) + "\n"
    assert main(["reset", REF, "--workflow", EC]) == 0
    fake.write_body.assert_called_once()
    assert fake.body == PRE


def test_reset_unclosed_block_exits_6_and_writes_nothing(fake: _Fake) -> None:
    fake.body = _UNCLOSED
    assert main(["reset", REF, "--workflow", EC]) == 6
    fake.write_body.assert_not_called()


def test_reset_then_status_exits_4(fake: _Fake) -> None:
    fake.body = _with_block(("bootstrap",))
    assert main(["reset", REF, "--workflow", EC]) == 0
    assert main(["status", REF, "--workflow", EC]) == 4


def test_init_then_reset_round_trips(fake: _Fake) -> None:
    assert main(["init", REF, "--workflow", EC]) == 0
    assert main(["reset", REF, "--workflow", EC]) == 0
    assert fake.body == PRE


# --- trust, errors and arguments ----------------------------------------------

_SUBCOMMANDS = [
    ["init", REF, "--workflow", EC],
    ["status", REF, "--workflow", EC],
    ["status", REF, "--workflow", EC, "--json"],
    ["tick", REF, "--workflow", EC, "bootstrap"],
    ["reset", REF, "--workflow", EC],
]

_UNTRUSTED = trust.Skipped(
    "o/.github", 7, "edit", "drive-by", trust_stubs.EDITED, "body", trust.UNTRUSTED
)


@pytest.mark.parametrize("argv", _SUBCOMMANDS)
def test_untrusted_body_exits_3_and_writes_nothing(
    fake: _Fake, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    fake.read_body.side_effect = trust.UntrustedContentError(
        _UNTRUSTED, "the issue body is untrusted"
    )
    assert main(argv) == 3
    fake.write_body.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "vrg-checkpoint: o/.github#7: stopped before any write: the issue body is untrusted: "
        f"o/.github#7 edit drive-by at {trust_stubs.EDITED} on body (untrusted)."
        " The human decides next.\n"
    )


@pytest.mark.parametrize(
    ("author", "editor", "code"),
    [
        (trust_stubs.HUMAN, None, 0),
        (trust_stubs.AGENT, trust_stubs.HUMAN, 0),
        (trust_stubs.OUTSIDER, None, 3),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, 3),
        (trust_stubs.IMPOSTOR, None, 3),
    ],
)
@pytest.mark.parametrize("argv", _SUBCOMMANDS)
def test_reads_the_body_through_the_trust_layer(
    author: dict[str, str], editor: dict[str, str] | None, code: int, argv: list[str]
) -> None:
    # The real read, on the trust layer's GraphQL answer: an untrusted body
    # (a box ticked by an outsider included) is never parsed or written back.
    body = _with_block() if argv[0] in {"status", "tick", "reset"} else PRE
    answer = trust_stubs.item(body=body, author=author, editor=editor)
    write = MagicMock()
    with (
        patch("vergil_tooling.lib.github.graphql", return_value=answer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        patch(f"{_MOD}.issue_body.write_body", write),
    ):
        rc = main(argv)
    assert rc == code
    expect_write = code == 0 and argv[0] != "status"
    assert write.call_count == (1 if expect_write else 0)


@pytest.mark.parametrize(
    "exc",
    [
        trust.TrustReadError("read_issue o/.github#7", "the body is missing"),
        TrustedAuthorsNotConfiguredError("no trusted authors configured"),
        github.GitHubAPIError(1, ["gh", "api"], output="", stderr="HTTP 404: Not Found"),
        ValueError("unexpected API response: no body field"),
    ],
)
@pytest.mark.parametrize("argv", _SUBCOMMANDS)
def test_read_failure_exits_1_and_writes_nothing(
    fake: _Fake, capsys: pytest.CaptureFixture[str], exc: Exception, argv: list[str]
) -> None:
    fake.read_body.side_effect = exc
    assert main(argv) == 1
    fake.write_body.assert_not_called()
    err = capsys.readouterr().err
    assert err.startswith("vrg-checkpoint: o/.github#7: ")
    assert err.endswith("; nothing was written\n")


def test_failed_write_exits_1(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.write_body.side_effect = github.GitHubAPIError(
        1, ["gh", "api"], output="", stderr="HTTP 502"
    )
    assert main(["init", REF, "--workflow", EC]) == 1
    err = capsys.readouterr().err
    assert "the edit may not have been applied" in err
    assert "nothing was written" not in err


@pytest.mark.parametrize("where", ["read_body", "write_body"])
def test_missing_token_is_left_to_cli_main(
    fake: _Fake, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    getattr(fake, where).side_effect = github.MissingGitHubTokenError(
        4, ("gh", "api"), "", "no token"
    )
    with pytest.raises(SystemExit) as info:
        main(["init", REF, "--workflow", EC])
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert "nothing was written" not in err
    assert "may not have been applied" not in err


def test_no_installation_exits_1(fake: _Fake, capsys: pytest.CaptureFixture[str]) -> None:
    fake.read_body.side_effect = github.NoInstallationError("o", [])
    assert main(["status", REF, "--workflow", EC]) == 1
    assert capsys.readouterr().err.startswith("vrg-checkpoint: ")


def test_unknown_workflow_exits_2(fake: _Fake) -> None:
    with pytest.raises(SystemExit) as info:
        main(["status", REF, "--workflow", "nope"])
    assert info.value.code == 2
    fake.read_body.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["status", REF],
        ["status", "#7", "--workflow", EC],
        ["status", "o/.github", "--workflow", EC],
        ["tick", REF, "--workflow", EC],
        ["reconcile", REF, "--workflow", EC],
    ],
)
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        parse_args(argv)
    assert info.value.code == 2


def test_runs_under_owner_org(fake: _Fake) -> None:
    fake.body = _with_block()
    assert main(["tick", REF, "--workflow", EC, "bootstrap"]) == 0
    assert fake.orgs == [("read", "o"), ("write", "o")]
