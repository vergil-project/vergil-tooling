"""Tests for vergil_tooling.bin.vrg_security_record (epic vergil-project/.github#517)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call, patch

import pytest

from vergil_tooling.bin import vrg_security_record
from vergil_tooling.bin.vrg_security_record import main
from vergil_tooling.lib import github, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.security_records import Advisory, RetargetError
from vergil_tooling.lib.security_records import advisory_home as real_advisory_home

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SR = "vergil_tooling.bin.vrg_security_record.security_records"
_EP = "vergil_tooling.bin.vrg_security_record.epics"
_GH = "vergil_tooling.bin.vrg_security_record.github"
_MOD = "vergil_tooling.bin.vrg_security_record"

HOME = "org/.github"
GHSA = "GHSA-abcd-2345-wxyz"
OTHER = "GHSA-zzzz-9999-aaaa"
INTAKE = IssueRef("org", ".github", 7)
ISSUE = IssueRef("org", ".github", 321)
URL = "https://github.com/org/.github/issues/321"
EXISTING = IssueRef("org", "tool", 55)
# Stands in for private advisory text: it must never reach stderr or a plain list.
SECRET = "FIXTURE-PRIVATE-SUMMARY-TEXT"


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    body = tmp_path / "body.md"
    body.write_text("A neutral description of the affected area.\n")
    details = tmp_path / "details.md"
    details.write_text(f"{SECRET}\n\nThe full private finding.\n")
    return {"body": str(body), "details": str(details)}


@pytest.fixture
def env() -> Iterator[MagicMock]:
    """Stub every boundary the CLI calls, attached to one manager to record order."""
    manager = MagicMock()
    with (
        patch(f"{_SR}.advisory_home", return_value=HOME) as home,
        patch(f"{_SR}.find_duplicate", return_value=None) as dup,
        patch(f"{_SR}.create_advisory", return_value=GHSA) as create_adv,
        patch(f"{_SR}.mark_issue") as mark,
        patch(f"{_SR}.retarget") as retarget,
        patch(f"{_SR}.list_advisories", return_value=[]) as list_adv,
        patch(f"{_SR}.read_text", return_value=(SECRET, f"{SECRET} details")) as read_text,
        patch(f"{_EP}.queue_home", return_value=(HOME, "org")) as queue_home,
        patch(f"{_EP}.ensure_intake_epic", return_value=INTAKE) as ensure_intake,
        patch(f"{_EP}.add_child") as add_child,
        patch(f"{_EP}.native_parent_of", return_value=None) as parent,
        patch(f"{_MOD}.ensure_labels_or_report", return_value=True) as ensure_labels,
        patch(f"{_GH}.create_issue", return_value=URL) as create_issue,
        patch(f"{_GH}.read_json_paginated", return_value=[]) as paginated,
        patch(f"{_GH}.read_json", return_value={"body": ""}) as read_json,
        patch(f"{_GH}.detect_org", return_value="org") as detect,
    ):
        for name, mock in {
            "advisory_home": home,
            "find_duplicate": dup,
            "create_advisory": create_adv,
            "mark_issue": mark,
            "retarget": retarget,
            "list_advisories": list_adv,
            "read_text": read_text,
            "queue_home": queue_home,
            "ensure_intake_epic": ensure_intake,
            "add_child": add_child,
            "native_parent_of": parent,
            "ensure_labels": ensure_labels,
            "create_issue": create_issue,
            "read_json_paginated": paginated,
            "read_json": read_json,
            "detect_org": detect,
        }.items():
            manager.attach_mock(mock, name)
        yield manager


def _create_argv(files: dict[str, str], *extra: str) -> list[str]:
    return [
        "create",
        "--repo",
        "org/tool",
        "--title",
        "Hardening needed in the request parser",
        "--body-file",
        files["body"],
        "--details-file",
        files["details"],
        "--severity",
        "high",
        *extra,
    ]


def _names(manager: MagicMock) -> list[str]:
    return [entry[0] for entry in manager.mock_calls if "." not in entry[0]]


def _gh_error(stdout: str = "", stderr: str = "HTTP 502") -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api", "x"], stdout, stderr)


# --- create: the full pair --------------------------------------------------


def test_create_full_pair_order(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_create_argv(files)) == 0
    order = [
        n
        for n in _names(env)
        if n
        in {
            "find_duplicate",
            "create_advisory",
            "ensure_labels",
            "create_issue",
            "add_child",
            "retarget",
        }
    ]
    assert order == [
        "find_duplicate",
        "create_advisory",
        "ensure_labels",
        "create_issue",
        "add_child",
        "retarget",
    ]
    env.find_duplicate.assert_called_once_with(HOME, summary=SECRET, affected_repo="org/tool")
    adv = env.create_advisory.call_args
    assert adv.kwargs["summary"] == SECRET
    assert adv.kwargs["affected_repo"] == "org/tool"
    assert adv.kwargs["severity"] == "high"
    assert "The full private finding." in adv.kwargs["details"]
    env.ensure_labels.assert_called_once_with("vrg-security-record", HOME, ["security"])
    issue = env.create_issue.call_args.kwargs
    assert issue["repo"] == HOME
    assert issue["labels"] == ["security"]
    assert issue["body"] == (
        f"A neutral description of the affected area.\n\nSecurity advisory: {HOME} {GHSA}"
    )
    assert SECRET not in issue["body"]
    assert SECRET not in issue["title"]
    env.add_child.assert_called_once_with(INTAKE, ISSUE)
    env.retarget.assert_called_once_with(HOME, GHSA, (ISSUE,))
    env.ensure_intake_epic.assert_called_once_with("org/tool")
    captured = capsys.readouterr()
    assert f"Recorded {GHSA} in {HOME}, tracked by {ISSUE.slug}." in captured.out
    assert SECRET not in captured.out + captured.err


def test_create_uses_explicit_summary(env: MagicMock, files: dict[str, str]) -> None:
    assert main(_create_argv(files, "--summary", "  Explicit summary  ")) == 0
    assert env.create_advisory.call_args.kwargs["summary"] == "Explicit summary"


def test_create_empty_body_is_just_the_advisory_line(
    env: MagicMock, files: dict[str, str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("\n")
    argv = _create_argv(files)
    argv[argv.index("--body-file") + 1] = str(empty)
    assert main(argv) == 0
    assert env.create_issue.call_args.kwargs["body"] == f"Security advisory: {HOME} {GHSA}"


def test_create_add_child_failure_prints_resume_with_issue(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.add_child.side_effect = _gh_error(stdout=SECRET)
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert f"advisory {GHSA} exists but linking {ISSUE.slug}" in err
    assert f"--resume {GHSA}" in err
    assert f"--issue '{ISSUE.slug}'" in err
    assert SECRET not in err
    env.retarget.assert_not_called()


def test_resume_with_issue_links_under_intake_idempotently(
    env: MagicMock, files: dict[str, str]
) -> None:
    argv = _create_argv(files, "--resume", GHSA, "--issue", ISSUE.slug)
    assert main(argv) == 0
    env.find_duplicate.assert_not_called()
    env.create_advisory.assert_not_called()
    env.create_issue.assert_not_called()
    env.mark_issue.assert_called_once_with(ISSUE, HOME, GHSA)
    env.add_child.assert_called_once_with(INTAKE, ISSUE)
    env.retarget.assert_called_once_with(HOME, GHSA, (ISSUE,))
    names = _names(env)
    assert names.index("add_child") < names.index("retarget")

    # Already linked: the re-run is a no-op for the link, then the PATCH.
    env.reset_mock()
    env.native_parent_of.return_value = INTAKE
    assert main(argv) == 0
    env.add_child.assert_not_called()
    env.retarget.assert_called_once_with(HOME, GHSA, (ISSUE,))


def test_create_attach_mode_marks_existing_issue(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_create_argv(files, "--issue", EXISTING.slug)) == 0
    env.create_advisory.assert_called_once()
    env.create_issue.assert_not_called()
    env.ensure_labels.assert_not_called()
    env.mark_issue.assert_called_once_with(EXISTING, HOME, GHSA)
    # Attach mode keeps the existing issue's current parent.
    env.add_child.assert_not_called()
    env.native_parent_of.assert_not_called()
    env.retarget.assert_called_once_with(HOME, GHSA, (EXISTING,))
    assert f"tracked by {EXISTING.slug}" in capsys.readouterr().out


def test_create_issue_failure_prints_resume_command(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.create_issue.side_effect = _gh_error(stdout=SECRET, stderr="HTTP 502: Bad Gateway")
    argv = _create_argv(files, "--summary", SECRET)
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "--resume GHSA-" in err
    assert "--issue" not in err
    assert "HTTP 502: Bad Gateway" in err
    assert SECRET not in err
    env.add_child.assert_not_called()


def test_create_tracking_failure_prints_resume_with_issue(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.retarget.side_effect = RetargetError(
        f"advisory {GHSA}: rewriting the tracking lines failed"
    )
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert f"writing the tracking line for {ISSUE.slug} failed" in err
    assert f"--resume {GHSA}" in err
    assert f"--issue '{ISSUE.slug}'" in err
    assert SECRET not in err


def test_create_resume_finds_issue_among_intake_children(
    env: MagicMock, files: dict[str, str]
) -> None:
    env.read_json_paginated.return_value = [
        {"number": 300, "repository_url": "https://api.github.com/repos/org/.github", "body": ""},
        {
            "number": 321,
            "repository_url": "https://api.github.com/repos/org/.github",
            "body": f"Neutral.\n\nSecurity advisory: {HOME} {GHSA}  \n",
        },
    ]
    with patch.object(github, "read_json") as search:
        assert main(_create_argv(files, "--resume", GHSA)) == 0
    endpoint = env.read_json_paginated.call_args.args[0]
    assert endpoint == "repos/org/.github/issues/7/sub_issues"
    search.assert_not_called()
    env.create_issue.assert_not_called()
    env.add_child.assert_not_called()
    env.retarget.assert_called_once_with(HOME, GHSA, (ISSUE,))


def test_create_resume_creates_issue_when_none_found(env: MagicMock, files: dict[str, str]) -> None:
    assert main(_create_argv(files, "--resume", GHSA)) == 0
    env.create_advisory.assert_not_called()
    env.create_issue.assert_called_once()
    env.add_child.assert_called_once_with(INTAKE, ISSUE)


def test_create_resume_ignores_empty_details(
    env: MagicMock, files: dict[str, str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("")
    argv = _create_argv(files, "--resume", GHSA)
    argv[argv.index("--details-file") + 1] = str(empty)
    assert main(argv) == 0


def test_create_refuses_duplicate(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.find_duplicate.return_value = OTHER
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert OTHER in err
    assert f"--resume {OTHER}" in err
    assert SECRET not in err
    env.create_advisory.assert_not_called()
    env.create_issue.assert_not_called()


def test_create_advisory_failure_reports_nothing_created(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.create_advisory.side_effect = _gh_error(stdout=SECRET)
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert "creating the advisory in org/.github failed" in err
    assert "duplicate guard" in err
    assert SECRET not in err
    env.create_issue.assert_not_called()


def test_create_label_failure_is_reported_with_resume(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.ensure_labels.return_value = False
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert f"advisory {GHSA} exists but its pair is unfinished" in err
    assert "'security' label could not be ensured in org/.github" in err
    assert f"--resume {GHSA}" in err
    env.create_issue.assert_not_called()


def test_create_unparseable_issue_url_is_reported(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.create_issue.return_value = "not a url"
    assert main(_create_argv(files)) == 1
    err = capsys.readouterr().err
    assert "not an issue URL" in err
    assert f"--resume {GHSA}" in err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--resume", "GHSA-bad"), "--resume must be a GHSA ID"),
        (("--issue", "#5"), "--issue must be owner/repo#N"),
        (("--summary", "   "), "the advisory summary is empty"),
        (("--summary", "x" * 1025), "longer than 1024 characters"),
    ],
)
def test_create_refuses_bad_input(
    env: MagicMock,
    files: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
    message: str,
) -> None:
    assert main(_create_argv(files, *extra)) == 1
    assert message in capsys.readouterr().err
    env.advisory_home.assert_not_called()


def test_create_refuses_bad_repo(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _create_argv(files)
    argv[argv.index("--repo") + 1] = "tool"
    assert main(argv) == 1
    assert "--repo must be owner/name, got 'tool'" in capsys.readouterr().err


def test_create_refuses_empty_details_first_line(
    env: MagicMock, files: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("")
    argv = _create_argv(files)
    argv[argv.index("--details-file") + 1] = str(empty)
    assert main(argv) == 1
    assert "the advisory summary is empty" in capsys.readouterr().err


def test_create_refuses_unreadable_file(
    env: MagicMock, files: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _create_argv(files)
    argv[argv.index("--body-file") + 1] = str(tmp_path / "missing.md")
    assert main(argv) == 1
    assert "cannot read --body-file" in capsys.readouterr().err


def test_create_no_installation_is_reported(
    env: MagicMock, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    env.advisory_home.side_effect = github.NoInstallationError("org", ["other"])
    assert main(_create_argv(files)) == 1
    assert "vrg-security-record:" in capsys.readouterr().err


# --- list -------------------------------------------------------------------


_ADVISORIES = [
    Advisory(GHSA, "draft", "tool", (ISSUE,), external=False),
    Advisory(OTHER, "closed", None, (), external=True),
]


def test_list_omits_summary_without_details(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    env.list_advisories.return_value = _ADVISORIES
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert f"{GHSA}  draft  tool  tracks: {ISSUE.slug}" in out
    assert f"{OTHER}  closed  (no affected repo)  tracks: (no tracking issue)" in out
    assert "[external report]" in out
    assert SECRET not in out
    env.read_text.assert_not_called()
    env.detect_org.assert_called_once_with()


def test_list_details_prints_summaries(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    env.list_advisories.return_value = _ADVISORIES[:1]
    assert main(["list", "--details"]) == 0
    assert f"    summary: {SECRET}" in capsys.readouterr().out
    env.read_text.assert_called_once_with(HOME, GHSA)


def test_list_filters_by_state_and_repo(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    env.list_advisories.return_value = _ADVISORIES
    assert main(["list", "--repo", "org/tool", "--state", "draft"]) == 0
    out = capsys.readouterr().out
    assert GHSA in out
    assert OTHER not in out
    env.detect_org.assert_not_called()

    assert main(["list", "--state", "closed", "--repo", "org/tool"]) == 0
    assert "No security records in org/.github match." in capsys.readouterr().out


def test_list_details_refused_under_epic_sweep(
    env: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")
    env.list_advisories.return_value = _ADVISORIES
    assert main(["list", "--details"]) == 1
    assert "--details is refused under the scheduled sweep" in capsys.readouterr().err
    env.advisory_home.assert_not_called()
    env.read_text.assert_not_called()
    # Without --details the sweep may still list.
    assert main(["list"]) == 0


def test_list_undetectable_org_is_refused(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    env.detect_org.return_value = None
    assert main(["list"]) == 1
    assert "could not determine the GitHub org" in capsys.readouterr().err


# --- show -------------------------------------------------------------------


def test_show_by_ghsa_prints_details(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    env.list_advisories.return_value = _ADVISORIES
    assert main(["show", GHSA]) == 0
    out = capsys.readouterr().out
    assert f"{GHSA}  draft  tool" in out
    assert f"summary: {SECRET}" in out
    assert f"{SECRET} details" in out


def test_show_by_issue_ref_reads_its_advisory_line(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    env.list_advisories.return_value = _ADVISORIES
    env.read_json.return_value = {"body": f"Neutral.\n\nSecurity advisory: {HOME} {GHSA}"}
    assert main(["show", ISSUE.slug]) == 0
    env.read_json.assert_called_once_with(
        "issue", "view", "321", "--repo", "org/.github", "--json", "body"
    )
    env.detect_org.assert_not_called()
    assert f"summary: {SECRET}" in capsys.readouterr().out


def test_show_issue_without_advisory_line(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", ISSUE.slug]) == 1
    assert f"{ISSUE.slug} has no Security advisory: line" in capsys.readouterr().err


def test_show_missing_advisory(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", GHSA]) == 1
    assert f"advisory {GHSA} does not exist in {HOME}" in capsys.readouterr().err
    env.read_text.assert_not_called()


def test_show_issue_prints_every_pair(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    env.list_advisories.return_value = _ADVISORIES
    env.read_json.return_value = {
        "body": f"Neutral.\n\nSecurity advisory: {HOME} {OTHER}\nSecurity advisory: {HOME} {GHSA}"
    }
    env.read_text.side_effect = lambda _home, ghsa: (f"summary of {ghsa}", f"details of {ghsa}")
    assert main(["show", ISSUE.slug]) == 0
    assert env.list_advisories.call_count == 1
    assert capsys.readouterr().out == (
        f"{OTHER}  closed  (no affected repo)  tracks: (no tracking issue)  [external report]\n"
        f"summary: summary of {OTHER}\n"
        "description:\n"
        f"details of {OTHER}\n"
        "\n"
        f"{GHSA}  draft  tool  tracks: {ISSUE.slug}\n"
        f"summary: summary of {GHSA}\n"
        "description:\n"
        f"details of {GHSA}\n"
    )


def test_show_issue_refuses_when_a_named_advisory_is_missing(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = "GHSA-mmmm-mmmm-mmmm"
    env.list_advisories.return_value = _ADVISORIES
    env.read_json.return_value = {
        "body": f"Security advisory: {HOME} {GHSA}\nSecurity advisory: {HOME} {missing}"
    }
    assert main(["show", ISSUE.slug]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"advisory {missing} does not exist in {HOME}" in captured.err
    assert GHSA not in captured.err
    env.read_text.assert_not_called()


def test_show_issue_refuses_naming_every_missing_advisory(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = "GHSA-mmmm-mmmm-mmmm", "GHSA-nnnn-nnnn-nnnn"
    env.list_advisories.return_value = _ADVISORIES
    env.read_json.return_value = {
        "body": (
            f"Security advisory: {HOME} {first}\n"
            f"Security advisory: {HOME} {GHSA}\n"
            f"Security advisory: {HOME} {second}"
        )
    }
    assert main(["show", ISSUE.slug]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"advisories {first}, {second} do not exist in {HOME}" in captured.err
    env.read_text.assert_not_called()


# --- retarget ---------------------------------------------------------------


def test_retarget_cli_moves_tracking(env: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    second = IssueRef("org", ".github", 400)
    assert main(["retarget", GHSA, "--to", ISSUE.slug, "--to", second.slug]) == 0
    env.retarget.assert_called_once_with(HOME, GHSA, [ISSUE, second])
    assert f"Retargeted {GHSA} to {ISSUE.slug}, {second.slug}." in capsys.readouterr().out


def test_retarget_cli_failure_exits_nonzero(
    env: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    env.retarget.side_effect = RetargetError(f"advisory {GHSA}: marking {ISSUE.slug} failed")
    assert main(["retarget", GHSA, "--to", ISSUE.slug]) == 1
    assert f"advisory {GHSA}: marking {ISSUE.slug} failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["retarget", "nope", "--to", "org/a#1"], "GHSA must be a GHSA ID"),
        (["retarget", GHSA, "--to", "#1"], "--to must be owner/repo#N"),
        (["retarget", GHSA, "--to", "org/a#1", "--to", "other/b#2"], "cross-org"),
    ],
)
def test_retarget_cli_refuses_bad_input(
    env: MagicMock, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert main(argv) == 1
    assert message in capsys.readouterr().err
    env.retarget.assert_not_called()


# --- private home -----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["list", "--repo", "org/tool"],
        ["show", GHSA],
        ["show", ISSUE.slug],
        ["retarget", GHSA, "--to", ISSUE.slug],
        None,  # create, filled in below
    ],
)
def test_private_home_refused_for_every_subcommand(
    env: MagicMock,
    files: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    argv: list[str] | None,
) -> None:
    env.advisory_home.side_effect = real_advisory_home
    with patch.object(security_records.github, "repo_visibility", return_value="PRIVATE"):
        rc = main(argv if argv is not None else _create_argv(files))
    assert rc == 1
    err = capsys.readouterr().err
    assert err.strip() == (
        "vrg-security-record: org/.github is not public; security records need a "
        "public org .github (private orgs are unsupported)"
    )
    for name in (
        "find_duplicate",
        "create_advisory",
        "create_issue",
        "mark_issue",
        "retarget",
        "list_advisories",
        "read_text",
    ):
        getattr(env, name).assert_not_called()


# --- helpers ----------------------------------------------------------------


def test_reason_keeps_stderr_drops_stdout() -> None:
    reason = vrg_security_record._reason
    text = reason(_gh_error(stdout=SECRET, stderr="  HTTP 422\n  Validation  "))
    assert "HTTP 422 Validation" in text
    assert SECRET not in text
    as_bytes = subprocess.CalledProcessError(1, ["gh"], SECRET.encode(), b"boom")
    assert reason(as_bytes).endswith("stderr: boom")
    no_stderr = subprocess.CalledProcessError(2, ["gh"], SECRET, None)
    assert "stderr" not in reason(no_stderr)
    assert SECRET not in reason(no_stderr)
    assert reason(ValueError("a\n b")) == "a b"


def test_ref_from_url() -> None:
    assert vrg_security_record._ref_from_url(URL + "/") == ISSUE
    with pytest.raises(ValueError, match="not an issue URL"):
        vrg_security_record._ref_from_url("https://github.com/org/.github/pull/3")


def test_main_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "vrg-security-record" in capsys.readouterr().err


def test_main_unknown_failure_propagates(env: MagicMock) -> None:
    env.list_advisories.side_effect = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        main(["list"])
    assert env.list_advisories.mock_calls == [call(HOME)]
