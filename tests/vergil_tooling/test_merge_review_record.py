"""Tests for the merge-review record (``lib.merge_review.record``, ``vrg-merge-review-close``)."""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin import vrg_merge_review_close
from vergil_tooling.lib import github, identity_mode, trust
from vergil_tooling.lib.change_fingerprint import change_fingerprint, commit_fingerprint
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.merge_review import record as record_module
from vergil_tooling.lib.merge_review.record import (
    NOT_ELIGIBLE_REASONS,
    RECORD_PREFIX,
    Record,
    RecordError,
    batch_member_change,
    check_outcome,
    closing_body,
    covered_change,
    parse_record,
    records,
    render_record,
)
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

HEAD = "a" * 40
LINE = f"Merge review record: head={HEAD} fingerprint={'b' * 64} outcome=eligible"
FP = "b" * 64
AUTHORS = TrustedAuthors(human=("human",), agents=("human-vergil-user",))
AGENT: dict[str, str] = {"__typename": "Bot", "login": "human-vergil-user"}
IMPOSTOR: dict[str, str] = {"__typename": "User", "login": "human-vergil-user"}
KEY = ("o/r", 7)

ELIGIBLE = "Merge review: done: eligible; accepted-risk 0"
ITEMS = "\n- R1: fixed\n- R2: kept by ruling\n"


def _closing(outcome: str = "eligible", head: str = HEAD, fp: str = FP) -> str:
    return (
        f"Merge review: done: {outcome}; accepted-risk 0\n"
        f"{render_record(head, fp, outcome)}\n{ITEMS}"
    )


# --- render and parse --------------------------------------------------------


@pytest.mark.parametrize(
    "outcome", ["eligible", *(f"not-eligible ({reason})" for reason in NOT_ELIGIBLE_REASONS)]
)
def test_record_round_trip(outcome: str) -> None:
    line = render_record(HEAD, FP, outcome)
    assert line == f"{RECORD_PREFIX} head={HEAD} fingerprint={FP} outcome={outcome}"
    assert parse_record(_closing(outcome)) == Record(HEAD, FP, outcome)
    with_branch = (
        f"Merge review: done: {outcome}; accepted-risk 2\nBranch as reviewed: no\n{line}\n{ITEMS}"
    )
    assert parse_record(with_branch) == Record(HEAD, FP, outcome)


@pytest.mark.parametrize(
    "outcome",
    [
        "stopped",
        "stopped: request 41",
        "waiting",
        "waiting: non-review items",
        "fatal",
        "error",
        "not-eligible",
        "not-eligible (because)",
        "not-eligible (human-ruling) ",
        "Eligible",
        "",
    ],
)
def test_incomplete_outcome_is_refused(outcome: str) -> None:
    with pytest.raises(RecordError, match="has no record"):
        check_outcome(outcome)
    with pytest.raises(RecordError):
        render_record(HEAD, FP, outcome)


@pytest.mark.parametrize(
    ("head", "fp"),
    [("a" * 39, FP), ("A" * 40, FP), (HEAD, "b" * 63), (HEAD, "B" * 64), ("HEAD", FP)],
)
def test_render_refuses_a_bad_head_or_fingerprint(head: str, fp: str) -> None:
    with pytest.raises(RecordError):
        render_record(head, fp, "eligible")


@pytest.mark.parametrize(
    "body",
    [
        # Not a closing comment: a step comment quoting a record in the reviewer's reply.
        f"Merge review: review: pass; defect 0\n\nRequest: x\n{LINE}",
        # The record line out of its place.
        f"{ELIGIBLE}\n\n- R1: fixed\n{render_record(HEAD, FP, 'eligible')}",
        # An outcome other than the eligibility line's.
        f"{ELIGIBLE}\n{render_record(HEAD, FP, 'not-eligible (human-ruling)')}",
        # Two record lines.
        f"{_closing()}{render_record(HEAD, FP, 'eligible')}\n",
        # A malformed record line.
        f"{ELIGIBLE}\n{RECORD_PREFIX} head={HEAD} fingerprint={FP}",
        f"{ELIGIBLE}\n{RECORD_PREFIX} head={HEAD} fingerprint={FP} outcome=eligible extra",
        # No record at all.
        f"{ELIGIBLE}\n{ITEMS}",
        ELIGIBLE,
        "",
    ],
)
def test_parse_record_refuses_a_record_out_of_place(body: str) -> None:
    assert parse_record(body) is None


# --- closing_body --------------------------------------------------------------


def test_closing_body_keeps_every_line_and_places_the_record() -> None:
    line = render_record(HEAD, FP, "eligible")
    assert closing_body(f"{ELIGIBLE}\n{ITEMS}", "eligible", line) == f"{ELIGIBLE}\n{line}\n{ITEMS}"
    branch = (
        "Merge review: done: not-eligible (human-ruling); accepted-risk 1\nBranch as reviewed: yes"
    )
    line = render_record(HEAD, FP, "not-eligible (human-ruling)")
    assert closing_body(f"{branch}\n{ITEMS}", "not-eligible (human-ruling)", line) == (
        f"{branch}\n{line}\n{ITEMS}"
    )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (f"- R1: fixed\n{ELIGIBLE}\n", "first line must be the eligibility line"),
        ("Merge review: done: eligible\n", "first line must be the eligibility line"),
        (
            "Merge review: done: not-eligible (check-failed); accepted-risk 0\n",
            "not --outcome 'eligible'",
        ),
        (f"{ELIGIBLE}\n\n{RECORD_PREFIX} head=x\n", "already has a line"),
        (f"{ELIGIBLE}\n" + "x" * 70000, "over 65536 bytes"),
    ],
)
def test_closing_body_refuses(body: str, match: str) -> None:
    with pytest.raises(RecordError, match=match):
        closing_body(body, "eligible", render_record(HEAD, FP, "eligible"))


# --- records -------------------------------------------------------------------


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> trust_stubs.FakeItems:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    return fake


def test_records_ignore_non_agent_authors(items: trust_stubs.FakeItems) -> None:
    body = _closing()
    items.items[KEY] = trust_stubs.item(
        nodes=[
            trust_stubs.comment(1, body, trust_stubs.HUMAN),
            trust_stubs.comment(2, body, trust_stubs.OUTSIDER),
            trust_stubs.comment(3, body, IMPOSTOR),
            trust_stubs.comment(4, body, {"__typename": "Bot", "login": "o-automation"}),
            trust_stubs.comment(5, body, AGENT, editor=AGENT),
            trust_stubs.comment(6, body, AGENT, editor=trust_stubs.HUMAN),
            trust_stubs.comment(7, f"{ELIGIBLE}\n{ITEMS}", AGENT),
            trust_stubs.comment(8, body, AGENT),
        ]
    )
    assert records("o/r#7", authors=AUTHORS) == [
        Record(HEAD, FP, "eligible", comment_id=8, author="human-vergil-user")
    ]


def test_records_narrow_the_stream_to_the_agent_role(
    items: trust_stubs.FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[trust.Role]] = []
    real = trust.read_comment_stream

    def spy(repo: str, number: int, **kwargs: Any) -> trust.CommentStream:
        seen.append(kwargs["roles"])
        return real(repo, number, **kwargs)

    monkeypatch.setattr(trust, "read_comment_stream", spy)
    items.items[KEY] = trust_stubs.item(nodes=[trust_stubs.comment(1, _closing(), AGENT)])
    assert len(records("o/r#7", authors=AUTHORS)) == 1
    assert seen == [frozenset({trust.Role.AGENTS})]


@pytest.mark.parametrize("answer", [{}, {"repository": None}])
def test_records_read_failure_raises(items: trust_stubs.FakeItems, answer: dict[str, Any]) -> None:
    items.items[KEY] = answer
    with pytest.raises(trust.TrustReadError):
        records("o/r#7", authors=AUTHORS)


def test_records_gh_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_query: str, **_variables: Any) -> Any:
        raise github.GitHubAPIError(1, ("gh", "api"), None, "HTTP 502")

    monkeypatch.setattr(github, "graphql", boom)
    with pytest.raises(trust.TrustReadError, match="HTTP 502"):
        records("o/r#7", authors=AUTHORS)


# --- vrg-merge-review-close ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of o/r on ``feature``, with ``origin/develop`` at its one base commit."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    (path / ".gitignore").write_text("build/\nbody.md\n")
    base = _commit(path, "a.txt", "one\ntwo\nthree\n", "base")
    _git(path, "update-ref", "refs/remotes/origin/develop", base)
    _git(path, "remote", "add", "origin", "https://github.com/o/r.git")
    _git(path, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(path)
    return path


class Poster:
    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []

    def __call__(self, path: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        del timeout
        self.posted.append((path, payload["body"]))
        return {"id": 100}


@pytest.fixture
def poster(items: trust_stubs.FakeItems, monkeypatch: pytest.MonkeyPatch) -> Poster:
    items.items.setdefault(KEY, trust_stubs.item())
    fake = Poster()
    monkeypatch.setattr(github, "post_json_once", fake)
    monkeypatch.setattr(vrg_merge_review_close, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.USER)

    @contextlib.contextmanager
    def no_org(_org: str | None) -> Generator[None]:
        yield

    monkeypatch.setattr(github, "target_org", no_org)
    return fake


def _close(repo: Path, body: str, outcome: str = "eligible", issue: str = "o/r#7") -> int:
    (repo / "body.md").write_text(body)
    return vrg_merge_review_close.main(
        ["--issue", issue, "--outcome", outcome, "--body-file", str(repo / "body.md")]
    )


def _posted_record(poster: Poster) -> Record:
    assert len(poster.posted) == 1
    path, body = poster.posted[0]
    assert path == "repos/o/r/issues/7/comments"
    found = parse_record(body)
    assert found is not None
    return found


@pytest.mark.parametrize("flag", ["--fingerprint", "--head"])
def test_close_computes_fingerprint_not_caller(
    repo: Path, poster: Poster, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    with pytest.raises(SystemExit) as exc:
        vrg_merge_review_close.main(
            ["--issue", "o/r#7", "--outcome", "eligible", "--body-file", "b", flag, FP]
        )
    assert exc.value.code == 2
    assert poster.posted == []
    capsys.readouterr()

    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 0
    assert capsys.readouterr().out == "100\n"
    recorded = _posted_record(poster)
    assert recorded == Record(head, change_fingerprint(repo, "origin/develop", head), "eligible")
    assert poster.posted[0][1] == (
        f"{ELIGIBLE}\n{render_record(head, recorded.fingerprint, 'eligible')}\n{ITEMS}"
    )


def test_close_record_survives_a_clean_rebase(repo: Path, poster: Poster) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 0
    recorded = _posted_record(poster).fingerprint
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, "b.txt", "elsewhere\n", "develop moves")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "origin/develop")
    assert change_fingerprint(repo, "origin/develop", "HEAD") == recorded


def test_close_keeps_the_branch_line_above_the_record(repo: Path, poster: Poster) -> None:
    head = _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    outcome = "not-eligible (human-ruling)"
    top = f"Merge review: done: {outcome}; accepted-risk 0\nBranch as reviewed: yes"
    assert _close(repo, f"{top}\n{ITEMS}", outcome) == 0
    body = poster.posted[0][1]
    assert body.split("\n")[:3] == [*top.split("\n"), body.split("\n")[2]]
    assert _posted_record(poster).head == head


@pytest.mark.parametrize("outcome", ["stopped", "waiting", "fatal", "error"])
def test_close_refuses_an_incomplete_outcome(
    repo: Path, poster: Poster, outcome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    assert _close(repo, f"Merge review: done: {outcome}; accepted-risk 0\n", outcome) == 1
    assert "has no record" in capsys.readouterr().err
    assert poster.posted == []


def test_close_refuses_a_dirty_worktree(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    (repo / "a.txt").write_text("uncommitted\n")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "not clean" in capsys.readouterr().err
    assert poster.posted == []


def test_close_refuses_an_empty_change(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "empty" in capsys.readouterr().err
    assert poster.posted == []


def test_close_refuses_another_repos_issue(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}", issue="o/other#7") == 1
    assert "o/other" in capsys.readouterr().err
    assert poster.posted == []


def test_close_refuses_outside_user_mode(
    repo: Path, poster: Poster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.AUDIT)
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "'user' identity mode" in capsys.readouterr().err
    assert poster.posted == []


def test_close_rerun_posts_nothing(
    repo: Path,
    poster: Poster,
    items: trust_stubs.FakeItems,
    capsys: pytest.CaptureFixture[str],
) -> None:
    head = _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    fp = change_fingerprint(repo, "origin/develop", head)
    items.items[KEY] = trust_stubs.item(
        nodes=[
            trust_stubs.comment(
                55, f"{ELIGIBLE}\n{render_record(head, fp, 'eligible')}\n{ITEMS}", AGENT
            )
        ]
    )
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 0
    captured = capsys.readouterr()
    assert captured.out == "55\n"
    assert "already posted" in captured.err
    assert poster.posted == []


def test_close_read_failure_posts_nothing(
    repo: Path,
    poster: Poster,
    items: trust_stubs.FakeItems,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    items.items[KEY] = {}
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert capsys.readouterr().err.startswith("vrg-merge-review-close: ")
    assert poster.posted == []


# --- a batch member ------------------------------------------------------------


def _batch(repo: Path) -> str:
    base = _git(repo, "rev-parse", "HEAD")
    batch_marker.write_markers(repo, ["#6", "#7"], base_sha=base)
    _commit(repo, "a.txt", "ONE\ntwo\nthree\n", "the first issue (#6)")
    return base


def test_close_batch_member_records_its_own_commit(repo: Path, poster: Poster) -> None:
    _batch(repo)
    previous = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "c.txt", "seven\n", "the second issue (#7)")
    head = _commit(repo, "c.txt", "seven, fixed\n", "fixup! the second issue (#7)")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 0
    found = _posted_record(poster)
    assert found.head == head
    assert found.fingerprint == change_fingerprint(repo, previous, head)
    assert found.fingerprint != change_fingerprint(repo, "origin/develop", head)
    # Folded, the issue's one commit has the recorded fingerprint.
    _git(repo, "-c", "sequence.editor=true", "rebase", "-q", "-i", "--autosquash", previous)
    assert _git(repo, "log", "--format=%s", f"{previous}..HEAD") == "the second issue (#7)"
    assert commit_fingerprint(repo, "HEAD") == found.fingerprint


def test_close_first_batch_member_uses_the_base(
    repo: Path, poster: Poster, items: trust_stubs.FakeItems
) -> None:
    items.items[("o/r", 6)] = trust_stubs.item()
    base = _batch(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}", issue="o/r#6") == 0
    _path, body = poster.posted[0]
    found = parse_record(body)
    assert found is not None
    assert found.fingerprint == commit_fingerprint(repo, head)
    assert found.fingerprint == change_fingerprint(repo, base, head)


@pytest.mark.parametrize(
    ("issue", "subjects", "match"),
    [
        # A later issue's commit is on top of the member's.
        ("o/r#6", ["the second issue (#7)"], "not a fixup"),
        # The member's first commit is missing.
        ("o/r#7", [], "no commit for #7"),
        # A fixup with no first commit.
        ("o/r#7", ["fixup! the second issue (#7)"], "not #7's first commit"),
        # A commit naming another issue.
        ("o/r#7", ["something else (#9)"], "not #7's first commit"),
        # A stray commit after the member's.
        ("o/r#7", ["the second issue (#7)", "a stray commit"], "not a fixup"),
    ],
)
def test_close_batch_refuses_a_branch_out_of_shape(
    repo: Path,
    poster: Poster,
    capsys: pytest.CaptureFixture[str],
    issue: str,
    subjects: list[str],
    match: str,
) -> None:
    _batch(repo)
    for n, subject in enumerate(subjects):
        _commit(repo, f"f{n}.txt", f"{n}\n", subject)
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}", issue=issue) == 1
    assert match in capsys.readouterr().err
    assert poster.posted == []


def test_close_batch_refuses_without_the_previous_issues_commit(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    batch_marker.write_markers(repo, ["#6", "#7"], base_sha=base)
    _commit(repo, "c.txt", "seven\n", "the second issue (#7)")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "previous issue #6" in capsys.readouterr().err
    assert poster.posted == []


def test_close_batch_refuses_a_merge_commit(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    _batch(repo)
    _git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    _commit(repo, "side.txt", "side\n", "a side line")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "--no-ff", "side")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "is a merge commit" in capsys.readouterr().err
    assert poster.posted == []


def test_close_refuses_an_unreadable_batch_marker(
    repo: Path, poster: Poster, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    batch_marker.write_markers(repo, ["#6", "#7"], base_sha=_git(repo, "rev-parse", "HEAD"))
    batch_marker.marker_path(repo, "#7").write_text("not json")
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert poster.posted == []


def test_batch_member_change_refuses_an_unreadable_range(repo: Path) -> None:
    marker = batch_marker.Marker(issues=["#6", "#7"], base_sha="0" * 40)
    with pytest.raises(RecordError, match="cannot read the commits"):
        batch_member_change(repo, marker, "#7", _git(repo, "rev-parse", "HEAD"))


def test_batch_member_change_refuses_unparsable_git_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_module.git, "read_output", lambda *_args: "garbage")
    marker = batch_marker.Marker(issues=["#6", "#7"], base_sha="0" * 40)
    with pytest.raises(RecordError, match="cannot parse the git rev-list line"):
        batch_member_change(repo, marker, "#7", "HEAD")


def test_covered_change_refuses_outside_a_repo(tmp_path: Path) -> None:
    with pytest.raises(RecordError, match="cannot read HEAD"):
        covered_change(tmp_path, 7, main_root=tmp_path)


def test_close_posts_past_another_agent_comment(
    repo: Path, poster: Poster, items: trust_stubs.FakeItems
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")
    items.items[KEY] = trust_stubs.item(nodes=[trust_stubs.comment(5, "an older note", AGENT)])
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 0
    assert len(poster.posted) == 1


def test_close_reports_a_git_failure(
    repo: Path, poster: Poster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> str:
        raise subprocess.CalledProcessError(128, ["git", "status"])

    monkeypatch.setattr(vrg_merge_review_close.git, "working_tree_status", boom)
    assert _close(repo, f"{ELIGIBLE}\n{ITEMS}") == 1
    assert "git failed (exit 128): git status" in capsys.readouterr().err
    assert poster.posted == []


def test_close_leaves_a_missing_token_to_the_cli_boundary(
    repo: Path, poster: Poster, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "a.txt", "one\nTWO\nthree\n", "the change")

    def no_token(*_args: Any, **_kwargs: Any) -> Any:
        raise github.MissingGitHubTokenError(4, ("gh", "api"), "", "set GH_TOKEN")

    monkeypatch.setattr(github, "graphql", no_token)
    with pytest.raises(SystemExit) as exc:
        _close(repo, f"{ELIGIBLE}\n{ITEMS}")
    assert exc.value.code == 1
    assert poster.posted == []
