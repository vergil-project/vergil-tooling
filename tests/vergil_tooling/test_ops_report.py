"""Tests for the ops reporter (``lib/ops_report.py``, epic vergil-project/.github#1494).

The trust layer runs for real: ``github.graphql`` is faked by :class:`FakeGitHub`,
which answers the layer's search and item queries from an in-memory repo, and
records every write. The sub-issue links go through the chain layer
(:mod:`vergil_tooling.lib.epic_chain`) for real, against the
:class:`FakeSubIssueGraph` :class:`FakeGitHub` routes every other query to; the
tests assert the graph's end state. The operations epic's lookup and the label
ensure are faked at the ``ops_report`` boundary.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin import vrg_ops_report
from vergil_tooling.lib import epic_chain, epics, github, identity_mode, ops_report, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

ORG = "acme"
NAME = "docs"
REPO = f"{ORG}/{NAME}"
JOB = "docs-refresh"
TITLE = "ops: docs / docs-refresh failing"
RUN = "https://github.com/acme/docs/actions/runs/7"
TODAY = date(2026, 11, 23)
CREATED = "2026-11-01T09:30:00Z"
EDITED = "2026-11-02T10:00:00Z"

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

_LINK_MUTATIONS = (epics._ADD_SUBISSUE, epics._REPARENT_SUBISSUE)


def _ref(number: int) -> IssueRef:
    return IssueRef(ORG, NAME, number)


def _page(nodes: list[Any]) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def _content(author: Any, body: str, editor: Any = None) -> dict[str, Any]:
    return {
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": CREATED,
        "lastEditedAt": None if editor is None else EDITED,
        "userContentEdits": _page([]),
    }


class FakeGitHub:
    """An in-memory repo behind ``github.graphql``, ``create_issue`` and ``run``.

    *lag* is how many searches a newly created issue stays out of, as the
    eventually consistent search index does. Every issue added is also added
    to :attr:`graph`, which answers the sub-issue queries and the REST reads
    behind them; with :attr:`fail_link` set, every link mutation fails.
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
        self.graph = FakeSubIssueGraph()
        self.fail_link = False

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
        editor: Any = None,
        renamer: Any = None,
    ) -> None:
        self.issues[number] = {
            "author": author,
            "editor": editor,
            "renamer": renamer,
            "title": title,
            "state": state,
            "labels": labels,
            "body": body,
            "comments": list(comments or []),
        }
        self.graph.add_issue(_ref(number), title=title, labels=labels, body=body, state=state)

    # --- github.graphql ---------------------------------------------------------

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        if query == trust._SEARCH_QUERIES["issue"]:
            return self._search(str(variables["q"]))
        if query == trust._ITEM_QUERY and (variables["owner"], variables["name"]) == (ORG, NAME):
            return self._item(int(variables["number"]))
        if self.fail_link and query in _LINK_MUTATIONS:
            raise github.GitHubAPIError(1, ["gh"], stderr="HTTP 502")
        # The sub-issue reads and writes, and the trust reads of the epics.
        return self.graph.graphql(query, **variables)

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
            **_content(issue["author"], issue["body"], issue["editor"]),
            "timelineItems": _page(
                []
                if issue["renamer"] is None
                else [
                    {
                        "__typename": "RenamedTitleEvent",
                        "actor": issue["renamer"],
                        "createdAt": EDITED,
                    }
                ]
            ),
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
                    **_content(issue["author"], issue["body"], issue["editor"]),
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
    """The operations epic's lookup and the label ensure, at the ``ops_report`` boundary.

    The links themselves are :attr:`graph`'s, which holds the operations epic
    and another epic.
    """

    def __init__(self, graph: FakeSubIssueGraph) -> None:
        self.graph = graph
        self.ensured_epics: list[str] = []
        self.ensured_labels: list[tuple[str, list[str]]] = []
        graph.add_issue(OPS_EPIC, title="Operations", labels=["epic"])
        graph.add_issue(OTHER_EPIC, title="Epic: other", labels=["epic"])

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
    monkeypatch.setattr(github, "read_output", fake.graph.read_output)
    monkeypatch.setattr(github, "read_json", fake.graph.read_json)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(ops_report, "load_trusted_authors", lambda: AUTHORS)
    return fake


@pytest.fixture
def links(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> Links:
    fake = Links(gh.graph)
    monkeypatch.setattr(ops_report.epics, "ensure_operations_epic", fake.ensure_operations_epic)
    monkeypatch.setattr(ops_report, "ensure_labels", fake.ensure_labels)
    return fake


def _report(result: ops_report.Result | None, *, step: str | None = "Build site") -> str:
    return ops_report.report(
        REPO, JOB, result, run_url=RUN, step=step, today=TODAY, sleep=lambda _s: None
    )


def _no_close(gh: FakeGitHub) -> None:
    assert not [args for args in gh.runs if "close" in args]


def _start_under(gh: FakeGitHub, epic: IssueRef, *numbers: int) -> None:
    """Link each ops issue *numbers* under *epic* as the starting state, logging nothing."""
    for number in numbers:
        gh.graph.link(epic, _ref(number))
    gh.graph.clear_log()


def _assert_linked(gh: FakeGitHub, container: IssueRef, number: int, *, op: str) -> None:
    """Ops issue *number* has one parent, *container*, written by the one *op* mutation."""
    issue = _ref(number)
    assert gh.graph.mutations == [(op, container, issue)]
    assert gh.graph.parent_of(issue) == container
    assert [ref for ref in gh.graph.issues if issue in gh.graph.children_of(ref)] == [container]


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


def test_skipped_open_reports_names_app_issue_edited_by_another(gh: FakeGitHub) -> None:
    gh.add(6, editor=HUMAN)
    gh.add(7, editor=OUTSIDER)
    gh.add(8, renamer=OUTSIDER)
    gh.add(9)
    gh.add(10, editor=APP)
    assert ops_report.skipped_open_reports(REPO, JOB) == [6, 7, 8]
    assert ops_report.find_open_reports(REPO, JOB) == [9, 10]


def test_skipped_open_reports_never_names_outsider_issue(gh: FakeGitHub) -> None:
    gh.add(3, author=OUTSIDER)
    gh.add(4, author=OUTSIDER, editor=OUTSIDER)
    gh.add(5, author=IMPOSTOR, editor=OUTSIDER)
    gh.add(11, author=HUMAN, editor=OUTSIDER)
    assert ops_report.skipped_open_reports(REPO, JOB) == []


def test_skipped_open_reports_ignores_closed(gh: FakeGitHub) -> None:
    gh.add(6, state="CLOSED", editor=OUTSIDER)
    assert ops_report.skipped_open_reports(REPO, JOB) == []


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


def test_latest_state_ignores_untrusted_body(gh: FakeGitHub) -> None:
    gh.add(6, author=OUTSIDER, body=ops_report.MARKER_RECOVERED)
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
    _assert_linked(gh, OPS_EPIC, 100, op="add")
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


SECRET = "edited text that must never be copied"


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_report_create_names_skipped_edited_issue_by_number_only(
    gh: FakeGitHub, links: Links, result: ops_report.Result
) -> None:
    gh.add(6, editor=OUTSIDER, body=f"{ops_report.MARKER_RECOVERED}\n\n{SECRET}")
    gh.add(7, renamer=OUTSIDER)
    gh.add(41, state="CLOSED")
    assert _report(result) == "created"
    [created] = gh.created
    body = created["body"]
    assert "- skipped (edited by someone other than the App): #6\n" in body
    assert "- skipped (edited by someone other than the App): #7\n" in body
    assert body.endswith("- previous occurrence: #41\n")
    assert SECRET not in body
    assert ops_report.MARKER_RECOVERED not in body
    # The edited issues stay as they were: no comment, no close, no read of their text.
    assert gh.comments == []
    assert gh.item_reads == []
    assert gh.issues[6]["state"] == gh.issues[7]["state"] == "OPEN"
    _no_close(gh)


def test_report_create_never_names_outsider_issue(gh: FakeGitHub, links: Links) -> None:
    gh.add(3, author=OUTSIDER, editor=OUTSIDER, body=SECRET)
    gh.add(4, author=HUMAN, editor=OUTSIDER)
    assert _report("failure") == "created"
    body = gh.created[0]["body"]
    assert "skipped" not in body
    assert "#3" not in body
    assert "#4" not in body
    assert SECRET not in body


def test_report_comment_does_not_name_skipped_issue(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, editor=OUTSIDER)
    gh.add(9)
    _start_under(gh, OPS_EPIC, 9)
    assert _report("failure") == "commented"
    [(number, body)] = gh.comments
    assert number == 9
    assert "skipped" not in body


def test_report_failure_open_comments(gh: FakeGitHub, links: Links) -> None:
    gh.add(6)
    _start_under(gh, OPS_EPIC, 6)
    assert _report("failure") == "commented"
    [(number, body)] = gh.comments
    assert number == 6
    assert body.splitlines()[0] == ops_report.MARKER_FAILURE
    assert RUN in body
    assert gh.created == []
    assert gh.graph.mutations == []
    assert gh.graph.parent_of(_ref(6)) == OPS_EPIC
    _no_close(gh)


def test_report_success_after_failure_records_recovery(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_FAILURE)])
    _start_under(gh, OPS_EPIC, 6)
    assert _report("success") == "recovered"
    [(number, body)] = gh.comments
    assert number == 6
    assert body.splitlines()[0] == ops_report.MARKER_RECOVERED
    assert f"Recovered on 2026-11-23, {RUN}." in body
    _no_close(gh)


def test_report_success_after_outsider_recovery_still_records(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(OUTSIDER, ops_report.MARKER_RECOVERED)])
    _start_under(gh, OPS_EPIC, 6)
    assert _report("success") == "recovered"


def test_report_success_after_recovery_does_nothing(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_RECOVERED)])
    _start_under(gh, OPS_EPIC, 6)
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
    assert gh.graph.mutations == []


def test_report_two_open_comments_oldest(gh: FakeGitHub, links: Links) -> None:
    gh.add(9)
    gh.add(6)
    _start_under(gh, OPS_EPIC, 6, 9)
    assert _report("failure") == "commented"
    [(number, body)] = gh.comments
    assert number == 6
    assert "also open with this title: #9" in body
    _no_close(gh)


def test_report_two_open_recovery_on_oldest(gh: FakeGitHub, links: Links) -> None:
    gh.add(9)
    gh.add(6)
    _start_under(gh, OPS_EPIC, 6, 9)
    assert _report("success") == "recovered"
    assert [n for n, _ in gh.comments] == [6]
    assert "#9" in gh.comments[0][1]


@pytest.mark.parametrize("result", ["failure", "success"])
def test_report_relinks_orphan(gh: FakeGitHub, links: Links, result: ops_report.Result) -> None:
    gh.add(6)
    _report(result)
    _assert_linked(gh, OPS_EPIC, 6, op="add")


def test_report_relinks_wrong_parent(gh: FakeGitHub, links: Links) -> None:
    gh.add(6, comments=[(APP, ops_report.MARKER_RECOVERED)])
    _start_under(gh, OTHER_EPIC, 6)
    assert _report("success") == "nothing"
    _assert_linked(gh, OPS_EPIC, 6, op="reparent")


def test_report_failed_link_raises(gh: FakeGitHub, links: Links) -> None:
    gh.add(6)
    gh.fail_link = True
    with pytest.raises(ops_report.OpsReportError, match="vrg-epic-move"):
        _report("failure")
    assert gh.comments == []
    assert gh.graph.mutations == []
    assert gh.graph.parent_of(_ref(6)) is None


def test_report_failed_link_after_create_names_issue(gh: FakeGitHub, links: Links) -> None:
    gh.fail_link = True
    with pytest.raises(ops_report.OpsReportError, match=f"{REPO}#100"):
        _report("failure")
    assert gh.graph.parent_of(_ref(100)) is None


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


@pytest.mark.parametrize("repo", ["docs", "acme/", "/docs", "acme/docs/x"])
def test_report_refuses_bad_repo(gh: FakeGitHub, links: Links, repo: str) -> None:
    with pytest.raises(ValueError, match="OWNER/NAME"):
        ops_report.report(repo, JOB, "failure", run_url=RUN, step=None, today=TODAY)
    assert gh.searches == []


def test_report_refuses_unknown_result(gh: FakeGitHub, links: Links) -> None:
    skipped: Any = "skipped"
    with pytest.raises(ValueError, match="unknown result 'skipped'"):
        ops_report.report(REPO, JOB, skipped, run_url=RUN, step=None, today=TODAY)
    assert gh.searches == []


def test_report_create_with_unreadable_url_raises(
    gh: FakeGitHub, links: Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "create_issue", lambda **_kwargs: "not a url")
    with pytest.raises(ops_report.OpsReportError, match="cannot read its number"):
        _report("failure")


def test_report_failed_listing_check_names_created_issue(
    gh: FakeGitHub, links: Links, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gh.graphql

    def fail_after_create(query: str, **variables: Any) -> dict[str, Any]:
        if gh.created and query == trust._SEARCH_QUERIES["issue"]:
            raise github.GitHubAPIError(1, ["gh"], stderr="HTTP 502: bad gateway")
        return real(query, **variables)

    monkeypatch.setattr(github, "graphql", fail_after_create)
    with pytest.raises(ops_report.OpsReportError, match=f"created {REPO}#100 but checking"):
        _report("failure")


# --- the chain layer (epic vergil-project/.github#1609) ---------------------------

OPS_CONTINUATION = IssueRef(ORG, ".github", 950)


def _fill(gh: FakeGitHub, epic: IssueRef, count: int) -> None:
    """Link *count* plain items under *epic*, in the epic's repo, numbered from 1000."""
    for number in range(1000, 1000 + count):
        item = gh.graph.add_issue(IssueRef(epic.owner, epic.repo, number), title="item")
        gh.graph.link(epic, item)


@pytest.mark.parametrize("case", ["create", "move", "already"])
def test_ops_report_links_through_chain(
    gh: FakeGitHub, links: Links, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """An ops issue goes into the operations epic's chain; one already in it stays put."""
    # The operations epic is full: 99 items and its continuation, which has room.
    _fill(gh, OPS_EPIC, 99)
    gh.graph.add_continuation(OPS_CONTINUATION, OPS_EPIC, OPS_EPIC)
    if case == "create":
        gh.graph.clear_log()
        assert _report("failure") == "created"
        _assert_linked(gh, OPS_CONTINUATION, 100, op="add")
        assert epics.native_parent_of(_ref(100)) == OPS_EPIC
    elif case == "move":
        # An ops issue under another epic moves into the chain in one re-parent.
        gh.add(6)
        _start_under(gh, OTHER_EPIC, 6)
        assert _report("failure") == "commented"
        _assert_linked(gh, OPS_CONTINUATION, 6, op="reparent")
        assert gh.graph.children_of(OTHER_EPIC) == []
    else:
        # One already linked in the continuation is under the operations epic.
        gh.add(6)
        _start_under(gh, OPS_CONTINUATION, 6)
        assert _report("failure") == "commented"
        assert gh.graph.mutations == []
        assert gh.graph.parent_of(_ref(6)) == OPS_CONTINUATION
    assert epic_chain.chain_of(OPS_EPIC) == [OPS_EPIC, OPS_CONTINUATION]
    _no_close(gh)


def _cli_report(monkeypatch: pytest.MonkeyPatch) -> int:
    """``vrg-ops-report`` for a failure of JOB in REPO, as the automation App."""
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.AUTOMATION)
    return vrg_ops_report.main(
        ["--repo", REPO, "--job", JOB, "--run-url", RUN, "--results", "failure"]
    )


@pytest.mark.parametrize("refusal", ["depth", "closed"])
def test_ops_report_refusal_creates_nothing(
    gh: FakeGitHub,
    links: Links,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
) -> None:
    """A refusal of the chain layer's capacity check is raised before any issue is created."""
    error: epics.EpicRefusalError
    if refusal == "depth":
        error = epic_chain.ChainDepthError(OPS_EPIC, items=700, containers=7)

        def refuse(head: IssueRef) -> None:
            assert head == OPS_EPIC
            raise error

        monkeypatch.setattr(epic_chain, "check_capacity", refuse)
    else:
        gh.graph.close(OPS_EPIC)
        error = epics.ClosedEpicError(OPS_EPIC)
    gh.graph.clear_log()

    with pytest.raises(type(error)) as excinfo:
        _report("failure")
    assert str(excinfo.value) == str(error)
    assert gh.created == []

    assert _cli_report(monkeypatch) == 1
    assert capsys.readouterr().err == f"vrg-ops-report: {REPO} / {JOB}: {error}\n"
    assert gh.created == []
    assert gh.comments == []
    assert gh.graph.mutations == []
