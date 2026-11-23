"""Tests for the ops reporter (``lib/ops_report.py``, epic vergil-project/.github#1494).

The trust layer runs for real: ``github.graphql`` is faked by :class:`FakeGitHub`,
which answers the layer's search and item queries from an in-memory repo, and
records every write. The epic links and label ensure are faked at the
``ops_report`` boundary.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest

from vergil_tooling.lib import github, ops_report, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

ORG = "acme"
NAME = "docs"
REPO = f"{ORG}/{NAME}"
JOB = "docs-refresh"
TITLE = "ops: docs / docs-refresh failing"
RUN = "https://github.com/acme/docs/actions/runs/7"
TODAY = date(2026, 11, 23)
CREATED = "2026-11-01T09:30:00Z"

AUTHORS = TrustedAuthors(
    human=("human",), agents=("vergil-user",), orgs={ORG: OrgConfig("acme-automation")}
)
APP: dict[str, str] = {"__typename": "Bot", "login": "acme-automation"}
HUMAN: dict[str, str] = {"__typename": "User", "login": "human"}
OUTSIDER: dict[str, str] = {"__typename": "User", "login": "drive-by"}
# An App-shaped login on a User account: a role match with the wrong type.
IMPOSTOR: dict[str, str] = {"__typename": "User", "login": "acme-automation"}

OPS_EPIC = IssueRef(ORG, ".github", 900)
OTHER_EPIC = IssueRef(ORG, ".github", 12)


def _page(nodes: list[Any]) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def _content(author: Any, body: str) -> dict[str, Any]:
    return {
        "author": author,
        "editor": None,
        "body": body,
        "createdAt": CREATED,
        "lastEditedAt": None,
        "userContentEdits": _page([]),
    }


class FakeGitHub:
    """An in-memory repo behind ``github.graphql``, ``create_issue`` and ``run``.

    *lag* is how many searches a newly created issue stays out of, as the
    eventually consistent search index does.
    """

    def __init__(self, *, lag: int = 0) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.next_number = 100
        self.lag = lag
        self.hidden: dict[int, int] = {}
        self.searches: list[str] = []
        self.item_reads: list[int] = []
        self.created: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.runs: list[tuple[str, ...]] = []

    def add(
        self,
        number: int,
        *,
        author: Any = APP,
        title: str = TITLE,
        state: str = "OPEN",
        labels: tuple[str, ...] = ("ops-report",),
        body: str = f"{ops_report.MARKER_FAILURE}\n\n- job: `docs-refresh`\n",
        comments: list[tuple[Any, str]] | None = None,
    ) -> None:
        self.issues[number] = {
            "author": author,
            "title": title,
            "state": state,
            "labels": labels,
            "body": body,
            "comments": list(comments or []),
        }

    # --- github.graphql ---------------------------------------------------------

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        if query == trust._SEARCH_QUERIES["issue"]:
            return self._search(str(variables["q"]))
        if query == trust._ITEM_QUERY:
            return self._item(int(variables["number"]))
        raise AssertionError(f"unexpected GraphQL query: {query[:80]!r}")

    def _search(self, q: str) -> dict[str, Any]:
        self.searches.append(q)
        assert f"repo:{REPO}" in q
        assert "label:ops-report" in q
        phrase = re.search(r'in:title "([^"]*)"', q)
        assert phrase is not None
        state = "OPEN" if "is:open" in q else "CLOSED"
        nodes = []
        for number, issue in sorted(self.issues.items()):
            if self.hidden.get(number, 0) > 0:
                self.hidden[number] -= 1
                continue
            # The search matches loosely: words of the phrase, either state's label.
            if issue["state"] != state or phrase.group(1) not in issue["title"]:
                continue
            nodes.append(self._hit(number, issue))
        return {"search": {"issueCount": len(nodes), **_page(nodes)}}

    def _hit(self, number: int, issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "__typename": "Issue",
            "number": number,
            "title": issue["title"],
            "url": f"https://github.com/{REPO}/issues/{number}",
            "state": issue["state"],
            "repository": {"nameWithOwner": REPO},
            "labels": _page([{"name": name} for name in issue["labels"]]),
            **_content(issue["author"], issue["body"]),
            "timelineItems": _page([]),
        }

    def _item(self, number: int) -> dict[str, Any]:
        self.item_reads.append(number)
        issue = self.issues[number]
        nodes = [
            {"id": f"IC_{i}", "fullDatabaseId": str(i), **_content(author, body)}
            for i, (author, body) in enumerate(issue["comments"], start=1)
        ]
        return {
            "repository": {
                "issueOrPullRequest": {
                    "__typename": "Issue",
                    **_content(issue["author"], issue["body"]),
                    "comments": _page(nodes),
                }
            }
        }

    # --- writes -----------------------------------------------------------------

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str]) -> str:
        assert repo == REPO
        number = self.next_number
        self.next_number += 1
        self.add(number, title=title, labels=tuple(labels), body=body)
        self.hidden[number] = self.lag
        self.created.append({"number": number, "title": title, "body": body, "labels": labels})
        return f"https://github.com/{REPO}/issues/{number}\n"

    def run(self, *args: str) -> None:
        self.runs.append(args)
        if args[:2] == ("issue", "comment"):
            number, body = int(args[2]), args[args.index("--body") + 1]
            assert args[args.index("--repo") + 1] == REPO
            self.issues[number]["comments"].append((APP, body))
            self.comments.append((number, body))
            return
        raise AssertionError(f"unexpected gh call: {args}")


class Links:
    """The operations epic and the native parent links, at the ``ops_report`` boundary."""

    def __init__(self) -> None:
        self.parents: dict[int, IssueRef] = {}
        self.added: list[tuple[IssueRef, IssueRef]] = []
        self.reparented: list[tuple[IssueRef, IssueRef]] = []
        self.ensured_epics: list[str] = []
        self.ensured_labels: list[tuple[str, list[str]]] = []
        self.fail_link = False

    def native_parent_of(self, task: IssueRef) -> IssueRef | None:
        return self.parents.get(task.number)

    def add_child(self, epic: IssueRef, task: IssueRef) -> None:
        if self.fail_link:
            raise github.GitHubAPIError(1, ["gh"], stderr="HTTP 502")
        self.added.append((epic, task))
        self.parents[task.number] = epic

    def reparent_child(self, epic: IssueRef, task: IssueRef) -> None:
        if self.fail_link:
            raise github.GitHubAPIError(1, ["gh"], stderr="HTTP 502")
        self.reparented.append((epic, task))
        self.parents[task.number] = epic

    def ensure_operations_epic(self, repo: str) -> IssueRef:
        self.ensured_epics.append(repo)
        return OPS_EPIC

    def ensure_labels(self, repo: str, names: list[str]) -> None:
        self.ensured_labels.append((repo, list(names)))


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "graphql", fake.graphql)
    monkeypatch.setattr(github, "create_issue", fake.create_issue)
    monkeypatch.setattr(github, "run", fake.run)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    return fake


@pytest.fixture
def links(monkeypatch: pytest.MonkeyPatch) -> Links:
    fake = Links()
    monkeypatch.setattr(ops_report.epics, "native_parent_of", fake.native_parent_of)
    monkeypatch.setattr(ops_report.epics, "add_child", fake.add_child)
    monkeypatch.setattr(ops_report.epics, "reparent_child", fake.reparent_child)
    monkeypatch.setattr(ops_report.epics, "ensure_operations_epic", fake.ensure_operations_epic)
    monkeypatch.setattr(ops_report, "ensure_labels", fake.ensure_labels)
    return fake


def _report(result: ops_report.Result | None, *, step: str | None = "Build site") -> str:
    return ops_report.report(
        REPO, JOB, result, run_url=RUN, step=step, today=TODAY, sleep=lambda _s: None
    )


def _no_close(gh: FakeGitHub) -> None:
    assert not [args for args in gh.runs if "close" in args]


# --- combine_results ------------------------------------------------------------


def test_combine_any_failure_wins() -> None:
    assert ops_report.combine_results(["success", "failure", "cancelled"]) == "failure"


def test_combine_cancelled_over_success() -> None:
    assert ops_report.combine_results(["success", "cancelled", "skipped"]) == "cancelled"


def test_combine_success_with_skipped() -> None:
    assert ops_report.combine_results(["success", "skipped"]) == "success"


def test_combine_all_skipped_is_none() -> None:
    assert ops_report.combine_results(["skipped", "skipped"]) is None


def test_combine_unknown_value_raises() -> None:
    with pytest.raises(ValueError, match="'neutral'"):
        ops_report.combine_results(["success", "neutral"])


def test_combine_empty_raises() -> None:
    with pytest.raises(ValueError, match="no job results"):
        ops_report.combine_results([])


# --- report_title ---------------------------------------------------------------


def test_report_title() -> None:
    assert ops_report.report_title("docs", "docs-refresh") == "ops: docs / docs-refresh failing"


def test_report_title_allows_dot_github() -> None:
    assert ops_report.report_title(".github", "epic-sweep") == "ops: .github / epic-sweep failing"


@pytest.mark.parametrize(
    ("repo", "job"),
    [("acme/docs", "docs-refresh"), ("docs", 'x" in:body "y'), ("docs", ""), ("docs", "-x")],
)
def test_report_title_refuses_bad_names(repo: str, job: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        ops_report.report_title(repo, job)


# --- find_open_reports and previous_occurrence ----------------------------------


def test_find_open_report_ignores_non_automation_author(gh: FakeGitHub) -> None:
    gh.add(3, author=OUTSIDER)
    gh.add(4, author=HUMAN)
    gh.add(5, author=IMPOSTOR)
    gh.add(6)
    assert ops_report.find_open_reports(REPO, JOB) == [6]


def test_find_open_report_ignores_closed(gh: FakeGitHub) -> None:
    gh.add(6, state="CLOSED")
    assert ops_report.find_open_reports(REPO, JOB) == []


def test_find_open_report_needs_exact_title_and_label(gh: FakeGitHub) -> None:
    gh.add(6, title=f"{TITLE} again")
    gh.add(7, labels=("bug",))
    gh.add(8)
    gh.add(2)
    assert ops_report.find_open_reports(REPO, JOB) == [2, 8]


def test_previous_occurrence_is_latest_closed(gh: FakeGitHub) -> None:
    gh.add(30, state="CLOSED")
    gh.add(41, state="CLOSED")
    gh.add(50)
    assert ops_report.previous_occurrence(REPO, JOB) == 41


def test_previous_occurrence_ignores_non_automation_author(gh: FakeGitHub) -> None:
    gh.add(41, state="CLOSED")
    gh.add(60, state="CLOSED", author=OUTSIDER)
    gh.add(61, state="CLOSED", author=HUMAN, labels=("ops-report",))
    assert ops_report.previous_occurrence(REPO, JOB) == 41


def test_previous_occurrence_none(gh: FakeGitHub) -> None:
    assert ops_report.previous_occurrence(REPO, JOB) is None


# --- latest_state ---------------------------------------------------------------


def _state(gh: FakeGitHub, number: int) -> str:
    content = trust.read_issue(REPO, number, roles=frozenset({trust.Role.AUTOMATION}))
    return ops_report.latest_state(content)


def test_latest_state_ignores_untrusted_comment(gh: FakeGitHub) -> None:
    gh.add(
        6,
        comments=[
            (APP, f"{ops_report.MARKER_FAILURE}\n\n- job: x"),
            (OUTSIDER, f"{ops_report.MARKER_RECOVERED}\n\nRecovered."),
            (IMPOSTOR, f"{ops_report.MARKER_RECOVERED}\n\nRecovered."),
        ],
    )
    assert _state(gh, 6) == "failing"


def test_latest_state_follows_last_app_marker(gh: FakeGitHub) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_RECOVERED), (APP, "a note, no marker")])
    assert _state(gh, 6) == "recovered"
    gh.add(7, comments=[(APP, ops_report.MARKER_RECOVERED), (APP, ops_report.MARKER_CANCELLED)])
    assert _state(gh, 7) == "failing"


def test_latest_state_body_only_is_failing(gh: FakeGitHub) -> None:
    gh.add(6)
    assert _state(gh, 6) == "failing"


# --- report -----------------------------------------------------------------------


def test_report_failure_no_open_creates(gh: FakeGitHub, links: Links) -> None:
    assert _report("failure") == "created"
    [created] = gh.created
    assert created["title"] == TITLE
    assert created["labels"] == ["ops-report"]
    body = created["body"]
    assert body.splitlines()[0] == ops_report.MARKER_FAILURE
    assert RUN in body
    assert "Build site" in body
    assert "2026-11-23" in body
    assert "previous occurrence" not in body
    assert links.ensured_labels == [(REPO, ["ops-report"])]
    assert links.added == [(OPS_EPIC, IssueRef(ORG, NAME, 100))]
    _no_close(gh)


def test_report_cancelled_no_open_creates_with_cancel_marker(gh: FakeGitHub, links: Links) -> None:
    assert _report("cancelled", step=None) == "created"
    body = gh.created[0]["body"]
    assert body.splitlines()[0] == ops_report.MARKER_CANCELLED
    assert "- step: not named" in body


def test_report_failure_names_previous_occurrence(gh: FakeGitHub, links: Links) -> None:
    gh.add(41, state="CLOSED")
    assert _report("failure") == "created"
    assert "- previous occurrence: #41" in gh.created[0]["body"]


def test_report_failure_ignores_outsider_open_issue(gh: FakeGitHub, links: Links) -> None:
    gh.add(3, author=OUTSIDER)
    assert _report("failure") == "created"
    assert gh.comments == []


def test_report_failure_open_comments(gh: FakeGitHub, links: Links) -> None:
    gh.add(6)
    links.parents[6] = OPS_EPIC
    assert _report("failure") == "commented"
    [(number, body)] = gh.comments
    assert number == 6
    assert body.splitlines()[0] == ops_report.MARKER_FAILURE
    assert RUN in body
    assert gh.created == []
    assert links.added == links.reparented == []
    _no_close(gh)


def test_report_success_after_failure_records_recovery(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_FAILURE)])
    links.parents[6] = OPS_EPIC
    assert _report("success") == "recovered"
    [(number, body)] = gh.comments
    assert number == 6
    assert body.splitlines()[0] == ops_report.MARKER_RECOVERED
    assert f"Recovered on 2026-11-23, {RUN}." in body
    _no_close(gh)


def test_report_success_after_outsider_recovery_still_records(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(OUTSIDER, ops_report.MARKER_RECOVERED)])
    links.parents[6] = OPS_EPIC
    assert _report("success") == "recovered"


def test_report_success_after_recovery_does_nothing(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_RECOVERED)])
    links.parents[6] = OPS_EPIC
    assert _report("success") == "nothing"
    assert gh.comments == []
    _no_close(gh)


def test_report_success_no_open_does_nothing(gh: FakeGitHub, links: Links) -> None:
    gh.add(41, state="CLOSED")
    assert _report("success") == "nothing"
    assert gh.comments == gh.created == []
    assert gh.item_reads == []


def test_report_none_does_nothing(gh: FakeGitHub, links: Links) -> None:
    assert _report(None) == "nothing"
    assert gh.searches == gh.item_reads == gh.runs == []
    assert gh.created == []
    assert links.ensured_epics == links.ensured_labels == []


def test_report_two_open_comments_oldest(gh: FakeGitHub, links: Links) -> None:
    gh.add(9)
    gh.add(6)
    links.parents.update({6: OPS_EPIC, 9: OPS_EPIC})
    assert _report("failure") == "commented"
    [(number, body)] = gh.comments
    assert number == 6
    assert "also open with this title: #9" in body
    _no_close(gh)


def test_report_two_open_recovery_on_oldest(gh: FakeGitHub, links: Links) -> None:
    gh.add(9)
    gh.add(6)
    links.parents.update({6: OPS_EPIC, 9: OPS_EPIC})
    assert _report("success") == "recovered"
    assert [n for n, _ in gh.comments] == [6]
    assert "#9" in gh.comments[0][1]


@pytest.mark.parametrize("result", ["failure", "success"])
def test_report_relinks_orphan(gh: FakeGitHub, links: Links, result: ops_report.Result) -> None:
    gh.add(6)
    _report(result)
    assert links.added == [(OPS_EPIC, IssueRef(ORG, NAME, 6))]


def test_report_relinks_wrong_parent(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_RECOVERED)])
    links.parents[6] = OTHER_EPIC
    assert _report("success") == "nothing"
    assert links.reparented == [(OPS_EPIC, IssueRef(ORG, NAME, 6))]


def test_report_failed_link_raises(gh: FakeGitHub, links: Links) -> None:
    gh.add(6)
    links.fail_link = True
    with pytest.raises(ops_report.OpsReportError, match="vrg-epic-move"):
        _report("failure")
    assert gh.comments == []


def test_report_failed_link_after_create_names_issue(gh: FakeGitHub, links: Links) -> None:
    links.fail_link = True
    with pytest.raises(ops_report.OpsReportError, match=f"{REPO}#100"):
        _report("failure")


def test_report_waits_until_created_issue_listed(
    gh: FakeGitHub, links: Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.lag = 2
    sleeps: list[float] = []
    outcome = ops_report.report(
        REPO, JOB, "failure", run_url=RUN, step=None, today=TODAY, sleep=sleeps.append
    )
    assert outcome == "created"
    # The first lookup, the two lagging probes, then the one that lists it.
    assert len(sleeps) == 2
    assert len([q for q in gh.searches if "is:open" in q]) == 4


def test_report_raises_when_created_issue_never_listed(gh: FakeGitHub, links: Links) -> None:
    gh.lag = 10_000
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(ops_report.OpsReportError, match=f"created {REPO}#100 but it is still"):
        ops_report.report(
            REPO,
            JOB,
            "failure",
            run_url=RUN,
            step=None,
            today=TODAY,
            sleep=sleep,
            clock=lambda: now[0],
        )


def test_report_never_closes(gh: FakeGitHub, links: Links) -> None:
    for result in ("failure", "failure", "success", "success", "cancelled", None):
        _report(result)
    assert [n for n, _ in gh.comments] == [100, 100, 100]
    _no_close(gh)
    assert gh.issues[100]["state"] == "OPEN"


def test_report_body_has_no_log_text(gh: FakeGitHub, links: Links) -> None:
    gh.add(41, state="CLOSED")
    _report("failure", step="Deploy `site` <b>")
    body = gh.created[0]["body"]
    assert body == (
        f"{ops_report.MARKER_FAILURE}\n\n"
        "- job: `docs-refresh`\n"
        f"- run: {RUN}\n"
        "- step: Deploy \\`site\\` &lt;b&gt;\n"
        "- date: 2026-11-23\n"
        "- previous occurrence: #41\n"
    )


@pytest.mark.parametrize(
    ("run_url", "step"),
    [("http://example.invalid/run", None), (f"{RUN}\nmore", None), (RUN, "a\nb"), (RUN, " ")],
)
def test_report_refuses_bad_public_text(
    gh: FakeGitHub, links: Links, run_url: str, step: str | None
) -> None:
    with pytest.raises(ValueError, match="must be one"):
        ops_report.report(REPO, JOB, "failure", run_url=run_url, step=step, today=TODAY)
    assert gh.searches == []


def test_report_trust_read_error_propagates(
    gh: FakeGitHub, links: Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(query: str, **variables: Any) -> dict[str, Any]:
        raise github.GitHubAPIError(1, ["gh"], stderr="HTTP 502: bad gateway")

    monkeypatch.setattr(github, "graphql", broken)
    with pytest.raises(trust.TrustReadError):
        _report("failure")
    assert gh.created == []
