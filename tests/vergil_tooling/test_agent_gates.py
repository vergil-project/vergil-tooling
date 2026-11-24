"""Tests for the shared gate evaluator (``lib.agent_gates``, epic .github#1384 Task 8)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import agent_gates, github, trust
from vergil_tooling.lib.agent_gates import (
    GateError,
    Override,
    agent_involved,
    evaluate,
    overrides,
    parse_override,
    render_override,
)
from vergil_tooling.lib.change_fingerprint import change_fingerprint, commit_fingerprint
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.merge_review.record import render_record
from vergil_tooling.lib.protected_paths import ProtectedChange
from vergil_tooling.lib.rulings.approvals import (
    render_exemption_request,
    render_protected_change_request,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from pathlib import Path

AUTHORS = TrustedAuthors(human=("human",), agents=("human-vergil-user",))
HUMAN: dict[str, str] = {"__typename": "User", "login": "human"}
AGENT: dict[str, str] = {"__typename": "Bot", "login": "human-vergil-user"}
IMPOSTOR: dict[str, str] = {"__typename": "User", "login": "human-vergil-user"}
OUTSIDER: dict[str, str] = {"__typename": "User", "login": "drive-by"}
URL = "https://github.com/o/r/compare/1111...2222"
APPROVE = "Looked at it.\nDecision: approve"
REFUSE = "Decision: refuse"
REF = "o/r#7"
WORKFLOW = ".github/workflows/ci.yml"


# --- a repository -----------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _blob(repo: Path, path: str) -> str:
    return _git(repo, "rev-parse", f"HEAD:{path}")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on ``feature``, forked from ``develop`` with a workflow and two files."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _commit(
        path,
        {
            "a.txt": "one\ntwo\nthree\n",
            "b.txt": "uno\ndos\n",
            WORKFLOW: "on: push\n",
            "vergil.toml": "[project]\n",
        },
        "base",
    )
    _git(path, "checkout", "-q", "-b", "feature")
    return path


# --- the comment stream -------------------------------------------------------------


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> trust_stubs.FakeItems:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    return fake


class Thread:
    """The comments of the issues under test, numbered across all of them."""

    def __init__(self, items: trust_stubs.FakeItems) -> None:
        self.items = items
        self.nodes: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.next_id = 100

    def add(self, ref: str, body: str, author: dict[str, str], *, editor: Any = None) -> None:
        repo, _, number = ref.partition("#")
        key = (repo, int(number))
        self.next_id += 1
        self.nodes.setdefault(key, []).append(
            trust_stubs.comment(self.next_id, body, author, editor=editor)
        )
        self.items.items[key] = trust_stubs.item(nodes=self.nodes[key])

    def empty(self, *refs: str) -> None:
        for ref in refs:
            repo, _, number = ref.partition("#")
            key = (repo, int(number))
            self.items.items.setdefault(key, trust_stubs.item(nodes=[]))

    def record(self, ref: str, head: str, fingerprint: str, outcome: str = "eligible") -> None:
        body = (
            f"Merge review: done: {outcome}; accepted-risk 0\n"
            f"{render_record(head, fingerprint, outcome)}\n\n- R1: fixed\n"
        )
        self.add(ref, body, AGENT)

    def approve(self, ref: str, changes: list[ProtectedChange], reply: str = APPROVE) -> None:
        self.add(ref, render_protected_change_request(changes, URL), AGENT)
        self.add(ref, reply, HUMAN)

    def exempt(self, ref: str, fingerprint: str) -> None:
        self.add(ref, render_exemption_request("a typo fix", fingerprint), AGENT)
        self.add(ref, APPROVE, HUMAN)

    def override(self, ref: str, fingerprint: str, author: dict[str, str] = HUMAN) -> None:
        self.add(ref, render_override(fingerprint, "no agent session"), author)


@pytest.fixture
def thread(items: trust_stubs.FakeItems) -> Thread:
    return Thread(items)


def _evaluate(repo: Path, refs: list[str] | None = None) -> agent_gates.GateResult:
    return evaluate(repo, base="develop", head="HEAD", issue_refs=refs or [REF], authors=AUTHORS)


def _fp(repo: Path) -> str:
    return change_fingerprint(repo, "develop", "HEAD")


# --- the merge-review rule ------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["eligible", "not-eligible (human-ruling)"])
def test_not_eligible_record_passes(repo: Path, thread: Thread, outcome: str) -> None:
    head = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.record(REF, head, _fp(repo), outcome)
    assert _evaluate(repo) == agent_gates.GateResult(ok=True, reasons=())


def test_missing_or_stale_record_fails(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.empty(REF)
    result = _evaluate(repo)
    assert not result.ok
    assert len(result.reasons) == 1
    assert "no merge-review record" in result.reasons[0]
    assert not result.read_failure
    thread.record(REF, head, _fp(repo))
    _commit(repo, {"b.txt": "uno\nDOS\n"}, "a later push")
    result = _evaluate(repo)
    assert not result.ok
    assert _fp(repo) in result.reasons[0]


def test_record_survives_a_clean_rebase(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.record(REF, head, _fp(repo))
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"b.txt": "uno\ndos\ntres\n"}, "develop moves on")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    assert _git(repo, "rev-parse", "HEAD") != head
    assert _evaluate(repo).ok


def test_records_by_others_do_not_cover(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    body = (
        "Merge review: done: eligible; accepted-risk 0\n"
        f"{render_record(head, _fp(repo), 'eligible')}\n"
    )
    thread.add(REF, body, HUMAN)
    thread.add(REF, body, IMPOSTOR)
    thread.add(REF, body, AGENT, editor=AGENT)
    assert not _evaluate(repo).ok


def test_exemption_stands_in_for_record_but_not_for_approval(repo: Path, thread: Thread) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.exempt(REF, _fp(repo))
    assert _evaluate(repo).ok

    _commit(repo, {WORKFLOW: "on: pull_request\n"}, "edit the workflow")
    thread.exempt(REF, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert len(result.reasons) == 1
    assert f"protected file {WORKFLOW}" in result.reasons[0]


def test_refused_exemption_does_not_cover(repo: Path, thread: Thread) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.add(REF, render_exemption_request("a typo fix", _fp(repo)), AGENT)
    thread.add(REF, REFUSE, HUMAN)
    assert not _evaluate(repo).ok


def test_override_stands_in_for_record_only(repo: Path, thread: Thread) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.override(REF, _fp(repo))
    assert _evaluate(repo).ok
    _commit(repo, {"vergil.toml": "[project]\nname = 'x'\n"}, "edit vergil.toml")
    thread.override(REF, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert "protected file vergil.toml" in result.reasons[0]


def test_override_by_non_human_role_ignored(repo: Path, thread: Thread) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fingerprint = _fp(repo)
    thread.override(REF, fingerprint, AGENT)
    thread.override(REF, fingerprint, OUTSIDER)
    thread.override(REF, fingerprint, {"__typename": "Bot", "login": "human"})
    thread.add(REF, render_override(fingerprint, "edited"), HUMAN, editor=HUMAN)
    assert overrides(REF, authors=AUTHORS) == []
    assert not _evaluate(repo).ok


def test_overrides_read_the_human_role_only(
    thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[trust.Role]] = []
    real = trust.read_comment_stream

    def spy(repo: str, number: int, **kwargs: Any) -> trust.CommentStream:
        seen.append(kwargs["roles"])
        return real(repo, number, **kwargs)

    monkeypatch.setattr(trust, "read_comment_stream", spy)
    fingerprint = "f" * 64
    thread.override(REF, fingerprint)
    found = overrides(REF, authors=AUTHORS)
    assert [(o.fingerprint, o.reason, o.author) for o in found] == [
        (fingerprint, "no agent session", "human")
    ]
    assert seen == [frozenset({trust.Role.HUMAN})]


@pytest.mark.parametrize("answer", [{}, {"repository": None}])
def test_overrides_read_failure_raises(
    items: trust_stubs.FakeItems, answer: dict[str, Any]
) -> None:
    items.items[("o/r", 7)] = answer
    with pytest.raises(trust.TrustReadError):
        overrides(REF, authors=AUTHORS)


def test_lookup_failure_fails_closed(
    repo: Path, items: trust_stubs.FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")

    def boom(_query: str, **_variables: Any) -> Any:
        raise github.GitHubAPIError(1, ("gh", "api"), None, "HTTP 502")

    monkeypatch.setattr(github, "graphql", boom)
    result = _evaluate(repo)
    assert not result.ok
    assert "merge review: lookup failed" in result.reasons[0]
    assert "HTTP 502" in result.reasons[0]
    assert result.read_failure


def test_unreadable_ref_fails_closed(repo: Path, thread: Thread) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    result = evaluate(repo, base="no-such-ref", head="HEAD", issue_refs=[REF], authors=AUTHORS)
    assert not result.ok
    assert [reason.split(":", 1)[0] for reason in result.reasons] == [
        "protected files",
        "merge review",
    ]


def test_malformed_registry_fails_closed(repo: Path, thread: Thread) -> None:
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"protected-paths.toml": "paths = 'not a list'\n"}, "a bad registry")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.record(REF, _git(repo, "rev-parse", "HEAD"), _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert result.reasons[0].startswith("protected files: lookup failed")
    assert "protected-paths.toml" in result.reasons[0]
    assert result.read_failure


@pytest.mark.parametrize("refs", [[], ["not-a-ref"], ["o/r#7", "o/other#8"]])
def test_unresolvable_task_issue_refuses(
    repo: Path, items: trust_stubs.FakeItems, refs: list[str]
) -> None:
    _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    result = evaluate(repo, base="develop", head="HEAD", issue_refs=refs, authors=AUTHORS)
    assert not result.ok
    assert result.reasons[0].startswith("task issue:")
    assert not result.read_failure
    assert items.reads == []


def test_a_repeated_task_issue_is_read_once(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "change")
    thread.record(REF, head, _fp(repo))
    assert _evaluate(repo, [REF, REF]).ok
    assert thread.items.reads == [("o/r", 7)]


# --- the protected-files rule ---------------------------------------------------------


def test_approved_protected_change_passes(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {WORKFLOW: "on: pull_request\n"}, "edit the workflow")
    thread.approve(REF, [ProtectedChange(WORKFLOW, _blob(repo, WORKFLOW))])
    thread.record(REF, head, _fp(repo))
    assert _evaluate(repo).ok


def test_refused_or_unanswered_approval_does_not_cover(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {WORKFLOW: "on: pull_request\n"}, "edit the workflow")
    changes = [ProtectedChange(WORKFLOW, _blob(repo, WORKFLOW))]
    thread.approve(REF, changes, REFUSE)
    thread.record(REF, head, _fp(repo))
    assert not _evaluate(repo).ok


def test_approval_for_other_path_does_not_cover(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {"vergil.toml": "on: pull_request\n"}, "edit vergil.toml")
    blob = _blob(repo, "vergil.toml")
    thread.approve(REF, [ProtectedChange(".github/workflows/other.yml", blob)])
    thread.record(REF, head, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert result.reasons == (
        f"protected file vergil.toml (blob {blob}) has no approved protected-change"
        f" approval on {REF}; ask for one with vrg-ruling request --kind protected-change",
    )


def test_deleted_approval_is_per_path(repo: Path, thread: Thread) -> None:
    head = _commit(repo, {WORKFLOW: None, "vergil.toml": None}, "delete both")
    thread.approve(REF, [ProtectedChange(WORKFLOW, "deleted")])
    thread.record(REF, head, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert len(result.reasons) == 1
    assert "protected file vergil.toml (blob deleted)" in result.reasons[0]
    thread.approve(REF, [ProtectedChange("vergil.toml", "deleted")])
    assert _evaluate(repo).ok


def test_edit_after_approval_voids_it(repo: Path, thread: Thread) -> None:
    _commit(repo, {WORKFLOW: "on: pull_request\n"}, "edit the workflow")
    thread.approve(REF, [ProtectedChange(WORKFLOW, _blob(repo, WORKFLOW))])
    head = _commit(repo, {WORKFLOW: "on: [pull_request, push]\n"}, "edit it again")
    thread.record(REF, head, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert f"protected file {WORKFLOW} (blob {_blob(repo, WORKFLOW)})" in result.reasons[0]


def test_registry_is_read_at_base(repo: Path, thread: Thread) -> None:
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"protected-paths.toml": "paths = ['b.txt']\n"}, "protect b.txt")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    head = _commit(
        repo, {"protected-paths.toml": "paths = []\n", "b.txt": "uno\n"}, "drop and edit b.txt"
    )
    thread.record(REF, head, _fp(repo))
    result = _evaluate(repo)
    assert not result.ok
    assert [reason.split(" (", 1)[0] for reason in result.reasons] == [
        "protected file b.txt",
        "protected file protected-paths.toml",
    ]


# --- batches ------------------------------------------------------------------------

ONE = "o/r#7"
TWO = "o/r#8"


def _batch(repo: Path) -> tuple[str, str]:
    first = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "first change (#7)")
    second = _commit(repo, {"b.txt": "uno\nDOS\n"}, "second change (#8)")
    return first, second


def test_batch_needs_each_issues_own_commit_record(repo: Path, thread: Thread) -> None:
    first, second = _batch(repo)
    thread.record(ONE, first, commit_fingerprint(repo, first))
    thread.empty(TWO)
    result = _evaluate(repo, [ONE, TWO])
    assert not result.ok
    assert len(result.reasons) == 1
    assert f"on {TWO} covers its commit {second[:12]}" in result.reasons[0]

    # A record of the second commit posted on the wrong issue covers nothing.
    thread.record(ONE, second, commit_fingerprint(repo, second))
    assert not _evaluate(repo, [ONE, TWO]).ok

    thread.record(TWO, second, commit_fingerprint(repo, second))
    assert _evaluate(repo, [ONE, TWO]).ok


def test_whole_change_record_covers_a_fix_to_an_open_batch(repo: Path, thread: Thread) -> None:
    first, second = _batch(repo)
    thread.record(ONE, first, commit_fingerprint(repo, first))
    thread.record(TWO, second, commit_fingerprint(repo, second))
    head = _commit(repo, {"a.txt": "one\nTWO\nTHREE\n"}, "fix: CI")
    result = _evaluate(repo, [ONE, TWO])
    assert not result.ok
    assert result.reasons == (
        f"merge review: commit {head[:12]} (fix: CI) is no issue's own commit, so only a"
        f" record, exemption or override covering the whole change ({_fp(repo)}) covers it",
    )
    thread.record(TWO, head, _fp(repo))
    assert _evaluate(repo, [ONE, TWO]).ok


def test_batch_commit_must_be_unique(repo: Path, thread: Thread) -> None:
    first, _second = _batch(repo)
    _commit(repo, {"a.txt": "one\nTWO\nthree\nfour\n"}, "more for the first (#7)")
    thread.record(ONE, first, commit_fingerprint(repo, first))
    thread.empty(TWO)
    result = _evaluate(repo, [ONE, TWO])
    assert not result.ok
    assert "holds 2 commits for o/r#7" in result.reasons[0]


def test_unfolded_fixup_is_not_covered_by_the_first_commit(repo: Path, thread: Thread) -> None:
    first, second = _batch(repo)
    fixup = _commit(repo, {"a.txt": "one\nTWO\nthree\nfour\n"}, "fixup! first change (#7)")
    thread.record(ONE, first, commit_fingerprint(repo, first))
    thread.record(TWO, second, commit_fingerprint(repo, second))
    result = _evaluate(repo, [ONE, TWO])
    assert not result.ok
    assert f"commit {fixup[:12]}" in result.reasons[0]


def test_batch_approval_on_any_issue_covers(repo: Path, thread: Thread) -> None:
    first = _commit(repo, {"a.txt": "one\nTWO\nthree\n"}, "first change (#7)")
    second = _commit(repo, {WORKFLOW: "on: pull_request\n"}, "the workflow (#8)")
    thread.approve(ONE, [ProtectedChange(WORKFLOW, _blob(repo, WORKFLOW))])
    thread.record(ONE, first, commit_fingerprint(repo, first))
    thread.record(TWO, second, commit_fingerprint(repo, second))
    assert _evaluate(repo, [ONE, TWO]).ok


# --- the override line ------------------------------------------------------------


def test_override_round_trip() -> None:
    line = render_override("e" * 64, "the agent VM is down")
    assert line == f"Merge review override: fingerprint={'e' * 64} reason=the agent VM is down"
    assert parse_override(f"{line}\nmore text") == Override("e" * 64, "the agent VM is down")


@pytest.mark.parametrize(
    "body",
    [
        f"> Merge review override: fingerprint={'e' * 64} reason=quoted",
        f"intro\nMerge review override: fingerprint={'e' * 64} reason=second line",
        f"Merge review override: fingerprint={'E' * 64} reason=upper",
        f"Merge review override: fingerprint={'e' * 63} reason=short",
        f"Merge review override: fingerprint={'e' * 64} reason=",
        f"Merge review override: fingerprint={'e' * 64} reason= padded",
        f"Merge review override: fingerprint={'e' * 64} reason=café",
    ],
)
def test_parse_override_refuses_any_other_form(body: str) -> None:
    assert parse_override(body) is None


@pytest.mark.parametrize(
    ("fingerprint", "reason"),
    [("e" * 63, "ok"), ("e" * 64, ""), ("e" * 64, "two\nlines"), ("e" * 64, "tab\there")],
)
def test_render_override_refuses_bad_input(fingerprint: str, reason: str) -> None:
    with pytest.raises(GateError):
        render_override(fingerprint, reason)


# --- agent involvement ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (IdentityMode.USER, True),
        (IdentityMode.AUDIT, True),
        (IdentityMode.HUMAN, False),
        (IdentityMode.AUTOMATION, False),
    ],
)
def test_agent_involved_at_submit_reads_the_recording_identity(
    mode: IdentityMode, *, expected: bool
) -> None:
    assert agent_involved(authors=AUTHORS, recorded_by=mode) is expected


@pytest.mark.parametrize(
    ("actors", "expected"),
    [
        ([trust.Identity("human", "User")], False),
        ([trust.Identity("human", "User"), trust.Identity("o-automation[bot]", "Bot")], False),
        ([trust.Identity("human", "User"), trust.Identity("human-vergil-user[bot]", "Bot")], True),
        ([trust.Identity("app/human-vergil-user", "Bot")], True),
        ([trust.Identity("human-vergil-user", "User")], True),
        ([trust.Identity(None, None)], True),
        ([trust.Identity("ghost", "User")], True),
        ([], False),
    ],
)
def test_agent_involved_for_a_pr_reads_every_actor(
    actors: list[trust.Identity], *, expected: bool
) -> None:
    assert agent_involved(authors=AUTHORS, actors=actors) is expected


def test_agent_involved_takes_exactly_one_kind_of_evidence() -> None:
    with pytest.raises(GateError):
        agent_involved(authors=AUTHORS)
    with pytest.raises(GateError):
        agent_involved(authors=AUTHORS, recorded_by=IdentityMode.HUMAN, actors=[])
