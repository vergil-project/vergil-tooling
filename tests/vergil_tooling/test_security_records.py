"""Tests for vergil_tooling.lib.security_records (epic vergil-project/.github#517)."""

from __future__ import annotations

import json
import subprocess
import traceback
from dataclasses import fields
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest

from vergil_tooling.lib import epics, github, labels, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.security_records import (
    Advisory,
    Classification,
    CloseError,
    NoteError,
    PrivateHomeError,
    RetargetError,
    SecurityIssue,
    advisory_home,
    advisory_line,
    check_note,
    classify,
    close_comment,
    close_record,
    create_advisory,
    find_duplicate,
    list_advisories,
    mark_issue,
    note_header,
    note_record,
    parse_advisory_lines,
    parse_advisory_refs,
    parse_tracking,
    pending_syncs,
    read_text,
    retarget,
    security_issues,
    set_state,
    sync_states,
    tracking_line,
    unmark_issue,
    with_note,
)

HOME = "org/.github"
GHSA = "GHSA-abcd-2345-wxyz"
OTHER = "GHSA-zzzz-9999-aaaa"
# A fixture string standing in for private advisory text: it must never reach
# an error message, a list result or a log.
SECRET = "FIXTURE-PRIVATE-SUMMARY-TEXT"

A = IssueRef("org", ".github", 10)
B = IssueRef("org", ".github", 11)
C = IssueRef("org", "tool", 12)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _ambient_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh_env", lambda: None)


# --- labels and home --------------------------------------------------------


def test_security_label_constants_are_in_the_registry() -> None:
    names = {entry["name"] for entry in labels.load_labels()["labels"]}
    assert security_records.SECURITY_LABEL in names
    assert security_records.DISCLOSED_LABEL in names
    assert security_records.SEVERITIES == ("critical", "high", "medium", "low")


def test_advisory_home_returns_public_dotgithub(monkeypatch: pytest.MonkeyPatch) -> None:
    visibility = MagicMock(return_value="PUBLIC")
    monkeypatch.setattr(github, "repo_visibility", visibility)
    assert advisory_home("org") == HOME
    visibility.assert_called_once_with(HOME)


@pytest.mark.parametrize("visibility", ["PRIVATE", "INTERNAL"])
def test_advisory_home_refuses_private(monkeypatch: pytest.MonkeyPatch, visibility: str) -> None:
    monkeypatch.setattr(github, "repo_visibility", lambda _repo: visibility)
    with pytest.raises(PrivateHomeError) as excinfo:
        advisory_home("org")
    assert str(excinfo.value) == (
        "vrg-security-record: org/.github is not public; security records need a "
        "public org .github (private orgs are unsupported)"
    )


# --- reference lines --------------------------------------------------------


def test_reference_lines_are_exact() -> None:
    assert advisory_line(HOME, GHSA) == f"Security advisory: org/.github {GHSA}"
    assert tracking_line(C) == "Tracking issue: org/tool#12"


def test_parse_advisory_refs_reads_every_line_in_order() -> None:
    first, second = "GHSA-aaaa-aaaa-aaaa", "GHSA-bbbb-bbbb-bbbb"
    body = (
        f"Neutral.\r\n\r\nSecurity advisory: org/.github {first}  \r\n"
        f"> Security advisory: org/.github {OTHER}\n"
        f"Security advisory: org/.github {second}\n"
        f"Security advisory: org/.github {first}\n"
    )
    assert parse_advisory_refs(body) == (first, second)


def test_parse_advisory_lines_keeps_each_lines_org() -> None:
    first, second = "GHSA-aaaa-aaaa-aaaa", "GHSA-bbbb-bbbb-bbbb"
    body = (
        f"Security advisory: org/.github {first}\r\n"
        f"> Security advisory: org/.github {OTHER}\n"
        f"Security advisory: other/.github {second}\n"
        f"Security advisory: org/.github {first}\n"
    )
    assert parse_advisory_lines(body) == (("org", first), ("other", second))
    assert parse_advisory_lines("no line") == ()


_LOOKALIKES = [
    "",
    f"See Security advisory: org/.github {GHSA} for more",
    f"> Security advisory: org/.github {GHSA}",
    f"  Security advisory: org/.github {GHSA}",
    f"Security advisory: org/tool {GHSA}",
    f"Security advisory: org/.github {GHSA} and more",
    "Security advisory: org/.github GHSA-1234",
    f"security advisory: org/.github {GHSA}",
    f"Security advisory: org/.githubx {GHSA}",
]


@pytest.mark.parametrize("body", _LOOKALIKES)
def test_parse_advisory_refs_ignores_lookalike_text(body: str) -> None:
    assert parse_advisory_refs(body) == ()


def test_parse_tracking_multiple_lines() -> None:
    description = (
        "Private details.\n"
        "Tracking issue: org/.github#10\r\n"
        "Mentions Tracking issue: org/x#1 inline\n"
        "Tracking issue: org/tool#12  \n"
        "Tracking issue: org/tool#12\n"
        "Tracking issue: org/tool#13 extra\n"
        " Tracking issue: org/tool#14\n"
    )
    assert parse_tracking(description) == (A, C)
    assert parse_tracking("") == ()


# --- create -----------------------------------------------------------------


def test_create_advisory_posts_once_without_retry() -> None:
    transient = _completed(returncode=1, stderr="gh: Bad Gateway (HTTP 502)")
    with (
        # One patch covers both paths: github and retry share subprocess.run, so
        # a retry would show up as a second call.
        patch("vergil_tooling.lib.github.subprocess.run", return_value=transient) as mock_run,
        patch("vergil_tooling.lib.retry.time.sleep"),
        pytest.raises(github.GitHubAPIError) as excinfo,
    ):
        create_advisory(
            HOME, summary=SECRET, details=SECRET, affected_repo="org/tool", severity="low"
        )
    assert mock_run.call_count == 1
    assert SECRET not in str(excinfo.value)


def test_create_advisory_body(monkeypatch: pytest.MonkeyPatch) -> None:
    post = MagicMock(return_value={"ghsa_id": GHSA, "summary": SECRET})
    monkeypatch.setattr(github, "post_json_once", post)
    assert (
        create_advisory(
            HOME, summary="Short", details="Long", affected_repo="org/tool", severity="high"
        )
        == GHSA
    )
    endpoint, body = post.call_args.args
    assert endpoint == "repos/org/.github/security-advisories"
    assert body == {
        "summary": "Short",
        "description": "Long",
        "severity": "high",
        "vulnerabilities": [{"package": {"ecosystem": "other", "name": "tool"}}],
    }
    assert "cve_id" not in body
    assert "start_private_fork" not in body


def test_create_advisory_failure_drops_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    err = github.GitHubAPIError(1, ("gh", "api"), f'{{"summary": "{SECRET}"}}', "HTTP 422")
    monkeypatch.setattr(github, "post_json_once", MagicMock(side_effect=err))
    with pytest.raises(github.GitHubAPIError) as excinfo:
        create_advisory(HOME, summary="s", details="d", affected_repo="tool", severity="low")
    assert excinfo.value.stdout is None
    assert "HTTP 422" in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert SECRET not in rendered


def test_create_advisory_rejects_unknown_severity(monkeypatch: pytest.MonkeyPatch) -> None:
    post = MagicMock()
    monkeypatch.setattr(github, "post_json_once", post)
    with pytest.raises(ValueError, match="severity must be one of"):
        create_advisory(HOME, summary="s", details="d", affected_repo="tool", severity="urgent")
    post.assert_not_called()


def test_create_advisory_without_ghsa_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "post_json_once", MagicMock(return_value={"summary": SECRET}))
    with pytest.raises(RuntimeError, match="returned no ghsa_id") as excinfo:
        create_advisory(HOME, summary="s", details="d", affected_repo="tool", severity="low")
    assert SECRET not in str(excinfo.value)


# --- duplicate guard ----------------------------------------------------------


def test_find_duplicate_matches_summary_and_affected_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"ghsa_id": OTHER, "summary": SECRET, "names": ["other-tool"]},
        {"ghsa_id": "GHSA-0000-0000-0000", "summary": "different", "names": ["tool"]},
        {"ghsa_id": GHSA, "summary": SECRET, "names": ["tool"]},
    ]
    read = MagicMock(return_value=rows)
    monkeypatch.setattr(github, "read_json_paginated", read)
    assert find_duplicate(HOME, summary=SECRET, affected_repo="org/tool") == GHSA
    assert read.call_args.args == ("repos/org/.github/security-advisories?state=draft",)
    assert "summary" in read.call_args.kwargs["jq"]


def test_find_duplicate_none(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"ghsa_id": GHSA, "summary": "other", "names": None}]
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(return_value=rows))
    assert find_duplicate(HOME, summary=SECRET, affected_repo="org/tool") is None


# --- list ---------------------------------------------------------------------


def test_advisory_record_has_no_text_fields() -> None:
    names = {f.name for f in fields(Advisory)}
    assert "summary" not in names
    assert "description" not in names


def test_list_advisories_projects_ids_states_package_and_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "ghsa_id": GHSA,
            "state": "draft",
            "affected": "tool",
            "submission": False,
            "tracking": ["Tracking issue: org/.github#10", "Tracking issue: org/tool#12"],
        },
        {"ghsa_id": OTHER, "state": "triage", "affected": None, "submission": False},
        {"ghsa_id": "GHSA-1111-2222-3333", "state": "closed", "submission": True},
    ]
    read = MagicMock(return_value=rows)
    monkeypatch.setattr(github, "read_json_paginated", read)
    result = list_advisories(HOME)
    assert result == [
        Advisory(GHSA, "draft", "tool", (A, C), external=False),
        Advisory(OTHER, "triage", None, (), external=True),
        Advisory("GHSA-1111-2222-3333", "closed", None, (), external=True),
    ]
    assert read.call_args.args == ("repos/org/.github/security-advisories",)
    jq = read.call_args.kwargs["jq"]
    # The projection names only IDs, states, the package, submission and
    # tracking lines; the text fields are never selected.
    assert "summary" not in jq
    assert "description // " in jq
    assert "{ghsa_id, state," in jq


def test_list_advisories_projection_against_jq() -> None:
    """The real filter, run through ``jq``, drops the text and keeps the lines."""
    import shutil

    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not installed")
    page = [
        {
            "ghsa_id": GHSA,
            "state": "draft",
            "summary": SECRET,
            "description": f"{SECRET}\r\nTracking issue: org/.github#10\r\n"
            f"Tracking issue: org/x#1 {SECRET}\nTracking issue: org/tool#12  \n",
            "vulnerabilities": [{"package": {"ecosystem": "other", "name": "tool"}}],
            "submission": None,
        },
        {
            "ghsa_id": OTHER,
            "state": "triage",
            "summary": SECRET,
            "description": None,
            "vulnerabilities": None,
            "submission": {"accepted": False},
        },
    ]
    out = subprocess.run(  # noqa: S603
        [jq, "-c", security_records._LIST_JQ],
        input=json.dumps(page),
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert SECRET not in out
    rows = json.loads(out)
    assert rows[0]["affected"] == "tool"
    assert rows[0]["submission"] is False
    assert len(rows[0]["tracking"]) == 2
    assert rows[1] == {
        "ghsa_id": OTHER,
        "state": "triage",
        "affected": None,
        "submission": True,
        "tracking": [],
    }


def test_list_advisories_rejects_a_malformed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(return_value=[{"state": "x"}]))
    with pytest.raises(ValueError, match="lacks a ghsa_id or state"):
        list_advisories(HOME)


def test_list_advisories_failure_does_not_echo_stdout() -> None:
    page1 = json.dumps([{"ghsa_id": GHSA, "state": "draft", "tracking": []}])
    failure = subprocess.CalledProcessError(
        1,
        ("gh", "api", "--paginate"),
        output=page1 + json.dumps([{"summary": SECRET}]),
        stderr="gh: Server Error (HTTP 500)",
    )
    with (
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=failure),
        pytest.raises(github.GitHubAPIError) as excinfo,
    ):
        list_advisories(HOME)
    error = excinfo.value
    assert SECRET not in str(error)
    assert error.stdout is None
    assert "HTTP 500" in str(error)
    assert SECRET not in "".join(traceback.format_exception(error))


def test_missing_token_keeps_its_type_when_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    err = github.MissingGitHubTokenError(4, ("gh",), SECRET, "no token")
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(side_effect=err))
    with pytest.raises(github.MissingGitHubTokenError) as excinfo:
        list_advisories(HOME)
    assert excinfo.value.stdout is None


# --- text, state ----------------------------------------------------------------


def test_read_text_returns_summary_and_description(monkeypatch: pytest.MonkeyPatch) -> None:
    read = MagicMock(return_value={"summary": "S", "description": None})
    monkeypatch.setattr(github, "read_json", read)
    assert read_text(HOME, GHSA) == ("S", "")
    assert read.call_args.args[1] == f"repos/org/.github/security-advisories/{GHSA}"


def test_read_text_rejects_missing_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", MagicMock(return_value={"description": "d"}))
    with pytest.raises(ValueError, match=f"advisory {GHSA} read returned no summary"):
        read_text(HOME, GHSA)


def test_set_state_patches_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock()
    monkeypatch.setattr(github, "write_json", write)
    set_state(HOME, GHSA, "closed")
    write.assert_called_once_with(
        "PATCH", f"repos/org/.github/security-advisories/{GHSA}", {"state": "closed"}
    )


def test_set_state_refuses_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock()
    monkeypatch.setattr(github, "write_json", write)
    with pytest.raises(ValueError, match="draft or closed"):
        set_state(HOME, GHSA, cast("Any", "published"))
    write.assert_not_called()


# --- issues: a small fake of the issue REST surface -----------------------------


class Issues:
    """Labels, bodies and comments per issue, behind the github calls this module makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.labels: dict[IssueRef, set[str]] = {}
        self.bodies: dict[IssueRef, str] = {}
        self.comments: dict[IssueRef, list[str]] = {}
        self.closed: set[IssueRef] = set()
        # Advisory states set through the API, by GHSA ID.
        self.states: dict[str, str] = {}
        # Transferred issues: old ref -> current ref. A read at the old number
        # follows the redirect; every other call there gets a 404, as on GitHub.
        self.moved: dict[IssueRef, IssueRef] = {}
        self.description = f"{SECRET}\n\nTracking issue: org/.github#10"
        # Per-advisory descriptions, for tests with several advisories; any
        # GHSA ID not listed here shares ``description``.
        self.descriptions: dict[str, str] = {}
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.fail_on: str | None = None
        self.ensure_labels = MagicMock()
        monkeypatch.setattr(labels, "ensure_labels", self.ensure_labels)
        monkeypatch.setattr(github, "read_json", self.read_json)
        monkeypatch.setattr(github, "write_json", self.write_json)
        # An unmark drops the label inside its one PATCH, never by a DELETE.
        monkeypatch.setattr(github, "delete", MagicMock(side_effect=AssertionError("DELETE")))
        monkeypatch.setattr(github, "read_json_paginated", self.read_json_paginated)
        monkeypatch.setattr(github, "list_org_repos", self.list_org_repos)
        monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)

    def add(self, ref: IssueRef, body: str = "", labels_: set[str] | None = None) -> None:
        self.labels[ref] = set(labels_ or set())
        self.bodies[ref] = body
        self.comments[ref] = []

    @staticmethod
    def _ref(endpoint: str) -> IssueRef:
        parts = endpoint.split("/")
        return IssueRef(parts[1], parts[2], int(parts[4]))

    def _maybe_fail(self, key: str) -> None:
        if self.fail_on is not None and self.fail_on in key:
            raise github.GitHubAPIError(1, ("gh", "api", key), SECRET, "HTTP 500")

    def _not_moved(self, endpoint: str) -> IssueRef:
        ref = self._ref(endpoint)
        if ref in self.moved:
            raise github.GitHubAPIError(1, ("gh", "api", endpoint), None, "Not Found (HTTP 404)")
        return ref

    def read_json(self, *args: str) -> Any:
        endpoint = args[1]
        self._maybe_fail(f"GET {endpoint}")
        if "security-advisories" in endpoint:
            ghsa_id = endpoint.rsplit("/", 1)[1]
            description = self.descriptions.get(ghsa_id, self.description)
            return {"summary": SECRET, "description": description}
        ref = self._ref(endpoint)
        ref = self.moved.get(ref, ref)
        return {
            "number": ref.number,
            "repository_url": f"https://api.github.com/repos/{ref.owner}/{ref.repo}",
            "state": "closed" if ref in self.closed else "open",
            "labels": sorted(self.labels[ref]),
            "body": self.bodies[ref],
        }

    def write_json(self, method: str, endpoint: str, body: dict[str, object]) -> None:
        self._maybe_fail(f"{method} {endpoint}")
        self.writes.append((method, endpoint, body))
        if "security-advisories" in endpoint:
            ghsa_id = endpoint.rsplit("/", 1)[1]
            if "state" in body:
                self.states[ghsa_id] = str(body["state"])
            elif ghsa_id in self.descriptions:
                self.descriptions[ghsa_id] = str(body["description"])
            else:
                self.description = str(body["description"])
            return
        ref = self._not_moved(endpoint)
        if endpoint.endswith("/labels"):
            self.labels[ref] |= {str(name) for name in cast("list[object]", body["labels"])}
        elif endpoint.endswith("/comments"):
            self.comments[ref].append(str(body["body"]))
        else:
            if "body" in body:
                self.bodies[ref] = str(body["body"])
            if "labels" in body:
                self.labels[ref] = {str(name) for name in cast("list[object]", body["labels"])}

    def list_org_repos(self, org: str) -> list[str]:
        return sorted({ref.repo for ref in self.labels if ref.owner == org})

    def read_json_paginated(self, endpoint: str, key: str | None = None, **_: Any) -> list[object]:
        if "?" in endpoint:
            self._maybe_fail(f"GET {endpoint}")
            owner, repo = endpoint.split("/")[1:3]
            return [
                {
                    "number": ref.number,
                    "state": "closed" if ref in self.closed else "open",
                    "body": self.bodies[ref],
                }
                for ref in sorted(self.labels, key=lambda r: r.number)
                if (ref.owner, ref.repo) == (owner, repo) and "security" in self.labels[ref]
            ]
        return list(self.comments[self._not_moved(endpoint)])

    def patches(self) -> list[tuple[str, str, dict[str, object]]]:
        return [w for w in self.writes if "security-advisories" in w[1]]


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> Issues:
    return Issues(monkeypatch)


def test_mark_issue_labels_and_appends_line(issues: Issues) -> None:
    issues.add(C, "Neutral body.\n")
    mark_issue(C, HOME, GHSA)
    issues.ensure_labels.assert_called_once_with("org/tool", ["security"])
    assert issues.labels[C] == {"security"}
    assert issues.bodies[C] == f"Neutral body.\n\nSecurity advisory: org/.github {GHSA}"


def test_mark_issue_is_idempotent(issues: Issues) -> None:
    issues.add(C, f"Body\r\nSecurity advisory: org/.github {GHSA} \r\n", {"security"})
    mark_issue(C, HOME, GHSA)
    assert issues.writes == []


def test_mark_issue_adds_a_second_advisory_line_and_keeps_the_first(issues: Issues) -> None:
    issues.add(C, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    mark_issue(C, HOME, OTHER)
    assert issues.bodies[C] == (
        f"Neutral.\n\n{advisory_line(HOME, GHSA)}\n\n{advisory_line(HOME, OTHER)}"
    )
    assert issues.labels[C] == {"security"}
    assert parse_advisory_refs(issues.bodies[C]) == (GHSA, OTHER)


def test_mark_issue_on_an_empty_body(issues: Issues) -> None:
    issues.add(C, "")
    mark_issue(C, HOME, GHSA)
    assert issues.bodies[C] == f"Security advisory: org/.github {GHSA}"


def test_unmark_issue_removes_label_line_and_comments(issues: Issues) -> None:
    issues.add(A, f"Neutral.\n\nSecurity advisory: org/.github {GHSA}\n\nMore.", {"security"})
    unmark_issue(A, GHSA, (B, C))
    assert issues.labels[A] == set()
    assert issues.bodies[A] == "Neutral.\n\nMore."
    assert issues.comments[A] == ["Security record moved to org/.github#11, org/tool#12"]


def test_unmark_issue_is_idempotent(issues: Issues) -> None:
    issues.add(A, "Neutral.")
    issues.comments[A] = ["Security record moved to org/tool#12"]
    unmark_issue(A, GHSA, (C,))
    assert issues.writes == []


def test_unmark_issue_strips_only_this_advisorys_line(issues: Issues) -> None:
    quoted = f"> {advisory_line(HOME, GHSA)}"
    other = advisory_line(HOME, OTHER)
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)} \r\n\n{other}\n{quoted}", {"bug"})
    unmark_issue(A, GHSA, (C,))
    assert issues.bodies[A] == f"Neutral.\n\n{other}\n{quoted}"
    assert issues.labels[A] == {"bug"}
    assert issues.comments[A] == ["Security record moved to org/tool#12"]
    assert [w[2].keys() for w in issues.writes if w[0] == "PATCH"] == [{"body"}]


def test_unmark_issue_keeps_label_while_another_advisory_remains(issues: Issues) -> None:
    other = advisory_line(HOME, OTHER)
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}\n{other}", {"security", "bug"})
    unmark_issue(A, GHSA, (C,))
    assert issues.bodies[A] == f"Neutral.\n\n{other}"
    assert issues.labels[A] == {"security", "bug"}
    assert issues.comments[A] == ["Security record moved to org/tool#12"]
    # Comment first, then one PATCH carrying the body only (the label stays).
    assert [(w[0], w[1].rsplit("/", 1)[1]) for w in issues.writes] == [
        ("POST", "comments"),
        ("PATCH", "10"),
    ]
    assert issues.writes[1][2].keys() == {"body"}


def test_unmark_issue_acts_on_a_later_line(issues: Issues) -> None:
    other = advisory_line(HOME, OTHER)
    issues.add(A, f"Neutral.\n\n{other}\n{advisory_line(HOME, GHSA)}", {"security"})
    unmark_issue(A, GHSA, (C,))
    assert issues.bodies[A] == f"Neutral.\n\n{other}"
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == ["Security record moved to org/tool#12"]


def test_unmark_issue_drops_label_with_the_last_line(issues: Issues) -> None:
    other = advisory_line(HOME, OTHER)
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}\n{other}", {"security", "bug"})
    unmark_issue(A, GHSA, (C,))
    unmark_issue(A, OTHER, (C,))
    assert issues.bodies[A] == "Neutral."
    assert issues.labels[A] == {"bug"}
    assert issues.comments[A] == ["Security record moved to org/tool#12"]
    patches = [w[2] for w in issues.writes if w[0] == "PATCH"]
    assert [p.keys() for p in patches] == [{"body"}, {"body", "labels"}]


@pytest.mark.parametrize(
    "body",
    [
        "Neutral.",
        f"Neutral.\n\n{advisory_line(HOME, OTHER)}",
        f"Neutral.\n\n> {advisory_line(HOME, GHSA)}\n{advisory_line(HOME, OTHER)}",
    ],
)
def test_unmark_issue_leaves_an_issue_not_naming_this_advisory_untouched(
    issues: Issues, body: str
) -> None:
    issues.add(A, body, {"security"})
    unmark_issue(A, GHSA, (C,))
    assert issues.writes == []
    assert issues.bodies[A] == body
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == []


def test_unmark_issue_requires_a_destination(issues: Issues) -> None:
    issues.add(A)
    with pytest.raises(ValueError, match="no issue the record moved to"):
        unmark_issue(A, GHSA, ())


# --- retarget -----------------------------------------------------------------


def test_retarget_marks_targets_before_rewriting_tracking(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "Neutral.")
    issues.fail_on = "POST repos/org/tool/issues/12/labels"
    with pytest.raises(RetargetError) as excinfo:
        retarget(HOME, GHSA, (C,))
    message = str(excinfo.value)
    assert message.startswith(f"advisory {GHSA}: marking org/tool#12 failed: ")
    assert SECRET not in message
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
    assert issues.patches() == []
    assert issues.labels[A] == {"security"}


def test_retarget_unmarks_dropped_issues(issues: Issues) -> None:
    issues.description = (
        f"{SECRET}\n\nTracking issue: org/.github#10\nTracking issue: org/.github#11\n"
    )
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.add(B, f"Kept.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.add(C, "New task.")
    retarget(HOME, GHSA, (B, C, C))
    # The dropped issue loses the label and the line and gets the moved comment.
    assert issues.labels[A] == set()
    assert issues.bodies[A] == "Neutral."
    assert issues.comments[A] == ["Security record moved to org/.github#11, org/tool#12"]
    # The kept target is untouched.
    assert issues.labels[B] == {"security"}
    assert issues.bodies[B] == f"Kept.\n\n{advisory_line(HOME, GHSA)}"
    assert issues.comments[B] == []
    assert not [w for w in issues.writes if "/issues/11" in w[1]]
    # The new target is marked, and the advisory tracks exactly the targets.
    assert issues.labels[C] == {"security"}
    assert issues.description == (
        f"{SECRET}\n\nTracking issue: org/.github#11\nTracking issue: org/tool#12"
    )


def test_retarget_onto_a_description_with_no_text(issues: Issues) -> None:
    issues.description = ""
    issues.add(C, "")
    retarget(HOME, GHSA, (C,))
    assert issues.description == "Tracking issue: org/tool#12"


def test_retarget_never_unmarks_a_renamed_issue(issues: Issues) -> None:
    """A transferred issue's old number is rewritten away but never unmarked."""
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    retarget(HOME, GHSA, (C,), renamed=(A,))
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert not [w for w in issues.writes if "/issues/10" in w[1]]


def test_retarget_requires_targets(issues: Issues) -> None:
    with pytest.raises(RetargetError, match=f"advisory {GHSA}: no target issues given"):
        retarget(HOME, GHSA, ())


@pytest.mark.parametrize(
    ("fail_on", "step"),
    [
        (f"GET repos/org/.github/security-advisories/{GHSA}", "reading the tracking lines"),
        (f"PATCH repos/org/.github/security-advisories/{GHSA}", "rewriting the tracking lines"),
        (
            "GET repos/org/.github/issues?state=all&labels=security",
            "listing the security issues",
        ),
        ("POST repos/org/.github/issues/10/comments", "unmarking org/.github#10"),
        ("PATCH repos/org/.github/issues/10", "unmarking org/.github#10"),
    ],
)
def test_retarget_names_the_failed_step(issues: Issues, fail_on: str, step: str) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "")
    issues.fail_on = fail_on
    with pytest.raises(RetargetError) as excinfo:
        retarget(HOME, GHSA, (C,))
    assert str(excinfo.value).startswith(f"advisory {GHSA}: {step} failed: ")
    assert SECRET not in str(excinfo.value)
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))


def test_retarget_wraps_a_non_gh_failure(issues: Issues) -> None:
    issues.add(C, "")
    issues.ensure_labels.side_effect = labels.UnknownLabelError(["security"])
    with pytest.raises(RetargetError, match="marking org/tool#12 failed") as excinfo:
        retarget(HOME, GHSA, (C,))
    assert isinstance(excinfo.value.__cause__, labels.UnknownLabelError)


# --- retarget: a re-run finishes an interrupted run (issue #3702) ---------------

D = IssueRef("org", "tool", 13)
MOVED = "Security record moved to org/tool#12"


def _dropping_a(issues: Issues) -> None:
    """A tracked and marked (alongside a label it keeps); C is the new target."""
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security", "bug"})
    issues.add(C, "New task.")


def _assert_moved_a_to_c(issues: Issues) -> None:
    assert issues.labels[A] == {"bug"}
    assert issues.bodies[A] == "Neutral."
    assert issues.comments[A] == [MOVED]
    assert issues.labels[C] == {"security"}
    assert issues.bodies[C] == f"New task.\n\n{advisory_line(HOME, GHSA)}"
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"


@pytest.mark.parametrize(
    "fail_on",
    [
        "POST repos/org/.github/issues/10/comments",  # before anything on A changed
        "PATCH repos/org/.github/issues/10",  # after the comment was posted
    ],
)
def test_retarget_rerun_after_unmark_failure_unmarks_once(issues: Issues, fail_on: str) -> None:
    _dropping_a(issues)
    issues.fail_on = fail_on
    with pytest.raises(RetargetError, match="unmarking org/.github#10 failed"):
        retarget(HOME, GHSA, (C,))
    # The tracking lines already name only C, so only A itself still points here.
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert issues.labels[A] == {"security", "bug"}
    issues.fail_on = None
    retarget(HOME, GHSA, (C,))
    _assert_moved_a_to_c(issues)
    assert len([w for w in issues.writes if w[1].endswith("/10/comments")]) == 1


@pytest.mark.parametrize(
    "fail_on",
    [
        "POST repos/org/tool/issues/12/labels",
        "PATCH repos/org/tool/issues/12",
        f"GET repos/org/.github/security-advisories/{GHSA}",
        f"PATCH repos/org/.github/security-advisories/{GHSA}",
        "GET repos/org/.github/issues?state=all&labels=security",
    ],
)
def test_retarget_rerun_after_an_early_failure_completes(issues: Issues, fail_on: str) -> None:
    _dropping_a(issues)
    issues.fail_on = fail_on
    with pytest.raises(RetargetError):
        retarget(HOME, GHSA, (C,))
    issues.fail_on = None
    retarget(HOME, GHSA, (C,))
    _assert_moved_a_to_c(issues)


def test_retarget_rerun_after_success_changes_nothing(issues: Issues) -> None:
    _dropping_a(issues)
    retarget(HOME, GHSA, (C,))
    writes = len(issues.writes)
    retarget(HOME, GHSA, (C,))
    # Only the idempotent advisory rewrite repeats; no issue is written again.
    assert [w[1] for w in issues.writes[writes:]] == [
        f"repos/org/.github/security-advisories/{GHSA}"
    ]
    _assert_moved_a_to_c(issues)


def test_retarget_unmarks_an_untracked_issue_that_names_the_advisory(issues: Issues) -> None:
    """The dropped set comes from the issues too, not only the old tracking lines."""
    issues.description = SECRET
    issues.add(D, f"Stray.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,))
    assert issues.labels[D] == set()
    assert issues.bodies[D] == "Stray."
    assert issues.comments[D] == [MOVED]


def test_retarget_never_unmarks_a_renamed_issue_that_carries_the_line(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    retarget(HOME, GHSA, (C,), renamed=(A,))
    assert not [w for w in issues.writes if "/issues/10" in w[1]]
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == []


# --- retarget: a dropped issue that was transferred (issue #4299) ---------------


def test_retarget_onto_a_transferred_issue_leaves_it_marked(issues: Issues) -> None:
    """The old number tracked, the issue moved to the target: nothing to unmark."""
    issues.add(C, "Moved.")
    issues.moved[A] = C
    notes = retarget(HOME, GHSA, (C,))
    assert notes == (
        "org/.github#10 was transferred to org/tool#12, a target: "
        "nothing left to unmark at org/.github#10",
    )
    assert issues.labels[C] == {"security"}
    assert issues.bodies[C] == f"Moved.\n\n{advisory_line(HOME, GHSA)}"
    assert issues.comments[C] == []
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert not [w for w in issues.writes if "/issues/10" in w[1]]


def test_retarget_follows_a_transfer_to_an_issue_not_targeted(issues: Issues) -> None:
    """The old number tracked, the issue moved elsewhere: unmark it where it is."""
    issues.add(D, f"Moved.\n\n{advisory_line(HOME, GHSA)}", {"security", "bug"})
    issues.add(C, "")
    issues.moved[A] = D
    notes = retarget(HOME, GHSA, (C,))
    assert notes == (
        "org/.github#10 was transferred to org/tool#13: followed the transfer and "
        "unmarked org/tool#13",
    )
    assert issues.labels[D] == {"bug"}
    assert issues.bodies[D] == "Moved."
    assert issues.comments[D] == [MOVED]
    assert issues.labels[C] == {"security"}
    assert not [w for w in issues.writes if "/issues/10" in w[1]]


def test_retarget_follows_a_transfer_to_an_issue_with_no_line(issues: Issues) -> None:
    issues.add(D, "Moved, line removed.", {"bug"})
    issues.add(C, "")
    issues.moved[A] = D
    notes = retarget(HOME, GHSA, (C,))
    assert notes == (
        "org/.github#10 was transferred to org/tool#13: followed the transfer and "
        "org/tool#13 has no line for this advisory, nothing to unmark",
    )
    assert not [w for w in issues.writes if "/issues/10" in w[1] or "/issues/13" in w[1]]


def test_retarget_reports_no_notes_without_a_transfer(issues: Issues) -> None:
    _dropping_a(issues)
    assert retarget(HOME, GHSA, (C,)) == ()


def test_retarget_never_touches_an_issue_naming_another_advisory(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(D, f"Other.\n\n{advisory_line(HOME, OTHER)}", {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,))
    assert not [w for w in issues.writes if "/issues/13" in w[1]]
    assert issues.bodies[D] == f"Other.\n\n{advisory_line(HOME, OTHER)}"
    assert issues.labels[D] == {"security"}
    assert issues.comments[A] == [MOVED]


@pytest.mark.parametrize("order", [(GHSA, OTHER), (OTHER, GHSA)])
def test_retarget_two_advisories_off_one_issue_in_either_order(
    issues: Issues, order: tuple[str, str]
) -> None:
    both = f"Neutral.\n\n{advisory_line(HOME, GHSA)}\n{advisory_line(HOME, OTHER)}"
    issues.add(A, both, {"security", "bug"})
    issues.add(C, "New task.")
    issues.descriptions = {
        GHSA: f"{SECRET}\n\nTracking issue: org/.github#10",
        OTHER: f"{SECRET}\n\nTracking issue: org/.github#10",
    }
    first, second = order
    retarget(HOME, first, (C,))
    # After the first: A keeps the other advisory's line and the label.
    assert parse_advisory_refs(issues.bodies[A]) == (second,)
    assert issues.labels[A] == {"security", "bug"}
    retarget(HOME, second, (C,))
    assert parse_advisory_refs(issues.bodies[A]) == ()
    assert issues.bodies[A] == "Neutral."
    assert issues.labels[A] == {"bug"}
    assert issues.comments[A] == [MOVED]
    assert issues.labels[C] == {"security"}
    assert parse_advisory_refs(issues.bodies[C]) == order
    assert issues.descriptions == {
        GHSA: f"{SECRET}\n\nTracking issue: org/tool#12",
        OTHER: f"{SECRET}\n\nTracking issue: org/tool#12",
    }


def test_retarget_never_touches_a_tracked_issue_naming_another_advisory(issues: Issues) -> None:
    """The old tracking lines name A, but A's body names a different advisory."""
    other = f"Other.\n\n{advisory_line(HOME, OTHER)}"
    issues.add(A, other, {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,))
    assert not [w for w in issues.writes if "/issues/10" in w[1]]
    assert issues.bodies[A] == other
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == []
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"


# --- security issues ------------------------------------------------------------


def test_security_issues_reads_every_managed_repo_in_every_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: [".github", "tool", "unmanaged"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda repo: repo != "org/unmanaged")
    pages = {
        "org/.github": [
            {"number": 10, "state": "open", "body": f"x\n{advisory_line(HOME, GHSA)}"},
            {"number": 11, "state": "closed", "body": ""},
        ],
        "org/tool": [{"number": 12, "state": "OPEN", "body": None}],
    }
    endpoints: list[str] = []

    def read(endpoint: str, key: str | None = None, *, jq: str | None = None) -> list[object]:
        endpoints.append(endpoint)
        assert jq is not None
        assert "pull_request == null" in jq
        return list(pages[endpoint.split("/issues")[0].removeprefix("repos/")])

    monkeypatch.setattr(github, "read_json_paginated", read)
    assert security_issues("org") == [
        SecurityIssue(A, open=True, ghsa_ids=(GHSA,)),
        SecurityIssue(B, open=False, ghsa_ids=()),
        SecurityIssue(C, open=True, ghsa_ids=()),
    ]
    assert endpoints == [
        "repos/org/.github/issues?state=all&labels=security",
        "repos/org/tool/issues?state=all&labels=security",
    ]


def test_security_issues_returns_every_ghsa_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    body = f"x\n{advisory_line(HOME, OTHER)}\n\n{advisory_line(HOME, GHSA)}"
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *_a, **_k: [{"number": 12, "state": "open", "body": body}],
    )
    assert security_issues("org") == [SecurityIssue(C, open=True, ghsa_ids=(OTHER, GHSA))]


# --- classification -------------------------------------------------------------


def _adv(
    ghsa: str = GHSA, tracking: tuple[IssueRef, ...] = (A,), state: str = "draft", **kw: Any
) -> Advisory:
    return Advisory(ghsa, state, "tool", tracking, external=kw.get("external", False))


def _iss(
    ref: IssueRef = A, ghsa: str | tuple[str, ...] | None = GHSA, *, open_: bool = True
) -> SecurityIssue:
    ghsa_ids = () if ghsa is None else (ghsa,) if isinstance(ghsa, str) else ghsa
    return SecurityIssue(ref, open_, ghsa_ids)


class TestClassifyPairedExternalBroken:
    """test_classify_paired_external_broken: one case per spec 4.3 rule."""

    def test_paired(self) -> None:
        advisory = _adv(tracking=(A, C))
        result = classify([advisory], [_iss(A), _iss(C, open_=False)])
        assert result == Classification(paired=[advisory], external=[], broken=[])

    @pytest.mark.parametrize("state", ["triage", "draft"])
    def test_external_report(self, state: str) -> None:
        advisory = _adv(tracking=(), state=state, external=True)
        assert classify([advisory], []) == Classification(paired=[], external=[advisory], broken=[])

    def test_draft_no_issue_references(self) -> None:
        result = classify([_adv()], [])
        assert result.broken == [f"{GHSA}: no security issue names this advisory"]
        assert result.paired == []

    def test_issue_names_missing_advisory(self) -> None:
        result = classify([], [_iss(A, OTHER)])
        assert result.broken == [f"org/.github#10: names {OTHER}, which does not exist"]

    def test_security_issue_without_advisory_line(self) -> None:
        result = classify([], [_iss(A, None)])
        assert result.broken == ["org/.github#10: security label but no Security advisory: line"]

    def test_advisory_without_tracking_lines(self) -> None:
        result = classify([_adv(tracking=())], [_iss(A)])
        assert result.broken == [
            f"{GHSA}: no Tracking issue: lines",
            f"org/.github#10: names {GHSA}, which does not track it",
        ]
        assert result.paired == []

    def test_tracking_names_missing_issue(self) -> None:
        result = classify([_adv(tracking=(A, C))], [_iss(A)])
        assert result.broken == [
            f"{GHSA}: tracks org/tool#12, which is not a security-labelled issue"
        ]
        assert result.paired == []

    def test_tracking_ref_does_not_point_back(self) -> None:
        other = _adv(OTHER, tracking=(C,))
        result = classify([_adv(tracking=(A, C)), other], [_iss(A), _iss(C, OTHER)])
        assert result.broken == [f"{GHSA}: tracks org/tool#12, which does not point back"]
        assert result.paired == [other]

    def test_issue_unmarked_by_retarget_not_reported(self) -> None:
        # After retarget A -> C, A has no security label, so it is not listed.
        advisory = _adv(tracking=(C,))
        result = classify([advisory], [_iss(C)])
        assert result == Classification(paired=[advisory], external=[], broken=[])

    def test_issue_naming_two_paired_advisories(self) -> None:
        first, second = _adv(tracking=(A,)), _adv(OTHER, tracking=(A,))
        result = classify([first, second], [_iss(A, (GHSA, OTHER))])
        assert result == Classification(paired=[first, second], external=[], broken=[])

    def test_second_line_names_missing_advisory(self) -> None:
        advisory = _adv(tracking=(A,))
        result = classify([advisory], [_iss(A, (GHSA, OTHER))])
        assert result.broken == [f"org/.github#10: names {OTHER}, which does not exist"]
        assert result.paired == [advisory]

    def test_second_line_names_advisory_not_tracking_it(self) -> None:
        advisory, other = _adv(tracking=(A,)), _adv(OTHER, tracking=(C,))
        result = classify([advisory, other], [_iss(A, (GHSA, OTHER)), _iss(C, OTHER)])
        assert result.broken == [f"org/.github#10: names {OTHER}, which does not track it"]

    def test_tracked_issue_pointing_back_on_its_second_line(self) -> None:
        advisory = _adv(OTHER, tracking=(A, C))
        first = _adv(tracking=(A,))
        result = classify([first, advisory], [_iss(A, (GHSA, OTHER)), _iss(C, OTHER)])
        assert result == Classification(paired=[first, advisory], external=[], broken=[])

    def test_broken_lines_name_only_ids_and_refs(self) -> None:
        result = classify(
            [_adv(), _adv(OTHER, tracking=())],
            [_iss(B, None), _iss(C, "GHSA-9999-9999-9999")],
        )
        assert result.broken
        for line in result.broken:
            assert "\n" not in line
            assert line.startswith(("GHSA-", "org/"))


# --- state sync (the sweep) ---------------------------------------------------


def _sync(
    monkeypatch: pytest.MonkeyPatch,
    advisories: list[Advisory],
    issue_list: list[SecurityIssue],
) -> tuple[int, MagicMock]:
    write = MagicMock()
    monkeypatch.setattr(github, "write_json", write)
    changed = sync_states(HOME, classify(advisories, issue_list), issue_list)
    return changed, write


def _patched_state(ghsa: str, state: str) -> Any:
    return call("PATCH", f"repos/org/.github/security-advisories/{ghsa}", {"state": state})


def test_sync_closes_when_all_tracked_issues_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = _adv(tracking=(A, C))
    changed, write = _sync(monkeypatch, [advisory], [_iss(A, open_=False), _iss(C, open_=False)])
    assert changed == 1
    assert write.call_args_list == [_patched_state(GHSA, "closed")]


def test_sync_keeps_draft_while_any_tracked_issue_open(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = _adv(tracking=(A, C))
    changed, write = _sync(monkeypatch, [advisory], [_iss(A, open_=False), _iss(C)])
    assert changed == 0
    write.assert_not_called()


def test_sync_reopens_when_issue_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = _adv(tracking=(A,), state="closed")
    changed, write = _sync(monkeypatch, [advisory], [_iss(A)])
    assert changed == 1
    assert write.call_args_list == [_patched_state(GHSA, "draft")]


def test_sync_leaves_a_closed_advisory_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    advisory = _adv(tracking=(A,), state="closed")
    changed, write = _sync(monkeypatch, [advisory], [_iss(A, open_=False)])
    assert changed == 0
    write.assert_not_called()


def test_sync_skips_external_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unpaired external report is never touched, and neither is one a pair
    # adopted while it is still marked as a submission.
    unpaired = _adv(OTHER, tracking=(), state="triage", external=True)
    adopted = _adv(tracking=(A,), external=True)
    changed, write = _sync(monkeypatch, [unpaired, adopted], [_iss(A, open_=False)])
    assert changed == 0
    write.assert_not_called()


@pytest.mark.parametrize("state", ["published", "withdrawn", "triage"])
def test_sync_never_moves_an_advisory_out_of_a_human_state(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    advisory = _adv(tracking=(A,), state=state)
    changed, write = _sync(monkeypatch, [advisory], [_iss(A, open_=False)])
    assert changed == 0
    write.assert_not_called()


def test_sync_skips_broken_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pair whose links are wrong is reported, never acted on.
    advisory = _adv(tracking=(A, C))
    changed, write = _sync(monkeypatch, [advisory], [_iss(A, open_=False)])
    assert changed == 0
    write.assert_not_called()


def test_pending_syncs_lists_only_mismatches() -> None:
    issue_list = [_iss(A, open_=False), _iss(C, OTHER)]
    advisories = [_adv(tracking=(A,)), _adv(OTHER, tracking=(C,))]
    assert pending_syncs(classify(advisories, issue_list), issue_list) == [(GHSA, "closed")]


def test_sync_failure_is_scrubbed_of_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock(side_effect=github.GitHubAPIError(1, ["gh"], SECRET, "HTTP 403"))
    monkeypatch.setattr(github, "write_json", write)
    issue_list = [_iss(A, open_=False)]
    with pytest.raises(github.GitHubAPIError) as excinfo:
        sync_states(HOME, classify([_adv()], issue_list), issue_list)
    assert SECRET not in str(excinfo.value)
    assert excinfo.value.stdout is None


def test_retarget_issue_calls_in_order(issues: Issues) -> None:
    """Marking happens, then the advisory PATCH, then the unmark — never out of order."""
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,))
    kinds = [(method, "advisory" if "advisories" in ep else ep) for method, ep, _ in issues.writes]
    assert kinds == [
        ("POST", "repos/org/tool/issues/12/labels"),
        ("PATCH", "repos/org/tool/issues/12"),
        ("PATCH", "advisory"),
        ("POST", "repos/org/.github/issues/10/comments"),
        ("PATCH", "repos/org/.github/issues/10"),
    ]
    assert call("org/tool", ["security"]) in issues.ensure_labels.call_args_list


# --- close (issue #4000) ------------------------------------------------------

REASON = "the epic was closed as not planned"
CLOSED_COMMENT = f"Security record {GHSA} closed: {REASON}"


def _listing(monkeypatch: pytest.MonkeyPatch, *advisories: Advisory) -> MagicMock:
    listed = MagicMock(return_value=list(advisories))
    monkeypatch.setattr(security_records, "list_advisories", listed)
    return listed


def test_close_comment_is_the_ghsa_id_and_reason_only() -> None:
    assert close_comment(GHSA, REASON) == CLOSED_COMMENT


def test_close_comments_on_every_issue_then_closes(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A, C)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed |= {A, C}
    result = close_record(HOME, GHSA, f"  {REASON}  ")
    assert result == security_records.CloseResult(issues=(A, C), notes=(), closed=True)
    assert issues.comments[A] == [CLOSED_COMMENT]
    assert issues.comments[C] == [CLOSED_COMMENT]
    assert issues.states == {GHSA: "closed"}
    # The comments go first, the state last; no body or label is touched.
    assert [(m, ep.rsplit("/", 1)[1]) for m, ep, _ in issues.writes] == [
        ("POST", "comments"),
        ("POST", "comments"),
        ("PATCH", GHSA),
    ]
    assert SECRET not in "".join(str(w) for w in issues.writes)


def test_close_also_comments_on_an_untracked_issue_naming_it(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=()))
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(C)
    assert close_record(HOME, GHSA, REASON).issues == (C,)
    assert issues.comments[C] == [CLOSED_COMMENT]
    assert issues.states == {GHSA: "closed"}


@pytest.mark.parametrize("open_ref", [A, C])
def test_close_refuses_while_any_issue_is_open(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, open_ref: IssueRef
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed |= {A, C} - {open_ref}
    with pytest.raises(CloseError, match=f"open issue {open_ref.slug}; close or retarget"):
        close_record(HOME, GHSA, REASON)
    assert issues.writes == []


def test_close_names_every_open_issue(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    _listing(monkeypatch, _adv(tracking=(A, C)))
    issues.add(A)
    issues.add(C)
    with pytest.raises(CloseError, match="open issues org/.github#10, org/tool#12;"):
        close_record(HOME, GHSA, REASON)
    assert issues.writes == []


@pytest.mark.parametrize(
    ("advisory", "message"),
    [
        (_adv(OTHER), f"advisory {GHSA} does not exist in {HOME}"),
        (_adv(state="published"), "is in state published; close only moves a draft"),
        (_adv(state="withdrawn"), "is in state withdrawn"),
        (_adv(state="triage"), "is in state triage"),
        (_adv(external=True), "is an external report"),
    ],
)
def test_close_refuses_an_advisory_it_must_not_close(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, advisory: Advisory, message: str
) -> None:
    _listing(monkeypatch, advisory)
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(A)
    with pytest.raises(CloseError, match=message):
        close_record(HOME, GHSA, REASON)
    assert issues.writes == []


def test_close_refuses_a_record_with_no_issue(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=()))
    with pytest.raises(CloseError, match="no issue can carry the close comment"):
        close_record(HOME, GHSA, REASON)
    assert issues.writes == []


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("   ", "the close reason is empty"),
        ("one\ntwo", "the close reason must be one line"),
        ("x" * 501, "longer than 500 characters"),
    ],
)
def test_close_refuses_a_bad_reason(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, reason: str, message: str
) -> None:
    listed = _listing(monkeypatch, _adv())
    with pytest.raises(CloseError, match=message):
        close_record(HOME, GHSA, reason)
    listed.assert_not_called()
    assert issues.writes == []


def test_close_follows_a_transferred_issue(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(C)
    issues.moved[A] = C
    result = close_record(HOME, GHSA, REASON)
    assert result.issues == (C,)
    assert result.notes == ("org/.github#10 was transferred to org/tool#12: commented there",)
    assert issues.comments[C] == [CLOSED_COMMENT]
    assert not [w for w in issues.writes if "/issues/10" in w[1]]


def test_close_rerun_after_a_failed_state_change_posts_once(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(A)
    issues.fail_on = f"PATCH repos/org/.github/security-advisories/{GHSA}"
    with pytest.raises(CloseError) as excinfo:
        close_record(HOME, GHSA, REASON)
    assert str(excinfo.value).startswith(f"advisory {GHSA}: setting the state to closed failed: ")
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
    assert issues.comments[A] == [CLOSED_COMMENT]
    issues.fail_on = None
    assert close_record(HOME, GHSA, REASON).closed is True
    assert issues.comments[A] == [CLOSED_COMMENT]
    assert issues.states == {GHSA: "closed"}


def test_close_of_a_closed_advisory_only_completes_the_comments(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A, C), state="closed"))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed |= {A, C}
    issues.comments[A] = [CLOSED_COMMENT]
    assert close_record(HOME, GHSA, REASON).closed is False
    assert issues.comments[C] == [CLOSED_COMMENT]
    assert [(m, ep) for m, ep, _ in issues.writes] == [
        ("POST", "repos/org/tool/issues/12/comments")
    ]


@pytest.mark.parametrize(
    ("fail_on", "step"),
    [
        ("GET repos/org/.github/issues?state=all&labels=security", "listing the security issues"),
        ("GET repos/org/.github/issues/10", "reading org/.github#10"),
        ("POST repos/org/.github/issues/10/comments", "commenting on org/.github#10"),
    ],
)
def test_close_names_the_failed_step(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, fail_on: str, step: str
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(A)
    issues.fail_on = fail_on
    with pytest.raises(CloseError) as excinfo:
        close_record(HOME, GHSA, REASON)
    assert str(excinfo.value).startswith(f"advisory {GHSA}: {step} failed: ")
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
    assert issues.states == {}


def test_close_list_failure_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    error = github.GitHubAPIError(1, ("gh", "api", "x"), SECRET, "HTTP 500")
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(side_effect=error))
    with pytest.raises(CloseError, match="reading the advisories failed") as excinfo:
        close_record(HOME, GHSA, REASON)
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))


# --- note (issue #3998) -------------------------------------------------------

# Stands in for the private note text: like SECRET, it must never reach a message.
NOTE = "FIXTURE-PRIVATE-NOTE-TEXT"
DAY = date(2026, 11, 16)
LATER = date(2026, 12, 1)


def test_note_header_is_dated() -> None:
    assert note_header(DAY) == "Note (2026-11-16):"


def test_note_appends_before_the_tracking_lines(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    assert note_record(HOME, GHSA, f"\n{NOTE}  \nsecond line\n\n", DAY) is True
    assert issues.description == (
        f"{SECRET}\n\nNote (2026-11-16):\n{NOTE}\nsecond line\n\nTracking issue: org/.github#10"
    )
    assert parse_tracking(issues.description) == (A,)
    # One write, the description only: no state, issue or label change.
    assert [(m, ep, sorted(body)) for m, ep, body in issues.writes] == [
        ("PATCH", f"repos/org/.github/security-advisories/{GHSA}", ["description"])
    ]


def test_note_keeps_every_tracking_line_last_in_order() -> None:
    description = (
        f"{SECRET}\n\nTracking issue: org/tool#12\n\nmore text\n\nTracking issue: org/.github#10"
    )
    result = with_note(description, NOTE, DAY)
    assert result == (
        f"{SECRET}\n\nmore text\n\nNote (2026-11-16):\n{NOTE}\n\n"
        "Tracking issue: org/tool#12\nTracking issue: org/.github#10"
    )
    assert parse_tracking(result) == (C, A)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (SECRET, f"{SECRET}\n\nNote (2026-11-16):\n{NOTE}"),
        ("", f"Note (2026-11-16):\n{NOTE}"),
        (
            "Tracking issue: org/.github#10",
            f"Note (2026-11-16):\n{NOTE}\n\nTracking issue: org/.github#10",
        ),
    ],
)
def test_with_note_without_text_or_tracking(description: str, expected: str) -> None:
    assert with_note(description, NOTE, DAY) == expected


def test_a_second_note_follows_the_first(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    note_record(HOME, GHSA, NOTE, DAY)
    assert note_record(HOME, GHSA, "a later note", LATER) is True
    assert issues.description == (
        f"{SECRET}\n\nNote (2026-11-16):\n{NOTE}\n\nNote (2026-12-01):\na later note"
        "\n\nTracking issue: org/.github#10"
    )


def test_note_rerun_adds_nothing(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    assert note_record(HOME, GHSA, NOTE, DAY) is True
    written = issues.description
    assert note_record(HOME, GHSA, f"{NOTE}\n", DAY) is False
    assert issues.description == written
    assert len(issues.writes) == 1


def test_note_on_a_closed_record(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    _listing(monkeypatch, _adv(state="closed"))
    assert note_record(HOME, GHSA, NOTE, DAY) is True
    assert NOTE in issues.description
    assert issues.states == {}


@pytest.mark.parametrize(
    ("advisory", "message"),
    [
        (_adv(OTHER), f"advisory {GHSA} does not exist in {HOME}"),
        (_adv(state="published"), "is in state published; note only annotates a draft or closed"),
        (_adv(state="withdrawn"), "is in state withdrawn"),
        (_adv(state="triage"), "is in state triage"),
        (_adv(external=True), "is an external report"),
    ],
)
def test_note_refuses_an_advisory_it_must_not_annotate(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, advisory: Advisory, message: str
) -> None:
    _listing(monkeypatch, advisory)
    with pytest.raises(NoteError, match=message) as excinfo:
        note_record(HOME, GHSA, NOTE, DAY)
    assert NOTE not in str(excinfo.value)
    assert issues.writes == []


@pytest.mark.parametrize(
    ("note", "message"),
    [
        ("", "the note is empty"),
        (" \n\t\n", "the note is empty"),
        (f"{NOTE}\nTracking issue: org/tool#12\n", "has a 'Tracking issue:' line"),
    ],
)
def test_note_refuses_a_bad_note(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, note: str, message: str
) -> None:
    listed = _listing(monkeypatch, _adv())
    with pytest.raises(NoteError, match=message) as excinfo:
        note_record(HOME, GHSA, note, DAY)
    assert NOTE not in str(excinfo.value)
    listed.assert_not_called()
    assert issues.writes == []


def test_check_note_keeps_a_mention_of_tracking_mid_line() -> None:
    text = "see the line Tracking issue: org/tool#12 above"
    assert check_note(f"{text}\r\n") == text


@pytest.mark.parametrize(
    ("fail_on", "step"),
    [
        (f"GET repos/org/.github/security-advisories/{GHSA}", "reading the description"),
        (f"PATCH repos/org/.github/security-advisories/{GHSA}", "writing the note"),
    ],
)
def test_note_names_the_failed_step(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, fail_on: str, step: str
) -> None:
    _listing(monkeypatch, _adv())
    issues.fail_on = fail_on
    with pytest.raises(NoteError) as excinfo:
        note_record(HOME, GHSA, NOTE, DAY)
    assert str(excinfo.value).startswith(f"advisory {GHSA}: {step} failed: ")
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert SECRET not in rendered
    assert NOTE not in rendered


def test_note_list_failure_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    error = github.GitHubAPIError(1, ("gh", "api", "x"), SECRET, "HTTP 500")
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(side_effect=error))
    with pytest.raises(NoteError, match="reading the advisories failed") as excinfo:
        note_record(HOME, GHSA, NOTE, DAY)
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
