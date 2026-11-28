"""Tests for the merge-review request builder, ``vrg-merge-review-request``."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin import vrg_merge_review_request
from vergil_tooling.lib import epic_audit, epic_chain, epics, github, proxies, trust
from vergil_tooling.lib.epics import ChildState, IssueRef, parse_issue_ref
from vergil_tooling.lib.github import GitHubAPIError, MissingGitHubTokenError
from vergil_tooling.lib.merge_review import open_pr
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.result import parse_result
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.progress import StageResult
from vergil_tooling.lib.proxies import ProxyLinkError
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.validate_record import write_record

Cap = pytest.CaptureFixture[str]

ISSUE = "acme/widget#42"
ISSUE_REPO_URL = "https://github.com/acme/widget.git"
HUMAN = "maintainer"
AGENT = f"{HUMAN}-vergil-user"

_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]

[merge-review]
docs-paths = ["docs/guide/"]
"""

_STAGES = [StageResult("common", "ok"), StageResult("lint", "ok"), StageResult("test", "ok")]


# -- a real repository ------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _record(repo: Path, **over: Any) -> None:
    fields: dict[str, Any] = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "exit_code": 0,
        "full": True,
        "clean_at_start": True,
        "results": _STAGES,
    }
    fields.update(over)
    write_record(repo, **fields)


@pytest.fixture
def graphql_calls() -> list[dict[str, object]]:
    """The variables of every item and comment-page query the fake received, in order."""
    return []


@pytest.fixture
def task_calls() -> list[tuple[str, str]]:
    """Every parent or sub-issue read of the sub-issue graph, ``(kind, issue slug)``, in order.

    *kind* is ``task`` for a parent read and ``subs`` for a container's
    sub-issue read (:func:`_graph_kind`).
    """
    return []


EPIC = "acme/.github#7"
EPIC_REF = parse_issue_ref(EPIC, default_repo="")
ISSUE_REF = parse_issue_ref(ISSUE, default_repo="")
NEXT = IssueRef("acme", "widget", 43)
DONE = IssueRef("acme", "widget", 44)


@pytest.fixture
def graph() -> FakeSubIssueGraph:
    """The task's sub-issue graph, read through ``epics`` and the chain layer.

    ``EPIC`` (labelled ``epic``) holds ``acme/widget#43`` (open), the task
    and ``acme/widget#44`` (closed), in that order. The graph answers the
    parent and sub-issue reads, node ids and labels; the trust layer's reads
    (bodies, a continuation's included) stay with :func:`comment_pages`.
    """
    fake = FakeSubIssueGraph()
    fake.add_issue(EPIC_REF, title="Epic: e", labels=["epic"])
    fake.add_issue(NEXT, title="The next task")
    fake.add_issue(ISSUE_REF, title="This task")
    fake.add_issue(DONE, title="A done task", state="CLOSED")
    for ref in (NEXT, ISSUE_REF, DONE):
        fake.link(EPIC_REF, ref)
    return fake


def _graph_kind(query: str) -> str:
    """``task`` for a parent read of the graph, ``subs`` for a sub-issue read."""
    return "task" if query in (epics._PARENT_QUERY, epics._PARENT_WITH_LABELS_QUERY) else "subs"


def _graph_number(variables: dict[str, object]) -> object:
    """The issue number a graph read is for: its node id is ``I_<owner>/<repo>#<N>``."""
    node_id = variables.get("id")
    return int(node_id.rsplit("#", 1)[1]) if isinstance(node_id, str) else None


def _move(graph: FakeSubIssueGraph, task: IssueRef, parent: IssueRef | None) -> None:
    """Move *task* from wherever it is linked to *parent* (unlinked for None)."""
    current = graph.parent_of(task)
    if current is not None:
        graph._remove(current, task)
    if parent is not None:
        graph.link(parent, task)


TASK_BODY = "Do the thing.\n"
_CREATED = "2026-11-01T09:30:00Z"
_EDITED = "2026-11-03T00:00:00Z"

# The actors of the fake's content, in the ``{ __typename login }`` shape.
HUMAN_USER: dict[str, str] = {"__typename": "User", "login": HUMAN}
AGENT_BOT: dict[str, str] = {"__typename": "Bot", "login": AGENT}
AUDIT_BOT: dict[str, str] = {"__typename": "Bot", "login": f"{HUMAN}-vergil-audit"}
OUTSIDER = trust_stubs.OUTSIDER
# A User account named like the agent App, and a Bot carrying the human's login.
IMPOSTOR_USER: dict[str, str] = {"__typename": "User", "login": AGENT}
IMPOSTOR_BOT: dict[str, str] = {"__typename": "Bot", "login": HUMAN}
SPAM_TEXT = trust_stubs.SPAM_TEXT


def _edits(editor: Any) -> dict[str, Any]:
    """A ``userContentEdits`` connection: one edit by *editor*, or none for ``None``."""
    nodes = [] if editor is None else [{"editedAt": _EDITED, "editor": editor}]
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def _node(
    comment_id: int,
    author: Any = HUMAN_USER,
    body: str = "text",
    *,
    edited: bool = False,
    editor: Any = None,
    created: Any = _CREATED,
) -> dict[str, Any]:
    """An ``IssueComment`` node; *edited* is an edit by its own author, *editor* by another."""
    if edited and editor is None:
        editor = author
    return {
        "id": f"IC_{comment_id}",
        "fullDatabaseId": str(comment_id),
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": created,
        "lastEditedAt": None if editor is None else _EDITED,
        "userContentEdits": _edits(editor),
    }


def _page(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    """A ``comments`` connection page; a *cursor* says another page follows."""
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def _body_item(
    body: Any,
    comments: Any,
    *,
    author: Any = HUMAN_USER,
    editor: Any = None,
    kind: str = "Issue",
) -> dict[str, Any]:
    """The layer's item query answer: *body* by *author*, edited once by *editor* if given."""
    return {
        "repository": {
            "issueOrPullRequest": {
                "__typename": kind,
                "author": author,
                "editor": editor,
                "body": body,
                "createdAt": _CREATED,
                "lastEditedAt": None if editor is None else _EDITED,
                "userContentEdits": _edits(editor),
                "comments": comments,
            }
        }
    }


def _branch(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """The branch lookup's answer: one page of pull requests."""
    info = {"hasNextPage": False, "endCursor": None}
    return {"repository": {"pullRequests": {"pageInfo": info, "nodes": nodes}}}


@pytest.fixture
def task_pages() -> dict[str, Any]:
    """The bodies the trust layer's item query serves.

    The item query gives an issue numbered in ``bodies`` that body
    (``TASK_BODY`` otherwise), written by the human; an issue numbered in
    ``items`` gets that whole item answer. The task's epic and its children
    are the :func:`graph`'s.
    """
    return {"bodies": {}, "items": {}}


@pytest.fixture
def pr_pages() -> dict[str, Any]:
    """The PR fake: an :func:`_open_pr` answer under ``pr``, and every branch query's variables."""
    return {"pr": None, "calls": []}


@pytest.fixture
def other_comments() -> dict[int, list[Any]]:
    """Comment pages of issues other than the task, by issue number, chained by cursor."""
    return {}


def _comments_page(pages: list[Any], after: object) -> Any:
    """The comment page after the one whose cursor is *after* (the first for ``None``)."""
    if not pages:
        return _page([])
    if after is None:
        return pages[0]
    for i, page in enumerate(pages[:-1]):
        if page["pageInfo"]["endCursor"] == after:
            return pages[i + 1]
    pytest.fail(f"no comment page after cursor {after!r}")


@pytest.fixture
def comment_pages(
    monkeypatch: pytest.MonkeyPatch,
    graphql_calls: list[dict[str, object]],
    task_calls: list[tuple[str, str]],
    task_pages: dict[str, Any],
    pr_pages: dict[str, Any],
    other_comments: dict[int, list[Any]],
    graph: FakeSubIssueGraph,
) -> list[Any]:
    """The task issue's comment pages (each a :func:`_page`), chained by their cursors.

    The fake serves the trust layer's item query (the body and the first
    comment page; the builder reads it once for the body, once for the
    comments) and its comment-page query from *task_pages*, these pages and
    *other_comments*; the branch lookup and the PR's item from *pr_pages*;
    and the parent and sub-issue reads, node ids and labels from *graph*.
    It configures the trusted authors, for the builder and the layer alike,
    with one human login, ``HUMAN``.
    """
    pages: list[Any] = []
    branch_queries = set(trust._BRANCH_QUERIES.values())

    def pages_of(number: object) -> list[Any]:
        if isinstance(number, int) and number in other_comments:
            return other_comments[number]
        return pages

    def graphql(query: str, **variables: object) -> Any:
        pr = pr_pages["pr"]
        if query in branch_queries:
            pr_pages["calls"].append(variables)
            if pr is None:
                return _branch([])
            if "branch_answer" in pr:
                return pr["branch_answer"]
            # GitHub filters on headRefName: a PR on another branch is not listed.
            return _branch([n for n in pr["nodes"] if n["headRefName"] == variables["branch"]])
        number = variables.get("number")
        if query == trust._ITEM_QUERY:
            graphql_calls.append(variables)
            if pr is not None and number == PR_NUMBER and "item" in pr:
                return pr["item"]
            if number in task_pages["items"]:
                return task_pages["items"][number]
            body = task_pages["bodies"].get(number, TASK_BODY)
            return _body_item(body, _comments_page(pages_of(number), None))
        if query == trust._COMMENTS_QUERY:
            graphql_calls.append(variables)
            page = _comments_page(pages_of(number), variables["after"])
            return {"repository": {"issueOrPullRequest": {"comments": page}}}
        task_calls.append((_graph_kind(query), graph._by_node_id(variables["id"]).slug))
        return graph.graphql(query, **variables)

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", graphql)
    monkeypatch.setattr("vergil_tooling.lib.github.read_output", graph.read_output)
    monkeypatch.setattr("vergil_tooling.lib.github.read_json", graph.read_json)
    authors = TrustedAuthors(human=(HUMAN,), agents=(AGENT, f"{HUMAN}-vergil-audit"))
    monkeypatch.setattr(req, "load_trusted_authors", lambda: authors)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: authors)
    return pages


def _query_kind(query: str, variables: dict[str, object], item_reads: list[object]) -> str:
    """Which read *query* is: ``branch``, ``body``, ``comments``, ``task`` or ``subs``.

    The builder reads an issue's item twice through the layer, first for its
    body and then for its comments: *item_reads* holds the issue numbers
    already read, so the second read of a number is its ``comments`` read.
    """
    if query in trust._BRANCH_QUERIES.values():
        return "branch"
    if query == trust._COMMENTS_QUERY:
        return "comments"
    if query == trust._ITEM_QUERY:
        number = variables.get("number")
        kind = "comments" if number in item_reads else "body"
        item_reads.append(number)
        return kind
    return _graph_kind(query)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comment_pages: list[Any]) -> Path:
    """A main clone on ``feature``: one base commit, one branch commit, a validate record."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "remote", "add", "origin", ISSUE_REPO_URL)
    _write(path, ".gitignore", "build/\n.vergil/\n")
    _write(path, "vergil.toml", _VERGIL_TOML)
    _write(path, "README.md", "# r\n")
    _write(path, "docs/guide/a.md", "a\n")
    _write(path, "src/x.py", "x = 1\n")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "feature")
    _write(path, "src/x.py", "x = 2\n")
    _commit(path, "branch")
    _record(path)
    monkeypatch.chdir(path)
    return path


def _merge_review_dir(repo: Path) -> Path:
    return repo / "build" / ".vergil-state" / "issue-implement" / "42" / "merge-review"


def _argv(repo: Path, step: str = "review", *extra: str, base: str = "develop") -> list[str]:
    return [
        "--issue",
        ISSUE,
        "--base",
        base,
        "--step",
        step,
        *extra,
    ]


def _check_argv(
    repo: Path,
    fix_base: str,
    results: list[str],
    *,
    step: str = "check",
    fix: tuple[str, ...] = (),
    ruled: tuple[str, ...] = (),
) -> list[str]:
    extra = [f"--fix-base={fix_base}"]
    extra += [arg for path in results for arg in ("--earlier-result", path)]
    extra += [arg for finding_id in fix for arg in ("--fix-id", finding_id)]
    extra += [arg for finding_id in ruled for arg in ("--ruled-id", finding_id)]
    return _argv(repo, step, *extra)


def _run(argv: list[str], capsys: Cap) -> tuple[int, str, str]:
    code = vrg_merge_review_request.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _built(argv: list[str], capsys: Cap) -> dict[str, Any]:
    code, out, err = _run(argv, capsys)
    assert code == 0, err
    path = Path(out.strip())
    step = argv[argv.index("--step") + 1]
    assert path.name == f"request-{step}.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _finding(finding_id: str, kind: str = "defect", **over: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": finding_id,
        "question": "merge-quality",
        "kind": kind,
        "security": False,
        "location": "src/x.py:1",
        "summary": "A problem.",
    }
    finding.update(over)
    return finding


def _store_result(
    repo: Path,
    step: str,
    findings: list[dict[str, Any]],
    fix_status: dict[str, str] | None = None,
) -> str:
    directory = _merge_review_dir(repo)
    request = json.loads((directory / f"request-{step}.json").read_text())
    block: dict[str, Any] = {
        "schema": "merge-review-result/1",
        "step": step,
        "request_id": request["request_id"],
        "findings": findings,
    }
    if fix_status is not None:
        block["fix_status"] = [{"id": i, "status": s} for i, s in fix_status.items()]
    path = directory / f"result-{step}.md"
    path.write_text(f"```merge-review-result\n{json.dumps(block)}\n```\n\nProse.\n")
    _capture(path, step, request["request_id"])
    return str(path)


def _capture(result: Path, step: str, request_id: str) -> None:
    """Write the capture record the plugin's hook would write beside *result*."""
    record = {
        "schema": "merge-review-capture/1",
        "step": step,
        "request_id": request_id,
        "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
        "agent_id": "agent-1",
    }
    (result.parent / f"capture-{step}.json").write_text(json.dumps(record))


def _fix(repo: Path, text: str = "x = 3\n") -> None:
    _write(repo, "src/x.py", text)
    _commit(repo, "fix")
    _record(repo)


def _reviewed(repo: Path, capsys: Cap, findings: list[dict[str, Any]]) -> tuple[str, str]:
    """Build the review, store its result, commit a fix: ``(review head, result path)``."""
    review = _built(_argv(repo), capsys)
    result = _store_result(repo, "review", findings)
    _fix(repo)
    return review["head_sha"], result


# -- the validate record ------------------------------------------------------------


def test_request_refuses_stale_validate(repo: Path, capsys: Cap) -> None:
    _write(repo, "src/x.py", "x = 9\n")
    _commit(repo, "after validate")
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "vrg-validate last ran at" in err
    assert not (repo / "build").exists()


def test_request_refuses_missing_record(repo: Path, capsys: Cap) -> None:
    (repo / ".vergil" / "vrg-validate-last.json").unlink()
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "no vrg-validate record" in err


def test_request_refuses_failed_stage(repo: Path, capsys: Cap) -> None:
    _record(repo, results=[StageResult("common", "ok"), StageResult("lint", "failed")])
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "did not pass every stage (lint)" in err


def test_request_refuses_nonzero_exit(repo: Path, capsys: Cap) -> None:
    _record(repo, exit_code=1)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "the last vrg-validate run exited 1" in err


def test_request_refuses_partial_run(repo: Path, capsys: Cap) -> None:
    _record(repo, full=False, results=[StageResult("lint", "ok")])
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "partial (--check)" in err


@pytest.mark.parametrize(
    "stage", [StageResult("audit", "skipped"), StageResult("audit", "interrupted")]
)
def test_request_refuses_skipped_or_interrupted(
    repo: Path, capsys: Cap, stage: StageResult
) -> None:
    _record(repo, results=[StageResult("common", "ok"), stage])
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "did not pass every stage (audit)" in err


def test_request_refuses_dirty_at_start(repo: Path, capsys: Cap) -> None:
    _record(repo, clean_at_start=False)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "started with uncommitted changes" in err


def test_request_accepts_warn(repo: Path, capsys: Cap) -> None:
    _record(repo, results=[StageResult("common", "ok"), StageResult("audit", "warn")])
    assert _built(_argv(repo), capsys)["validate_stages"] == ["common", "audit"]


@pytest.mark.parametrize("path", ["src/x.py", "notes.txt"])
def test_request_refuses_dirty_worktree(repo: Path, capsys: Cap, path: str) -> None:
    _write(repo, path, "dirty\n")
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "uncommitted or untracked changes" in err


# -- the private directory ----------------------------------------------------------


def test_request_refuses_public_state_root(repo: Path, capsys: Cap) -> None:
    root = repo / "build" / ".vergil-state"
    root.mkdir(parents=True)
    root.chmod(0o755)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "has mode 755, not 700" in err
    assert list(root.iterdir()) == []


def test_ensure_dir_prints_path(repo: Path, capsys: Cap) -> None:
    code, out, _err = _run(["--issue", ISSUE, "--ensure-dir"], capsys)
    assert code == 0
    directory = _merge_review_dir(repo)
    assert out == f"{directory}\n"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(directory.iterdir()) == []


def test_ensure_dir_refuses_symlinked_level(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = repo / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    (state / "issue-implement").symlink_to(elsewhere)
    code, out, err = _run(["--issue", ISSUE, "--ensure-dir"], capsys)
    assert code == 1
    assert out == ""
    assert "issue-implement is a symlink" in err
    assert list(elsewhere.iterdir()) == []


def test_ensure_dir_from_a_linked_worktree_uses_the_main_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Cap
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(linked), "develop")
    monkeypatch.chdir(linked)
    code, out, _err = _run(["--issue", ISSUE, "--ensure-dir"], capsys)
    assert code == 0
    assert out == f"{_merge_review_dir(repo)}\n"


# -- the clone must be the issue's repository -----------------------------------------


@pytest.mark.parametrize("url", [ISSUE_REPO_URL, "git@github.com:Acme/Widget.git"])
def test_ensure_dir_accepts_a_clone_of_the_issue_repo(repo: Path, capsys: Cap, url: str) -> None:
    _git(repo, "remote", "set-url", "origin", url)
    code, out, err = _run(["--issue", ISSUE, "--ensure-dir"], capsys)
    assert (code, err) == (0, "")
    assert out == f"{_merge_review_dir(repo)}\n"


@pytest.mark.parametrize("ensure_dir", [True, False])
def test_request_refuses_a_clone_of_another_repo(
    repo: Path, capsys: Cap, *, ensure_dir: bool
) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    argv = ["--issue", ISSUE, "--ensure-dir"] if ensure_dir else _argv(repo)
    code, out, err = _run(argv, capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/other, not the issue's repo acme/widget" in err
    assert "run vrg-merge-review-request in a clone of acme/widget" in err
    assert not (repo / "build").exists()


def test_request_refuses_a_clone_without_origin(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "remove", "origin")
    code, out, err = _run(["--issue", ISSUE, "--ensure-dir"], capsys)
    assert code == 1
    assert out == ""
    assert "cannot read the push URL of the 'origin' remote" in err
    assert not (repo / "build").exists()


# -- the review request -------------------------------------------------------------


def test_review_request_fields(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n")
    request = _built(_argv(repo, "review", "--design-source", str(spec)), capsys)
    directory = _merge_review_dir(repo)
    assert set(request) == {
        "schema",
        "request_id",
        "step",
        "repo",
        "issue",
        "issue_body",
        "issue_comments",
        "issue_comments_excluded_edited",
        "base_sha",
        "head_sha",
        "worktree",
        "diff_path",
        "change_class",
        "design_sources",
        "validate_stages",
        "epic_open_tasks",
    }
    assert request["schema"] == "merge-review-request/1"
    assert len(request["request_id"]) == 32
    assert request["step"] == "review"
    assert request["repo"] == "acme/widget"
    assert request["issue"] == ISSUE
    assert request["issue_body"] == "Do the thing.\n"
    assert request["issue_comments"] == []
    assert request["issue_comments_excluded_edited"] == 0
    assert request["base_sha"] == _git(repo, "rev-parse", "develop")
    assert request["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert request["worktree"] == str(repo)
    assert request["diff_path"] == str(directory / "diff-review.patch")
    assert request["change_class"] == "code"
    assert request["design_sources"] == [str(spec)]
    assert request["validate_stages"] == ["common", "lint", "test"]
    assert request["epic_open_tasks"] == [{"ref": "acme/widget#43", "title": "The next task"}]
    assert "+x = 2" in (directory / "diff-review.patch").read_text()
    assert not (directory / "fix-review.patch").exists()


def test_review_request_ids_are_new_each_time(repo: Path, capsys: Cap) -> None:
    first = _built(_argv(repo), capsys)["request_id"]
    assert _built(_argv(repo), capsys)["request_id"] != first


def test_request_diffs_from_merge_base(repo: Path, capsys: Cap) -> None:
    cut = _git(repo, "merge-base", "develop", "feature")
    _git(repo, "checkout", "-q", "develop")
    _write(repo, "README.md", "# moved on\n")
    _commit(repo, "base moved after the cut")
    _git(repo, "checkout", "-q", "feature")
    request = _built(_argv(repo), capsys)
    assert request["base_sha"] == cut
    diff = Path(request["diff_path"]).read_text()
    assert "src/x.py" in diff
    assert "moved on" not in diff


def test_docs_only_branch(repo: Path, capsys: Cap) -> None:
    _git(repo, "reset", "-q", "--hard", "develop")
    _write(repo, "docs/guide/a.md", "better\n")
    _commit(repo, "docs")
    _record(repo)
    assert _built(_argv(repo), capsys)["change_class"] == "docs-only"


def test_allow_list_comes_from_the_merge_base(repo: Path, capsys: Cap) -> None:
    # The working tree's vergil.toml lists src/, an edit hidden from git
    # status; the merge base's list does not, so the code change stays code.
    _git(repo, "update-index", "--skip-worktree", "vergil.toml")
    widened = _VERGIL_TOML.replace('["docs/guide/"]', '["docs/guide/", "src/"]')
    _write(repo, "vergil.toml", widened)
    assert _git(repo, "status", "--porcelain") == ""
    assert _built(_argv(repo), capsys)["change_class"] == "code"


def _branch_from(repo: Path, config: str | None) -> None:
    """Commit *config* as vergil.toml on develop (none: delete it), then branch from it."""
    _git(repo, "checkout", "-q", "develop")
    if config is None:
        _git(repo, "rm", "-q", "vergil.toml")
    else:
        _write(repo, "vergil.toml", config)
    _commit(repo, "config")
    _git(repo, "checkout", "-q", "-b", "feature2")
    _write(repo, "docs/guide/a.md", "changed\n")
    _commit(repo, "guide")
    _record(repo)


def test_request_refuses_merge_base_without_config(repo: Path, capsys: Cap) -> None:
    _branch_from(repo, None)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "vergil.toml not found at" in err


def test_request_refuses_invalid_config_at_merge_base(repo: Path, capsys: Cap) -> None:
    _branch_from(repo, _VERGIL_TOML.replace('["docs/guide/"]', '"docs/"'))
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "docs-paths must be a list of strings" in err


def test_request_unknown_base(repo: Path, capsys: Cap) -> None:
    code, _out, err = _run(_argv(repo, base="no-such-ref"), capsys)
    assert code == 1
    assert "ERROR: git failed" in err


def test_request_carries_only_the_humans_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append(
        _page(
            [
                _node(1, body="Scope: gap 1 only; gap 2 is a later task."),
                _node(2, author=AGENT_BOT, body="Noted."),
                _node(3, author={"__typename": "User", "login": "stranger"}, body="Also gap 3."),
                _node(4, author=None, body="From a deleted account."),
                _node(5, author=AUDIT_BOT, body="Audit."),
                _node(6, body="And keep the CLI unchanged.\n", created="2026-11-02T10:00:00+00:00"),
            ]
        )
    )
    request = _built(_argv(repo), capsys)
    assert request["issue_comments"] == [
        {
            "author": HUMAN,
            "created_at": _CREATED,
            "body": "Scope: gap 1 only; gap 2 is a later task.",
        },
        {
            "author": HUMAN,
            "created_at": "2026-11-02T10:00:00+00:00",
            "body": "And keep the CLI unchanged.\n",
        },
    ]
    assert request["issue_comments_excluded_edited"] == 0


@pytest.mark.parametrize(
    ("impostor", "cause"),
    [(IMPOSTOR_USER, "wrong-type"), (IMPOSTOR_BOT, "wrong-type"), (OUTSIDER, "untrusted")],
    ids=["user-named-like-the-agent", "bot-with-the-humans-login", "outsider"],
)
def test_request_leaves_out_an_impostors_comment(
    repo: Path,
    capsys: Cap,
    comment_pages: list[Any],
    impostor: dict[str, str],
    cause: str,
) -> None:
    comment_pages.append(_page([_node(1, body="Kept."), _node(2, author=impostor, body=SPAM_TEXT)]))
    code, out, err = _run(_argv(repo), capsys)
    assert code == 0, err
    request = json.loads(Path(out.strip()).read_text())
    assert request["issue_comments"] == [{"author": HUMAN, "created_at": _CREATED, "body": "Kept."}]
    assert (
        f"skipped untrusted content: acme/widget#42 comment {impostor['login']} at {_CREATED}"
        f" on comment 2 ({cause})"
    ) in err
    assert SPAM_TEXT not in out
    assert SPAM_TEXT not in err
    assert SPAM_TEXT not in json.dumps(request)


def test_request_leaves_out_a_human_comment_an_outsider_edited(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append(_page([_node(1, body=SPAM_TEXT, editor=OUTSIDER), _node(2, body="Kept.")]))
    code, out, err = _run(_argv(repo), capsys)
    assert code == 0, err
    request = json.loads(Path(out.strip()).read_text())
    assert [c["body"] for c in request["issue_comments"]] == ["Kept."]
    # Left out as untrusted, not counted as the human's own edit.
    assert request["issue_comments_excluded_edited"] == 0
    assert (
        f"skipped untrusted content: acme/widget#42 edit drive-by at {_EDITED}"
        " on comment 1 (untrusted)"
    ) in err
    assert SPAM_TEXT not in out
    assert SPAM_TEXT not in err
    assert SPAM_TEXT not in json.dumps(request)


def test_request_asks_for_the_task_issue(
    repo: Path, capsys: Cap, comment_pages: list[Any], graphql_calls: list[dict[str, object]]
) -> None:
    _built(_argv(repo), capsys)
    # Read once for the body, once for the comments.
    assert graphql_calls == [{"owner": "acme", "name": "widget", "number": 42}] * 2


def test_request_leaves_out_and_counts_edited_human_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append(
        _page(
            [
                _node(1, body="Scope as first written.", edited=True),
                _node(2, body="Kept."),
                _node(3, author=AGENT_BOT, body="An edited agent comment.", edited=True),
                _node(4, body="Also edited.", edited=True),
            ]
        )
    )
    request = _built(_argv(repo), capsys)
    assert [c["body"] for c in request["issue_comments"]] == ["Kept."]
    assert request["issue_comments_excluded_edited"] == 2


def test_request_reads_every_page_of_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any], graphql_calls: list[dict[str, object]]
) -> None:
    first = [_node(i, body=f"c{i}") for i in range(1, 101)]
    comment_pages.extend([_page(first, cursor="c1"), _page([_node(101, body="last")])])
    request = _built(_argv(repo), capsys)
    bodies = [c["body"] for c in request["issue_comments"]]
    assert bodies == [f"c{i}" for i in range(1, 101)] + ["last"]
    assert graphql_calls[1] == {"owner": "acme", "name": "widget", "number": 42, "after": "c1"}


def test_check_request_carries_the_humans_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    comment_pages.append(_page([_node(1, body="Scope."), _node(2, body="Old.", edited=True)]))
    request = _built(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert request["issue_comments"] == [
        {"author": HUMAN, "created_at": _CREATED, "body": "Scope."}
    ]
    assert request["issue_comments_excluded_edited"] == 1


@pytest.mark.parametrize("human", [(), ("one", "two")])
def test_request_refuses_without_exactly_one_human_login(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch, human: tuple[str, ...]
) -> None:
    authors = TrustedAuthors(human=human, agents=(AGENT,))
    monkeypatch.setattr(req, "load_trusted_authors", lambda: authors)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert f"exactly one human login; found {len(human)}" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_request_refuses_unconfigured_trusted_authors(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconfigured() -> TrustedAuthors:
        msg = "trusted authors are not configured"
        raise TrustedAuthorsNotConfiguredError(msg)

    monkeypatch.setattr(req, "load_trusted_authors", unconfigured)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue's comments: trusted authors are not configured" in err


def test_request_refuses_unconfigured_trusted_authors_for_the_body(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconfigured() -> TrustedAuthors:
        msg = "trusted authors are not configured"
        raise TrustedAuthorsNotConfiguredError(msg)

    monkeypatch.setattr(trust, "load_trusted_authors", unconfigured)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "cannot read the issue or its epic: trusted authors are not configured" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


_SECRET = "s3cr3t-value"


@pytest.mark.parametrize("created", [_SECRET, "2026-11-01T09:30:00"])
def test_request_refuses_a_human_comment_without_a_zoned_time(
    repo: Path, capsys: Cap, comment_pages: list[Any], created: str
) -> None:
    comment_pages.append(_page([_node(7, body=_SECRET, created=created)]))
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "comment 7 on the issue has a created_at that is not an ISO 8601" in err
    assert _SECRET not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def _failing_on(
    monkeypatch: pytest.MonkeyPatch, failing_kind: str, answer: Any, number: int = 42
) -> None:
    """Wrap the fake: the *failing_kind* read of issue *number* raises or returns *answer*.

    The kinds are :func:`_query_kind`'s; a ``branch`` read fails whatever its
    number. An exception *answer* is raised, any other value returned.
    """
    fake = github.graphql
    item_reads: list[object] = []

    def failing(query: str, **variables: object) -> Any:
        kind = _query_kind(query, variables, item_reads)
        read_of = variables.get("number", _graph_number(variables))
        if kind == failing_kind and (kind == "branch" or read_of == number):
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return fake(query, **variables)

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", failing)


def test_request_refuses_an_unreadable_comment_stream(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_on(monkeypatch, "comments", {"repository": {"issueOrPullRequest": None}})
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert (
        "cannot read the issue's comments: cannot read who wrote the content"
        " (read_issue acme/widget#42): the issue or pull request is missing or not an object"
    ) in err


def test_request_refuses_a_graphql_error(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_on(monkeypatch, "comments", RuntimeError("GraphQL errors: [boom]"))
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert (
        "cannot read the issue's comments: cannot read who wrote the content"
        " (read_issue acme/widget#42): the GraphQL response carried errors or no data"
    ) in err


def test_request_refuses_a_github_api_error(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = GitHubAPIError(1, ["gh", "api", "graphql"], output="", stderr="HTTP 502")
    _failing_on(monkeypatch, "comments", error)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue's comments:" in err
    assert "the GraphQL call failed: HTTP 502" in err
    assert "git failed" not in err


@pytest.mark.parametrize("failing_kind", ["body", "task", "subs", "comments"])
def test_request_reports_a_missing_github_token(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch, failing_kind: str
) -> None:
    error = MissingGitHubTokenError(4, ("gh", "api", "graphql"), None, "no token")
    _failing_on(monkeypatch, failing_kind, error, number=7 if failing_kind == "subs" else 42)
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_request.main(_argv(repo))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-merge-review-request:")
    assert "git failed" not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


# -- the issue body and the epic's open tasks ---------------------------------------


def test_request_takes_the_body_from_github(
    repo: Path,
    capsys: Cap,
    task_pages: dict[str, Any],
    task_calls: list[Any],
    graphql_calls: list[dict[str, object]],
) -> None:
    task_pages["bodies"][42] = "The task's own body.\n"
    request = _built(_argv(repo), capsys)
    assert request["issue_body"] == "The task's own body.\n"
    assert graphql_calls[0] == {"owner": "acme", "name": "widget", "number": 42}
    assert task_calls[0] == ("task", ISSUE)


def test_request_keeps_an_empty_body(repo: Path, capsys: Cap, task_pages: dict[str, Any]) -> None:
    task_pages["bodies"][42] = ""
    assert _built(_argv(repo), capsys)["issue_body"] == ""


def test_request_takes_a_body_the_agent_wrote(
    repo: Path, capsys: Cap, task_pages: dict[str, Any]
) -> None:
    task_pages["items"][42] = _body_item("The agent's scope.\n", _page([]), author=AGENT_BOT)
    assert _built(_argv(repo), capsys)["issue_body"] == "The agent's scope.\n"


@pytest.mark.parametrize(
    ("item", "who"),
    [
        (
            _body_item(f"Scope.\n{SPAM_TEXT}\n", _page([]), editor=OUTSIDER),
            f"acme/widget#42 edit drive-by at {_EDITED} on body (untrusted)",
        ),
        (
            _body_item(f"Scope.\n{SPAM_TEXT}\n", _page([]), author=IMPOSTOR_USER),
            f"acme/widget#42 issue {AGENT} at {_CREATED} on body (wrong-type)",
        ),
        (
            _body_item(f"Scope.\n{SPAM_TEXT}\n", _page([]), author=IMPOSTOR_BOT),
            f"acme/widget#42 issue {HUMAN} at {_CREATED} on body (wrong-type)",
        ),
    ],
    ids=["edited-by-an-outsider", "user-named-like-the-agent", "bot-with-the-humans-login"],
)
def test_merge_review_request_untrusted_body_raises(
    repo: Path,
    capsys: Cap,
    task_pages: dict[str, Any],
    item: dict[str, Any],
    who: str,
) -> None:
    task_pages["items"][42] = item
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert (
        "ERROR: cannot read the issue or its epic: stopped before any write:"
        f" the issue body is untrusted: {who}."
    ) in err
    assert SPAM_TEXT not in err
    assert "Scope." not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()
    written = [path for path in _merge_review_dir(repo).rglob("*") if path.is_file()]
    assert all(SPAM_TEXT not in path.read_text() for path in written)


def test_request_refuses_an_issue_without_a_body_string(
    repo: Path, capsys: Cap, task_pages: dict[str, Any]
) -> None:
    task_pages["items"][42] = _body_item(None, _page([]))
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert (
        "ERROR: cannot read the issue or its epic: cannot read who wrote the content"
        " (read_issue acme/widget#42): the issue body has no usable body"
    ) in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_task_under_an_epic_lists_its_other_open_tasks(
    repo: Path, capsys: Cap, graph: FakeSubIssueGraph, task_calls: list[Any]
) -> None:
    graph.issues[EPIC_REF].labels = ["epic", "enhancement"]
    cased = graph.add_issue(IssueRef("Acme", "Widget", 42), title="This task, cased differently")
    docs = graph.add_issue(IssueRef("acme", ".github", 9), title="Docs task in another repo")
    graph.link(EPIC_REF, cased)
    graph.link(EPIC_REF, docs)
    request = _built(_argv(repo), capsys)
    assert request["epic_open_tasks"] == [
        {"ref": "acme/widget#43", "title": "The next task"},
        {"ref": "acme/.github#9", "title": "Docs task in another repo"},
    ]
    assert task_calls[0] == ("task", ISSUE)
    assert ("subs", EPIC) in task_calls


C2 = IssueRef("acme", ".github", 70)


def _chained(graph: FakeSubIssueGraph, task_pages: dict[str, Any]) -> IssueRef:
    """Fill ``EPIC`` to 99 items, link continuation ``C2`` in its last slot, move the task there.

    The fillers are closed; ``C2`` also holds ``acme/widget#45``, open. The
    trust layer serves ``C2``'s body as the chain layer writes it.
    """
    _move(graph, ISSUE_REF, None)
    for number in range(100, 100 + epic_chain.ITEM_CAP - 2):
        filler = graph.add_issue(IssueRef("acme", "widget", number), title="t", state="CLOSED")
        graph.link(EPIC_REF, filler)
    graph.add_continuation(C2, EPIC_REF, EPIC_REF)
    task_pages["bodies"][C2.number] = graph.issues[C2].body
    graph.link(C2, ISSUE_REF)
    graph.link(C2, graph.add_issue(IssueRef("acme", "widget", 45), title="In the continuation"))
    return C2


def test_merge_review_open_tasks_span_chain(
    repo: Path,
    capsys: Cap,
    graph: FakeSubIssueGraph,
    task_pages: dict[str, Any],
    task_calls: list[Any],
) -> None:
    # A task in C2 is under the head, and its epic's open tasks are the whole
    # chain's: the head's and C2's, never C2 itself.
    _chained(graph, task_pages)
    graph.clear_log()
    _body, parent, is_proxy = req._fetch_task(ISSUE_REF)
    assert (parent, is_proxy) == (EPIC_REF, False)
    request = _built(_argv(repo), capsys)
    assert request["epic_open_tasks"] == [
        {"ref": "acme/widget#43", "title": "The next task"},
        {"ref": "acme/widget#45", "title": "In the continuation"},
    ]
    assert ("subs", C2.slug) in task_calls
    assert graph.events == []


def test_merge_review_chain_drift_refuses(
    repo: Path, capsys: Cap, graph: FakeSubIssueGraph, task_pages: dict[str, Any]
) -> None:
    # A continuation whose Continues: line names another head is drift: the
    # request is refused, naming it, never built on a guessed epic.
    _chained(graph, task_pages)
    task_pages["bodies"][C2.number] = graph.issues[C2].body.replace(EPIC, "acme/.github#8")
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "ERROR: cannot read the issue or its epic: chain drift: acme/.github#70" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


@pytest.mark.parametrize(
    ("labels", "parent"),
    [
        (["epic"], None),
        (["epic", "ad-hoc"], EPIC_REF),
        (["bug"], EPIC_REF),
        ([], EPIC_REF),
    ],
    ids=["no-parent", "ad-hoc", "not-an-epic", "unlabelled"],
)
def test_task_outside_an_epic_has_no_epic_tasks(
    repo: Path,
    capsys: Cap,
    graph: FakeSubIssueGraph,
    task_calls: list[Any],
    labels: list[str],
    parent: IssueRef | None,
) -> None:
    graph.issues[EPIC_REF].labels = labels
    _move(graph, ISSUE_REF, parent)
    assert _built(_argv(repo), capsys)["epic_open_tasks"] == []
    assert [kind for kind, _slug in task_calls] == ["task"]


PROXY = "acme/.github#9"
PRIMARY = "other/.github#5"


def _under_proxy(graph: FakeSubIssueGraph) -> None:
    """Move the task under ``PROXY``, an ``epic`` + ``proxy`` epic."""
    proxy = graph.add_issue(
        parse_issue_ref(PROXY, default_repo=""), title="Epic (proxy): p", labels=["epic", "proxy"]
    )
    _move(graph, ISSUE_REF, proxy)


@pytest.fixture
def proxy_links(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fakes for the proxy readers: the primary, its listed proxies and its children.

    The task's parent is ``PROXY`` (``epic`` + ``proxy``), whose primary is
    ``PRIMARY``; the primary lists ``PROXY`` and ``acme/.github#11``. Each
    fake records its argument under ``calls``; an ``Exception`` value under
    a reader's key is raised instead.
    """
    links: dict[str, Any] = {
        "primary": parse_issue_ref(PRIMARY, default_repo=""),
        "proxies": (
            parse_issue_ref(PROXY, default_repo=""),
            parse_issue_ref("acme/.github#11", default_repo=""),
        ),
        "children": [
            ChildState(parse_issue_ref("other/thing#1", default_repo=""), "OPEN", "Primary task"),
            ChildState(parse_issue_ref(ISSUE, default_repo=""), "OPEN", "This task"),
            ChildState(parse_issue_ref("acme/widget#43", default_repo=""), "CLOSED", "Done"),
            ChildState(parse_issue_ref("acme/tool#3", default_repo=""), "OPEN", "Other proxy's"),
        ],
        "calls": [],
    }

    def answer(name: str, ref: IssueRef) -> Any:
        links["calls"].append((name, ref.slug))
        value = links[name]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(proxies, "primary_of", lambda ref: answer("primary", ref))
    monkeypatch.setattr(proxies, "proxies_of", lambda ref: answer("proxies", ref))
    monkeypatch.setattr(epic_audit, "epic_children", lambda ref: answer("children", ref))
    return links


def test_proxy_task_open_tasks_from_primary(
    repo: Path,
    capsys: Cap,
    graph: FakeSubIssueGraph,
    task_calls: list[Any],
    proxy_links: dict[str, Any],
) -> None:
    _under_proxy(graph)
    request = _built(_argv(repo), capsys)
    assert request["epic_open_tasks"] == [
        {"ref": "other/thing#1", "title": "Primary task"},
        {"ref": "acme/tool#3", "title": "Other proxy's"},
    ]
    assert proxy_links["calls"] == [
        ("primary", PROXY),
        ("proxies", PRIMARY),
        ("children", PRIMARY),
    ]
    # The proxy's own sub-issues are never read: the primary's children hold them.
    assert [kind for kind, _vars in task_calls] == ["task"]


def test_proxy_task_open_tasks_leave_out_the_batch(
    comment_pages: list[Any], graph: FakeSubIssueGraph, proxy_links: dict[str, Any]
) -> None:
    _under_proxy(graph)
    context = req.fetch_task_context(
        parse_issue_ref(ISSUE, default_repo=""), [parse_issue_ref("Acme/Tool#3", default_repo="")]
    )
    assert context.epic_open_tasks == [{"ref": "other/thing#1", "title": "Primary task"}]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"primary": ProxyLinkError(f"proxy {PROXY} has no 'Primary:' line in its body")},
            f"proxy {PROXY} has no 'Primary:' line in its body",
        ),
        (
            {"proxies": ()},
            f"proxy {PROXY} names {PRIMARY} as its primary, but {PRIMARY} has no trusted"
            f" 'Proxy: {PROXY}' comment",
        ),
        (
            {"children": ProxyLinkError(f"{PRIMARY} lists the proxy acme/.github#11, whose")},
            f"{PRIMARY} lists the proxy acme/.github#11, whose",
        ),
        (
            {"proxies": GitHubAPIError(1, ["gh"], output="", stderr="HTTP 404")},
            "HTTP 404",
        ),
        (
            {"primary": github.NoInstallationError("other", ["acme"])},
            "no installation for owner 'other'",
        ),
        (
            {"proxies": TrustedAuthorsNotConfiguredError("no trusted authors configured")},
            "no trusted authors configured",
        ),
        (
            {
                "primary": trust.UntrustedContentError(
                    trust.Skipped("acme/.github", 9, "issue", "drive-by", "t", "body", "untrusted"),
                    "the issue body is untrusted",
                )
            },
            "stopped before any write: the issue body is untrusted: acme/.github#9 issue drive-by",
        ),
        (
            {"primary": trust.TrustReadError("read_issue acme/.github#9", "truncated")},
            "cannot read who wrote the content (read_issue acme/.github#9): truncated",
        ),
    ],
    ids=[
        "no-primary-line",
        "primary-does-not-name-proxy",
        "another-proxy-broken",
        "read-failure",
        "no-installation",
        "trusted-authors-unconfigured",
        "untrusted-proxy-body",
        "unreadable-proxy-body",
    ],
)
def test_proxy_task_unresolvable_primary_raises(
    repo: Path,
    capsys: Cap,
    graph: FakeSubIssueGraph,
    proxy_links: dict[str, Any],
    override: dict[str, Any],
    message: str,
) -> None:
    _under_proxy(graph)
    proxy_links.update(override)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "ERROR: cannot read the issue or its epic:" in err
    assert message in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_plain_epic_task_unchanged(
    repo: Path,
    capsys: Cap,
    task_calls: list[Any],
    proxy_links: dict[str, Any],
) -> None:
    request = _built(_argv(repo), capsys)
    assert request["epic_open_tasks"] == [{"ref": "acme/widget#43", "title": "The next task"}]
    assert [kind for kind, _slug in task_calls] == ["task", "subs"]
    assert proxy_links["calls"] == []


def test_check_request_takes_the_body_and_tasks_from_github(
    repo: Path, capsys: Cap, task_pages: dict[str, Any], graph: FakeSubIssueGraph
) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    task_pages["bodies"][42] = "Narrowed.\n"
    graph.close(NEXT)
    request = _built(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert request["issue_body"] == "Narrowed.\n"
    assert request["epic_open_tasks"] == []


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"labels": None}, f"{EPIC}: its labels are unreadable"),
        ([], f"{EPIC}: its labels are unreadable"),
        ({"labels": [{}]}, f"{EPIC}: a label has no name"),
        ({"labels": ["epic"]}, f"{EPIC}: a label has no name"),
    ],
)
def test_request_refuses_unreadable_epic_labels(
    repo: Path,
    capsys: Cap,
    monkeypatch: pytest.MonkeyPatch,
    graph: FakeSubIssueGraph,
    answer: Any,
    message: str,
) -> None:
    def read_json(*args: str) -> Any:
        if args[:3] == ("issue", "view", str(EPIC_REF.number)) and args[-1] == "labels":
            return answer
        return graph.read_json(*args)

    monkeypatch.setattr("vergil_tooling.lib.github.read_json", read_json)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert f"ERROR: cannot read the issue or its epic: {message}" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


@pytest.mark.parametrize("failing_kind", ["task", "subs"])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (RuntimeError("GraphQL errors: [boom]"), "GraphQL errors: [boom]"),
        (GitHubAPIError(1, ["gh", "api", "graphql"], output="", stderr="HTTP 502"), "HTTP 502"),
    ],
)
def test_request_refuses_a_failed_fetch(
    repo: Path,
    capsys: Cap,
    monkeypatch: pytest.MonkeyPatch,
    failing_kind: str,
    error: Exception,
    message: str,
) -> None:
    _failing_on(monkeypatch, failing_kind, error, number=7 if failing_kind == "subs" else 42)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "ERROR: cannot read the issue or its epic:" in err
    assert message in err
    assert "git failed" not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_request_refuses_missing_design_source(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    argv = _argv(repo, "review", "--design-source", str(tmp_path / "nope.md"))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert "is not a readable file" in err


@pytest.mark.parametrize("issue", ["widget#42", "acme/widget", "#42", "acme/widget#0"])
def test_request_refuses_bad_issue(issue: str, capsys: Cap) -> None:
    code, _out, err = _run(["--issue", issue, "--ensure-dir"], capsys)
    assert code == 1
    assert "--issue must" in err


# -- usage --------------------------------------------------------------------------

_BUILDING = ["--base", "d"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--ensure-dir", "--step", "review"],
        ["--ensure-dir", "--design-source", "x.md"],
        ["--step", "review"],
        [*_BUILDING, "--step", "fix"],
        [*_BUILDING, "--step", "check", "--fix-id", "R1"],
        [*_BUILDING, "--step", "review", "--fix-id", "R1"],
    ],
)
def test_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_request.main(["--issue", ISSUE, *argv])
    assert exc.value.code == 2


# -- the check request --------------------------------------------------------------


def test_check_request_fields(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1"), _finding("R2")])
    request = _built(_check_argv(repo, review_head, [result], fix=("R1", "R2")), capsys)
    directory = _merge_review_dir(repo)
    assert request["fix_base_sha"] == review_head
    assert request["fix_diff_path"] == str(directory / "fix-check.patch")
    assert request["earlier_result_paths"] == [result]
    assert request["earlier_finding_ids"] == ["R1", "R2"]
    fix = (directory / "fix-check.patch").read_text()
    assert "-x = 2" in fix
    assert "+x = 3" in fix
    whole = (directory / "diff-check.patch").read_text()
    assert "-x = 1" in whole
    assert "+x = 3" in whole
    # The result check accepts a reply that answers this request.
    block = {
        "schema": "merge-review-result/1",
        "step": "check",
        "request_id": request["request_id"],
        "findings": [],
        "fix_status": [{"id": "R1", "status": "fixed"}, {"id": "R2", "status": "fixed"}],
    }
    reply = f"```merge-review-result\n{json.dumps(block)}\n```\n"
    assert parse_result(reply, request).fix_status == {"R1": "fixed", "R2": "fixed"}


def test_check_request_omits_unlisted(repo: Path, capsys: Cap) -> None:
    findings = [
        _finding("R1"),
        _finding("R2"),
        _finding("R3", "pre-existing"),
        _finding("R4", "design-question"),
    ]
    review_head, result = _reviewed(repo, capsys, findings)
    request = _built(_check_argv(repo, review_head, [result], fix=("R1", "R2")), capsys)
    assert request["earlier_finding_ids"] == ["R1", "R2"]


def test_check_ruled_defect_not_listed(repo: Path, capsys: Cap) -> None:
    security = _finding("R2", question="production-safety", security=True)
    del security["summary"]
    review_head, result = _reviewed(repo, capsys, [_finding("R1"), security, _finding("R3")])
    argv = _check_argv(repo, review_head, [result], fix=("R3",), ruled=("R1", "R2"))
    assert _built(argv, capsys)["earlier_finding_ids"] == ["R3"]


@pytest.mark.parametrize(
    ("fix", "ruled", "message"),
    [
        (("R9",), (), "no earlier result raised R9"),
        (("R2",), (), "earlier defect R1 named by neither --fix-id nor --ruled-id"),
        (("R1", "R2", "R1"), (), "--fix-id names R1 more than once"),
        (("R1",), ("R2", "R2"), "--ruled-id names R2 more than once"),
        (("R1",), ("R1", "R2"), "R1 given to both --fix-id and --ruled-id"),
        ((), ("R1", "R2"), "nothing to check"),
    ],
)
def test_check_refuses_bad_finding_ids(
    repo: Path, capsys: Cap, fix: tuple[str, ...], ruled: tuple[str, ...], message: str
) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1"), _finding("R2")])
    code, out, err = _run(_check_argv(repo, review_head, [result], fix=fix, ruled=ruled), capsys)
    assert code == 1
    assert out == ""
    assert message in err
    assert not (_merge_review_dir(repo) / "request-check.json").exists()


@pytest.mark.parametrize("ruled", [False, True])
@pytest.mark.parametrize("stop_id", ["M1", "D1", "B1"])
def test_check_refuses_stop_item_ids(repo: Path, capsys: Cap, ruled: bool, stop_id: str) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    if ruled:
        argv = _check_argv(repo, review_head, [result], fix=("R1",), ruled=(stop_id,))
    else:
        argv = _check_argv(repo, review_head, [result], fix=("R1", stop_id))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert f"no earlier result raised {stop_id}" in err


def test_check_refuses_invalid_earlier_result(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    Path(result).write_text("No block here.\n")
    request = json.loads((_merge_review_dir(repo) / "request-review.json").read_text())
    _capture(Path(result), "review", request["request_id"])
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "the review result" in err
    assert "fails the result check: rule 1 (shape)" in err


def test_check_refuses_earlier_result_edited_after_capture(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    # Still a valid result, but not the bytes the hook captured.
    path = Path(result)
    path.write_text(path.read_text().replace("Prose.", "Tidied prose."))
    code, out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert out == ""
    assert "the review result" in err
    assert "fails the result check: rule 10 (capture)" in err
    assert "the result was changed after the capture" in err
    assert not (_merge_review_dir(repo) / "request-check.json").exists()


def test_check_refuses_earlier_result_without_capture(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    (_merge_review_dir(repo) / "capture-review.json").unlink()
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "rule 10 (capture): the result has no capture record" in err
    assert not (_merge_review_dir(repo) / "request-check.json").exists()


def test_check_refuses_capture_of_another_request(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    _capture(Path(result), "review", "0" * 32)
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "rule 10 (capture): the capture record's request_id is not the request's" in err
    assert "0" * 32 not in err


def test_check_refuses_malformed_earlier_request_before_capture(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    stored = _merge_review_dir(repo) / "request-review.json"
    request = json.loads(stored.read_text())
    request["request_id"] = "not-an-id"
    stored.write_text(json.dumps(request))
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "fails the result check: the request: request_id must be 32 lowercase hex" in err
    assert "not-an-id" not in err


@pytest.mark.parametrize(
    ("content", "message"),
    [(None, "cannot read the review request or result"), ("[]", "must hold a JSON object")],
)
def test_check_refuses_bad_earlier_request(
    repo: Path, capsys: Cap, content: str | None, message: str
) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    stored = _merge_review_dir(repo) / "request-review.json"
    if content is None:
        stored.unlink()
    else:
        stored.write_text(content)
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert message in err


def test_check_refuses_wrong_earlier_result_count(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    argv = _check_argv(repo, review_head, [result], step="check-2", fix=("R1",))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert "one --earlier-result per earlier step, in order (review, check); got 1" in err


def test_check_refuses_rebased_fix_base(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    _git(repo, "reset", "-q", "--hard", "develop")
    _write(repo, "src/x.py", "x = 7\n")
    _commit(repo, "rewritten")
    _record(repo)
    code, out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 3
    assert out == ""
    assert "is not an ancestor of HEAD: the branch was rebased" in err


@pytest.mark.parametrize(
    ("fix_base", "message"),
    [("-x", "--fix-base must be a commit"), ("no-such-ref", "ERROR: git failed")],
)
def test_check_refuses_bad_fix_base(repo: Path, capsys: Cap, fix_base: str, message: str) -> None:
    _review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    code, _out, err = _run(_check_argv(repo, fix_base, [result], fix=("R1",)), capsys)
    assert code == 1
    assert message in err


def test_check_refuses_fix_base_not_the_judged_head(repo: Path, capsys: Cap) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    earlier = _git(repo, "rev-parse", f"{review_head}~1")
    code, out, err = _run(_check_argv(repo, earlier, [result], fix=("R1",)), capsys)
    assert code == 1
    assert out == ""
    assert (
        f"--fix-base {earlier[:12]} is not the HEAD the review judged"
        f" ({review_head[:12]}, the head_sha of request-review.json): give that commit"
    ) in err
    directory = _merge_review_dir(repo)
    assert not (directory / "request-check.json").exists()
    assert not (directory / "fix-check.patch").exists()


@pytest.mark.parametrize("head_sha", [None, 7, "abc123", "../../etc"])
def test_check_refuses_stored_request_without_a_head_sha(
    repo: Path, capsys: Cap, head_sha: object
) -> None:
    review_head, result = _reviewed(repo, capsys, [_finding("R1")])
    stored = _merge_review_dir(repo) / "request-review.json"
    request = json.loads(stored.read_text())
    if head_sha is None:
        del request["head_sha"]
    else:
        request["head_sha"] = head_sha
    stored.write_text(json.dumps(request))
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "must hold a head_sha that is a full commit SHA" in err
    assert "../../etc" not in err


def _checked(repo: Path, capsys: Cap) -> tuple[str, list[str]]:
    """Review R1 and R2, check them (R1 fixed, R2 not, C1 raised), commit a second fix."""
    review_head, review_result = _reviewed(repo, capsys, [_finding("R1"), _finding("R2")])
    check = _built(_check_argv(repo, review_head, [review_result], fix=("R1", "R2")), capsys)
    statuses = {"R1": "fixed", "R2": "not-fixed"}
    check_result = _store_result(repo, "check", [_finding("C1")], statuses)
    _fix(repo, "x = 4\n")
    return check["head_sha"], [review_result, check_result]


def test_check_2_request_lists_review_and_check_ids(repo: Path, capsys: Cap) -> None:
    check_head, results = _checked(repo, capsys)
    argv = _check_argv(repo, check_head, results, step="check-2", fix=("R2", "C1"))
    request = _built(argv, capsys)
    assert request["earlier_finding_ids"] == ["R2", "C1"]
    assert request["earlier_result_paths"] == results
    assert request["fix_base_sha"] == check_head


def test_check_2_refuses_a_defect_still_not_fixed(repo: Path, capsys: Cap) -> None:
    check_head, results = _checked(repo, capsys)
    argv = _check_argv(repo, check_head, results, step="check-2", fix=("C1",))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert "earlier defect R2 named by neither" in err


def test_check_2_refuses_check_result_edited_after_capture(repo: Path, capsys: Cap) -> None:
    check_head, results = _checked(repo, capsys)
    path = Path(results[1])
    path.write_bytes(path.read_bytes() + b"\n")
    argv = _check_argv(repo, check_head, results, step="check-2", fix=("R2", "C1"))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert "the check result" in err
    assert "fails the result check: rule 10 (capture)" in err
    assert not (_merge_review_dir(repo) / "request-check-2.json").exists()


def test_check_2_refuses_the_review_head_as_fix_base(repo: Path, capsys: Cap) -> None:
    check_head, results = _checked(repo, capsys)
    review = json.loads((_merge_review_dir(repo) / "request-review.json").read_text())
    argv = _check_argv(repo, review["head_sha"], results, step="check-2", fix=("R2", "C1"))
    code, _out, err = _run(argv, capsys)
    assert code == 1
    assert (
        f"is not the HEAD the check judged ({check_head[:12]}, the head_sha of request-check.json)"
    ) in err
    assert not (_merge_review_dir(repo) / "request-check-2.json").exists()


# -- the library ----------------------------------------------------------------------


def test_refuse_carries_its_exit_code() -> None:
    with pytest.raises(req.RequestError) as exc:
        req.refuse("rebased", req.EXIT_REBASED)
    assert exc.value.exit_code == 3
    assert str(exc.value) == "rebased"


@pytest.mark.parametrize("content", [None, "{", "[]"])
def test_check_fix_base_refuses_unreadable_stored_request(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "request-review.json").write_text(content)
    with pytest.raises(req.RequestError) as exc:
        req.check_fix_base(tmp_path, "check", "a" * 40)
    assert exc.value.exit_code == 1
    if content == "[]":
        assert "must hold a head_sha that is a full commit SHA" in str(exc.value)
    else:
        assert "cannot read the review request" in str(exc.value)


def test_file_names() -> None:
    assert req.request_file("check-2") == "request-check-2.json"
    assert req.diff_file("review") == "diff-review.patch"
    assert req.fix_file("check") == "fix-check.patch"
    assert req.commits_file("check") == "commits-check.patch"


# -- a fix to an open PR -------------------------------------------------------------

BATCH_BODY = "Summary.\n\nCloses #42\nCloses #43\n"
PR_NUMBER = 7


def _head(repo: Path) -> str:
    """The commit ``HEAD`` names in *repo*: the head a fake PR reports when it is at ``HEAD``."""
    return _git(repo, "rev-parse", "HEAD")


def _pr_node(
    number: int = PR_NUMBER,
    *,
    state: str = "OPEN",
    head: str = "feature",
    head_oid: str = "f" * 40,
    author: Any = AGENT_BOT,
    fork: str | None = None,
    title: str = "A fix",
) -> dict[str, Any]:
    """A branch lookup's ``PullRequest`` node; a *fork* owner makes it a cross-repo PR.

    *head_oid* is the PR's ``headRefOid``. The default names no commit of the
    test repository, so a request that reads it must pass the PR's real head.
    """
    return {
        "number": number,
        "url": f"https://github.com/acme/widget/pull/{number}",
        "title": title,
        "headRefName": head,
        "headRefOid": head_oid,
        "baseRefName": "develop",
        "state": state,
        "createdAt": _CREATED,
        "author": author,
        "mergeCommit": None,
        "isCrossRepository": fork is not None,
        "headRepositoryOwner": {"login": fork or "acme"},
        "headRepository": {"name": "widget"},
    }


def _open_pr(
    body: Any = BATCH_BODY,
    state: str = "OPEN",
    head: str = "feature",
    *,
    nodes: list[dict[str, Any]] | None = None,
    body_author: Any = AGENT_BOT,
    editor: Any = None,
    head_oid: str = "f" * 40,
) -> dict[str, Any]:
    """The PR fake's answer: PR #7's branch lookup nodes, and its item (body by the agent)."""
    return {
        "nodes": [_pr_node(state=state, head=head, head_oid=head_oid)] if nodes is None else nodes,
        "item": _body_item(body, _page([]), author=body_author, editor=editor, kind="PullRequest"),
    }


def _batch_branch(repo: Path) -> tuple[str, str]:
    """Rebuild ``feature`` as a two-issue batch, ``(#42)`` then ``(#43)``, validated."""
    _git(repo, "reset", "-q", "--hard", "develop")
    _write(repo, "src/x.py", "x = 2\n")
    first = _commit(repo, "the task's change (#42)")
    _write(repo, "src/y.py", "y = 1\n")
    second = _commit(repo, "the other issue's change (#43)")
    _record(repo)
    return first, second


def test_batch_pr_request_carries_every_issues_scope(
    repo: Path,
    capsys: Cap,
    pr_pages: dict[str, Any],
    task_pages: dict[str, Any],
    other_comments: dict[int, list[Any]],
) -> None:
    first, second = _batch_branch(repo)
    pr_pages["pr"] = _open_pr(head_oid=second)
    task_pages["bodies"] = {43: "Do the other thing.\n"}
    other_comments[43] = [
        _page(
            [
                _node(1, body="Only half of it."),
                _node(2, author=AGENT_BOT, body="Agent note."),
                _node(3, body="Edited.", edited=True),
            ]
        )
    ]
    request = _built(_argv(repo, "review", "--pr", "7"), capsys)
    directory = _merge_review_dir(repo)
    assert pr_pages["calls"] == [{"owner": "acme", "name": "widget", "branch": "feature"}]
    assert request["issue_body"] == "Do the thing.\n"
    assert request["batch_issues"] == [
        {
            "issue": "acme/widget#43",
            "issue_body": "Do the other thing.\n",
            "issue_comments": [
                {"author": HUMAN, "created_at": _CREATED, "body": "Only half of it."}
            ],
            "issue_comments_excluded_edited": 1,
        }
    ]
    assert request["batch_commits"] == [
        {"sha": first, "subject": "the task's change (#42)", "issue": ISSUE},
        {"sha": second, "subject": "the other issue's change (#43)", "issue": "acme/widget#43"},
    ]
    assert request["batch_commits_path"] == str(directory / "commits-review.patch")
    # The PR is at the reviewed HEAD, so the fix is empty; its head is the one GitHub reports.
    assert request["pr_head_sha"] == second
    assert request["fix_commits"] == []
    # The batch's other issue is in the request's scope, not an open task of the epic.
    assert request["epic_open_tasks"] == []
    patch = (directory / "commits-review.patch").read_text()
    assert patch.index(f"commit {first}") < patch.index(f"commit {second}")
    assert "Subject: the task's change (#42)" in patch
    assert "+y = 1" in patch.split(f"commit {second}")[1]
    assert "+y = 1" not in patch.split(f"commit {second}")[0]


def test_batch_pr_request_maps_a_fixup_to_its_issue(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    _, second = _batch_branch(repo)
    _write(repo, "src/x.py", "x = 3\n")
    fixup = _commit(repo, "fixup! the task's change (#42)")
    _record(repo)
    # The PR was submitted at the batch's second commit; the fixup is the fix.
    pr_pages["pr"] = _open_pr(head_oid=second)
    request = _built(_argv(repo, "review", "--pr", "https://github.com/Acme/Widget/pull/7"), capsys)
    assert request["batch_commits"][-1] == {
        "sha": fixup,
        "subject": "fixup! the task's change (#42)",
        "issue": ISSUE,
    }
    assert request["pr_head_sha"] == second
    assert request["fix_commits"] == [{"sha": fixup, "subject": "fixup! the task's change (#42)"}]


@pytest.mark.parametrize("body", ["Ref #42\n", "Closes #42\n", "Closes acme/widget#42\n"])
def test_single_issue_pr_request_is_unchanged(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any], body: str
) -> None:
    pr_pages["pr"] = _open_pr(body=body, head_oid=_head(repo))
    plain = _built(_argv(repo), capsys)
    with_pr = _built(_argv(repo, "review", "--pr", "7"), capsys)
    # Only the fix fields are new; a request without --pr carries neither.
    assert set(plain).isdisjoint({"pr_head_sha", "fix_commits"})
    assert set(with_pr) == set(plain) | {"pr_head_sha", "fix_commits"}
    assert with_pr["epic_open_tasks"] == plain["epic_open_tasks"]
    assert not (_merge_review_dir(repo) / "commits-review.patch").exists()


def test_single_issue_pr_at_head_has_no_fix_commits(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=_head(repo))
    request = _built(_argv(repo, "review", "--pr", "7"), capsys)
    assert request["pr_head_sha"] == _head(repo)
    assert request["fix_commits"] == []
    assert "batch_issues" not in request


def test_single_issue_pr_lists_its_fix_commits_oldest_first(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    pr_head = _head(repo)
    _fix(repo, "x = 3\n")
    first = _head(repo)
    _fix(repo, "x = 4\n")
    second = _head(repo)
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=pr_head)
    request = _built(_argv(repo, "review", "--pr", "7"), capsys)
    assert request["pr_head_sha"] == pr_head
    assert request["fix_commits"] == [
        {"sha": first, "subject": "fix"},
        {"sha": second, "subject": "fix"},
    ]
    assert first != second


def test_single_issue_pr_refuses_a_merge_commit_in_its_fix_range(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    pr_head = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side", "develop")
    _write(repo, "src/z.py", "z = 1\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    _record(repo)
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=pr_head)
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert "is a merge commit" in err


def test_pr_head_not_an_ancestor_gives_null_fix_commits(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    # The PR's head is a commit the branch no longer holds: the branch was rebased since submit.
    _git(repo, "checkout", "-q", "-b", "rewritten", "develop")
    _write(repo, "src/y.py", "y = 1\n")
    rewritten = _commit(repo, "the change, rewritten")
    _git(repo, "checkout", "-q", "feature")
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=rewritten)
    request = _built(_argv(repo, "review", "--pr", "7"), capsys)
    assert request["pr_head_sha"] == rewritten
    assert request["fix_commits"] is None


def test_pr_head_this_clone_lacks_refuses(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    missing = "e" * 40
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=missing)
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert out == ""
    assert f"cannot read the PR head {missing[:12]}" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


_NOT_MATCHED = (
    "PR acme/widget#7 is not a same-repo PR with a trusted author whose head is"
    " the current branch 'feature'"
)


@pytest.mark.parametrize(
    ("pr", "message"),
    [
        (_open_pr(state="MERGED"), "PR acme/widget#7 is merged, not open"),
        (_open_pr(head="other"), _NOT_MATCHED),
        (_open_pr(nodes=[]), _NOT_MATCHED),
        (_open_pr(nodes=[_pr_node(8)]), _NOT_MATCHED),
        (_open_pr(body="Ref #41\n"), "PR acme/widget#7 does not link acme/widget#42"),
        (_open_pr(body="No linkage.\n"), "PR acme/widget#7 does not link acme/widget#42"),
        (
            _open_pr(body="Closes #43\nCloses #44\n"),
            "batch PR acme/widget#7 does not close acme/widget#42",
        ),
        (
            _open_pr(body="Closes #42\nCloses acme/other#43\n"),
            "closes acme/other#43, an issue outside acme/widget",
        ),
        (_open_pr(body="Closes #42\nCloses #42\n"), "lists Closes #42 more than once"),
        (
            {"branch_answer": {"repository": None}},
            "cannot read PR acme/widget#7: cannot read who wrote the content"
            " (prs_for_branch acme/widget feature): the repository is missing or not an object",
        ),
        (
            _open_pr(body=None),
            "cannot read PR acme/widget#7: cannot read who wrote the content"
            " (read_issue acme/widget#7): the pr body has no usable body",
        ),
    ],
)
def test_open_pr_refusals(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any], pr: Any, message: str
) -> None:
    pr_pages["pr"] = pr
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert out == ""
    assert message in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


@pytest.mark.parametrize(
    ("node", "skipped"),
    [
        (
            _pr_node(fork="drive-by", author=OUTSIDER, title=SPAM_TEXT),
            f"acme/widget#7 pr drive-by at {_CREATED} on head (cross-repository)",
        ),
        (
            _pr_node(fork="drive-by", author=AGENT_BOT, title=SPAM_TEXT),
            f"acme/widget#7 pr {AGENT} at {_CREATED} on head (cross-repository)",
        ),
        (
            _pr_node(author=OUTSIDER, title=SPAM_TEXT),
            f"acme/widget#7 pr drive-by at {_CREATED} on author (untrusted)",
        ),
        (
            _pr_node(author=IMPOSTOR_USER, title=SPAM_TEXT),
            f"acme/widget#7 pr {AGENT} at {_CREATED} on author (wrong-type)",
        ),
    ],
    ids=["fork-by-an-outsider", "fork-by-the-agents-login", "outsider-author", "impostor-author"],
)
def test_open_pr_never_matches_a_fork_or_an_untrusted_pr(
    repo: Path,
    capsys: Cap,
    pr_pages: dict[str, Any],
    graphql_calls: list[dict[str, object]],
    node: dict[str, Any],
    skipped: str,
) -> None:
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr(body=f"{BATCH_BODY}{SPAM_TEXT}\n", nodes=[node])
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert out == ""
    assert f"skipped untrusted content: {skipped}" in err
    assert _NOT_MATCHED in err
    assert SPAM_TEXT not in err
    # The unmatched PR's body is never read.
    assert all(call["number"] != PR_NUMBER for call in graphql_calls)
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_open_pr_matches_the_same_repo_pr_beside_a_fork_on_the_branch(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    _batch_branch(repo)
    fork = _pr_node(8, fork="drive-by", author=OUTSIDER, title=SPAM_TEXT)
    pr_pages["pr"] = _open_pr(nodes=[fork, _pr_node(head_oid=_head(repo))])
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 0, err
    request = json.loads(Path(out.strip()).read_text())
    assert [entry["issue"] for entry in request["batch_issues"]] == ["acme/widget#43"]
    assert (
        f"skipped untrusted content: acme/widget#8 pr drive-by at {_CREATED}"
        " on head (cross-repository)"
    ) in err
    assert SPAM_TEXT not in out
    assert SPAM_TEXT not in err


@pytest.mark.parametrize(
    ("editor", "who"),
    [
        (OUTSIDER, f"acme/widget#7 edit drive-by at {_EDITED} on body (untrusted)"),
        (IMPOSTOR_USER, f"acme/widget#7 edit {AGENT} at {_EDITED} on body (wrong-type)"),
    ],
    ids=["outsider", "user-named-like-the-agent"],
)
def test_open_pr_closes_from_untrusted_body_ignored(
    repo: Path,
    capsys: Cap,
    pr_pages: dict[str, Any],
    graphql_calls: list[dict[str, object]],
    task_calls: list[Any],
    editor: dict[str, str],
    who: str,
) -> None:
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr(body=f"{BATCH_BODY}Closes #44\n{SPAM_TEXT}\n", editor=editor)
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert out == ""
    assert (
        f"cannot read PR acme/widget#7: stopped before any write: the pr body is untrusted: {who}."
    ) in err
    assert SPAM_TEXT not in err
    assert "Closes #4" not in err
    # No issue the untrusted body names is read, the task's own included.
    assert [call["number"] for call in graphql_calls] == [PR_NUMBER]
    assert task_calls == []
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


@pytest.mark.parametrize(
    ("pr", "message"),
    [
        ("seven", "--pr must be a PR number or"),
        ("0", "--pr must be a PR number or"),
        ("https://github.com/acme/widget/issues/7", "--pr must be a PR number or"),
        ("https://github.com/acme/other/pull/7", "--pr names a PR in acme/other"),
    ],
)
def test_pr_argument_refusals(repo: Path, capsys: Cap, pr: str, message: str) -> None:
    code, _out, err = _run(_argv(repo, "review", "--pr", pr), capsys)
    assert code == 1
    assert message in err


@pytest.mark.parametrize(
    ("subjects", "message"),
    [
        (["the task (#42)", "no ref"], "names no issue of the batch PR"),
        (["the task (#42)", "another issue (#44)"], "names no issue of the batch PR"),
        (["the task (#42)"], "has no commit for acme/widget#43"),
    ],
)
def test_batch_pr_refuses_commits_it_cannot_map(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any], subjects: list[str], message: str
) -> None:
    _git(repo, "reset", "-q", "--hard", "develop")
    for n, subject in enumerate(subjects):
        _write(repo, f"src/f{n}.py", f"f = {n}\n")
        _commit(repo, subject)
    _record(repo)
    pr_pages["pr"] = _open_pr()
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert message in err


def test_batch_pr_refuses_a_merge_commit(repo: Path, capsys: Cap, pr_pages: dict[str, Any]) -> None:
    _batch_branch(repo)
    _git(repo, "checkout", "-q", "-b", "side", "develop")
    _write(repo, "src/z.py", "z = 1\n")
    _commit(repo, "side (#43)")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge (#42)", "side")
    _record(repo)
    pr_pages["pr"] = _open_pr()
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert "is a merge commit" in err


@pytest.mark.parametrize(
    ("failing_kind", "call"),
    [("branch", "prs_for_branch acme/widget feature"), ("body", "read_issue acme/widget#7")],
)
def test_request_refuses_a_failed_pr_read(
    repo: Path,
    capsys: Cap,
    monkeypatch: pytest.MonkeyPatch,
    pr_pages: dict[str, Any],
    failing_kind: str,
    call: str,
) -> None:
    pr_pages["pr"] = _open_pr()
    _failing_on(monkeypatch, failing_kind, RuntimeError("GraphQL errors: [boom]"), number=7)
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert (
        f"cannot read PR acme/widget#7: cannot read who wrote the content ({call}):"
        " the GraphQL response carried errors or no data"
    ) in err


def test_request_refuses_a_pr_without_trusted_authors(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch, pr_pages: dict[str, Any]
) -> None:
    def unconfigured() -> TrustedAuthors:
        msg = "trusted authors are not configured"
        raise TrustedAuthorsNotConfiguredError(msg)

    pr_pages["pr"] = _open_pr()
    monkeypatch.setattr(trust, "load_trusted_authors", unconfigured)
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert "cannot read PR acme/widget#7: trusted authors are not configured" in err


@pytest.mark.parametrize(
    ("failing_kind", "number"),
    [("branch", 7), ("body", 7), ("task", 43)],
    ids=["pr", "pr-body", "other-issue"],
)
def test_open_pr_reports_a_missing_github_token(
    repo: Path,
    capsys: Cap,
    monkeypatch: pytest.MonkeyPatch,
    pr_pages: dict[str, Any],
    failing_kind: str,
    number: int,
) -> None:
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr()
    error = MissingGitHubTokenError(4, ("gh", "api", "graphql"), None, "no token")
    _failing_on(monkeypatch, failing_kind, error, number=number)
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_request.main(_argv(repo, "review", "--pr", "7"))
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("vrg-merge-review-request:")


def test_batch_pr_refuses_an_unreadable_other_issue(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch, pr_pages: dict[str, Any]
) -> None:
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr()
    _failing_on(monkeypatch, "task", RuntimeError("GraphQL errors: [gone]"), number=43)
    code, _out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert "cannot read the issue or its epic: GraphQL errors: [gone]" in err


def test_batch_pr_refuses_an_untrusted_other_issue_body(
    repo: Path,
    capsys: Cap,
    pr_pages: dict[str, Any],
    task_pages: dict[str, Any],
) -> None:
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr()
    task_pages["items"][43] = _body_item(SPAM_TEXT, _page([]), editor=OUTSIDER)
    code, out, err = _run(_argv(repo, "review", "--pr", "7"), capsys)
    assert code == 1
    assert out == ""
    assert (
        "cannot read the issue or its epic: stopped before any write: the issue body is"
        f" untrusted: acme/widget#43 edit drive-by at {_EDITED} on body (untrusted)."
    ) in err
    assert SPAM_TEXT not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (subprocess.CalledProcessError(128, ["git", "rev-list"]), "cannot read the commits"),
        ("not one record", "cannot parse the git rev-list line"),
    ],
)
def test_batch_commits_refuses_an_unreadable_history(
    monkeypatch: pytest.MonkeyPatch, output: object, message: str
) -> None:
    def read_output(*args: str) -> str:
        if isinstance(output, Exception):
            raise output
        return str(output)

    monkeypatch.setattr("vergil_tooling.lib.git.read_output", read_output)
    with pytest.raises(req.RequestError) as exc:
        open_pr.batch_commits("a" * 40, "b" * 40, [req.parse_task_issue(ISSUE)])
    assert message in str(exc.value)


@pytest.mark.parametrize(
    ("ancestor", "output", "message"),
    [
        (subprocess.CalledProcessError(128, ["git", "merge-base"]), "", "cannot read the PR head"),
        (
            True,
            subprocess.CalledProcessError(128, ["git", "rev-list"]),
            "cannot read the fix commits",
        ),
        (True, "not one record", "cannot parse the git rev-list line"),
    ],
    ids=["unknown-head", "unreadable-history", "unparsable-record"],
)
def test_fix_commits_refuses_a_git_failure(
    monkeypatch: pytest.MonkeyPatch, ancestor: object, output: object, message: str
) -> None:
    def is_ancestor(*args: str) -> bool:
        if isinstance(ancestor, Exception):
            raise ancestor
        return bool(ancestor)

    def read_output(*args: str) -> str:
        if isinstance(output, Exception):
            raise output
        return str(output)

    monkeypatch.setattr("vergil_tooling.lib.git.is_ancestor", is_ancestor)
    monkeypatch.setattr("vergil_tooling.lib.git.read_output", read_output)
    with pytest.raises(req.RequestError) as exc:
        open_pr.fix_commits("a" * 40, "b" * 40)
    assert message in str(exc.value)
    assert exc.value.exit_code == 1


def test_fix_commits_is_null_when_the_head_is_not_an_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vergil_tooling.lib.git.is_ancestor", lambda *_args: False)
    assert open_pr.fix_commits("a" * 40, "b" * 40) is None


def test_fix_commits_refuses_a_merge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    merge = "\x1f".join(["c" * 40, "a" * 40 + " " + "d" * 40, "merge side"])
    monkeypatch.setattr("vergil_tooling.lib.git.is_ancestor", lambda *_args: True)
    monkeypatch.setattr("vergil_tooling.lib.git.read_output", lambda *_args: merge)
    with pytest.raises(req.RequestError) as exc:
        open_pr.fix_commits("a" * 40, "b" * 40)
    assert "is a merge commit" in str(exc.value)
    assert exc.value.exit_code == 1


def _batch_reviewed(repo: Path, capsys: Cap, pr_pages: dict[str, Any]) -> tuple[str, str]:
    """A batch PR's review with one defect, and its fix: ``(review head, result path)``."""
    _batch_branch(repo)
    pr_pages["pr"] = _open_pr(head_oid=_head(repo))
    review = _built(_argv(repo, "review", "--pr", "7"), capsys)
    result = _store_result(repo, "review", [_finding("R1")])
    _write(repo, "src/x.py", "x = 3\n")
    _commit(repo, "fixup! the task's change (#42)")
    _record(repo)
    return review["head_sha"], result


def test_batch_pr_check_carries_the_batch(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    review_head, result = _batch_reviewed(repo, capsys, pr_pages)
    argv = [*_check_argv(repo, review_head, [result], fix=("R1",)), "--pr", "7"]
    request = _built(argv, capsys)
    assert [entry["issue"] for entry in request["batch_issues"]] == ["acme/widget#43"]
    assert [c["issue"] for c in request["batch_commits"]] == [ISSUE, "acme/widget#43", ISSUE]
    assert request["batch_commits_path"].endswith("commits-check.patch")
    # The check's fix commits are read from the PR's head as it is now: the fixup is the fix.
    assert request["pr_head_sha"] == review_head
    assert [c["subject"] for c in request["fix_commits"]] == ["fixup! the task's change (#42)"]


def test_check_of_a_single_issue_pr_lists_the_fix_commits_so_far(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    # The PR stays at the reviewed head (the fix is not pushed to it), so the fix is what follows.
    pr_pages["pr"] = _open_pr(body="Ref #42\n", head_oid=_head(repo))
    review = _built(_argv(repo, "review", "--pr", "7"), capsys)
    result = _store_result(repo, "review", [_finding("R1")])
    _fix(repo)
    fixed = _head(repo)
    argv = [*_check_argv(repo, review["head_sha"], [result], fix=("R1",)), "--pr", "7"]
    request = _built(argv, capsys)
    assert request["pr_head_sha"] == review["head_sha"]
    assert request["fix_commits"] == [{"sha": fixed, "subject": "fix"}]


def test_batch_pr_check_refuses_a_dropped_batch(
    repo: Path, capsys: Cap, pr_pages: dict[str, Any]
) -> None:
    review_head, result = _batch_reviewed(repo, capsys, pr_pages)
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "the review request carried the batch issues acme/widget#43, this check none" in err


@pytest.mark.parametrize("content", [None, "{", '{"batch_issues": [1]}', '{"batch_issues": {}}'])
def test_check_same_batch_refuses_an_unreadable_stored_request(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "request-review.json").write_text(content)
    with pytest.raises(req.RequestError) as exc:
        open_pr.check_same_batch(tmp_path, "check", [])
    if content is None or content == "{":
        assert "cannot read the review request" in str(exc.value)
    else:
        assert "has a malformed batch_issues" in str(exc.value)


def test_ensure_dir_refuses_pr(capsys: Cap) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_request.main(["--issue", ISSUE, "--ensure-dir", "--pr", "7"])
    assert exc.value.code == 2


# -- a batch member: its own commit, never a later issue's ----------------------

MEMBER_PATHS = {41: "src/one.py", 42: "src/two.py", 43: "src/three.py"}
# Issue 42, the --issue, first, middle and last in the branch.
MEMBER_ORDERS = [(42, 41, 43), (41, 42, 43), (41, 43, 42)]


def _member_repo(
    repo: Path,
    order: tuple[int, ...] = (41, 42, 43),
    *,
    fixup: bool = False,
    paths: dict[int, str] | None = None,
) -> dict[int, str]:
    """Commit issues 41 to 43 in *order* on a feature branch cut from develop, with their marker.

    The marker lists the three issues and names the develop tip as its
    ``base_sha``. A *fixup* of issue 42 follows its commit. Returns each
    issue's own commit, by issue number.
    """
    files = {**MEMBER_PATHS, **(paths or {})}
    _git(repo, "checkout", "-q", "-B", "feature", "develop")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "develop")
    develop = _git(repo, "rev-parse", "develop")
    batch_marker.write_markers(repo, ["#41", "#42", "#43"], base_sha=develop)
    owns: dict[int, str] = {}
    for number in order:
        _write(repo, files[number], f"{number}\n")
        owns[number] = _commit(repo, f"the issue {number} (#{number})")
        if fixup and number == 42:
            _write(repo, files[42], "42, fixed\n")
            _commit(repo, "fixup! the issue 42 (#42)")
    _record(repo)
    return owns


def _sha_of_subject(repo: Path, subject: str) -> str:
    """The sha of the one commit of ``origin/develop..HEAD`` whose subject is *subject*."""
    lines = _git(repo, "log", "--format=%H%x1f%s", "origin/develop..HEAD").splitlines()
    found = [line.split("\x1f", 1)[0] for line in lines if line.split("\x1f", 1)[1] == subject]
    assert len(found) == 1, found
    return found[0]


def _develop_moves_on(repo: Path) -> None:
    """Commit on develop and move ``origin/develop`` to it; the feature branch is left alone."""
    _git(repo, "checkout", "-q", "develop")
    _write(repo, "README.md", "# moved on\n")
    _commit(repo, "develop moves on")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "develop")
    _git(repo, "checkout", "-q", "feature")


def _diff_of(request: dict[str, Any]) -> str:
    return Path(request["diff_path"]).read_text()


@pytest.mark.parametrize("order", MEMBER_ORDERS)
@pytest.mark.parametrize("fixup", [False, True])
def test_member_review_holds_only_its_own_change(
    repo: Path, capsys: Cap, order: tuple[int, ...], fixup: bool
) -> None:
    owns = _member_repo(repo, order, fixup=fixup)
    request = _built(_argv(repo), capsys)
    diff = _diff_of(request)
    assert "src/two.py" in diff
    assert ("+42, fixed" in diff) is fixup
    assert "src/one.py" not in diff
    assert "src/three.py" not in diff
    # The parent of issue 42's own commit: the develop tip, or the commit before it.
    assert request["base_sha"] == _git(repo, "rev-parse", f"{owns[42]}^")
    assert request["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert request["change_class"] == "code"


@pytest.mark.parametrize("order", MEMBER_ORDERS)
@pytest.mark.parametrize("fixup", [False, True])
def test_member_review_after_a_clean_rebase(
    repo: Path, capsys: Cap, order: tuple[int, ...], fixup: bool
) -> None:
    _member_repo(repo, order, fixup=fixup)
    stale = batch_marker.read_marker(repo, "#42")
    assert stale is not None
    _develop_moves_on(repo)
    _git(repo, "rebase", "-q", "origin/develop")
    _record(repo)
    request = _built(_argv(repo), capsys)
    own = _sha_of_subject(repo, "the issue 42 (#42)")
    diff = _diff_of(request)
    assert "src/two.py" in diff
    assert ("+42, fixed" in diff) is fixup
    for path in ("src/one.py", "src/three.py", "README.md"):
        assert path not in diff
    # The marker's base is the develop tip from before the rebase: never used.
    assert request["base_sha"] == _git(repo, "rev-parse", f"{own}^")
    assert request["base_sha"] != stale.base_sha


def test_member_review_classes_its_own_change(repo: Path, capsys: Cap) -> None:
    _member_repo(repo, paths={42: "docs/guide/two.md"})
    request = _built(_argv(repo), capsys)
    assert request["change_class"] == "docs-only"
    diff = _diff_of(request)
    assert "docs/guide/two.md" in diff
    assert "src/three.py" not in diff


def test_member_review_refuses_a_fixup_behind_another_commit(repo: Path, capsys: Cap) -> None:
    _member_repo(repo, (41, 42, 43))
    _write(repo, MEMBER_PATHS[42], "42, fixed\n")
    _commit(repo, "fixup! the issue 42 (#42)")
    _record(repo)
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "fold it into its commit first" in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_member_review_refuses_an_unreadable_marker(repo: Path, capsys: Cap) -> None:
    _member_repo(repo)
    batch_marker.marker_path(repo, "#42").write_text("{")
    code, out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert out == ""
    assert "is not valid JSON" in err
