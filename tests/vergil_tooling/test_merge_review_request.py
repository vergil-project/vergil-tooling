"""Tests for the merge-review request builder, ``vrg-merge-review-request``."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.bin import vrg_merge_review_request
from vergil_tooling.lib.github import GitHubAPIError, MissingGitHubTokenError
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.result import parse_result
from vergil_tooling.lib.progress import StageResult
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.validate_record import write_record

Cap = pytest.CaptureFixture[str]

ISSUE = "acme/widget#42"
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
    """The variables of every GraphQL call the fake received, in order."""
    return []


@pytest.fixture
def comment_pages(
    monkeypatch: pytest.MonkeyPatch, graphql_calls: list[dict[str, object]]
) -> list[Any]:
    """The issue's comment pages, served in turn; an empty page once they run out.

    Also configures the trusted authors with one human login, ``HUMAN``.
    """
    pages: list[Any] = []

    def graphql(query: str, **variables: object) -> Any:
        assert "createdAt" in query
        graphql_calls.append(variables)
        return pages.pop(0) if pages else _page([])

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", graphql)
    authors = TrustedAuthors(human=(HUMAN,), agents=(AGENT, f"{HUMAN}-vergil-audit"))
    monkeypatch.setattr(req, "load_trusted_authors", lambda: authors)
    return pages


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
    (tmp_path / "body.md").write_text("Do the thing.\n")
    tasks = [{"ref": "acme/widget#43", "title": "The next task"}]
    (tmp_path / "tasks.json").write_text(json.dumps(tasks))
    monkeypatch.chdir(path)
    return path


def _merge_review_dir(repo: Path) -> Path:
    return repo / "build" / ".vergil-state" / "issue-implement" / "42" / "merge-review"


def _argv(repo: Path, step: str = "review", *extra: str, base: str = "develop") -> list[str]:
    files = repo.parent
    return [
        "--issue",
        ISSUE,
        "--base",
        base,
        "--step",
        step,
        "--issue-body-file",
        str(files / "body.md"),
        "--epic-tasks-file",
        str(files / "tasks.json"),
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
    return str(path)


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


def test_request_refuses_unreadable_issue_body(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    (tmp_path / "body.md").unlink()
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue body file" in err


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "cannot read the epic tasks file"),
        ('{"ref": "acme/widget#1"}', "must hold a JSON list"),
        ('[{"ref": "acme/widget#1"}]', "entry 1 must be {ref, title} strings"),
        ('[{"ref": 1, "title": "t"}]', "entry 1 must be {ref, title} strings"),
        ('[{"ref": "acme/widget#1", "title": 2}]', "entry 1 must be {ref, title} strings"),
        ('["acme/widget#1"]', "entry 1 must be {ref, title} strings"),
        ('[{"ref": "#1", "title": "t"}]', "entry 1's ref must be owner/name#N"),
    ],
)
def test_request_refuses_bad_epic_tasks(
    repo: Path, tmp_path: Path, capsys: Cap, content: str, message: str
) -> None:
    (tmp_path / "tasks.json").write_text(content)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert message in err


_CREATED = "2026-11-01T09:30:00Z"


def _node(
    comment_id: int,
    login: str | None = HUMAN,
    body: str = "text",
    *,
    edited: bool = False,
    created: Any = _CREATED,
) -> dict[str, Any]:
    return {
        "fullDatabaseId": str(comment_id),
        "author": None if login is None else {"login": login},
        "body": body,
        "createdAt": created,
        "lastEditedAt": "2026-11-03T00:00:00Z" if edited else None,
    }


def _page(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return {"repository": {"issue": {"comments": {"pageInfo": info, "nodes": nodes}}}}


def test_request_carries_only_the_humans_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append(
        _page(
            [
                _node(1, body="Scope: gap 1 only; gap 2 is a later task."),
                _node(2, login=f"app/{AGENT}", body="Noted."),
                _node(3, login="stranger", body="Also do gap 3."),
                _node(4, login=None, body="From a deleted account."),
                _node(5, login=f"{HUMAN}-vergil-audit", body="Audit."),
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


def test_request_asks_for_the_task_issue(
    repo: Path, capsys: Cap, comment_pages: list[Any], graphql_calls: list[dict[str, object]]
) -> None:
    _built(_argv(repo), capsys)
    assert graphql_calls == [{"owner": "acme", "name": "widget", "number": 42}]


def test_request_leaves_out_and_counts_edited_human_comments(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append(
        _page(
            [
                _node(1, body="Scope as first written.", edited=True),
                _node(2, body="Kept."),
                _node(3, login=AGENT, body="An edited agent comment.", edited=True),
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


def test_request_refuses_an_unreadable_comment_stream(
    repo: Path, capsys: Cap, comment_pages: list[Any]
) -> None:
    comment_pages.append({"repository": {"issue": None}})
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue's comments: acme/widget#42: issue not found" in err


def test_request_refuses_a_graphql_error(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(query: str, **variables: object) -> Any:
        msg = "GraphQL errors: [boom]"
        raise RuntimeError(msg)

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", failing)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue's comments: GraphQL errors" in err


def test_request_refuses_a_github_api_error(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(query: str, **variables: object) -> Any:
        raise GitHubAPIError(1, ["gh", "api", "graphql"], output="", stderr="HTTP 502")

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", failing)
    code, _out, err = _run(_argv(repo), capsys)
    assert code == 1
    assert "cannot read the issue's comments:" in err
    assert "git failed" not in err


def test_request_reports_a_missing_github_token(
    repo: Path, capsys: Cap, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(query: str, **variables: object) -> Any:
        raise MissingGitHubTokenError(4, ("gh", "api", "graphql"), None, "no token")

    monkeypatch.setattr("vergil_tooling.lib.github.graphql", failing)
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_request.main(_argv(repo))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-merge-review-request:")
    assert "git failed" not in err
    assert not (_merge_review_dir(repo) / "request-review.json").exists()


def test_request_accepts_no_epic_tasks(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    (tmp_path / "tasks.json").write_text("[]")
    assert _built(_argv(repo), capsys)["epic_open_tasks"] == []


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

_BUILDING = ["--base", "d", "--issue-body-file", "b", "--epic-tasks-file", "t"]


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
    code, _out, err = _run(_check_argv(repo, review_head, [result], fix=("R1",)), capsys)
    assert code == 1
    assert "the review result" in err
    assert "fails the result check: rule 1 (shape)" in err


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
