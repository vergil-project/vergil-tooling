"""Tests for vergil_tooling.lib.security_records (epic vergil-project/.github#517)."""

from __future__ import annotations

import contextlib
import json
import subprocess
import traceback
from dataclasses import fields
from datetime import date
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, call, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import epics, github, labels, security_records, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.security_records import (
    Advisory,
    Classification,
    CloseError,
    GuardResult,
    NoteError,
    RetargetError,
    RetargetRefusedError,
    SecurityIssue,
    advisory_home,
    advisory_line,
    check_note,
    classify,
    close_comment,
    close_record,
    create_advisory,
    find_duplicate,
    fix_org,
    guard_check,
    list_advisories,
    mark_issue,
    moved_from_line,
    moved_to_line,
    note_header,
    note_record,
    pair,
    pair_check,
    parse_advisory_lines,
    parse_advisory_refs,
    parse_moved_from,
    parse_moved_to,
    parse_tracking,
    pending_syncs,
    read_text,
    records_pass,
    retarget,
    security_issues,
    set_state,
    sync_states,
    tracking_line,
    unmark_issue,
    with_note,
)

if TYPE_CHECKING:
    from collections.abc import Generator

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


def test_advisory_home_is_org_dotgithub_without_a_gh_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every repo is public (epic vergil-project/.github#1476): the home is a
    # fixed name, read from no gh call.
    no_gh = MagicMock(side_effect=AssertionError("no gh call"))
    monkeypatch.setattr(github, "read_json", no_gh)
    monkeypatch.setattr(github, "read_output", no_gh)
    assert advisory_home("org") == HOME


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
            f"Tracking issue: org/x#1 {SECRET}\nTracking issue: org/tool#12  \n"
            f"Moved from: {OTHER}\r\n> Moved from: {GHSA}\nMoved to: fix/.github {OTHER} \n"
            f"Moved to: fix/.github {OTHER} {SECRET}\nMoved from: {OTHER}x\n",
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
    assert rows[0]["moved_from"] == [f"Moved from: {OTHER}"]
    assert rows[0]["moved_to"] == [f"Moved to: fix/.github {OTHER} "]
    assert rows[1] == {
        "ghsa_id": OTHER,
        "state": "triage",
        "affected": None,
        "submission": True,
        "tracking": [],
        "moved_from": [],
        "moved_to": [],
    }


def test_list_advisories_parses_the_move_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "ghsa_id": GHSA,
            "state": "closed",
            "submission": False,
            "moved_from": [f"Moved from: {OTHER}"],
            "moved_to": [f"Moved to: fix/.github {OTHER}\r"],
        }
    ]
    monkeypatch.setattr(github, "read_json_paginated", MagicMock(return_value=rows))
    assert list_advisories(HOME) == [
        Advisory(
            GHSA,
            "closed",
            None,
            (),
            external=False,
            moved_from=(OTHER,),
            moved_to=(("fix", OTHER),),
        )
    ]


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
        self.severity: str | None = "high"
        self.ensure_labels = MagicMock()
        monkeypatch.setattr(labels, "ensure_labels", self.ensure_labels)
        monkeypatch.setattr(github, "read_json", self.read_json)
        monkeypatch.setattr(github, "write_json", self.write_json)
        # An unmark drops the label inside its one PATCH, never by a DELETE.
        monkeypatch.setattr(github, "delete", MagicMock(side_effect=AssertionError("DELETE")))
        monkeypatch.setattr(github, "read_json_paginated", self.read_json_paginated)
        # The comment dedups read through the trust layer: every comment in
        # ``comments`` is a trusted human's; ``untrusted`` adds an issue's
        # comment nodes by anyone, after those.
        self.untrusted: dict[IssueRef, list[dict[str, Any]]] = {}
        # Who wrote and last edited each body, when not the trusted human.
        self.body_author: dict[IssueRef, dict[str, str]] = {}
        self.body_editor: dict[IssueRef, dict[str, str]] = {}
        self.trust_reads: list[IssueRef] = []
        monkeypatch.setattr(github, "graphql", self.graphql)
        monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
        monkeypatch.setattr(security_records, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
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
            return {"summary": SECRET, "description": description, "severity": self.severity}
        ref = self._ref(endpoint)
        ref = self.moved.get(ref, ref)
        return {
            "number": ref.number,
            "repository_url": f"https://api.github.com/repos/{ref.owner}/{ref.repo}",
            "state": "closed" if ref in self.closed else "open",
            "labels": sorted(self.labels[ref]),
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
                }
                for ref in sorted(self.labels, key=lambda r: r.number)
                if (ref.owner, ref.repo) == (owner, repo) and "security" in self.labels[ref]
            ]
        return list(self.comments[self._not_moved(endpoint)])

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        """The trust layer's item read: body and comments, followed past a transfer.

        The body is the only copy the module sees (the REST reads carry none),
        written by the trusted human unless ``body_author`` or ``body_editor``
        names someone else for the issue.
        """
        assert query == trust._ITEM_QUERY
        owner, repo, number = variables["owner"], variables["name"], variables["number"]
        ref = IssueRef(str(owner), str(repo), int(str(number)))
        self._maybe_fail(f"GRAPHQL {ref.slug}")
        ref = self.moved.get(ref, ref)
        self.trust_reads.append(ref)
        nodes = trust_stubs.comments(*self.comments[ref]) + self.untrusted.get(ref, [])
        return trust_stubs.item(
            body=self.bodies[ref],
            author=self.body_author.get(ref, trust_stubs.HUMAN),
            editor=self.body_editor.get(ref),
            nodes=nodes,
        )

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


def test_unmark_dedup_ignores_outsider_comment(
    issues: Issues, capsys: pytest.CaptureFixture[str]
) -> None:
    # An outsider's "moved" comment never stands in for the record's own.
    moved = "Security record moved to org/tool#12"
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.untrusted[A] = [
        trust_stubs.comment(31, moved, trust_stubs.OUTSIDER),
        trust_stubs.comment(32, moved, trust_stubs.IMPOSTOR),
    ]
    unmark_issue(A, GHSA, (C,))
    assert issues.comments[A] == [moved]
    assert issues.bodies[A] == "Neutral."
    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"skipped untrusted content: org/.github#10 comment drive-by at {trust_stubs.CREATED}"
        " on comment 31 (untrusted)",
        f"skipped untrusted content: org/.github#10 comment vergil-user at {trust_stubs.CREATED}"
        " on comment 32 (wrong-type)",
    ]


def test_unmark_dedup_keeps_a_trusted_comment(issues: Issues) -> None:
    # The record's own comment, from an interrupted run, is not posted twice.
    moved = "Security record moved to org/tool#12"
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.comments[A] = [moved]
    unmark_issue(A, GHSA, (C,))
    assert issues.comments[A] == [moved]
    assert [w[0] for w in issues.writes] == ["PATCH"]


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
        retarget(HOME, GHSA, (C,), source=A)
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
    retarget(HOME, GHSA, (B, C, C), source=A)
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


def test_pair_onto_a_description_with_no_text(issues: Issues) -> None:
    issues.description = ""
    issues.add(C, "")
    pair(HOME, GHSA, C)
    assert issues.description == "Tracking issue: org/tool#12"


def test_retarget_never_unmarks_a_renamed_issue(issues: Issues) -> None:
    """A transferred issue's old number is rewritten away but never unmarked."""
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.moved[A] = C
    retarget(HOME, GHSA, (C,), source=A, renamed=(A,))
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert not [w for w in issues.writes if "/issues/10" in w[1]]


def test_retarget_requires_targets(issues: Issues) -> None:
    with pytest.raises(RetargetError, match=f"advisory {GHSA}: no target issues given"):
        retarget(HOME, GHSA, (), source=A)


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
        retarget(HOME, GHSA, (C,), source=A)
    assert str(excinfo.value).startswith(f"advisory {GHSA}: {step} failed: ")
    assert SECRET not in str(excinfo.value)
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))


def test_retarget_wraps_a_non_gh_failure(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "")
    issues.ensure_labels.side_effect = labels.UnknownLabelError(["security"])
    with pytest.raises(RetargetError, match="marking org/tool#12 failed") as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
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
        retarget(HOME, GHSA, (C,), source=A)
    # The tracking lines already name only C, so only A itself still points here.
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert issues.labels[A] == {"security", "bug"}
    issues.fail_on = None
    retarget(HOME, GHSA, (C,), source=A)
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
        retarget(HOME, GHSA, (C,), source=A)
    issues.fail_on = None
    retarget(HOME, GHSA, (C,), source=A)
    _assert_moved_a_to_c(issues)


def test_retarget_rerun_after_success_changes_nothing(issues: Issues) -> None:
    _dropping_a(issues)
    retarget(HOME, GHSA, (C,), source=A)
    writes = len(issues.writes)
    retarget(HOME, GHSA, (C,), source=A)
    # The source carries the moved-to comment naming C: the re-run writes nothing.
    assert issues.writes[writes:] == []
    _assert_moved_a_to_c(issues)


def test_retarget_unmarks_an_untracked_issue_that_names_the_advisory(issues: Issues) -> None:
    """The dropped set comes from the issues too, not only the old tracking lines."""
    _dropping_a(issues)
    issues.add(D, f"Stray.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.closed.add(D)
    retarget(HOME, GHSA, (C,), source=A)
    assert issues.labels[D] == set()
    assert issues.bodies[D] == "Stray."
    assert issues.comments[D] == [MOVED]


def test_retarget_never_unmarks_a_renamed_issue_that_carries_the_line(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    retarget(HOME, GHSA, (C,), source=A, renamed=(A,))
    assert not [w for w in issues.writes if "/issues/10" in w[1]]
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == []


# --- retarget: a dropped issue that was transferred (issue #4299) ---------------


def test_retarget_onto_a_transferred_issue_leaves_it_marked(issues: Issues) -> None:
    """The old number tracked, the issue moved to the target: nothing to unmark."""
    issues.add(C, "Moved.")
    issues.moved[A] = C
    notes = retarget(HOME, GHSA, (C,), source=A)
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
    notes = retarget(HOME, GHSA, (C,), source=A)
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
    notes = retarget(HOME, GHSA, (C,), source=A)
    assert notes == (
        "org/.github#10 was transferred to org/tool#13: followed the transfer and "
        "org/tool#13 has no line for this advisory, nothing to unmark",
    )
    assert not [w for w in issues.writes if "/issues/10" in w[1] or "/issues/13" in w[1]]


def test_retarget_reports_no_notes_without_a_transfer(issues: Issues) -> None:
    _dropping_a(issues)
    assert retarget(HOME, GHSA, (C,), source=A) == ()


def test_retarget_never_touches_an_issue_naming_another_advisory(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(D, f"Other.\n\n{advisory_line(HOME, OTHER)}", {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,), source=A)
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
    retarget(HOME, first, (C,), source=A)
    # After the first: A keeps the other advisory's line and the label.
    assert parse_advisory_refs(issues.bodies[A]) == (second,)
    assert issues.labels[A] == {"security", "bug"}
    retarget(HOME, second, (C,), source=A)
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
    retarget(HOME, GHSA, (C,), source=A)
    assert not [w for w in issues.writes if "/issues/10" in w[1]]
    assert issues.bodies[A] == other
    assert issues.labels[A] == {"security"}
    assert issues.comments[A] == []
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"


# --- retarget: the source and drop checks (vergil-tooling#4718) -----------------
#
# A is the source (the planning issue, say), C the new fix task, B another
# tracked issue and D a second dropped one.

TRACKING_A_B = f"{SECRET}\n\nTracking issue: org/.github#10\nTracking issue: org/.github#11"


def _issue_writes(issues: Issues) -> list[tuple[str, str, dict[str, object]]]:
    return [w for w in issues.writes if "security-advisories" not in w[1]]


def test_retarget_refuses_untracked_source(issues: Issues) -> None:
    _dropping_a(issues)
    issues.add(B, "Not tracked.")
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=B)
    message = str(excinfo.value)
    assert message == (
        f"advisory {GHSA}: --from org/.github#11 is not an issue it tracks (it tracks "
        f"org/.github#10); run: vrg-security-record retarget {GHSA} --from 'org/.github#10' "
        "--to 'org/tool#12'"
    )
    assert SECRET not in message
    # Nothing was marked, rewritten or unmarked.
    assert issues.writes == []
    issues.ensure_labels.assert_not_called()


def test_retarget_refuses_a_source_when_nothing_is_tracked(issues: Issues) -> None:
    issues.description = SECRET
    issues.add(A, "Neutral.")
    issues.add(C, "")
    with pytest.raises(RetargetRefusedError, match="it tracks no issue") as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
    assert "create --resume" in str(excinfo.value)
    assert issues.writes == []


def test_retarget_refuses_dropping_open_issue(issues: Issues) -> None:
    issues.description = TRACKING_A_B
    _dropping_a(issues)
    issues.add(B, f"Other task.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
    message = str(excinfo.value)
    assert message.startswith(
        f"advisory {GHSA}: this would stop tracking the open issue org/.github#11; "
        f"to keep tracking every open issue, run: vrg-security-record retarget {GHSA} "
        "--from 'org/.github#10' --to 'org/.github#11' --to 'org/tool#12'. "
    )
    assert "human's decision" in message
    assert "--drop" not in message
    assert issues.writes == []
    # With B named in drop, the move goes ahead and unmarks B too.
    retarget(HOME, GHSA, (C,), source=A, drop=(B,))
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert parse_advisory_refs(issues.bodies[B]) == ()
    _assert_moved_a_to_c(issues)


def test_retarget_keeps_an_open_issue_named_as_a_target(issues: Issues) -> None:
    issues.description = TRACKING_A_B
    _dropping_a(issues)
    issues.add(B, f"Other task.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    retarget(HOME, GHSA, (B, C), source=A)
    assert issues.description == (
        f"{SECRET}\n\nTracking issue: org/.github#11\nTracking issue: org/tool#12"
    )
    assert not [w for w in issues.writes if "/issues/11" in w[1]]


def test_retarget_drops_a_closed_issue_without_drop(issues: Issues) -> None:
    issues.description = TRACKING_A_B
    _dropping_a(issues)
    issues.add(B, f"Done.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.closed.add(B)
    retarget(HOME, GHSA, (C,), source=A)
    assert parse_advisory_refs(issues.bodies[B]) == ()


def test_retarget_source_may_be_dropped(issues: Issues) -> None:
    """The open source is the issue being handed on: no drop is needed for it."""
    _dropping_a(issues)
    assert A not in issues.closed
    retarget(HOME, GHSA, (C,), source=A)
    _assert_moved_a_to_c(issues)


def test_retarget_unmarks_source_last(issues: Issues) -> None:
    issues.description = f"{SECRET}\n\nTracking issue: org/.github#10\nTracking issue: org/tool#13"
    _dropping_a(issues)
    issues.add(D, f"Stray.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    retarget(HOME, GHSA, (C,), source=A, drop=(D,))
    comments = [w[1] for w in issues.writes if w[1].endswith("/comments")]
    assert comments == ["repos/org/tool/issues/13/comments", "repos/org/.github/issues/10/comments"]


def test_retarget_rerun_after_interrupted_unmark(issues: Issues) -> None:
    """Lines already rewritten to C; A still names the advisory: the re-run unmarks A."""
    _dropping_a(issues)
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    mark_issue(C, HOME, GHSA)
    issues.writes.clear()
    retarget(HOME, GHSA, (C,), source=A)
    _assert_moved_a_to_c(issues)
    # Only A's unmark was written: no mark, no rewrite.
    assert [(w[0], w[1]) for w in issues.writes] == [
        ("POST", "repos/org/.github/issues/10/comments"),
        ("PATCH", "repos/org/.github/issues/10"),
    ]


def test_retarget_rerun_after_interrupted_other_unmark(issues: Issues) -> None:
    """D unmarked already, A (the source, unmarked last) still marked: the re-run finishes."""
    _dropping_a(issues)
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    mark_issue(C, HOME, GHSA)
    issues.add(D, "Stray.", set())
    issues.comments[D] = [MOVED]
    issues.writes.clear()
    retarget(HOME, GHSA, (C,), source=A, drop=(D,))
    _assert_moved_a_to_c(issues)
    assert not [w for w in issues.writes if "/issues/13" in w[1]]


def test_retarget_rerun_after_completed_run(issues: Issues) -> None:
    _dropping_a(issues)
    retarget(HOME, GHSA, (C,), source=A)
    issues.writes.clear()
    issues.ensure_labels.reset_mock()
    notes = retarget(HOME, GHSA, (C,), source=A)
    assert notes == ()
    assert issues.writes == []
    _assert_moved_a_to_c(issues)


@pytest.mark.parametrize(
    "comments",
    [
        [],
        ["Security record moved to org/tool#13"],
        ["Security record moved to org/tool#12, org/tool#13"],
    ],
)
def test_retarget_refuses_unrelated_source_when_targets_match(
    issues: Issues, comments: list[str]
) -> None:
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.add(B, "Unrelated.")
    issues.comments[B] = comments
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=B)
    assert "it tracks org/tool#12" in str(excinfo.value)
    assert "--from 'org/tool#12' --to 'org/tool#12'" in str(excinfo.value)
    assert issues.writes == []


@pytest.mark.parametrize(
    "author", [trust_stubs.OUTSIDER, trust_stubs.IMPOSTOR], ids=["outsider", "impostor"]
)
def test_retarget_rerun_ignores_outsider_moved_comment(
    issues: Issues, capsys: pytest.CaptureFixture[str], author: dict[str, str]
) -> None:
    # Only the record's own "moved" comment ties a source to a finished move:
    # an untrusted copy naming exactly the targets is refused like no comment.
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.add(B, "Unrelated.")
    issues.untrusted[B] = [trust_stubs.comment(41, "Security record moved to org/tool#12", author)]
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=B)
    assert "it tracks org/tool#12" in str(excinfo.value)
    assert issues.writes == []
    err = capsys.readouterr().err
    assert f"org/.github#11 comment {author['login']} at {trust_stubs.CREATED}" in err
    assert SECRET not in err
    # The same comment from a trusted author is the finished move: nothing to do.
    issues.untrusted[B] = []
    issues.comments[B] = ["Security record moved to org/tool#12"]
    assert retarget(HOME, GHSA, (C,), source=B) == ()
    assert issues.writes == []


def test_retarget_rerun_from_a_transferred_source(issues: Issues) -> None:
    """A transfer's recovery command: --from the old number, which leads to the target."""
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    issues.add(C, f"Moved.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.moved[A] = C
    assert retarget(HOME, GHSA, (C,), source=A) == ()
    assert issues.writes == []


def test_transfer_recovery_command_is_accepted(issues: Issues) -> None:
    """intake-route's recovery after a transfer: --from the old number, keeping B."""
    issues.description = TRACKING_A_B
    issues.add(B, f"Other task.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.add(C, f"Moved.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    issues.moved[A] = C
    notes = retarget(HOME, GHSA, (B, C), source=A)
    assert notes == (
        "org/.github#10 was transferred to org/tool#12, a target: "
        "nothing left to unmark at org/.github#10",
    )
    assert issues.description == (
        f"{SECRET}\n\nTracking issue: org/.github#11\nTracking issue: org/tool#12"
    )
    assert issues.comments[B] == []


def test_drop_check_reads_unlisted_issue_state(issues: Issues) -> None:
    """A tracked issue without the security label is absent from the listing."""
    issues.description = TRACKING_A_B
    _dropping_a(issues)
    issues.add(B, "Lost its label.", set())
    with pytest.raises(RetargetRefusedError, match="the open issue org/.github#11"):
        retarget(HOME, GHSA, (C,), source=A)
    assert issues.writes == []
    # A failed state read refuses too, before any write.
    issues.fail_on = "GET repos/org/.github/issues/11"
    with pytest.raises(RetargetError, match="reading the state of org/.github#11 failed"):
        retarget(HOME, GHSA, (C,), source=A)
    assert issues.writes == []
    # Closed, it is dropped without drop.
    issues.fail_on = None
    issues.closed.add(B)
    retarget(HOME, GHSA, (C,), source=A)
    _assert_moved_a_to_c(issues)


# --- pair: the first pairing, for create (vergil-tooling#4718) ------------------


def test_pair_marks_then_writes_the_first_tracking_line(issues: Issues) -> None:
    issues.description = SECRET
    issues.add(C, "Neutral.")
    pair(HOME, GHSA, C)
    assert issues.labels[C] == {"security"}
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    kinds = [(w[0], "advisory" if "advisories" in w[1] else w[1]) for w in issues.writes]
    assert kinds[-1] == ("PATCH", "advisory")
    assert ("PATCH", "repos/org/tool/issues/12") in kinds[:-1]


def test_pair_resumed_finishes_the_mark(issues: Issues) -> None:
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    issues.add(C, "Neutral.")
    pair(HOME, GHSA, C)
    assert parse_advisory_refs(issues.bodies[C]) == (GHSA,)
    assert issues.patches() == []


def test_pair_refuses_existing_tracking(issues: Issues) -> None:
    issues.add(B, "Neutral.")
    with pytest.raises(RetargetRefusedError) as excinfo:
        pair(HOME, GHSA, B)
    assert str(excinfo.value) == (
        f"advisory {GHSA} already tracks org/.github#10, so its first pair is made; to move "
        f"it, run: vrg-security-record retarget {GHSA} --from 'org/.github#10' "
        "--to 'org/.github#11'"
    )
    assert issues.writes == []
    issues.ensure_labels.assert_not_called()


def test_pair_check_without_an_issue_refuses_any_tracking(issues: Issues) -> None:
    with pytest.raises(RetargetRefusedError, match="already tracks org/.github#10"):
        pair_check(HOME, GHSA, None)
    issues.description = SECRET
    pair_check(HOME, GHSA, None)
    assert issues.writes == []


# --- security issues ------------------------------------------------------------


def test_security_issues_reads_every_managed_repo_in_every_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: [".github", "tool", "unmanaged"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda repo: repo != "org/unmanaged")
    pages = {
        "org/.github": [
            {"number": 10, "state": "open", "state_reason": "reopened"},
            {"number": 11, "state": "closed", "state_reason": "NOT_PLANNED"},
        ],
        "org/tool": [{"number": 12, "state": "OPEN"}],
    }
    endpoints: list[str] = []

    def read(endpoint: str, key: str | None = None, *, jq: str | None = None) -> list[object]:
        endpoints.append(endpoint)
        assert jq is not None
        assert "pull_request == null" in jq
        assert "state_reason" in jq
        assert "body" not in jq  # the body comes only from the trust layer
        return list(pages[endpoint.split("/issues")[0].removeprefix("repos/")])

    monkeypatch.setattr(github, "read_json_paginated", read)
    _trusted_bodies(
        monkeypatch,
        {
            ("org/.github", 10): trust_stubs.item(body=f"x\n{advisory_line(HOME, GHSA)}"),
            ("org/.github", 11): trust_stubs.item(body=""),
            ("org/tool", 12): trust_stubs.item(body=""),
        },
    )
    assert security_issues("org") == [
        SecurityIssue(A, open=True, ghsa_ids=(GHSA,), state_reason=None),
        SecurityIssue(B, open=False, ghsa_ids=(), state_reason="not_planned"),
        SecurityIssue(C, open=True, ghsa_ids=(), state_reason=None),
    ]
    assert endpoints == [
        "repos/org/.github/issues?state=all&labels=security",
        "repos/org/tool/issues?state=all&labels=security",
    ]


def _trusted_bodies(
    monkeypatch: pytest.MonkeyPatch, items: dict[tuple[str, int], dict[str, Any]]
) -> trust_stubs.FakeItems:
    """Serve the trust layer's item reads from *items*, every other read failing the test."""
    fake = trust_stubs.FakeItems(items, strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    monkeypatch.setattr(security_records, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return fake


def test_security_issues_returns_every_ghsa_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    body = f"x\n{advisory_line(HOME, OTHER)}\n\n{advisory_line(HOME, GHSA)}"
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *_a, **_k: [{"number": 12, "state": "open"}]
    )
    _trusted_bodies(monkeypatch, {("org/tool", 12): trust_stubs.item(body=body)})
    assert security_issues("org") == [
        SecurityIssue(C, open=True, ghsa_ids=(OTHER, GHSA), state_reason=None)
    ]


def test_security_issues_never_takes_lines_from_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even a listing row that carried a body is ignored: the lines are the
    # trusted body's, read through the trust layer.
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    row = {"number": 12, "state": "open", "body": advisory_line(HOME, OTHER)}
    monkeypatch.setattr(github, "read_json_paginated", lambda *_a, **_k: [row])
    _trusted_bodies(
        monkeypatch, {("org/tool", 12): trust_stubs.item(body=advisory_line(HOME, GHSA))}
    )
    assert security_issues("org") == [SecurityIssue(C, True, (GHSA,), None)]


@pytest.mark.parametrize(
    ("author", "editor", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "untrusted"),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, "untrusted"),
        (trust_stubs.IMPOSTOR, None, "wrong-type"),
    ],
)
def test_security_issues_lists_an_untrusted_body_with_no_lines(
    monkeypatch: pytest.MonkeyPatch, author: dict[str, str], editor: Any, cause: str
) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *_a, **_k: [{"number": 12, "state": "open"}]
    )
    body = advisory_line(HOME, GHSA)
    _trusted_bodies(
        monkeypatch, {("org/tool", 12): trust_stubs.item(body=body, author=author, editor=editor)}
    )
    [issue] = security_issues("org")
    assert issue.ghsa_ids == ()
    assert issue.skipped is not None
    assert (issue.skipped.repo, issue.skipped.number, issue.skipped.field) == (
        "org/tool",
        12,
        "body",
    )
    assert issue.skipped.cause == cause


def test_security_issues_trust_read_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *_a, **_k: [{"number": 12, "state": "open"}]
    )
    error = github.GitHubAPIError(1, ("gh", "api", "graphql"), SECRET, "HTTP 502")
    monkeypatch.setattr(github, "graphql", MagicMock(side_effect=error))
    monkeypatch.setattr(security_records, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    with pytest.raises(trust.TrustReadError, match="HTTP 502") as excinfo:
        security_issues("org")
    assert SECRET not in str(excinfo.value)


def test_security_issues_loads_no_authors_with_no_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    monkeypatch.setattr(github, "read_json_paginated", lambda *_a, **_k: [])
    monkeypatch.setattr(
        security_records, "load_trusted_authors", MagicMock(side_effect=AssertionError("load"))
    )
    assert security_issues("org") == []


def test_security_issues_makes_no_parent_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    # security_issues serves retarget and close too: the guard's parent and
    # label reads happen only in guard_check, never in the listing. Its one
    # GraphQL read per issue is the trust layer's item read of the body.
    monkeypatch.setattr(github, "list_org_repos", lambda _org: ["tool"])
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    row = {"number": 12, "state": "closed", "state_reason": "completed"}
    monkeypatch.setattr(github, "read_json_paginated", lambda *_a, **_k: [row])
    for name in ("native_parent_of", "queue_or_archive_kind", "queue_kind", "_labels"):
        monkeypatch.setattr(epics, name, MagicMock(side_effect=AssertionError(name)))
    for name in ("read_json", "read_output"):
        monkeypatch.setattr(github, name, MagicMock(side_effect=AssertionError(name)))
    fake = _trusted_bodies(monkeypatch, {("org/tool", 12): trust_stubs.item(body="")})
    assert security_issues("org") == [SecurityIssue(C, False, (), "completed")]
    assert fake.reads == [("org/tool", 12)]


# --- classification -------------------------------------------------------------


def _adv(
    ghsa: str = GHSA, tracking: tuple[IssueRef, ...] = (A,), state: str = "draft", **kw: Any
) -> Advisory:
    return Advisory(ghsa, state, "tool", tracking, external=kw.get("external", False))


_UNSET = "unset"


def _iss(
    ref: IssueRef = A,
    ghsa: str | tuple[str, ...] | None = GHSA,
    *,
    open_: bool = True,
    reason: str | None = _UNSET,
) -> SecurityIssue:
    """A security issue; a closed one is closed as completed unless *reason* says otherwise."""
    ghsa_ids = () if ghsa is None else (ghsa,) if isinstance(ghsa, str) else ghsa
    if reason == _UNSET:
        reason = None if open_ else "completed"
    return SecurityIssue(ref, open_, ghsa_ids, reason)


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


DEV_EPIC = IssueRef("org", ".github", 500)
PLANNING_EPIC = IssueRef("org", ".github", 501)
PLANNING_ARCHIVE = IssueRef("org", ".github", 502)
# Each parent epic's labels and title, as epics._labels_and_title reads them.
EPIC_ROWS: dict[IssueRef, tuple[set[str], str]] = {
    DEV_EPIC: ({"epic"}, "Epic: build a thing"),
    PLANNING_EPIC: ({"epic", "planning"}, "Epic (planning): org"),
    PLANNING_ARCHIVE: ({"archive", "planning"}, "Archive (planning): org — 2026-Q3"),
}


class Parents:
    """A fake of the guard's reads: each issue's native parent and each epic's row.

    ``queue_or_archive_kind`` runs for real over :data:`EPIC_ROWS`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.of: dict[IssueRef, IssueRef] = {}
        self.fail: dict[IssueRef, Exception] = {}
        self.reads: list[IssueRef] = []
        monkeypatch.setattr(epics, "native_parent_of", self.native_parent_of)
        monkeypatch.setattr(epics, "_labels_and_title", lambda ref: EPIC_ROWS[ref])

    def native_parent_of(self, ref: IssueRef) -> IssueRef | None:
        self.reads.append(ref)
        if ref in self.fail:
            raise self.fail[ref]
        return self.of.get(ref)


@pytest.fixture
def parents(monkeypatch: pytest.MonkeyPatch) -> Parents:
    return Parents(monkeypatch)


def _sync(
    monkeypatch: pytest.MonkeyPatch,
    advisories: list[Advisory],
    issue_list: list[SecurityIssue],
) -> tuple[int, MagicMock]:
    # Every issue is unparented unless a test installs its own Parents first.
    if not isinstance(getattr(epics.native_parent_of, "__self__", None), Parents):
        Parents(monkeypatch)
    write = MagicMock()
    monkeypatch.setattr(github, "write_json", write)
    result = classify(advisories, issue_list)
    changed = sync_states(HOME, result, issue_list, guard_check(result, issue_list))
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


_NO_GUARD = GuardResult(held=[], errors=[])


def test_pending_syncs_lists_only_mismatches() -> None:
    issue_list = [_iss(A, open_=False), _iss(C, OTHER)]
    advisories = [_adv(tracking=(A,)), _adv(OTHER, tracking=(C,))]
    result = classify(advisories, issue_list)
    assert pending_syncs(result, issue_list, _NO_GUARD) == [(GHSA, "closed")]


def test_sync_failure_is_scrubbed_of_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock(side_effect=github.GitHubAPIError(1, ["gh"], SECRET, "HTTP 403"))
    monkeypatch.setattr(github, "write_json", write)
    issue_list = [_iss(A, open_=False)]
    with pytest.raises(github.GitHubAPIError) as excinfo:
        sync_states(HOME, classify([_adv()], issue_list), issue_list, _NO_GUARD)
    assert SECRET not in str(excinfo.value)
    assert excinfo.value.stdout is None


# --- the sync close guard (spec 6.2) ------------------------------------------


def _guarded(
    advisories: list[Advisory], issue_list: list[SecurityIssue]
) -> tuple[GuardResult, list[tuple[str, str]]]:
    result = classify(advisories, issue_list)
    guard = guard_check(result, issue_list)
    return guard, list(pending_syncs(result, issue_list, guard))


def test_guard_holds_queue_item(parents: Parents) -> None:
    parents.of[A] = PLANNING_EPIC
    guard, changes = _guarded([_adv()], [_iss(A, open_=False)])
    assert changes == []
    assert guard == GuardResult(held=[(GHSA, (A,))], errors=[])


def test_guard_holds_drained_queue_item(parents: Parents) -> None:
    parents.of[A] = PLANNING_ARCHIVE
    guard, changes = _guarded([_adv()], [_iss(A, open_=False)])
    assert changes == []
    assert guard.held == [(GHSA, (A,))]


@pytest.mark.parametrize("reason", ["not_planned", "duplicate", "some_new_reason", None])
def test_guard_holds_non_completed_reasons(parents: Parents, reason: str | None) -> None:
    # Review Focus 4: a fix task closed as a duplicate (or with any reason but
    # completed, or none) was the advisory's last open issue. Its parent is a
    # development epic, so only the close reason fires the guard, and the
    # guard needs no parent read for it.
    parents.of[A] = DEV_EPIC
    parents.of[C] = DEV_EPIC
    advisory = _adv(tracking=(A, C))
    issue_list = [_iss(A, open_=False), _iss(C, open_=False, reason=reason)]
    guard, changes = _guarded([advisory], issue_list)
    assert changes == []
    assert guard == GuardResult(held=[(GHSA, (C,))], errors=[])
    assert parents.reads == [A]


def test_guard_names_every_ref_that_fires_it(parents: Parents) -> None:
    parents.of[A] = PLANNING_EPIC
    issue_list = [_iss(A, open_=False), _iss(C, open_=False, reason="not_planned")]
    guard, _ = _guarded([_adv(tracking=(A, C))], issue_list)
    assert guard.held == [(GHSA, (A, C))]


def test_guard_leaves_closed_advisory_alone(parents: Parents) -> None:
    # Review Focus 3: a human closed the advisory with close; its tracked
    # issue is a closed planning item already drained into its archive.
    parents.of[A] = PLANNING_ARCHIVE
    issue_list = [_iss(A, open_=False, reason="not_planned")]
    guard, changes = _guarded([_adv(state="closed")], issue_list)
    assert changes == []
    assert guard == _NO_GUARD
    assert parents.reads == []


def test_guard_never_holds_an_advisory_with_an_open_issue(parents: Parents) -> None:
    # A closed advisory whose queue item reopened still goes back to draft.
    parents.of[A] = PLANNING_EPIC
    guard, changes = _guarded([_adv(state="closed")], [_iss(A)])
    assert changes == [(GHSA, "draft")]
    assert guard == _NO_GUARD
    assert parents.reads == []


def test_completed_fix_tasks_close_advisory(parents: Parents) -> None:
    parents.of[A] = DEV_EPIC
    parents.of[C] = DEV_EPIC
    issue_list = [_iss(A, open_=False), _iss(C, open_=False)]
    guard, changes = _guarded([_adv(tracking=(A, C))], issue_list)
    assert changes == [(GHSA, "closed")]
    assert guard == _NO_GUARD


def test_guard_reads_each_parent_once(parents: Parents) -> None:
    parents.of[A] = DEV_EPIC
    issue_list = [_iss(A, (GHSA, OTHER), open_=False)]
    guard, changes = _guarded([_adv(), _adv(OTHER)], issue_list)
    assert changes == [(GHSA, "closed"), (OTHER, "closed")]
    assert guard == _NO_GUARD
    assert parents.reads == [A]


def test_guard_skips_external_reports(parents: Parents) -> None:
    guard, _ = _guarded([_adv(external=True)], [_iss(A, open_=False, reason="not_planned")])
    assert guard == _NO_GUARD


def test_guard_error_is_scrubbed(parents: Parents) -> None:
    parents.fail[A] = github.GitHubAPIError(1, ["gh"], SECRET, "HTTP 502\nBad gateway")
    guard, changes = _guarded([_adv()], [_iss(A, open_=False)])
    assert changes == []
    assert guard.held == []
    [(ghsa_id, ref, error)] = guard.errors
    assert (ghsa_id, ref) == (GHSA, A)
    assert SECRET not in error
    assert "HTTP 502 Bad gateway" in error


def test_guard_reports_a_graphql_error(parents: Parents) -> None:
    parents.fail[A] = RuntimeError("GraphQL errors: [NOT_FOUND]")
    guard, _ = _guarded([_adv()], [_iss(A, open_=False)])
    assert guard.errors == [(GHSA, A, "GraphQL errors: [NOT_FOUND]")]


def test_guard_lets_a_missing_token_propagate(parents: Parents) -> None:
    parents.fail[A] = github.MissingGitHubTokenError(1, ["gh"], None, "no token")
    with pytest.raises(github.MissingGitHubTokenError):
        _guarded([_adv()], [_iss(A, open_=False)])


def _records_pass(
    monkeypatch: pytest.MonkeyPatch,
    advisories: list[Advisory],
    issue_list: list[SecurityIssue],
    *,
    apply: bool = True,
) -> tuple[int, MagicMock]:
    write = MagicMock()
    monkeypatch.setattr(security_records, "security_issues", lambda _org: issue_list)
    monkeypatch.setattr(security_records, "advisory_home", lambda _org: HOME)
    monkeypatch.setattr(security_records, "list_advisories", lambda _home: advisories)
    monkeypatch.setattr(github, "write_json", write)
    return records_pass("org", apply=apply, prog="prog"), write


@pytest.mark.parametrize("apply", [True, False])
def test_records_pass_reports_held_without_failing(
    monkeypatch: pytest.MonkeyPatch,
    parents: Parents,
    capsys: pytest.CaptureFixture[str],
    *,
    apply: bool,
) -> None:
    parents.of[A] = PLANNING_EPIC
    rc, write = _records_pass(monkeypatch, [_adv()], [_iss(A, open_=False)], apply=apply)
    assert rc == 0
    write.assert_not_called()
    out = capsys.readouterr().out
    mode, verb = ("", "synced") if apply else (" (preview)", "would sync")
    assert (
        f"Security records{mode}: 0 advisory states {verb}, 0 broken pairs, 1 held, 0 errors."
        in out
    )
    assert (
        f"- held: {GHSA} (org/.github#10): retarget it to a fix task, "
        "or close it with vrg-security-record close"
    ) in out
    assert "- broken:" not in out


def test_parent_read_error_is_per_advisory(
    monkeypatch: pytest.MonkeyPatch, parents: Parents, capsys: pytest.CaptureFixture[str]
) -> None:
    parents.fail[A] = github.GitHubAPIError(1, ["gh"], SECRET, "HTTP 502")
    parents.of[C] = DEV_EPIC
    issue_list = [_iss(A, open_=False), _iss(C, OTHER, open_=False)]
    advisories = [_adv(tracking=(A,)), _adv(OTHER, tracking=(C,))]
    rc, write = _records_pass(monkeypatch, advisories, issue_list)
    assert rc == 1
    assert write.call_args_list == [_patched_state(OTHER, "closed")]
    out = capsys.readouterr().out
    assert "1 advisory state synced, 0 broken pairs, 0 held, 1 error." in out
    assert f"- error: {GHSA} (org/.github#10): reading its parent failed, left unchanged: " in out
    assert SECRET not in out


def test_parent_read_error_preview_exits_zero(
    monkeypatch: pytest.MonkeyPatch, parents: Parents, capsys: pytest.CaptureFixture[str]
) -> None:
    parents.fail[A] = github.GitHubAPIError(1, ["gh"], None, "HTTP 502")
    rc, write = _records_pass(monkeypatch, [_adv()], [_iss(A, open_=False)], apply=False)
    assert rc == 0
    write.assert_not_called()
    assert f"- error: {GHSA} (org/.github#10)" in capsys.readouterr().out


def test_retarget_issue_calls_in_order(issues: Issues) -> None:
    """Marking happens, then the advisory PATCH, then the unmark — never out of order."""
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "")
    retarget(HOME, GHSA, (C,), source=A)
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


def test_close_dedup_ignores_outsider_comment(
    issues: Issues, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An outsider's copy of the close comment, or a trusted one an outsider
    # edited, is no audit trail: the comment is still posted.
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.closed.add(A)
    issues.untrusted[A] = [
        trust_stubs.comment(21, CLOSED_COMMENT, trust_stubs.OUTSIDER),
        trust_stubs.comment(22, CLOSED_COMMENT, trust_stubs.HUMAN, editor=trust_stubs.OUTSIDER),
    ]
    assert close_record(HOME, GHSA, REASON).closed is True
    assert issues.comments[A] == [CLOSED_COMMENT]
    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"skipped untrusted content: org/.github#10 comment drive-by at {trust_stubs.CREATED}"
        " on comment 21 (untrusted)",
        f"skipped untrusted content: org/.github#10 edit drive-by at {trust_stubs.EDITED}"
        " on comment 22 (untrusted)",
    ]
    assert SECRET not in err


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
        # The listing reads each security issue's body through the trust layer.
        ("GRAPHQL org/.github#10", "listing the security issues"),
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


def test_note_that_is_a_prefix_of_an_earlier_note_is_added(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    assert note_record(HOME, GHSA, f"{NOTE} and more", DAY) is True
    assert note_record(HOME, GHSA, NOTE, DAY) is True
    assert issues.description == (
        f"{SECRET}\n\nNote (2026-11-16):\n{NOTE} and more\n\nNote (2026-11-16):\n{NOTE}"
        "\n\nTracking issue: org/.github#10"
    )


def test_note_repeating_an_older_note_is_added(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    note_record(HOME, GHSA, NOTE, DAY)
    note_record(HOME, GHSA, "another note", DAY)
    assert note_record(HOME, GHSA, NOTE, DAY) is True
    assert issues.description.count(f"Note (2026-11-16):\n{NOTE}\n") == 2


def test_note_rerun_on_a_description_of_only_the_note(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=()))
    issues.description = ""
    assert note_record(HOME, GHSA, NOTE, DAY) is True
    assert note_record(HOME, GHSA, NOTE, DAY) is False
    assert issues.description == f"Note (2026-11-16):\n{NOTE}"


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


# --- the cross-org move (epic vergil-project/.github#1417, spec 4.6) ---------------

FIX_HOME = "fix/.github"
FIX = IssueRef("fix", ".github", 20)
FIX2 = IssueRef("fix", "tool", 21)
COPY = "GHSA-c0py-2345-wxyz"
SECOND = "GHSA-c0py-6789-wxyz"
MOVED_COMMENT = "Security record moved to fix/.github#20, fix/tool#21"


class Move:
    """Two advisory homes over :class:`Issues`: the original's org and the fix org.

    Each advisory lives in one home with a state and a description; the list
    reads derive from the description exactly as the ``--jq`` projection does,
    a create adds a copy, and every write records the org its token was for.
    """

    def __init__(self, issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
        self.issues = issues
        self.homes: dict[str, str] = {GHSA: HOME}
        issues.states[GHSA] = "draft"
        self.external: set[str] = set()
        self.next_ids = [COPY, SECOND]
        self.created: list[tuple[str, dict[str, object]]] = []
        self.org: str | None = None
        self.write_orgs: list[tuple[str | None, str, str]] = []
        issues.descriptions[GHSA] = f"{SECRET}\n\nTracking issue: org/.github#10"
        issues.add(A, f"Planning.\n\n{advisory_line(HOME, GHSA)}", {"security"})
        issues.add(FIX, "Fix one.")
        issues.add(FIX2, "Fix two.")
        inner_write = issues.write_json

        def write_json(method: str, endpoint: str, body: dict[str, object]) -> None:
            inner_write(method, endpoint, body)
            self.write_orgs.append((self.org, method, endpoint))

        monkeypatch.setattr(github, "write_json", write_json)
        monkeypatch.setattr(github, "post_json_once", self.post_json_once)
        monkeypatch.setattr(security_records, "list_advisories", self.list_advisories)
        monkeypatch.setattr(github, "target_org", self.target_org)

    @contextlib.contextmanager
    def target_org(self, org: str | None) -> Generator[None]:
        before, self.org = self.org, org
        try:
            yield
        finally:
            self.org = before

    def add(self, ghsa_id: str, home: str, description: str, state: str = "draft") -> None:
        self.homes[ghsa_id] = home
        self.issues.descriptions[ghsa_id] = description
        self.issues.states[ghsa_id] = state

    def post_json_once(self, endpoint: str, body: dict[str, object]) -> dict[str, object]:
        self.issues._maybe_fail(f"POST {endpoint}")
        home = "/".join(endpoint.split("/")[1:3])
        ghsa_id = self.next_ids.pop(0)
        self.created.append((home, body))
        self.write_orgs.append((self.org, "POST", endpoint))
        self.add(ghsa_id, home, str(body["description"]))
        return {"ghsa_id": ghsa_id}

    def list_advisories(self, home: str) -> list[Advisory]:
        self.issues._maybe_fail(f"LIST {home}")
        found: list[Advisory] = []
        for ghsa_id, where in sorted(self.homes.items()):
            if where != home:
                continue
            state = self.issues.states.get(ghsa_id, "draft")
            text = self.issues.descriptions[ghsa_id]
            found.append(
                Advisory(
                    ghsa_id,
                    state,
                    "tool",
                    parse_tracking(text),
                    external=ghsa_id in self.external or state == "triage",
                    moved_from=parse_moved_from(text),
                    moved_to=parse_moved_to(text),
                )
            )
        return found

    def run(self, *, drop: tuple[IssueRef, ...] = ()) -> tuple[str, ...]:
        return retarget(HOME, GHSA, (FIX, FIX2), source=A, drop=drop)


@pytest.fixture
def move(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> Move:
    return Move(issues, monkeypatch)


def _assert_moved(move: Move, copy_id: str = COPY) -> None:
    issues = move.issues
    assert issues.descriptions[copy_id] == (
        f"{SECRET}\n\nMoved from: {GHSA}\n\nTracking issue: fix/.github#20\n"
        "Tracking issue: fix/tool#21"
    )
    for ref in (FIX, FIX2):
        assert issues.labels[ref] == {"security"}
        assert parse_advisory_lines(issues.bodies[ref]) == (("fix", copy_id),)
    assert issues.descriptions[GHSA] == f"{SECRET}\n\nMoved to: fix/.github {copy_id}"
    assert issues.states[GHSA] == "closed"
    assert issues.states[copy_id] == "draft"
    assert issues.labels[A] == set()
    assert issues.bodies[A] == "Planning."
    assert issues.comments[A] == [MOVED_COMMENT]


def test_move_lines_are_exact() -> None:
    assert moved_from_line(GHSA) == f"Moved from: {GHSA}"
    assert moved_to_line(FIX_HOME, COPY) == f"Moved to: fix/.github {COPY}"
    text = f"x\nMoved from: {GHSA} \r\nMoved to: fix/.github {COPY}\n> Moved from: {OTHER}"
    assert parse_moved_from(text) == (GHSA,)
    assert parse_moved_to(text) == (("fix", COPY),)
    assert parse_moved_to(f"Moved to: fix/tool {COPY}") == ()


def test_move_creates_in_fix_org_with_moved_from(move: Move) -> None:
    notes = move.run()
    assert notes == (moved_to_line(FIX_HOME, COPY),)
    [(home, body)] = move.created
    assert home == FIX_HOME
    assert body["summary"] == SECRET
    assert body["severity"] == "high"
    assert body["description"] == f"{SECRET}\n\nMoved from: {GHSA}"
    assert body["vulnerabilities"] == [{"package": {"ecosystem": "other", "name": ".github"}}]
    _assert_moved(move)
    # Every write in the fix org ran under its token, every other under the original's.
    for org, _method, endpoint in move.write_orgs:
        assert org == endpoint.split("/")[1], endpoint


def test_move_writes_moved_to_then_closes_original(move: Move) -> None:
    move.run()
    advisory = f"repos/org/.github/security-advisories/{GHSA}"
    original = [
        (method, endpoint, body)
        for method, endpoint, body in move.issues.writes
        if endpoint.startswith("repos/org/")
    ]
    assert [(m, e) for m, e, _ in original] == [
        ("PATCH", advisory),
        ("POST", "repos/org/.github/issues/10/comments"),
        ("PATCH", "repos/org/.github/issues/10"),
        ("PATCH", advisory),
        ("PATCH", advisory),
    ]
    # The Moved to: line lands while the original still tracks --from.
    assert original[0][2] == {
        "description": (
            f"{SECRET}\n\nMoved to: fix/.github {COPY}\n\nTracking issue: org/.github#10"
        )
    }
    assert original[3][2] == {"description": f"{SECRET}\n\nMoved to: fix/.github {COPY}"}
    assert original[4][2] == {"state": "closed"}
    # The copy exists and tracks its fix before the original changes at all.
    first_original = move.write_orgs.index(("org", "PATCH", advisory))
    fix_writes = [i for i, (org, _m, _e) in enumerate(move.write_orgs) if org == "fix"]
    assert fix_writes
    assert max(fix_writes) < first_original


@pytest.mark.parametrize(
    "fail_on",
    [
        "POST repos/fix/.github/issues/20/labels",
        "PATCH repos/fix/tool/issues/21",
        f"PATCH repos/fix/.github/security-advisories/{COPY}",
        f"PATCH repos/org/.github/security-advisories/{GHSA}",
        "POST repos/org/.github/issues/10/comments",
        "PATCH repos/org/.github/issues/10",
    ],
)
def test_move_rerun_reuses_moved_from(move: Move, fail_on: str) -> None:
    """A re-run after the copy exists but before the original closes reuses the copy."""
    move.issues.fail_on = fail_on
    with pytest.raises(RetargetError) as excinfo:
        move.run()
    assert not isinstance(excinfo.value, RetargetRefusedError)
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
    assert len(move.created) == 1
    move.issues.fail_on = None
    assert move.run() == (moved_to_line(FIX_HOME, COPY),)
    assert len(move.created) == 1
    _assert_moved(move)


def test_move_rerun_after_tracking_removed_closes_it(
    move: Move, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = security_records.set_state
    failing = MagicMock(side_effect=github.GitHubAPIError(1, ("gh",), SECRET, "HTTP 500"))
    monkeypatch.setattr(security_records, "set_state", failing)
    with pytest.raises(RetargetError, match="setting the state to closed failed"):
        move.run()
    assert move.issues.states[GHSA] == "draft"
    assert parse_tracking(move.issues.descriptions[GHSA]) == ()
    monkeypatch.setattr(security_records, "set_state", real)
    move.run()
    assert len(move.created) == 1
    _assert_moved(move)


def test_move_rerun_after_success_writes_nothing(move: Move) -> None:
    move.run()
    before = len(move.write_orgs)
    assert move.run() == (moved_to_line(FIX_HOME, COPY),)
    assert len(move.write_orgs) == before
    assert len(move.created) == 1


def test_move_create_failure_rerun_finds_the_copy(
    move: Move, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create whose response was lost left the copy; the re-run finds it, never a second."""

    def lost(endpoint: str, body: dict[str, object]) -> dict[str, object]:
        move.post_json_once(endpoint, body)
        raise github.GitHubAPIError(1, ("gh", "api", endpoint), SECRET, "HTTP 502")

    monkeypatch.setattr(github, "post_json_once", lost)
    with pytest.raises(RetargetError, match=f"creating its copy in {FIX_HOME} failed") as excinfo:
        move.run()
    assert SECRET not in "".join(traceback.format_exception(excinfo.value))
    monkeypatch.setattr(github, "post_json_once", move.post_json_once)
    move.run()
    assert len(move.created) == 1
    _assert_moved(move)


@pytest.mark.parametrize(
    ("state", "external", "kind"),
    [
        ("triage", False, "an external report"),
        ("draft", True, "an external report"),
        ("closed", False, "closed"),
    ],
)
def test_move_ignores_external_report_with_moved_from(
    move: Move,
    state: str,
    external: bool,
    kind: str,  # noqa: FBT001
) -> None:
    """A report carrying the line, or a copy no longer a draft, is never reused."""
    move.add(SECOND, FIX_HOME, f"Outside text.\n\nMoved from: {GHSA}", state)
    if external:
        move.external.add(SECOND)
    with pytest.raises(RetargetRefusedError) as excinfo:
        move.run()
    message = str(excinfo.value)
    assert f"{SECOND} ({kind})" in message
    assert "reuses none of these" in message
    assert move.issues.writes == []
    assert move.created == []
    assert SECRET not in message


def test_move_two_moved_from_refused(move: Move) -> None:
    move.add(COPY, FIX_HOME, f"One.\n\nMoved from: {GHSA}")
    move.add(SECOND, FIX_HOME, f"Two.\n\nMoved from: {GHSA}")
    with pytest.raises(RetargetRefusedError) as excinfo:
        move.run()
    assert f"{COPY} (draft), {SECOND} (draft)" in str(excinfo.value)
    assert move.issues.writes == []
    assert move.created == []


@pytest.mark.parametrize(
    "line",
    [
        f"> Moved from: {GHSA}",
        f"Moved from: {GHSA} and more",
        f"see Moved from: {GHSA}",
        f"Moved from: {OTHER}",
    ],
)
def test_move_moved_from_not_alone_ignored(move: Move, line: str) -> None:
    move.next_ids = [SECOND]
    move.add(COPY, FIX_HOME, f"Unrelated.\n\n{line}")
    move.run()
    assert [home for home, _body in move.created] == [FIX_HOME]
    _assert_moved(move, SECOND)
    assert move.issues.descriptions[COPY] == f"Unrelated.\n\n{line}"
    assert not [w for w in move.issues.writes if COPY in w[1]]


def test_move_keeps_drop_rule(move: Move) -> None:
    move.issues.descriptions[GHSA] = (
        f"{SECRET}\n\nTracking issue: org/.github#10\nTracking issue: org/.github#11"
    )
    move.issues.add(B, f"Open work.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    with pytest.raises(RetargetRefusedError) as excinfo:
        move.run()
    message = str(excinfo.value)
    assert "this would stop tracking the open issue org/.github#11" in message
    assert "a move to fix/.github closes this advisory" in message
    command = (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#10' "
        "--to 'fix/.github#20' --to 'fix/tool#21'"
    )
    assert f"run: {command}." in message
    assert "--drop" not in message
    assert "Leaving an open issue untracked is the human's decision" in message
    assert move.issues.writes == []
    assert move.created == []
    # Naming it with --drop is the human's call; the move then unmarks it too.
    move.run(drop=(B,))
    assert move.issues.labels[B] == set()
    assert move.issues.comments[B] == [MOVED_COMMENT]
    assert move.issues.states[GHSA] == "closed"


def test_move_drop_refusal_repeats_the_given_drops(move: Move) -> None:
    third = IssueRef("org", "tool", 13)
    move.issues.descriptions[GHSA] = (
        f"{SECRET}\n\nTracking issue: org/.github#10\nTracking issue: org/.github#11\n"
        "Tracking issue: org/tool#13"
    )
    move.issues.add(B, advisory_line(HOME, GHSA), {"security"})
    move.issues.add(third, advisory_line(HOME, GHSA), {"security"})
    with pytest.raises(RetargetRefusedError) as excinfo:
        move.run(drop=(third,))
    assert "--drop 'org/tool#13'" in str(excinfo.value)
    assert "open issue org/.github#11" in str(excinfo.value)


def test_targets_span_orgs_refused(move: Move) -> None:
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C, FIX), source=A)
    message = str(excinfo.value)
    assert "span orgs (fix, org)" in message
    assert "duplicate the advisory into each org" in message
    assert "each copy tracking that org's own fix tasks" in message
    assert "break the finding up into separate advisories" in message
    assert move.issues.writes == []
    assert fix_org(GHSA, (FIX, FIX2)) == "fix"


@pytest.mark.parametrize("state", ["published", "withdrawn", "closed"])
def test_move_refuses_an_original_it_must_not_move(move: Move, state: str) -> None:
    move.issues.states[GHSA] = state
    with pytest.raises(RetargetRefusedError, match=f"is in state {state}; a move hands on"):
        move.run()
    assert move.issues.writes == []


def test_move_refuses_an_original_with_two_moved_to_lines(move: Move) -> None:
    move.issues.descriptions[GHSA] += (
        f"\nMoved to: fix/.github {COPY}\nMoved to: fix/.github {SECOND}"
    )
    with pytest.raises(RetargetRefusedError, match="more than one Moved to: line"):
        move.run()
    assert move.issues.writes == []


def test_move_refuses_an_original_that_is_itself_a_copy(move: Move) -> None:
    move.issues.descriptions[GHSA] += f"\nMoved from: {SECOND}"
    with pytest.raises(RetargetRefusedError) as excinfo:
        move.run()
    message = str(excinfo.value)
    assert f"is itself the copy of a move (it has a Moved from: {SECOND} line)" in message
    assert "one copy of the finding per org" in message
    assert move.issues.writes == []
    assert move.created == []


def test_move_refuses_a_moved_to_line_naming_no_copy(move: Move) -> None:
    move.issues.descriptions[GHSA] = (
        f"{SECRET}\n\nMoved to: fix/.github {SECOND}\n\nTracking issue: org/.github#10"
    )
    with pytest.raises(RetargetRefusedError, match=f"already moved to fix/.github {SECOND}"):
        move.run()
    assert move.issues.writes == []
    assert move.created == []


def test_move_refuses_a_copy_tracking_other_issues(move: Move) -> None:
    move.add(COPY, FIX_HOME, f"{SECRET}\n\nMoved from: {GHSA}\n\nTracking issue: fix/tool#99")
    with pytest.raises(RetargetRefusedError, match="already tracks fix/tool#99"):
        move.run()
    assert move.issues.writes == []


def test_move_refuses_an_unrelated_source_once_tracking_is_gone(move: Move) -> None:
    move.run()
    move.issues.add(B, "Unrelated.")
    with pytest.raises(RetargetRefusedError, match="is not the source of a move"):
        retarget(HOME, GHSA, (FIX, FIX2), source=B)


@pytest.mark.parametrize("severity", [None, "moderate"])
def test_move_refuses_a_severity_the_copy_cannot_take(move: Move, severity: str | None) -> None:
    move.issues.severity = severity
    with pytest.raises(RetargetRefusedError, match="set its severity first"):
        move.run()
    assert move.issues.writes == []
    assert move.created == []


def test_move_refuses_a_missing_original(move: Move) -> None:
    del move.homes[GHSA]
    with pytest.raises(RetargetRefusedError, match=f"{GHSA} does not exist in {HOME}"):
        move.run()
    assert move.issues.writes == []


def test_move_fails_on_an_unreadable_original(move: Move, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", MagicMock(return_value={"summary": None}))
    with pytest.raises(RetargetError, match="reading the advisory failed: .* no summary"):
        move.run()
    assert move.issues.writes == []


def test_move_refuses_an_untracked_source(move: Move) -> None:
    move.issues.add(B, "Unrelated.")
    with pytest.raises(RetargetRefusedError, match="is not an issue it tracks"):
        retarget(HOME, GHSA, (FIX, FIX2), source=B)
    assert move.issues.writes == []


def test_same_org_retarget_unchanged(issues: Issues, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retarget inside one org lists no advisory, creates none and moves nothing."""
    for name in ("list_advisories", "create_advisory"):
        monkeypatch.setattr(security_records, name, MagicMock(side_effect=AssertionError(name)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "")
    assert retarget(HOME, GHSA, (C,), source=A) == ()
    assert issues.description == f"{SECRET}\n\nTracking issue: org/tool#12"
    assert issues.states == {}
    assert "Moved" not in issues.description


# --- moved: classification and sync ------------------------------------------------


def _moved(
    ghsa: str = GHSA, to: tuple[tuple[str, str], ...] = (("fix", COPY),), **kw: Any
) -> Advisory:
    return Advisory(
        ghsa,
        kw.get("state", "closed"),
        "tool",
        kw.get("tracking", ()),
        external=kw.get("external", False),
        moved_to=to,
    )


def test_classify_moved_not_broken() -> None:
    advisory = _moved()
    assert classify([advisory], []) == Classification(
        paired=[], external=[], broken=[], moved=[advisory]
    )
    # Mid-move it may still track an issue that names it: still moved, not paired.
    midway = _moved(state="draft", tracking=(A,))
    result = classify([midway], [_iss(A)])
    assert result.moved == [midway]
    assert result.broken == []
    assert result.paired == []


def test_classify_two_moved_to_lines_broken() -> None:
    advisory = _moved(to=(("fix", COPY), ("fix", SECOND)))
    result = classify([advisory], [])
    assert result.broken == [f"{GHSA}: more than one Moved to: line"]
    assert result.moved == []


def test_classify_external_report_with_moved_to_stays_external() -> None:
    advisory = _moved(state="triage", external=True)
    result = classify([advisory], [])
    assert result.external == [advisory]
    assert result.moved == []


def _moved_pass(
    monkeypatch: pytest.MonkeyPatch, homes: dict[str, list[Advisory]], *, apply: bool = True
) -> tuple[int, MagicMock, list[str | None]]:
    write = MagicMock()
    orgs: list[str | None] = []
    current: list[str | None] = [None]

    @contextlib.contextmanager
    def target_org(org: str | None) -> Generator[None]:
        current.append(org)
        try:
            yield
        finally:
            current.pop()

    def list_advisories(home: str) -> list[Advisory]:
        orgs.append(current[-1])
        return homes[home]

    monkeypatch.setattr(security_records, "security_issues", lambda _org: [])
    monkeypatch.setattr(github, "repo_exists", lambda *_a: True)
    monkeypatch.setattr(security_records, "advisory_home", lambda org: f"{org}/.github")
    monkeypatch.setattr(security_records, "list_advisories", list_advisories)
    monkeypatch.setattr(github, "target_org", target_org)
    monkeypatch.setattr(github, "write_json", write)
    return records_pass("org", apply=apply, prog="prog"), write, orgs


def _copy(**kw: Any) -> Advisory:
    return Advisory(
        COPY,
        "draft",
        "tool",
        kw.get("tracking", (FIX,)),
        external=False,
        moved_from=kw.get("moved_from", (GHSA,)),
    )


def test_sync_passes_a_moved_advisory_whose_copy_holds_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    homes = {HOME: [_moved()], FIX_HOME: [_copy()]}
    rc, write, orgs = _moved_pass(monkeypatch, homes)
    assert rc == 0
    write.assert_not_called()
    assert orgs == [None, "fix"]
    out = capsys.readouterr().out
    assert "0 advisory states synced, 0 broken pairs, 0 held, 0 errors." in out
    assert "- broken:" not in out


@pytest.mark.parametrize(
    ("copies", "problem"),
    [
        ([], "which does not exist"),
        ([_copy(moved_from=())], f"which has no Moved from: {GHSA} line"),
        ([_copy(tracking=())], "which tracks no issue"),
    ],
)
def test_sync_reports_moved_target_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    copies: list[Advisory],
    problem: str,
) -> None:
    homes = {HOME: [_moved()], FIX_HOME: copies}
    rc, write, _orgs = _moved_pass(monkeypatch, homes)
    assert rc == 1
    write.assert_not_called()
    out = capsys.readouterr().out
    assert "0 advisory states synced, 1 broken pair, 0 held, 0 errors." in out
    assert f"- broken: {GHSA}: moved to fix/.github {COPY}, {problem}" in out
    rc, _write, _orgs = _moved_pass(monkeypatch, homes, apply=False)
    assert rc == 0


def test_sync_reads_each_moved_to_home_once(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _moved(OTHER, to=(("fix", SECOND),))
    second = Advisory(SECOND, "closed", "tool", (FIX2,), external=False, moved_from=(OTHER,))
    homes = {HOME: [_moved(), other], FIX_HOME: [_copy(), second]}
    rc, _write, orgs = _moved_pass(monkeypatch, homes)
    assert rc == 0
    assert orgs == [None, "fix"]


def test_sync_moved_to_home_read_failure_fails_the_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Homes(dict[str, list[Advisory]]):
        def __missing__(self, home: str) -> list[Advisory]:
            raise github.GitHubAPIError(1, ("gh", "api", home), None, "HTTP 403")

    rc, write, _orgs = _moved_pass(monkeypatch, Homes({HOME: [_moved()]}), apply=False)
    assert rc == 1
    write.assert_not_called()
    err = capsys.readouterr().err
    assert "prog: security records pass failed" in err
    assert "HTTP 403" in err


def test_note_refuses_a_move_line() -> None:
    for line in (f"Moved from: {GHSA}", f"Moved to: fix/.github {COPY}"):
        with pytest.raises(NoteError, match="would change where the record lives"):
            check_note(f"Some text.\n{line}\n")
    assert check_note(f"mid-line Moved from: {GHSA} mention") == (
        f"mid-line Moved from: {GHSA} mention"
    )


def test_refuse_move_lines_raises_the_given_error_without_quoting() -> None:
    class RefusedError(Exception):
        pass

    for line in (f"Moved from: {GHSA}", f"Moved to: fix/.github {COPY} \r"):
        with pytest.raises(RefusedError, match="^--details-file has a .Moved from:. or") as excinfo:
            security_records.refuse_move_lines(
                f"{SECRET}\n{line}\n", "--details-file", RefusedError
            )
        assert SECRET not in str(excinfo.value)
        assert "only retarget writes those lines" in str(excinfo.value)
    # A mention inside a line is not a move line.
    security_records.refuse_move_lines(
        f"see Moved from: {GHSA}\n> Moved to: fix/.github {COPY}", "x", RefusedError
    )


# --- trusted issue bodies only (epic vergil-project/.github#1467) -------------------


def _untrusted_body(issues: Issues, ref: IssueRef, *, by: str = "author") -> None:
    """Make *ref*'s body untrusted: an outsider wrote it, or last edited it."""
    if by == "author":
        issues.body_author[ref] = trust_stubs.OUTSIDER
    else:
        issues.body_editor[ref] = trust_stubs.OUTSIDER


@pytest.mark.parametrize("by", ["author", "editor"])
def test_mark_issue_refuses_an_untrusted_body_before_any_write(issues: Issues, by: str) -> None:
    issues.add(C, "Neutral body.")
    _untrusted_body(issues, C, by=by)
    with pytest.raises(trust.UntrustedContentError, match="stopped before any write") as excinfo:
        mark_issue(C, HOME, GHSA)
    assert "Neutral body." not in str(excinfo.value)
    assert "org/tool#12" in str(excinfo.value)
    assert issues.writes == []
    issues.ensure_labels.assert_not_called()
    assert issues.bodies[C] == "Neutral body."


def test_mark_issue_writes_back_the_trusted_body(issues: Issues) -> None:
    # The body appended to is the trust layer's copy; the REST read carries none.
    issues.add(C, "Trusted text.")
    mark_issue(C, HOME, GHSA)
    assert issues.trust_reads == [C]
    [(method, endpoint, body)] = [w for w in issues.writes if w[0] == "PATCH"]
    assert (method, endpoint) == ("PATCH", "repos/org/tool/issues/12")
    assert body == {"body": f"Trusted text.\n\n{advisory_line(HOME, GHSA)}"}


def test_unmark_issue_refuses_an_untrusted_body_before_any_write(issues: Issues) -> None:
    issues.add(A, f"Neutral.\n\n{advisory_line(HOME, GHSA)}", {"security"})
    _untrusted_body(issues, A, by="editor")
    with pytest.raises(trust.UntrustedContentError, match="stopped before any write"):
        unmark_issue(A, GHSA, (C,))
    assert issues.writes == []
    assert issues.comments[A] == []


def test_retarget_refuses_an_untrusted_target_before_any_write(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "New task.")
    _untrusted_body(issues, C)
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
    message = str(excinfo.value)
    assert message.startswith(f"advisory {GHSA}: stopped before any write: ")
    assert "org/tool#12 issue drive-by" in message
    assert "\n" not in message
    assert SECRET not in message
    assert issues.writes == []
    issues.ensure_labels.assert_not_called()


def test_retarget_refuses_an_untrusted_source_body_before_any_write(issues: Issues) -> None:
    # The source is the last issue unmarked; its body is checked before the
    # first target is marked, so the run never stops part-way.
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "New task.")
    _untrusted_body(issues, A, by="editor")
    with pytest.raises(RetargetRefusedError, match="stopped before any write"):
        retarget(HOME, GHSA, (C,), source=A)
    assert issues.writes == []


def test_retarget_refuses_an_untrusted_listed_security_issue(issues: Issues) -> None:
    # An untracked security issue whose body is untrusted may or may not name
    # the advisory; which issues to unmark cannot be known, so nothing moves.
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(B, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "New task.")
    _untrusted_body(issues, B)
    with pytest.raises(RetargetRefusedError) as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
    message = str(excinfo.value)
    assert "a security issue's body is untrusted" in message
    assert "org/.github#11 issue drive-by" in message
    assert issues.writes == []


def test_retarget_rerun_refuses_an_untrusted_source_tie(issues: Issues) -> None:
    # After a rewrite, a source is tied to the move by its body still naming
    # the advisory; an untrusted body ties nothing and stops the run.
    issues.description = f"{SECRET}\n\nTracking issue: org/tool#12"
    issues.add(A, advisory_line(HOME, GHSA))
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    _untrusted_body(issues, A)
    with pytest.raises(RetargetError, match="stopped before any write") as excinfo:
        retarget(HOME, GHSA, (C,), source=A)
    assert f"advisory {GHSA}: reading org/.github#10 failed: " in str(excinfo.value)
    assert issues.writes == []


def test_retarget_trust_read_failure_fails_before_any_write(issues: Issues) -> None:
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, "New task.")
    issues.fail_on = "GRAPHQL org/tool#12"
    with pytest.raises(RetargetError, match="reading org/tool#12 failed: cannot read who wrote"):
        retarget(HOME, GHSA, (C,), source=A)
    assert issues.writes == []


def test_move_refuses_an_untrusted_target_before_the_copy(move: Move) -> None:
    _untrusted_body(move.issues, FIX2)
    with pytest.raises(RetargetRefusedError, match="fix/tool#21 issue drive-by"):
        move.run()
    assert move.created == []
    assert move.issues.writes == []


def test_move_refuses_an_untrusted_listed_issue_before_the_copy(move: Move) -> None:
    move.issues.add(B, "Other.", {"security"})
    _untrusted_body(move.issues, B)
    with pytest.raises(RetargetRefusedError, match="a security issue's body is untrusted"):
        move.run()
    assert move.created == []
    assert move.issues.writes == []


def test_pair_refuses_an_untrusted_issue_before_any_write(issues: Issues) -> None:
    issues.description = SECRET
    issues.add(C, "Attached.")
    _untrusted_body(issues, C)
    with pytest.raises(RetargetError, match=r"marking org/tool#12 failed: stopped before any"):
        pair(HOME, GHSA, C)
    assert issues.writes == []


def test_close_refuses_an_untrusted_security_issue_before_any_write(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, advisory_line(HOME, GHSA), {"security"})
    issues.add(C, advisory_line(HOME, GHSA), {"security"})
    issues.closed |= {A, C}
    _untrusted_body(issues, C)
    with pytest.raises(CloseError) as excinfo:
        close_record(HOME, GHSA, REASON)
    message = str(excinfo.value)
    assert message.startswith(f"advisory {GHSA}: stopped before any write: ")
    assert "a security issue's body is untrusted" in message
    assert "org/tool#12 issue drive-by" in message
    assert issues.writes == []
    assert issues.states == {}


def test_close_comment_dedup_read_failure_names_its_step(
    issues: Issues, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tracked issue that lost its label is not listed: its one trust read
    # is the comment dedup's.
    _listing(monkeypatch, _adv(tracking=(A,)))
    issues.add(A, "")
    issues.closed.add(A)
    issues.fail_on = "GRAPHQL org/.github#10"
    with pytest.raises(CloseError) as excinfo:
        close_record(HOME, GHSA, REASON)
    assert str(excinfo.value).startswith(f"advisory {GHSA}: commenting on org/.github#10 failed: ")
    assert issues.writes == []


def test_classify_an_untrusted_issue_is_neither_paired_nor_broken() -> None:
    skip = trust.Skipped("org/tool", 12, "issue", "drive-by", "t", "body", "untrusted")
    untrusted = SecurityIssue(C, False, (), "completed", skipped=skip)
    result = classify(
        [_adv(tracking=(C,)), _adv(OTHER, tracking=(A,))], [untrusted, _iss(A, OTHER)]
    )
    assert [a.ghsa_id for a in result.untrusted] == [GHSA]
    assert [a.ghsa_id for a in result.paired] == [OTHER]
    assert result.broken == []


def test_classify_an_untrusted_issue_never_pairs_the_advisory_it_names() -> None:
    # Its lines are never read, so the advisory it would name has no issue
    # naming it back and is reported broken as before.
    skip = trust.Skipped("org/tool", 12, "issue", "drive-by", "t", "body", "untrusted")
    untrusted = SecurityIssue(C, True, (), None, skipped=skip)
    result = classify([_adv(tracking=(A,))], [untrusted])
    assert result.untrusted == []
    assert result.paired == []
    assert result.broken == [f"{GHSA}: no security issue names this advisory"]


@pytest.mark.parametrize("apply", [True, False])
def test_records_pass_reports_an_untrusted_body_and_leaves_it(
    monkeypatch: pytest.MonkeyPatch,
    parents: Parents,
    capsys: pytest.CaptureFixture[str],
    *,
    apply: bool,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    skip = trust.Skipped("org/tool", 12, "issue", "drive-by", "t", "body", "untrusted")
    issue_list = [
        SecurityIssue(C, False, (), "completed", skipped=skip),
        _iss(A, OTHER, open_=False),
    ]
    advisories = [_adv(tracking=(C,)), _adv(OTHER, tracking=(A,))]
    rc, write = _records_pass(monkeypatch, advisories, issue_list, apply=apply)
    assert rc == 1
    # The advisory tracking the untrusted issue is left draft; the other syncs.
    if apply:
        assert write.call_args_list == [_patched_state(OTHER, "closed")]
    else:
        write.assert_not_called()
    captured = capsys.readouterr()
    mode, verb = ("", "synced") if apply else (" (preview)", "would sync")
    assert (
        f"Security records{mode}: 1 advisory state {verb}, 0 broken pairs, 0 held, 0 errors."
        in captured.out
    )
    assert (
        f"- untrusted: {GHSA}: an issue it tracks has an untrusted body, left unchanged"
        in captured.out
    )
    assert "org/tool#12" not in captured.out
    assert "- broken:" not in captured.out
    assert captured.err == (
        "skipped untrusted content: org/tool#12 issue drive-by at t on body (untrusted)\n"
    )


def test_records_pass_under_actions_counts_untrusted_bodies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    skip = trust.Skipped("org/tool", 12, "issue", "drive-by", "t", "body", "untrusted")
    issue_list = [SecurityIssue(C, True, (), None, skipped=skip)]
    rc, _write = _records_pass(monkeypatch, [_adv(tracking=(C,))], issue_list, apply=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err == "skipped 1 untrusted item(s) (untrusted: 1)\n"
    assert "drive-by" not in captured.out + captured.err
    assert "org/tool#12" not in captured.out + captured.err


@pytest.mark.parametrize(
    "error",
    [
        trust.TrustReadError("read_issue org/tool#12", "the GraphQL call failed: HTTP 502"),
        security_records.TrustedAuthorsNotConfiguredError("trusted authors are not configured"),
    ],
)
def test_records_pass_trust_errors_are_one_line_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    monkeypatch.setattr(security_records, "security_issues", MagicMock(side_effect=error))
    assert records_pass("org", apply=True, prog="prog") == 1
    err = capsys.readouterr().err
    assert err == f"prog: security records pass failed: {error}\n"
