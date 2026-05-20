"""Tests for vergil_tooling.bin.vrg_gh."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_gh import main

# -- no arguments / missing subcommand ----------------------------------------


def test_no_args_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_none_argv_reads_sys_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_gh.sys.argv", ["vrg-gh"]):
        assert main(None) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_top_level_only_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["issue"]) != 0


# -- allowed subcommand pairs ------------------------------------------------

_ALLOWED_PAIRS: list[tuple[str, str]] = [
    ("issue", "view"),
    ("issue", "create"),
    ("issue", "close"),
    ("issue", "edit"),
    ("issue", "list"),
    ("issue", "comment"),
    ("pr", "view"),
    ("pr", "checks"),
    ("pr", "list"),
    ("pr", "diff"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("run", "list"),
    ("run", "view"),
    ("run", "watch"),
    ("repo", "view"),
    ("label", "list"),
    ("label", "create"),
]


@pytest.mark.parametrize(("top", "sub"), _ALLOWED_PAIRS)
def test_allowed_pair_passes(top: str, sub: str) -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="fake-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main([top, sub])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "gh"
    assert args[1] == top
    assert args[2] == sub


# -- unrecognized subcommands ------------------------------------------------


def test_unrecognized_top_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["codespace", "list"]) != 0
    err = capsys.readouterr().err
    assert "codespace" in err


def test_unrecognized_second_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["issue", "pin"]) != 0
    err = capsys.readouterr().err
    assert "pin" in err


# -- denied subcommand pairs -------------------------------------------------

_DENIED_PAIRS: list[tuple[str, str]] = [
    ("repo", "edit"),
    ("repo", "create"),
    ("repo", "delete"),
]


@pytest.mark.parametrize(("top", "sub"), _DENIED_PAIRS)
def test_denied_pair(top: str, sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([top, sub]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


def test_pr_create_denied_suggests_vrg_submit_pr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["pr", "create"])
    err = capsys.readouterr().err
    assert "vrg-submit-pr" in err


def test_pr_close_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "close"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


# -- top-level denials -------------------------------------------------------


def test_api_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "repos/owner/repo"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


def test_auth_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["auth", "login"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


# -- pr review flag gating ---------------------------------------------------


def test_pr_review_comment_allowed() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="fake-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main(["pr", "review", "--comment", "-b", "looks good"])
    assert rc == 0


def test_pr_review_no_flags_allowed() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="fake-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main(["pr", "review"])
    assert rc == 0


def test_pr_review_approve_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "review", "--approve"]) != 0
    err = capsys.readouterr().err
    assert "approve" in err.lower()


# -- credential selection: account discovery ----------------------------------


_AUTH_STATUS_TWO_ACCOUNTS = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
"""


def test_discover_accounts() -> None:
    from vergil_tooling.lib.github import _discover_accounts

    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_TWO_ACCOUNTS,
        )
        human, agent = _discover_accounts()
    assert human == "jdoe"
    assert agent == "jdoe-vergil"


_AUTH_STATUS_DUPLICATE_HUMAN = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe (token)
  - Active account: false
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
"""


def test_discover_accounts_deduplicates() -> None:
    from vergil_tooling.lib.github import _discover_accounts

    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_DUPLICATE_HUMAN,
        )
        human, agent = _discover_accounts()
    assert human == "jdoe"
    assert agent == "jdoe-vergil"


_AUTH_STATUS_MANY_ACCOUNTS = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
  ✓ Logged in to github.com account jdoe-mimir (keyring)
  - Active account: false
  ✓ Logged in to github.com account jdoe-agent (keyring)
  - Active account: false
"""


def test_discover_accounts_ignores_other_accounts() -> None:
    from vergil_tooling.lib.github import _discover_accounts

    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_MANY_ACCOUNTS,
        )
        human, agent = _discover_accounts()
    assert human == "jdoe"
    assert agent == "jdoe-vergil"


_AUTH_STATUS_NO_AGENT = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
"""


def test_discover_accounts_missing_vergil(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vergil_tooling.lib.github import _discover_accounts

    with (
        patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        pytest.raises(SystemExit),
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_NO_AGENT,
        )
        _discover_accounts()


# -- credential selection: workaround uses human for all (#799) ---------------


def test_default_uses_human_token_workaround() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh._discover_accounts",
            return_value=("jdoe", "jdoe-vergil"),
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="human-token\n")
        from vergil_tooling.bin.vrg_gh import _get_token

        token = _get_token(["issue", "list"])
    assert token == "human-token"  # noqa: S105
    token_call = mock_run.call_args_list[-1]
    assert "jdoe" in token_call[0][0]
    assert "jdoe-vergil" not in token_call[0][0]


# -- credential selection: escalation for pr merge ---------------------------


def test_issue_close_escalates_to_human() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh._discover_accounts",
            return_value=("jdoe", "jdoe-vergil"),
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="human-token\n")
        from vergil_tooling.bin.vrg_gh import _get_token

        token = _get_token(["issue", "close", "42"])
    assert token == "human-token"  # noqa: S105
    token_call = mock_run.call_args_list[-1]
    assert "jdoe" in token_call[0][0]
    assert "jdoe-vergil" not in token_call[0][0]


def test_pr_merge_release_branch_escalates() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh._discover_accounts",
            return_value=("jdoe", "jdoe-vergil"),
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
        patch("vergil_tooling.bin.vrg_gh._validate_merge_context"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="human-token\n")
        from vergil_tooling.bin.vrg_gh import _get_token

        token = _get_token(["pr", "merge", "42"])
    assert token == "human-token"  # noqa: S105
    token_call = mock_run.call_args_list[-1]
    assert "jdoe" in token_call[0][0]
    assert "jdoe-vergil" not in token_call[0][0]


def test_pr_merge_allowed_with_valid_context() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="human-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main(["pr", "merge", "42"])
    assert rc == 0


def test_pr_merge_denied_without_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["pr", "merge"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


# -- GH_TOKEN injection ------------------------------------------------------


def test_gh_token_injected_into_env() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="injected-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GH_TOKEN"] == "injected-token"  # noqa: S105


# -- subprocess passthrough ---------------------------------------------------


def test_subprocess_uses_shell_false() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="fake-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("shell") is not True


def test_returns_subprocess_exit_code() -> None:
    with (
        patch("vergil_tooling.bin.vrg_gh._get_token", return_value="fake-token"),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 128
        rc = main(["issue", "list"])
    assert rc == 128
