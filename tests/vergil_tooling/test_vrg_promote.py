"""Tests for vrg-promote CLI."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING
from unittest.mock import patch

from tests.vergil_tooling.test_promote import (  # noqa: F401 (autouse fixture)
    NEW,
    OLD,
    OLD_REF,
    _git_responses,
    _no_installation_token,
)
from vergil_tooling.bin.vrg_promote import main

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    import pytest

_RUN = "vergil_tooling.lib.promote.subprocess.run"
_READ = "vergil_tooling.lib.promote.github.read_output"
_LEASE = f"--force-with-lease=refs/tags/v2.1:{OLD_REF}"


def _write_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        '\n[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["3.14"]\n'
    )


def _pushes(mock_run: MagicMock) -> list[list[str]]:
    return [list(c.args[0]) for c in mock_run.call_args_list if c.args[0][1] == "push"]


def _mutations(mock_run: MagicMock) -> list[list[str]]:
    return [list(c.args[0]) for c in mock_run.call_args_list if c.args[0][1] in ("tag", "push")]


def _never_prompt(*_a: object) -> str:
    msg = "input() must not be called"
    raise AssertionError(msg)


def test_refuses_agent_identity(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_promote.identity_mode.any_agent_signal", return_value=True),
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN) as mock_run,
    ):
        assert main() == 1
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "vrg-promote" in captured.err
    assert "human maintainer action" in captured.err
    assert captured.out == ""


def test_refuses_agent_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_promote.identity_mode.is_agent", return_value=True),
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN) as mock_run,
    ):
        assert main() == 1
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    # The CLI's own refusal, not the library's PromoteError reported as Error:
    assert "Agents cannot run vrg-promote" in captured.err
    assert "Error:" not in captured.err
    assert captured.out == ""


def test_dry_run_prints_repo_and_commits(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "--dry-run", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("builtins.input", side_effect=_never_prompt),
    ):
        assert main() == 0
    out = capsys.readouterr().out
    assert "origin: vergil-project/vergil-claude-plugin" in out
    assert f"v2.1 {OLD[:12]} -> v2.1.50 {NEW[:12]}" in out
    assert _mutations(mock_run) == []


def test_dry_run_refuses_bad_target(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "--dry-run", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses(on_main=1)),
        patch(_READ, return_value="false"),
    ):
        assert main() == 1
    assert "not on origin/main" in capsys.readouterr().err


def test_refuses_without_tty(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=False),
        patch("builtins.input", side_effect=_never_prompt),
    ):
        assert main() == 1
    assert _pushes(mock_run) == []
    assert "without a terminal" in capsys.readouterr().err


def test_declined_confirmation_pushes_nothing() -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="n"),
    ):
        assert main() == 1
    assert _mutations(mock_run) == []


def test_eof_at_prompt_pushes_nothing() -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=EOFError),
    ):
        assert main() == 1
    assert _mutations(mock_run) == []


def test_confirmed_run_promotes(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y") as mock_input,
    ):
        assert main() == 0
    refspec = "refs/tags/v2.1.50:refs/tags/v2.1"
    assert _pushes(mock_run) == [["git", "push", "origin", refspec, _LEASE]]
    prompt = "Move v2.1 on vergil-project/vergil-claude-plugin? [y/N] "
    assert mock_input.call_args.args[0] == prompt
    out = capsys.readouterr().out
    assert out.index("origin: vergil-project/vergil-claude-plugin") < out.index("Promoted")


def test_confirmed_run_refuses_when_origin_moved(capsys: pytest.CaptureFixture[str]) -> None:
    c = "c" * 40
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses(rolling_seq=[(OLD_REF, OLD), (c, c)])) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="yes"),
    ):
        assert main() == 1
    assert _mutations(mock_run) == []
    assert "changed since it was confirmed" in capsys.readouterr().err


def test_already_promoted_exits_without_prompt(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses(rolling=NEW, rolling_raw=NEW)) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=False),
        patch("builtins.input", side_effect=_never_prompt),
    ):
        assert main() == 0
    assert _mutations(mock_run) == []
    assert "already promoted" in capsys.readouterr().out


def test_promote_explicit_version(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses()),
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        result = main()
    assert result == 0
    assert "Promoted" in capsys.readouterr().out


def test_promote_from_version_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("3.1.0\n")
    with (
        patch("vergil_tooling.bin.vrg_promote.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-promote"]),
        patch(_RUN, side_effect=_git_responses()) as mock_run,
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        result = main()
    assert result == 0
    assert "v3.1" in capsys.readouterr().out
    assert ["git", "tag", "-f", "v3.1", "refs/tags/v3.1.0"] in _mutations(mock_run)


def test_promote_no_version_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_promote.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-promote"]),
    ):
        result = main()
    assert result == 1
    err = capsys.readouterr().err
    assert "VERSION" in err


def test_promote_invalid_version(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["vrg-promote", "invalid"]), patch(_RUN) as mock_run:
        result = main()
    assert result == 1
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "Error" in err


def test_push_failure_reports_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = _sp.CalledProcessError(1, ["git", "push"])
    err.stderr = "! [rejected] v2.1 (stale info)\n"
    err.stdout = ""
    with (
        patch("sys.argv", ["vrg-promote", "2.1.50"]),
        patch(_RUN, side_effect=_git_responses(fail={"push": err})),
        patch(_READ, return_value="false"),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        assert main() == 1
    err = capsys.readouterr().err
    assert err.count("stale info") == 1
    assert "Error: origin did not accept the move of v2.1" in err
