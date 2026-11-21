"""Tests for vergil_tooling.bin.vrg_epic_create."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_create import main, parse_args
from vergil_tooling.lib import github, proxies
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_epic_create"
_URL = "https://github.com/org/.github/issues/50"


@pytest.fixture(autouse=True)
def _ensure_labels_ok() -> Iterator[MagicMock]:
    """Patch label ensuring as vrg_epic_create imports it: no test reaches gh for it."""
    with patch(f"{_MOD}.ensure_labels_or_report", return_value=True) as mock_ensure:
        yield mock_ensure


def test_title_required_without_proxy_for(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_args([]).title is None
    with patch(f"{_MOD}.github.create_issue") as mock_create:
        rc = main([])
    assert rc == 1
    assert "--title is required" in capsys.readouterr().err
    mock_create.assert_not_called()


def test_default_target_is_current_repo_public_goes_to_dotgithub() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github") as home,
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "Epic: X", "--body", "B"])
    assert rc == 0
    home.assert_called_once_with("org", "tooling")
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["title"] == "Epic: X"
    assert mock_create.call_args.kwargs["labels"] == ["epic", "epic-development"]


def test_main_scopes_token_to_target_owner() -> None:
    """Epic creation runs under the --repo owner's App installation (#2722).

    Without target_org(owner) the App token is minted for the cwd org, so a
    cross-org epic misroutes or misreports the epic's location.
    """
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="other-org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        rc = main(["--repo", "other-org/repo", "--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_explicit_private_target_homes_in_self() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab") as home,
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--repo", "org/lab", "--title", "T"])
    assert rc == 0
    home.assert_called_once_with("org", "lab")
    assert mock_create.call_args.kwargs["repo"] == "org/lab"


def test_prints_resolved_home(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        main(["--repo", "org/lab", "--title", "T"])
    assert "epic home: org/lab [PRIVATE]" in capsys.readouterr().out


def test_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--label", "ad-hoc"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "ad-hoc"]


def test_dedups_epic_label() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "epic"])
    assert mock_create.call_args.kwargs["labels"] == ["epic", "epic-development"]


# --- the derived category label (vergil-project/.github#1450) -------------------


def _create(argv: list[str]) -> tuple[int, MagicMock]:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(argv)
    return rc, mock_create


def test_plain_epic_gets_development_label() -> None:
    rc, mock_create = _create(["--title", "T"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "epic-development"]


def test_management_label_derived() -> None:
    rc, mock_create = _create(["--title", "T", "--label", "intake"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "intake", "epic-management"]


def test_adhoc_gets_no_new_label() -> None:
    rc, mock_create = _create(["--title", "T", "--label", "ad-hoc"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "ad-hoc"]


@pytest.mark.parametrize("label", ["epic-development", "epic-management"])
def test_refuses_caller_category_label(label: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_create = _create(["--title", "T", "--label", label])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-epic-create: ")
    assert label in err
    mock_create.assert_not_called()


def test_refuses_ambiguous_labels(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_create = _create(["--title", "T", "--label", "ad-hoc", "--label", "proxy"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-epic-create: ")
    assert "ad-hoc" in err
    assert "proxy" in err
    mock_create.assert_not_called()


def test_ensures_labels_before_create(_ensure_labels_ok: MagicMock) -> None:
    calls: list[str] = []
    _ensure_labels_ok.side_effect = lambda *_args: calls.append("ensure") or True
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(
            f"{_MOD}.github.create_issue",
            side_effect=lambda **_kwargs: calls.append("create") or _URL,
        ),
    ):
        rc = main(["--title", "T", "--label", "intake"])
    assert rc == 0
    _ensure_labels_ok.assert_called_once_with(
        "vrg-epic-create", "org/.github", ["epic", "intake", "epic-management"]
    )
    assert calls == ["ensure", "create"]
    # A failed ensure (already reported on stderr) creates nothing.
    _ensure_labels_ok.side_effect = None
    _ensure_labels_ok.return_value = False
    rc, mock_create = _create(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


def test_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.create_issue") as mock_create:
        rc = main(["--repo", "noslash", "--title", "T"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_create.assert_not_called()


def test_current_repo_error_propagates() -> None:
    # No --repo and current_repo fails (not in a repo) -> fail loud, not silent.
    with (
        patch(
            f"{_MOD}.github.current_repo",
            side_effect=github.GitHubAPIError(1, "gh repo view", "not a repo"),
        ),
        pytest.raises(github.GitHubAPIError),
    ):
        main(["--title", "T"])


# --- --proxy-for ----------------------------------------------------------------

_PRIMARY = IssueRef("vergil-project", ".github", 1417)
_PROXY = IssueRef("logical-minds-foundry", ".github", 320)
_PROXY_ARGS = ["--proxy-for", "vergil-project/.github#1417", "--repo", "logical-minds-foundry/x"]


def _result(
    *,
    created: bool = False,
    reopened: bool = False,
    retitled: bool = False,
    commented: bool = False,
) -> object:
    return proxies.ProxyResult(
        _PROXY, created=created, reopened=reopened, retitled=retitled, commented=commented
    )


def test_proxy_for_creates_and_comments(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        f"{_MOD}.proxies.create_proxy", return_value=_result(created=True, commented=True)
    ) as create:
        rc = main(_PROXY_ARGS)
    assert rc == 0
    create.assert_called_once_with(_PRIMARY, "logical-minds-foundry/x")
    out = capsys.readouterr().out
    assert "Created proxy logical-minds-foundry/.github#320" in out
    assert "Posted 'Proxy: logical-minds-foundry/.github#320' on vergil-project/.github#1417" in out


def test_proxy_for_existing_open_proxy_prints_it(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy", return_value=_result()):
        rc = main(_PROXY_ARGS)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Proxy logical-minds-foundry/.github#320 for vergil-project/.github#1417 already" in out
    assert "already records" in out


def test_proxy_for_closed_proxy_reopened(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy", return_value=_result(reopened=True)):
        rc = main(_PROXY_ARGS)
    assert rc == 0
    assert "Reopened proxy logical-minds-foundry/.github#320" in capsys.readouterr().out


def test_proxy_for_retitled_proxy_prints_it(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy", return_value=_result(retitled=True)):
        rc = main(_PROXY_ARGS)
    assert rc == 0
    assert "Retitled proxy logical-minds-foundry/.github#320" in capsys.readouterr().out


def test_proxy_for_current_title_prints_no_retitle(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy", return_value=_result()):
        rc = main(_PROXY_ARGS)
    assert rc == 0
    assert "Retitled" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "error", [proxies.ProxyRefusedError("nope"), proxies.ProxyLinkError("drift")]
)
def test_proxy_for_refusal_exits_1(error: Exception, capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy", side_effect=error):
        rc = main(_PROXY_ARGS)
    assert rc == 1
    assert f"vrg-epic-create: {error}" in capsys.readouterr().err


def test_proxy_for_token_missing_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.proxies.create_proxy",
            side_effect=github.NoInstallationError("logical-minds-foundry", []),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(_PROXY_ARGS)
    assert excinfo.value.code == 1
    assert "no installation for owner 'logical-minds-foundry'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra", [["--title", "T"], ["--body", "B"], ["--body-file", "f"], ["--label", "x"]]
)
def test_proxy_for_refuses_derived_flags(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(f"{_MOD}.proxies.create_proxy") as create:
        rc = main([*_PROXY_ARGS, *extra])
    assert rc == 1
    assert extra[0] in capsys.readouterr().err
    create.assert_not_called()


def test_proxy_for_needs_repo(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy") as create:
        rc = main(["--proxy-for", "vergil-project/.github#1417"])
    assert rc == 1
    assert "needs --repo" in capsys.readouterr().err
    create.assert_not_called()


@pytest.mark.parametrize("ref", ["#1417", "vergil-project/.github", "1417", "a/b#1 x"])
def test_proxy_for_needs_full_ref(ref: str, capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.proxies.create_proxy") as create:
        rc = main(["--proxy-for", ref, "--repo", "logical-minds-foundry/x"])
    assert rc == 1
    assert "full 'owner/repo#N' ref" in capsys.readouterr().err
    create.assert_not_called()
