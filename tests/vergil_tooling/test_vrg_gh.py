"""Tests for vergil_tooling.bin.vrg_gh."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_gh import main
from vergil_tooling.lib import github, identity_mode


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# -- no arguments / missing subcommand ----------------------------------------


def test_no_args_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_help_prints_wrapper_help(capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("-h", "--help"):
        assert main([flag]) == 0
        out = capsys.readouterr().out
        assert "gh wrapper" in out.lower()
        assert "denied outright" in out.lower()


def test_none_argv_reads_sys_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_gh.sys.argv", ["vrg-gh"]):
        assert main(None) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_top_level_only_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["issue"]) != 0


# -- allowed subcommand pairs ------------------------------------------------

_ALLOWED_PAIRS: list[tuple[str, str]] = [
    ("issue", "view"),
    ("issue", "close"),
    ("issue", "reopen"),
    ("issue", "edit"),
    ("issue", "list"),
    ("issue", "comment"),
    ("issue", "transfer"),
    ("pr", "view"),
    ("pr", "checks"),
    ("pr", "list"),
    ("pr", "diff"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("pr", "update-branch"),
    ("run", "list"),
    ("run", "view"),
    ("run", "watch"),
    ("run", "rerun"),
    ("workflow", "list"),
    ("workflow", "view"),
    ("cache", "list"),
    ("repo", "view"),
    ("repo", "list"),
    ("release", "list"),
    ("release", "view"),
    ("release", "download"),
    ("label", "list"),
    ("label", "create"),
    ("variable", "list"),
    ("variable", "get"),
    ("search", "code"),
    ("search", "commits"),
    ("search", "issues"),
    ("search", "prs"),
    ("search", "repos"),
]


@pytest.mark.parametrize(("top", "sub"), _ALLOWED_PAIRS)
def test_allowed_pair_passes(top: str, sub: str) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main([top, sub])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "gh"
    assert args[1] == top
    assert args[2] == sub


# -- search (read-only, cross-org code search) -------------------------------


def test_search_code_with_query_and_owner_passes_through() -> None:
    # Acceptance: `vrg-gh search code '<query>' --owner <org>` runs read-only
    # for cross-org fleet sweeps (issue #2505). The query and flags forward
    # unchanged.
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["search", "code", "secrets: inherit", "--owner", "vergil-project"])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[:4] == ["gh", "search", "code", "secrets: inherit"]
    assert "--owner" in args
    assert "vergil-project" in args


def test_search_has_no_write_subcommands() -> None:
    # Guard against accidentally widening `search` beyond gh's read-only
    # subcommands (code/commits/issues/prs/repos).
    from vergil_tooling.bin.vrg_gh import _ALLOWED

    assert _ALLOWED["search"] == {"code", "commits", "issues", "prs", "repos"}


# -- release (read-only) -----------------------------------------------------


def test_release_is_read_only() -> None:
    # Guard against widening `release` beyond the actions that change nothing
    # on GitHub: `list`/`view` (issue #2539) and `download` (issue #5002).
    from vergil_tooling.bin.vrg_gh import _ALLOWED, _ALLOWED_AUDIT

    assert _ALLOWED["release"] == {"list", "view", "download"}
    assert "release" not in _ALLOWED_AUDIT


def test_release_download_passes_through_for_the_user_identity() -> None:
    argv = [
        "release",
        "download",
        "v1.2.3",
        "--repo",
        "vergil-project/fixture",
        "--pattern",
        "*.json",
        "--dir",
        "build/evidence",
    ]
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.identity_mode.current_mode",
            return_value=identity_mode.IdentityMode.USER,
        ),
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(argv)
    assert rc == 0
    assert mock_run.call_args[0][0] == ["gh", *argv]


@pytest.mark.parametrize("sub", ["create", "edit", "delete", "upload"])
def test_release_write_actions_rejected(sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    # Write/mutating release actions are not on the allowlist and are rejected
    # by the action-level deny-by-default (issue #2539).
    assert main(["release", sub]) != 0
    err = capsys.readouterr().err
    assert sub in err


# -- variable (read-only) ----------------------------------------------------


def test_variable_is_read_only() -> None:
    # Guard against widening `variable` beyond its read actions (issue #5001).
    from vergil_tooling.bin.vrg_gh import _ALLOWED, _ALLOWED_AUDIT

    assert _ALLOWED["variable"] == {"list", "get"}
    assert "variable" not in _ALLOWED_AUDIT


@pytest.mark.parametrize("sub", ["set", "delete"])
def test_variable_write_actions_rejected(sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        rc = main(["variable", sub, "NAME", "--body", "x"])
    assert rc != 0
    mock_run.assert_not_called()
    assert f"variable {sub} is not recognized" in capsys.readouterr().err


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


def test_pr_close_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "close"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_issue_create_denied_redirects_to_vrg_issue_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["issue", "create"]) != 0
    err = capsys.readouterr().err.lower()
    assert "denied" in err
    assert "vrg-issue-create" in err


def test_issue_create_denied_names_vrg_intake_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["issue", "create"]) != 0
    err = capsys.readouterr().err
    assert "vrg-intake-create" in err
    assert "intake issue linked under the intake epic" in err
    assert "vrg-triage-create" not in err


# -- workflow and cache (read-only; workflow run is a human action) ----------


@pytest.mark.parametrize("mode", ["human", "user", "audit"])
def test_workflow_run_denied_for_every_identity(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Dispatching a workflow can start CD or a release, which is a human
    # action, so no identity may dispatch one through the wrapper (#3420).
    monkeypatch.setenv("VRG_IDENTITY_MODE", mode)
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        assert main(["workflow", "run", "release.yml"]) != 0
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "workflow run is denied" in err
    assert "CD or a release" in err
    assert "human action" in err


@pytest.mark.parametrize(("top", "sub"), [("workflow", "enable"), ("workflow", "disable")])
def test_workflow_write_actions_rejected(
    top: str, sub: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        assert main([top, sub]) != 0
    mock_run.assert_not_called()
    assert f"{top} {sub} is not recognized" in capsys.readouterr().err


def test_cache_delete_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        assert main(["cache", "delete", "--all"]) != 0
    mock_run.assert_not_called()
    assert "cache delete is not recognized" in capsys.readouterr().err


# -- top-level denials -------------------------------------------------------


def test_auth_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["auth", "login"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


# -- pr review flag gating ---------------------------------------------------


def test_pr_review_comment_allowed() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["pr", "review", "--comment", "-b", "looks good"])
    assert rc == 0


def test_pr_review_no_flags_allowed() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["pr", "review"])
    assert rc == 0


def test_pr_review_approve_denied_for_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert main(["pr", "review", "--approve"]) != 0
    err = capsys.readouterr().err
    assert "approve" in err.lower()


# -- token injection --------------------------------------------------------


def test_injects_app_token_when_available() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value="ghs_app_token",
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GH_TOKEN"] == "ghs_app_token"  # noqa: S105


def test_no_env_injection_when_no_app_token() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert "env" not in kwargs or kwargs.get("env") is None


# -- installation selection via -R/--repo (#1413) -----------------------------


class TestRepoFlagInstallationSelection:
    """The -R/--repo owner, not the cwd, selects the App installation."""

    @pytest.mark.parametrize(
        "extra",
        [
            ["-R", "other-owner/repo"],
            ["--repo", "other-owner/repo"],
            ["--repo=other-owner/repo"],
            ["-Rother-owner/repo"],
            ["-R", "github.com/other-owner/repo"],
            ["-R", "https://github.com/other-owner/repo"],
        ],
    )
    def test_repo_flag_selects_target_owner_installation(self, extra: list[str]) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value="ghs_other_token",
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "list", *extra])
        assert rc == 0
        kwargs = mock_token.call_args.kwargs
        assert kwargs.get("org") == "other-owner"
        assert kwargs.get("require_installation") is True

    def test_no_repo_flag_keeps_cwd_detection(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "list"])
        assert rc == 0
        kwargs = mock_token.call_args.kwargs
        assert kwargs.get("org") is None
        assert not kwargs.get("require_installation")

    def test_last_repo_flag_wins(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value="ghs_token",
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "list", "-R", "first-owner/repo", "-R", "second-owner/repo"])
        assert rc == 0
        assert mock_token.call_args.kwargs.get("org") == "second-owner"

    @pytest.mark.parametrize(
        "extra",
        [
            ["-R"],  # dangling flag: gh itself rejects it downstream
            ["-R", "not-a-repo"],  # unparseable value: gh rejects it downstream
        ],
    )
    def test_unusable_repo_flag_falls_back_to_cwd_detection(self, extra: list[str]) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "list", *extra])
        assert rc == 0
        kwargs = mock_token.call_args.kwargs
        assert kwargs.get("org") is None
        assert not kwargs.get("require_installation")

    def test_unknown_owner_fails_loudly(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = github.NoInstallationError("unknown-owner", ["vergil-project", "wphillipmoore"])
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                side_effect=err,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["issue", "list", "-R", "unknown-owner/repo"])
        assert rc != 0
        mock_run.assert_not_called()
        captured = capsys.readouterr().err
        assert "unknown-owner" in captured
        assert "vergil-project" in captured
        assert "wphillipmoore" in captured

    @pytest.mark.parametrize("extra", [[], ["-R", "vergil-project/repo"]])
    def test_app_auth_failure_fails_loudly_without_ambient_fallback(
        self, extra: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A broken App key must never run gh on ambient (possibly human) auth (#3214).
        err = github.AppAuthError(
            "vergil-project", "12345", Path("/keys/app.pem"), "HTTP Error 401: Unauthorized"
        )
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                side_effect=err,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["issue", "list", *extra])
        assert rc == 1
        mock_run.assert_not_called()
        captured = capsys.readouterr().err
        assert captured.startswith("vrg-gh: GitHub App authentication failed")
        assert "HTTP Error 401" in captured
        assert "Refusing to fall back to ambient gh auth" in captured

    def test_no_target_org_in_app_mode_fails_loudly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # App configured, no -R and no repo cwd: never run gh on ambient auth (#3217).
        err = github.NoTargetOrgError("12345", Path("/keys/app.pem"))
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                side_effect=err,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["issue", "list"])
        assert rc == 1
        mock_run.assert_not_called()
        captured = capsys.readouterr().err
        assert captured.startswith("vrg-gh: GitHub App mode is configured")
        assert "no target org could be determined" in captured
        assert "-R owner/repo" in captured


# -- installation selection from every form of target owner (#4762) ----------


def _token_call(argv: list[str], *, app_mode: bool = True) -> tuple[int, dict[str, object] | None]:
    """Run ``main(argv)``; return its exit code and get_installation_token's kwargs."""
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value="ghs_token",
        ) as mock_token,
        patch("vergil_tooling.bin.vrg_gh.github.is_app_mode", return_value=app_mode),
        # Human mode allows every command under test, `api` included.
        patch(
            "vergil_tooling.bin.vrg_gh.identity_mode.current_mode",
            return_value=identity_mode.IdentityMode.HUMAN,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(argv)
    kwargs = dict(mock_token.call_args.kwargs) if mock_token.call_args else None
    return rc, kwargs


class TestTargetOwnerInstallationSelection:
    """Every way a command names its target owner selects that owner's installation."""

    @pytest.mark.parametrize(
        "argv",
        [
            # repo view: positional OWNER/REPO, HOST/OWNER/REPO, URL
            ["repo", "view", "other-owner/repo"],
            ["repo", "view", "github.com/other-owner/repo"],
            ["repo", "view", "https://github.com/other-owner/repo"],
            ["repo", "view", "other-owner/repo", "--json", "nameWithOwner"],
            ["repo", "view", "--json", "nameWithOwner", "other-owner/repo"],
            # repo list: positional owner, before or after flags
            ["repo", "list", "other-owner"],
            ["repo", "list", "--limit", "1000", "other-owner", "--json", "name"],
            ["repo", "list", "-L1000", "other-owner"],
            # issue / pr: a URL positional
            ["issue", "view", "https://github.com/other-owner/repo/issues/7"],
            ["issue", "comment", "https://github.com/other-owner/repo/issues/7", "-b", "hi"],
            ["issue", "edit", "https://github.com/other-owner/repo/issues/7", "--title", "t"],
            ["pr", "view", "https://github.com/other-owner/repo/pull/3", "--json", "state"],
            ["pr", "checks", "--json", "name", "https://github.com/other-owner/repo/pull/3"],
            # a boolean short flag never consumes the next token
            ["pr", "view", "-c", "https://github.com/other-owner/repo/pull/3"],
            # issue transfer: the destination repo
            ["issue", "transfer", "7", "other-owner/dest"],
            # search: --owner, --repo and query qualifiers
            ["search", "prs", "--owner", "other-owner", "--state", "open"],
            ["search", "prs", "--owner=other-owner"],
            ["search", "issues", "--repo", "other-owner/repo"],
            ["search", "issues", "-R", "other-owner/repo", "bug"],
            ["search", "issues", "is:open org:other-owner"],
            ["search", "prs", "repo:other-owner/repo", "is:open"],
            ["search", "repos", "user:other-owner"],
            ["search", "issues", "(org:other-owner OR org:Other-Owner)"],
            ["search", "code", "secrets: inherit", "--owner", "other-owner"],
            # release download: -R/--repo, with a value flag's value never a target
            ["release", "download", "v1.0.0", "-R", "other-owner/repo", "-p", "*.json"],
            ["release", "download", "--dir", "evidence/x", "--repo=other-owner/repo"],
            # variable: -R/--repo, and the --org/-o an org's variables are read from
            ["variable", "list", "--repo", "other-owner/repo"],
            ["variable", "list", "--org", "other-owner"],
            ["variable", "list", "-o", "other-owner", "--json", "name"],
            ["variable", "get", "NAME", "--org=other-owner"],
            ["variable", "get", "-e", "prod", "NAME", "-R", "other-owner/repo"],
            # api: an owner-scoped endpoint
            ["api", "repos/other-owner/repo/actions/runs"],
            ["api", "/orgs/other-owner/repos", "--paginate"],
            ["api", "-H", "Accept: x", "users/other-owner"],
            # api: a full-URL endpoint, github.com and GHES
            ["api", "https://api.github.com/repos/other-owner/repo/actions/runs"],
            ["api", "--paginate", "https://api.github.com/orgs/other-owner/repos"],
            ["api", "https://ghes.example.com/api/v3/repos/other-owner/repo"],
        ],
    )
    def test_target_owner_selects_its_installation(self, argv: list[str]) -> None:
        rc, kwargs = _token_call(argv)
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") == "other-owner"
        assert kwargs.get("require_installation") is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["repo", "view"],
            ["repo", "view", "bare-repo-name"],
            ["repo", "list"],
            ["issue", "view", "7"],
            ["pr", "view", "feature/12-some-branch"],
            ["pr", "view", "123", "--json", "title"],
            # a flag's value is never read as a target
            ["issue", "comment", "7", "--body", "see https://github.com/other-owner/r/issues/1"],
            ["pr", "review", "7", "-b", "https://github.com/other-owner/r/pull/1", "--comment"],
            ["repo", "view", "--branch", "other-owner/branch"],
            ["search", "prs", "--json", "repository", "--jq", "{org: .repository}"],
            ["search", "issues", "bug", "-repo:other-owner/r"],
            ["variable", "list"],
            ["variable", "get", "other-owner"],
            # --org names an owner only for variable, not for other commands
            ["repo", "list", "--org=other-owner"],
            ["api", "repos/{owner}/{repo}/pulls"],
            ["api", "user"],
            ["api", "https://api.github.com/rate_limit"],
        ],
    )
    def test_no_target_owner_keeps_cwd_detection(self, argv: list[str]) -> None:
        rc, kwargs = _token_call(argv)
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") is None
        assert not kwargs.get("require_installation")

    def test_double_dash_ends_flags(self) -> None:
        rc, kwargs = _token_call(["search", "issues", "--", "-label:x", "org:other-owner"])
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") == "other-owner"

    def test_clustered_short_flags_read_the_value_flag(self) -> None:
        rc, kwargs = _token_call(["issue", "view", "-cR", "other-owner/repo", "7"])
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") == "other-owner"

    def test_same_owner_named_twice_is_one_owner(self) -> None:
        rc, kwargs = _token_call(
            ["pr", "view", "https://github.com/Other-Owner/repo/pull/3", "-R", "other-owner/repo"]
        )
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") == "Other-Owner"

    @pytest.mark.parametrize(
        "argv",
        [
            ["search", "prs", "--owner", "owner-a", "--owner", "owner-b"],
            ["search", "issues", "--repo", "owner-a/r", "--repo", "owner-b/r"],
            ["search", "issues", "org:owner-a", "--owner", "owner-b"],
            ["pr", "view", "https://github.com/owner-a/r/pull/3", "-R", "owner-b/r"],
            ["issue", "transfer", "https://github.com/owner-a/r/issues/7", "owner-b/dest"],
        ],
    )
    def test_multiple_owners_refused_in_app_mode(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("vergil_tooling.bin.vrg_gh.github.get_installation_token") as mock_token,
            patch("vergil_tooling.bin.vrg_gh.github.is_app_mode", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_gh.identity_mode.current_mode",
                return_value=identity_mode.IdentityMode.HUMAN,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(argv)
        assert rc == 1
        mock_token.assert_not_called()
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "more than one owner (owner-a, owner-b)" in err
        assert "Run it once per owner" in err

    @pytest.mark.parametrize(
        "argv",
        [
            ["api", "graphql", "-f", "query={viewer{login}}"],
            ["api", "/graphql", "-F", "owner=other-owner"],
            ["api", "https://api.github.com/graphql", "-f", "query=x"],
            ["api", "https://ghes.example.com/api/graphql", "-f", "query=x"],
        ],
    )
    def test_graphql_refused_in_app_mode(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A GraphQL call names its owner inside the query, out of vrg-gh's sight,
        # so in App mode it would silently take the cwd org's token.
        with (
            patch("vergil_tooling.bin.vrg_gh.github.get_installation_token") as mock_token,
            patch("vergil_tooling.bin.vrg_gh.github.is_app_mode", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_gh.identity_mode.current_mode",
                return_value=identity_mode.IdentityMode.HUMAN,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(argv)
        assert rc == 1
        mock_token.assert_not_called()
        mock_run.assert_not_called()
        assert "api graphql is refused in GitHub App mode" in capsys.readouterr().err

    def test_graphql_allowed_on_ambient_auth(self) -> None:
        rc, kwargs = _token_call(["api", "graphql", "-f", "query=x"], app_mode=False)
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") is None

    def test_multiple_owners_allowed_on_ambient_auth(self) -> None:
        # Without App mode there is no installation token to scope: ambient
        # (human) gh auth serves every owner, so nothing is refused.
        rc, kwargs = _token_call(
            ["search", "prs", "--owner", "owner-a", "--owner", "owner-b"], app_mode=False
        )
        assert rc == 0
        assert kwargs is not None
        assert kwargs.get("org") == "owner-a"

    def test_positional_owner_without_installation_fails_loudly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = github.NoInstallationError("other-owner", ["vergil-project"])
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                side_effect=err,
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["repo", "list", "other-owner"])
        assert rc == 1
        mock_run.assert_not_called()
        assert mock_token.call_args.kwargs.get("require_installation") is True
        captured = capsys.readouterr().err
        assert "no installation for owner 'other-owner' (the command's target)" in captured
        assert "vergil-project" in captured


# -- subprocess passthrough ---------------------------------------------------


def test_subprocess_uses_shell_false() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("shell") is not True


def test_returns_subprocess_exit_code() -> None:
    err = subprocess.CalledProcessError(128, ["gh", "issue", "list"])
    err.stdout = ""
    err.stderr = "fatal\n"
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "list"])
    assert rc == 128


def test_stdout_and_stderr_replayed_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(stdout="output\n", stderr="warning\n")
        main(["issue", "list"])
    captured = capsys.readouterr()
    assert "output" in captured.out
    assert "warning" in captured.err


def test_stdout_and_stderr_replayed_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, ["gh"])
    err.stdout = "partial\n"
    err.stderr = "HTTP 404 Not Found\n"
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "view", "42"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "partial" in captured.out
    assert "404" in captured.err


def test_failure_with_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, ["gh"])
    err.stdout = ""
    err.stderr = ""
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "view", "42"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# -- retry behaviour ----------------------------------------------------------


def _api_error(
    returncode: int = 1, stderr: str = "", stdout: str = ""
) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode=returncode, cmd=["gh"])
    exc.stderr = stderr
    exc.stdout = stdout
    return exc


class TestVrgGhRetry:
    def test_retries_on_502_then_succeeds(self) -> None:
        err = _api_error(stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed(stdout="ok\n")],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            rc = main(["issue", "list"])
        assert rc == 0
        assert mock_run.call_count == 2

    def test_gives_up_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 503 Service Unavailable")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            rc = main(["issue", "list"])
        assert rc == 1

    def test_no_retry_on_non_transient_error(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
        ):
            rc = main(["issue", "view", "42"])
        assert rc == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()


# -- identity-aware restrictions ----------------------------------------------


class TestAgentDenials:
    """Commands blocked for the user agent identity."""

    @pytest.fixture(autouse=True)
    def _agent_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("pr", "edit"),
            ("pr", "merge"),
        ],
    )
    def test_agent_denied_pair(
        self, top: str, sub: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([top, sub]) != 0
        err = capsys.readouterr().err
        assert "denied" in err.lower()

    def test_issue_edit_allowed_for_user_agent(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "edit", "42", "--add-label", "chore"])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "edit"]

    def test_issue_close_allowed_for_user_agent(self) -> None:
        # Re-allowed for USER: closing is now automated and documented as
        # mechanical, so a manual close is safe and useful (issue #1966).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "close", "42"])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "close"]

    def test_issue_reopen_allowed_for_user_agent(self) -> None:
        # Re-allowed for USER: reopen is non-destructive and reversible
        # (an errant reopen is trivially re-closed), mirroring `close`. This
        # lets an agent run reopen remediation directly (issue #2260).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "reopen", "42"])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "reopen"]

    def test_issue_transfer_allowed_for_user_agent(self) -> None:
        # Allowed for USER: transferring a finite epic's issues into a
        # relocation target is a mechanical repository move, and the
        # audit identity stays barred because `issue` is absent from its
        # allowlist (issue #2270).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["issue", "transfer", "42", "vergil-project/other-repo"])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "transfer"]

    @pytest.mark.parametrize("extra", [[], ["--rebase"]])
    def test_pr_update_branch_allowed_for_user_agent(self, extra: list[str]) -> None:
        # Allowed for USER: a server-side update of a PR branch onto its base
        # needs no checkout or force-push and crosses no human gate (issue
        # #3542). AUDIT stays barred (see TestAuditAllowlist).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "update-branch", "42", *extra])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args == ["gh", "pr", "update-branch", "42", *extra]

    @pytest.mark.parametrize("extra", [[], ["--failed"]])
    def test_run_rerun_allowed_for_user_agent(self, extra: list[str]) -> None:
        # Allowed for USER: retrying a failed CI job repeats an existing run
        # and crosses no human gate (issue #3420). AUDIT stays barred (see
        # TestAuditAllowlist).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["run", "rerun", "123", *extra])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args == ["gh", "run", "rerun", "123", *extra]

    def test_pr_merge_denied_unconditionally_for_agent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["pr", "merge", "42"]) != 0
        err = capsys.readouterr().err
        assert "denied" in err.lower()

    def test_pr_create_no_vrg_submit_pr_mention(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["pr", "create"])
        err = capsys.readouterr().err
        assert "vrg-submit-pr" not in err


class TestHumanAllowlist:
    """Human identity retains full allowlist."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("issue", "close"),
            ("issue", "reopen"),
            ("issue", "edit"),
            ("pr", "edit"),
        ],
    )
    def test_human_allowed_pair(self, top: str, sub: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main([top, sub])
        assert rc == 0

    def test_pr_merge_allowed_for_human_with_context(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "merge", "42"])
        assert rc == 0

    def test_pr_merge_denied_without_context_for_human(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["pr", "merge"]) != 0
        err = capsys.readouterr().err
        assert "denied" in err.lower()

    def test_pr_create_mentions_vrg_submit_pr(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["pr", "create"])
        err = capsys.readouterr().err
        assert "vrg-submit-pr" in err


class TestAuditAllowlist:
    """Audit identity can only do PR read/review operations."""

    @pytest.fixture(autouse=True)
    def _audit_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("pr", "view"),
            ("pr", "diff"),
            ("pr", "list"),
            ("pr", "checks"),
            ("pr", "comment"),
        ],
    )
    def test_audit_pr_read_allowed(self, top: str, sub: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main([top, sub])
        assert rc == 0

    def test_audit_pr_review_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "review"])
        assert rc == 0

    def test_audit_pr_review_approve_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "review", "--approve"])
        assert rc == 0

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("issue", "view"),
            ("issue", "list"),
            ("issue", "create"),
            ("issue", "edit"),
            ("issue", "close"),
            ("issue", "reopen"),
            ("run", "list"),
            ("repo", "view"),
            ("label", "list"),
            ("search", "code"),
        ],
    )
    def test_audit_non_pr_denied(
        self, top: str, sub: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([top, sub]) != 0

    @pytest.mark.parametrize("extra", [[], ["--rebase"]])
    def test_audit_pr_update_branch_denied(
        self, extra: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # update-branch writes to the PR branch, so the read/review-shaped
        # audit identity stays barred (issue #3542).
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            assert main(["pr", "update-branch", "42", *extra]) != 0
        mock_run.assert_not_called()
        assert "pr update-branch is not recognized" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("workflow", "list"),
            ("workflow", "view"),
            ("cache", "list"),
        ],
    )
    def test_audit_ci_read_allowed(self, top: str, sub: str) -> None:
        # Read-only CI configuration and state, which a reviewer may need to
        # judge a PR's checks (issue #3420).
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main([top, sub])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args == ["gh", top, sub]

    def test_audit_run_rerun_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        # rerun starts CI work, so the read/review-shaped audit identity
        # stays barred (issue #3420).
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            assert main(["run", "rerun", "123"]) != 0
        mock_run.assert_not_called()
        assert "run is not recognized" in capsys.readouterr().err


class TestApiAccess:
    """`gh api` is identity-aware: human full, audit GET-only, user denied."""

    def test_user_api_denied(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        rc = main(["api", "repos/o/r/pulls/1/reviews"])
        assert rc == 1
        assert "denied for the user identity" in capsys.readouterr().err

    def test_audit_api_get_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1/reviews"])
        assert rc == 0

    def test_audit_api_explicit_get_method_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1", "--method=GET"])
        assert rc == 0

    def test_audit_api_trailing_method_flag_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A dangling ``-X`` with no value leaves the verb at gh's GET default.
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1", "-X"])
        assert rc == 0

    @pytest.mark.parametrize(
        "extra",
        [
            ["-X", "POST"],
            ["--method", "DELETE"],
            ["--method=PATCH"],
            ["-f", "title=x"],
            ["--field", "body=y"],
            ["--field=body=z"],
        ],
    )
    def test_audit_api_write_denied(
        self,
        extra: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        rc = main(["api", "repos/o/r/issues/1/comments", *extra])
        assert rc == 1
        assert "read-only GET" in capsys.readouterr().err

    def test_human_api_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1/merge", "-X", "PUT"])
        assert rc == 0


class TestRunJobLog:
    """`run job-log` reads one job's log from the per-job endpoint only (#4597)."""

    _ENDPOINT = "repos/vergil-project/vergil-containers/actions/jobs/44088128201/logs"

    @pytest.mark.parametrize("mode", ["user", "audit", "human"])
    @pytest.mark.parametrize(
        "args",
        [
            ["--repo", "vergil-project/vergil-containers", "--job", "44088128201"],
            ["-R", "vergil-project/vergil-containers", "--job", "44088128201"],
            ["--job=44088128201", "--repo=vergil-project/vergil-containers"],
            ["-R=vergil-project/vergil-containers", "--job", "44088128201"],
        ],
    )
    def test_calls_only_the_per_job_log_endpoint(
        self,
        mode: str,
        args: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", mode)
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value="ghs_tok",
            ) as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="line 1\nline 2\n")
            rc = main(["run", "job-log", *args])
        assert rc == 0
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == [
            "gh",
            "api",
            "--method",
            "GET",
            "--allow-escape-sequences",
            self._ENDPOINT,
        ]
        assert mock_run.call_args.kwargs["env"]["GH_TOKEN"] == "ghs_tok"
        assert mock_token.call_args.kwargs == {
            "org": "vergil-project",
            "require_installation": True,
        }
        assert capsys.readouterr().out == "line 1\nline 2\n"

    @pytest.mark.parametrize(
        ("raw", "printed"),
        [
            ("\x1b[36;1mgit config\x1b[0m\n", "git config\n"),
            ("a\x1b]0;title\x07b\n", "ab\n"),
            ("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b\n", "alinkb\n"),
            ("a\x1b]0;unterminated\nnext\n", "a\nnext\n"),
            ("a\x1bcb\n", "ab\n"),
            ("a\x1b", "a"),
            ("a\x9b31mb\x9d0;t\x07c\n", "a31mb0;tc\n"),
            ("crlf\r\nnul\x00bell\x07del\x7f\n", "crlf\nnulbelldel\n"),
            ("﻿tab\there é✓\n", "﻿tab\there é✓\n"),
        ],
    )
    def test_terminal_controls_stripped(
        self, raw: str, printed: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout=raw, stderr="warn \x1b[31mred\x1b[0m\n")
            rc = main(["run", "job-log", "--repo", "o/r", "--job", "1"])
        assert rc == 0
        out, err = capsys.readouterr()
        assert out == printed
        assert err == "warn red\n"

    def test_terminal_controls_stripped_on_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(
            1, ["gh"], output="\x1b[2Jout\n", stderr="\x1b]0;t\x07HTTP 404\n"
        )
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
        ):
            rc = main(["run", "job-log", "--repo", "o/r", "--job", "1"])
        assert rc == 1
        assert capsys.readouterr() == ("out\n", "HTTP 404\n")

    def test_other_commands_output_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="\x1b[1mbold\x1b[0m\r\n")
            assert main(["run", "view", "1"]) == 0
        assert capsys.readouterr().out == "\x1b[1mbold\x1b[0m\r\n"

    def test_repo_name_with_dots_accepted(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["run", "job-log", "--repo", "vergil-project/.github", "--job", "7"])
        assert rc == 0
        assert mock_run.call_args[0][0][-1] == "repos/vergil-project/.github/actions/jobs/7/logs"

    def test_no_installation_fails_loudly(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = github.NoInstallationError("elsewhere", ["vergil-project"])
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                side_effect=err,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["run", "job-log", "--repo", "elsewhere/r", "--job", "1"])
        assert rc == 1
        mock_run.assert_not_called()
        assert "no installation for owner 'elsewhere'" in capsys.readouterr().err

    def test_gh_failure_returns_its_exit_code(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 404: Not Found\n")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
        ):
            rc = main(["run", "job-log", "--repo", "o/r", "--job", "1"])
        assert rc == 1
        assert "HTTP 404" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ([], "missing --repo and --job"),
            (["--repo", "o/r"], "missing --job"),
            (["--job", "1"], "missing --repo"),
            (["--repo"], "--repo requires a value"),
            (["--repo", "o/r", "--job", "1", "--repo", "o/s"], "more than once"),
            (["--repo", "o/r", "--job", "1", "-X", "POST"], "unexpected argument '-X'"),
            (["--repo", "o/r", "--job", "1", "--method=DELETE"], "unexpected argument"),
            (["--repo", "o/r", "--job", "1", "-f", "a=b"], "unexpected argument '-f'"),
            (["--repo", "o/r", "--job", "1", "--input", "x"], "unexpected argument"),
            (["--repo", "o/r", "--job", "1", "--hostname", "evil"], "unexpected argument"),
            (["--repo", "o/r", "--job", "1", "extra"], "unexpected argument 'extra'"),
            (["-Ro/r", "--job", "1"], "unexpected argument"),
            (["--repo", "o", "--job", "1"], "--repo must be"),
            (["--repo", "o/r/x", "--job", "1"], "--repo must be"),
            (["--repo", "github.com/o/r", "--job", "1"], "--repo must be"),
            (["--repo", "https://github.com/o/r", "--job", "1"], "--repo must be"),
            (["--repo", "o/..", "--job", "1"], "--repo must be"),
            (["--repo", "o/.", "--job", "1"], "--repo must be"),
            (["--repo", "../r", "--job", "1"], "--repo must be"),
            (["--repo", "-o/r", "--job", "1"], "--repo must be"),
            (["--repo", "o/r?x=1", "--job", "1"], "--repo must be"),
            (["--repo", "o/r#x", "--job", "1"], "--repo must be"),
            (["--repo", "o/r%2F..", "--job", "1"], "--repo must be"),
            (["--repo", "{owner}/{repo}", "--job", "1"], "--repo must be"),
            (["--repo", "o /r", "--job", "1"], "--repo must be"),
            (["--repo", "o/r\n", "--job", "1"], "--repo must be"),
            (["--repo", "/r", "--job", "1"], "--repo must be"),
            (["--repo", "o/", "--job", "1"], "--repo must be"),
            (["--repo", "o/r", "--job", "0"], "--job must be"),
            (["--repo", "o/r", "--job", "-1"], "--job must be"),
            (["--repo", "o/r", "--job", "1/../../hooks"], "--job must be"),
            (["--repo", "o/r", "--job", "1?per_page=1"], "--job must be"),
            (["--repo", "o/r", "--job", "12\n"], "--job must be"),
            (["--repo", "o/r", "--job", "1" * 21], "--job must be"),
            (["--repo", "o/r", "--job", "abc"], "--job must be"),
        ],
    )
    def test_invalid_arguments_refused_without_calling_gh(
        self,
        args: list[str],
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with (
            patch("vergil_tooling.bin.vrg_gh.github.get_installation_token") as mock_token,
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            rc = main(["run", "job-log", *args])
        assert rc == 2  # noqa: PLR2004
        mock_run.assert_not_called()
        mock_token.assert_not_called()
        err = capsys.readouterr().err
        assert message in err
        assert "usage: vrg-gh run job-log" in err

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_does_not_call_gh(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            assert main(["run", "job-log", flag]) == 0
        mock_run.assert_not_called()
        assert "per-job log endpoint" in capsys.readouterr().out

    def test_user_api_still_denied(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The new command widens no other access: raw `gh api` on the same
        # endpoint stays refused for the user identity.
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            assert main(["api", self._ENDPOINT]) == 1
        mock_run.assert_not_called()
        assert "denied for the user identity" in capsys.readouterr().err

    def test_audit_other_run_actions_still_denied(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            assert main(["run", "view", "1", "--log"]) != 0
        mock_run.assert_not_called()
        assert "run is not recognized" in capsys.readouterr().err

    def test_listed_in_wrapper_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--help"]) == 0
        assert "run job-log" in capsys.readouterr().out
