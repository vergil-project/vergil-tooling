"""Tests for vergil_tooling.lib.security_records (epic vergil-project/.github#517)."""

from __future__ import annotations

import json
import subprocess
import traceback
from dataclasses import fields
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest

from vergil_tooling.lib import epics, github, labels, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.security_records import (
    Advisory,
    Classification,
    PrivateHomeError,
    RetargetError,
    SecurityIssue,
    advisory_home,
    advisory_line,
    classify,
    create_advisory,
    find_duplicate,
    list_advisories,
    mark_issue,
    parse_advisory_ref,
    parse_tracking,
    read_text,
    retarget,
    security_issues,
    set_state,
    tracking_line,
    unmark_issue,
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


def test_parse_advisory_ref_reads_the_exact_line() -> None:
    body = f"Neutral text.\r\n\r\nSecurity advisory: org/.github {GHSA}  \r\n"
    assert parse_advisory_ref(body) == GHSA


@pytest.mark.parametrize(
    "body",
    [
        "",
        f"See Security advisory: org/.github {GHSA} for more",
        f"> Security advisory: org/.github {GHSA}",
        f"  Security advisory: org/.github {GHSA}",
        f"Security advisory: org/tool {GHSA}",
        f"Security advisory: org/.github {GHSA} and more",
        "Security advisory: org/.github GHSA-1234",
        f"security advisory: org/.github {GHSA}",
        f"Security advisory: org/.githubx {GHSA}",
    ],
)
def test_parse_advisory_ref_ignores_lookalike_text(body: str) -> None:
    assert parse_advisory_ref(body) is None


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
        self.description = f"{SECRET}\n\nTracking issue: org/.github#10"
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.fail_on: str | None = None
        self.ensure_labels = MagicMock()
        monkeypatch.setattr(labels, "ensure_labels", self.ensure_labels)
        monkeypatch.setattr(github, "read_json", self.read_json)
        monkeypatch.setattr(github, "write_json", self.write_json)
        monkeypatch.setattr(github, "delete", self.delete)
        monkeypatch.setattr(github, "read_json_paginated", self.read_json_paginated)

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

    def read_json(self, *args: str) -> Any:
        endpoint = args[1]
        self._maybe_fail(f"GET {endpoint}")
        if "security-advisories" in endpoint:
            return {"summary": SECRET, "description": self.description}
        ref = self._ref(endpoint)
        return {"labels": sorted(self.labels[ref]), "body": self.bodies[ref]}

    def write_json(self, method: str, endpoint: str, body: dict[str, object]) -> None:
        self._maybe_fail(f"{method} {endpoint}")
        self.writes.append((method, endpoint, body))
        if "security-advisories" in endpoint:
            self.description = str(body["description"])
            return
        ref = self._ref(endpoint)
        if endpoint.endswith("/labels"):
            self.labels[ref] |= {str(name) for name in cast("list[object]", body["labels"])}
        elif endpoint.endswith("/comments"):
            self.comments[ref].append(str(body["body"]))
        else:
            self.bodies[ref] = str(body["body"])

    def delete(self, endpoint: str) -> None:
        self._maybe_fail(f"DELETE {endpoint}")
        self.writes.append(("DELETE", endpoint, {}))
        self.labels[self._ref(endpoint)].discard(endpoint.rsplit("/", 1)[-1])

    def read_json_paginated(self, endpoint: str, key: str | None = None, **_: Any) -> list[object]:
        return list(self.comments[self._ref(endpoint)])

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


def test_mark_issue_on_an_empty_body(issues: Issues) -> None:
    issues.add(C, "")
    mark_issue(C, HOME, GHSA)
    assert issues.bodies[C] == f"Security advisory: org/.github {GHSA}"


def test_unmark_issue_removes_label_line_and_comments(issues: Issues) -> None:
    issues.add(A, f"Neutral.\n\nSecurity advisory: org/.github {GHSA}\n\nMore.", {"security"})
    unmark_issue(A, (B, C))
    assert issues.labels[A] == set()
    assert issues.bodies[A] == "Neutral.\n\nMore."
    assert issues.comments[A] == ["Security record moved to org/.github#11, org/tool#12"]


def test_unmark_issue_is_idempotent(issues: Issues) -> None:
    issues.add(A, "Neutral.")
    issues.comments[A] = ["Security record moved to org/tool#12"]
    unmark_issue(A, (C,))
    assert issues.writes == []


def test_unmark_issue_requires_a_destination(issues: Issues) -> None:
    issues.add(A)
    with pytest.raises(ValueError, match="no issue the record moved to"):
        unmark_issue(A, ())


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


def test_retarget_requires_targets(issues: Issues) -> None:
    with pytest.raises(RetargetError, match=f"advisory {GHSA}: no target issues given"):
        retarget(HOME, GHSA, ())


@pytest.mark.parametrize(
    ("fail_on", "step"),
    [
        (f"GET repos/org/.github/security-advisories/{GHSA}", "reading the tracking lines"),
        (f"PATCH repos/org/.github/security-advisories/{GHSA}", "rewriting the tracking lines"),
        ("DELETE repos/org/.github/issues/10", "unmarking org/.github#10"),
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
        SecurityIssue(A, open=True, ghsa_id=GHSA),
        SecurityIssue(B, open=False, ghsa_id=None),
        SecurityIssue(C, open=True, ghsa_id=None),
    ]
    assert endpoints == [
        "repos/org/.github/issues?state=all&labels=security",
        "repos/org/tool/issues?state=all&labels=security",
    ]


# --- classification -------------------------------------------------------------


def _adv(
    ghsa: str = GHSA, tracking: tuple[IssueRef, ...] = (A,), state: str = "draft", **kw: Any
) -> Advisory:
    return Advisory(ghsa, state, "tool", tracking, external=kw.get("external", False))


def _iss(ref: IssueRef = A, ghsa: str | None = GHSA, *, open_: bool = True) -> SecurityIssue:
    return SecurityIssue(ref, open_, ghsa)


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

    def test_broken_lines_name_only_ids_and_refs(self) -> None:
        result = classify(
            [_adv(), _adv(OTHER, tracking=())],
            [_iss(B, None), _iss(C, "GHSA-9999-9999-9999")],
        )
        assert result.broken
        for line in result.broken:
            assert "\n" not in line
            assert line.startswith(("GHSA-", "org/"))


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
        ("PATCH", "repos/org/.github/issues/10"),
        ("DELETE", "repos/org/.github/issues/10/labels/security"),
        ("POST", "repos/org/.github/issues/10/comments"),
    ]
    assert call("org/tool", ["security"]) in issues.ensure_labels.call_args_list
