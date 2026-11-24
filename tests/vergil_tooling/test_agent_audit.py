"""Tests for the daily agent audit (``lib.agent_audit``, ``vrg-agent-audit``; .github#1384 Task 12).

Each test builds a real ``origin`` (a bare repository) with ``develop`` and
``main`` histories and the merged PRs' ``refs/pull/<n>/head``, and a clone of
it to audit. GitHub's answers come from :class:`FakeGitHub`, which holds the
marker refs and answers the REST reads in the projected shapes the audit's
``--jq`` filters give. The gate evaluator and the provenance check are
stubbed: their own tests cover them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_agent_audit
from vergil_tooling.lib import agent_audit, agent_gates, github, pr_provenance, trust
from vergil_tooling.lib.agent_gates import GateResult
from vergil_tooling.lib.trusted_authors import (
    OrgConfig,
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
)

if TYPE_CHECKING:
    from pathlib import Path

REPO = "o/r"
AUTHORS = TrustedAuthors(
    human=("human",),
    agents=("human-vergil-user",),
    orgs={"o": OrgConfig(automation_app="o-automation")},
)
HUMAN = {"login": "human", "type": "User"}
AGENT = {"login": "human-vergil-user[bot]", "type": "Bot"}
AUTOMATION = {"login": "o-automation[bot]", "type": "Bot"}
OUTSIDER = {"login": "drive-by", "type": "User"}
WORKFLOW = ".github/workflows/ci.yml"


# --- git ----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class Origin:
    """A bare ``origin`` and a scratch clone that writes its history."""

    def __init__(self, root: Path) -> None:
        self.bare = root / "origin.git"
        _git(root, "init", "-q", "--bare", "-b", "develop", str(self.bare))
        self.work = root / "work"
        _git(root, "clone", "-q", str(self.bare), str(self.work))
        _git(self.work, "checkout", "-q", "-b", "develop")
        self.commit({"a.txt": "one\n", WORKFLOW: "on: push\n"}, "base")
        _git(self.work, "push", "-q", "origin", "develop", "develop:main")

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", message)
        return _git(self.work, "rev-parse", "HEAD")

    def tip(self, branch: str) -> str:
        return _git(self.bare, "rev-parse", branch)

    def squash(self, number: int, files: dict[str, str], *, onto: str = "develop") -> str:
        """Open PR *number* off *onto* with one commit, then squash-merge it: the merge commit."""
        _git(self.work, "checkout", "-q", "-B", f"pr{number}", f"origin/{onto}")
        self.commit(files, f"pr {number}")
        _git(self.work, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head")
        _git(self.work, "checkout", "-q", "-B", onto, f"origin/{onto}")
        _git(self.work, "merge", "-q", "--squash", f"pr{number}")
        _git(self.work, "commit", "-q", "-m", f"squash {number}")
        _git(self.work, "push", "-q", "origin", onto)
        _git(self.work, "fetch", "-q", "origin")
        return _git(self.work, "rev-parse", "HEAD")

    def pull_head(self, number: int) -> str:
        return _git(self.bare, "rev-parse", f"refs/pull/{number}/head")


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "VRG_APP_ID", "VRG_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    return Origin(tmp_path)


@pytest.fixture
def clone(tmp_path: Path, origin: Origin) -> Path:
    path = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin.bare), str(path))
    return path


# --- GitHub -------------------------------------------------------------------------


@dataclass
class PR:
    number: int
    merge_commit: str
    commits: tuple[str, ...]
    author: dict[str, str] | None
    merged_by: dict[str, str] | None
    base: str = "develop"
    events: list[dict[str, Any]] = field(default_factory=list)
    body: str | None = "Summary.\n\n- Ref #7\n"


class Markers(MutableMapping[str, str]):
    """The marker branches, kept as real branches of ``origin``, by name."""

    def __init__(self, origin: Origin) -> None:
        self.bare = origin.bare

    def _names(self) -> list[str]:
        out = _git(self.bare, "for-each-ref", "--format=%(refname)", "refs/heads/vergil/")
        return [line.removeprefix("refs/heads/") for line in out.splitlines()]

    def __getitem__(self, name: str) -> str:
        if name not in self._names():
            raise KeyError(name)
        return _git(self.bare, "rev-parse", f"refs/heads/{name}")

    def __setitem__(self, name: str, sha: str) -> None:
        _git(self.bare, "update-ref", f"refs/heads/{name}", sha)

    def __delitem__(self, name: str) -> None:
        _git(self.bare, "update-ref", "-d", f"refs/heads/{name}")

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


class FakeGitHub:
    """The marker refs and the REST reads of one repository."""

    def __init__(self, origin: Origin) -> None:
        self.origin = origin
        self.refs = Markers(origin)
        self.prs: dict[int, PR] = {}
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.fail_on: set[str] = set()

    # the refs
    def read_ref(self, repo: str, branch: str) -> str | None:
        assert repo == REPO
        if branch in ("develop", "main"):
            return self.origin.tip(branch)
        return self.refs.get(branch)

    def write_json(self, method: str, endpoint: str, body: dict[str, object]) -> None:
        self.writes.append((method, endpoint, body))
        if method == "POST":
            assert endpoint == f"repos/{REPO}/git/refs"
            name = str(body["ref"]).removeprefix("refs/heads/")
            if name in self.refs:
                raise subprocess.CalledProcessError(1, ("gh",), "", "Reference already exists")
            self.refs[name] = str(body["sha"])
            return
        assert method == "PATCH"
        assert body["force"] is False
        name = endpoint.removeprefix(f"repos/{REPO}/git/refs/heads/")
        self.refs[name] = str(body["sha"])

    # the reads
    def add(self, pr: PR, *, merger: dict[str, str] | None = None) -> PR:
        if merger is not None:
            pr.events.append({"event": "merged", "actor": merger, "has_actor": True})
        self.prs[pr.number] = pr
        return pr

    def _check(self, endpoint: str) -> None:
        if any(part in endpoint for part in self.fail_on):
            raise subprocess.CalledProcessError(1, ("gh", "api", endpoint), "", "HTTP 502")

    def read_json_paginated(
        self, endpoint: str, key: str | None = None, *, jq: str | None = None
    ) -> list[object]:
        self._check(endpoint)
        assert jq is not None
        parts = endpoint.removeprefix(f"repos/{REPO}/").split("/")
        if parts[0] == "commits":
            sha = parts[1]
            return [
                {
                    "number": pr.number,
                    "merged_at": "2026-11-22T10:00:00Z",
                    "merge_commit_sha": pr.merge_commit,
                    "base": pr.base,
                }
                for pr in self.prs.values()
                if sha in pr.commits
            ]
        if parts[0] == "issues" and parts[2] == "timeline":
            return list(self.prs[int(parts[1])].events)
        raise AssertionError(endpoint)

    def read_json(self, *args: str) -> dict[str, object]:
        assert args[0] == "api"
        self._check(args[1])
        number = int(args[1].rsplit("/", 1)[1])
        pr = self.prs[number]
        return {
            "number": number,
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "merged": True,
            "merge_commit_sha": pr.merge_commit,
            "user": pr.author,
            "merged_by": pr.merged_by,
        }

    def read_issue(self, repo: str, number: int, **_: object) -> trust.IssueContent:
        self._check(f"body/{number}")
        body = self.prs[number].body
        return trust.IssueContent(
            body=body,
            body_role=None if body is None else trust.Role.HUMAN,
            author_role=trust.Role.HUMAN,
            comments=(),
            skipped=(),
        )


@dataclass
class Gates:
    calls: list[dict[str, Any]] = field(default_factory=list)
    result: GateResult = GateResult(ok=True, reasons=())

    def __call__(self, repo: Path, **kwargs: Any) -> GateResult:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def gh(origin: Origin, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub(origin)
    monkeypatch.setattr(agent_audit, "_read_ref", fake.read_ref)
    monkeypatch.setattr(github, "write_json", fake.write_json)
    monkeypatch.setattr(github, "read_json_paginated", fake.read_json_paginated)
    monkeypatch.setattr(github, "read_json", fake.read_json)
    monkeypatch.setattr(trust, "read_issue", fake.read_issue)
    monkeypatch.setattr(pr_provenance, "check_pr", lambda pr: pr_provenance.ProvenanceResult())
    return fake


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> Gates:
    fake = Gates()
    monkeypatch.setattr(agent_gates, "evaluate", fake)
    return fake


def _mark(gh: FakeGitHub, origin: Origin) -> None:
    for branch in ("develop", "main"):
        gh.refs[agent_audit.marker_branch(branch)] = origin.tip(branch)


def _kinds(results: list[agent_audit.BranchAudit]) -> list[str]:
    return [finding.kind for result in results for finding in result.findings]


def _squash_pr(
    gh: FakeGitHub,
    origin: Origin,
    number: int,
    files: dict[str, str],
    *,
    author: dict[str, str] | None = AGENT,
    merger: dict[str, str] | None = AGENT,
    base: str = "develop",
) -> PR:
    merge = origin.squash(number, files, onto=base)
    return gh.add(
        PR(number, merge, (merge,), author=author, merged_by=merger, base=base), merger=merger
    )


# --- the markers -------------------------------------------------------------------


def test_init_sets_marker_and_audits_nothing(origin: Origin, gh: FakeGitHub, gates: Gates) -> None:
    done = agent_audit.init_markers(REPO)
    assert done == [
        ("vergil/agent-audit/develop", origin.tip("develop"), True),
        ("vergil/agent-audit/main", origin.tip("main"), True),
    ]
    assert gh.refs == {
        "vergil/agent-audit/develop": origin.tip("develop"),
        "vergil/agent-audit/main": origin.tip("main"),
    }
    assert gates.calls == []
    assert agent_audit.MARKER_BRANCHES == (
        "vergil/agent-audit/develop",
        "vergil/agent-audit/main",
    )


def test_init_leaves_an_existing_marker_untouched(origin: Origin, gh: FakeGitHub) -> None:
    old = origin.tip("develop")
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    gh.refs["vergil/agent-audit/develop"] = old
    done = agent_audit.init_markers(REPO)
    assert done[0] == ("vergil/agent-audit/develop", old, False)
    assert gh.refs["vergil/agent-audit/develop"] == old
    assert [w[0] for w in gh.writes] == ["POST"]


def test_missing_marker_is_a_finding(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    gh.refs["vergil/agent-audit/main"] = origin.tip("main")
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    results = agent_audit.audit(clone, REPO, AUTHORS)
    develop = results[0]
    assert [(f.kind, f.repo, f.branch) for f in develop.findings] == [
        ("missing-marker", REPO, "develop")
    ]
    assert develop.audited_to is None
    assert "vergil/agent-audit/develop" not in gh.refs
    assert not [w for w in gh.writes if w[0] == "POST"]
    assert gates.calls == []


def test_marker_off_the_branch_history_is_a_finding(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    stray = origin.commit({"z.txt": "z\n"}, "never pushed to develop")
    _git(origin.work, "push", "-q", "origin", "HEAD:refs/heads/elsewhere")
    gh.refs["vergil/agent-audit/develop"] = stray
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["marker-not-ancestor"]
    assert gh.refs["vergil/agent-audit/develop"] == stray


# --- catching up ----------------------------------------------------------------------


def test_missed_run_is_caught_up_from_marker(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    first = _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    second = _squash_pr(gh, origin, 12, {"c.txt": "c\n"})
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == []
    assert [call["head"] for call in gates.calls] == [origin.pull_head(11), origin.pull_head(12)]
    assert gh.refs["vergil/agent-audit/develop"] == second.merge_commit
    assert first.merge_commit != second.merge_commit


def test_main_merges_caught_up_from_main_marker(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    one = _squash_pr(gh, origin, 21, {"r1.txt": "1\n"}, author=HUMAN, merger=HUMAN, base="main")
    two = _squash_pr(gh, origin, 22, {"r2.txt": "2\n"}, author=HUMAN, merger=HUMAN, base="main")
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == []
    assert gh.refs["vergil/agent-audit/main"] == two.merge_commit
    assert gh.refs["vergil/agent-audit/develop"] == origin.tip("develop")
    assert one.merge_commit in {pr.merge_commit for pr in gh.prs.values()}
    assert gates.calls == []  # main is checked for its actors only


def test_marker_stays_when_nothing_landed(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == []
    assert [r.audited_to for r in results] == [None, None]
    assert gh.writes == []


def test_commit_with_no_pr_is_a_finding(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _git(origin.work, "checkout", "-q", "develop")
    direct = origin.commit({"d.txt": "d\n"}, "direct push")
    _git(origin.work, "push", "-q", "origin", "develop")
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["no-pr"]
    assert results[0].findings[0].subject == direct[:12]
    assert gh.refs["vergil/agent-audit/develop"] == direct


# --- reading a merged PR ----------------------------------------------------------------


def test_merged_squash_pr_read_from_pull_ref(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    before = origin.tip("develop")
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    agent_audit.audit(clone, REPO, AUTHORS)
    assert gates.calls == [
        {
            "base": before,
            "head": origin.pull_head(11),
            "issue_refs": ["o/r#7"],
            "authors": AUTHORS,
        }
    ]
    assert pr.merge_commit != origin.pull_head(11)


def test_merged_rebase_batch_read_from_pull_ref(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    before = origin.tip("develop")
    _git(origin.work, "checkout", "-q", "-B", "batch", "origin/develop")
    c1 = origin.commit({"b.txt": "b\n"}, "first (#7)")
    c2 = origin.commit({"c.txt": "c\n"}, "second (#8)")
    _git(origin.work, "push", "-q", "origin", "HEAD:refs/pull/13/head")
    _git(origin.work, "checkout", "-q", "-B", "develop", "origin/develop")
    origin.commit({"x.txt": "develop moved\n"}, "unrelated (#99)")
    _git(origin.work, "push", "-q", "origin", "develop")
    gh.add(
        PR(99, origin.tip("develop"), (origin.tip("develop"),), author=HUMAN, merged_by=HUMAN),
        merger=HUMAN,
    )
    mid = origin.tip("develop")
    _git(origin.work, "cherry-pick", c1, c2)
    _git(origin.work, "push", "-q", "origin", "develop")
    r2 = origin.tip("develop")
    r1 = _git(origin.work, "rev-parse", "HEAD^")
    gh.add(
        PR(13, r2, (r1, r2), author=AGENT, merged_by=AGENT, body="- Closes #7\n- Closes #8\n"),
        merger=AGENT,
    )
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == []
    assert gates.calls == [
        {
            "base": mid,
            "head": origin.pull_head(13),
            "issue_refs": ["o/r#7", "o/r#8"],
            "authors": AUTHORS,
        }
    ]
    assert before != mid
    assert gh.refs["vergil/agent-audit/develop"] == r2


# --- the actor check --------------------------------------------------------------------


def test_untrusted_actor_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=OUTSIDER)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["untrusted-actor"]
    assert "drive-by" in results[0].findings[0].detail


def test_unexpected_actor_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    # The user agent merging the human's PR, and the automation App merging the agent's.
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=AGENT)
    _squash_pr(gh, origin, 12, {"c.txt": "c\n"}, author=AGENT, merger=AUTOMATION)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert [(f.kind, f.subject) for f in results[0].findings] == [
        ("unexpected-actor", "PR #11"),
        ("unexpected-actor", "PR #12"),
    ]


def test_auto_merge_enabled_by_outsider_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=HUMAN)
    pr.events.insert(0, {"event": "auto_squash_enabled", "actor": OUTSIDER, "has_actor": True})
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert "untrusted-actor" in _kinds(results)


def test_no_recorded_merge_actor_is_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=None)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["unexpected-actor"]


def test_deleted_merge_actor_is_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=HUMAN)
    pr.merged_by = None
    pr.events = [{"event": "merged", "actor": None, "has_actor": True}]
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["untrusted-actor"]
    assert "(deleted account)" in results[0].findings[0].detail


def test_main_merge_by_agent_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 21, {"r.txt": "r\n"}, author=AGENT, merger=AGENT, base="main")
    _squash_pr(gh, origin, 22, {"s.txt": "s\n"}, author=AUTOMATION, merger=AUTOMATION, base="main")
    results = agent_audit.audit(clone, REPO, AUTHORS)
    main = results[1]
    assert [(f.kind, f.subject) for f in main.findings] == [
        ("main-merge", "PR #21"),
        ("main-merge", "PR #22"),
    ]


def test_provenance_violation_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    violation = pr_provenance.Action("human-vergil-user", pr_provenance.Role.USER, "approved")
    monkeypatch.setattr(
        pr_provenance,
        "check_pr",
        lambda pr: pr_provenance.ProvenanceResult(violations=[violation]),
    )
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["provenance"]


# --- agent involvement and the gates -------------------------------------------------


def test_automation_only_pr_skips_record_check(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _squash_pr(
        gh, origin, 11, {WORKFLOW: "on: pull_request\n"}, author=AUTOMATION, merger=AUTOMATION
    )
    _squash_pr(gh, origin, 12, {"b.txt": "b\n"}, author=HUMAN, merger=HUMAN)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == []
    assert gates.calls == []


def test_agent_action_on_human_pr_makes_it_involved(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=HUMAN)
    pr.events.insert(0, {"event": "head_ref_force_pushed", "actor": AGENT, "has_actor": True})
    agent_audit.audit(clone, REPO, AUTHORS)
    assert len(gates.calls) == 1


def test_mention_of_agent_does_not_make_it_involved(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=HUMAN, merger=HUMAN)
    for event in ("mentioned", "subscribed", "cross-referenced"):
        pr.events.insert(0, {"event": event, "actor": AGENT, "has_actor": True})
    agent_audit.audit(clone, REPO, AUTHORS)
    assert gates.calls == []


def test_deleted_author_is_not_human_only(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"}, author=None, merger=HUMAN)
    agent_audit.audit(clone, REPO, AUTHORS)
    assert len(gates.calls) == 1


def test_gate_failure_is_a_finding_and_the_marker_moves(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    gates.result = GateResult(ok=False, reasons=("merge review: no record covers it",))
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert [(f.kind, f.detail) for f in results[0].findings] == [
        ("gates", "merge review: no record covers it")
    ]
    assert gh.refs["vergil/agent-audit/develop"] == pr.merge_commit


def test_untrusted_pr_body_is_a_finding(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    pr = _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    pr.body = None
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["task-issue"]
    assert gates.calls == []


def test_squash_changed_approved_file_is_its_own_condition(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    # The PR edits the workflow's first line; develop meanwhile adds a second,
    # so the squash commit's workflow is neither side's blob.
    _git(origin.work, "checkout", "-q", "-B", "pr11", "origin/develop")
    origin.commit({WORKFLOW: "on: pull_request\n"}, "pr 11")
    _git(origin.work, "push", "-q", "origin", "HEAD:refs/pull/11/head")
    _git(origin.work, "checkout", "-q", "-B", "develop", "origin/develop")
    origin.commit({WORKFLOW: "on: push\njobs: {}\n"}, "human edit")
    _git(origin.work, "push", "-q", "origin", "develop")
    gh.add(
        PR(98, origin.tip("develop"), (origin.tip("develop"),), author=HUMAN, merged_by=HUMAN),
        merger=HUMAN,
    )
    origin.commit({WORKFLOW: "on: pull_request\njobs: {}\n"}, "squash 11")
    _git(origin.work, "push", "-q", "origin", "develop")
    gh.add(
        PR(11, origin.tip("develop"), (origin.tip("develop"),), author=AGENT, merged_by=AGENT),
        merger=AGENT,
    )
    results = agent_audit.audit(clone, REPO, AUTHORS)
    findings = results[0].findings
    assert [(f.kind, f.subject) for f in findings] == [("merge-changed-approved-file", "PR #11")]
    assert WORKFLOW in findings[0].detail


# --- read failures --------------------------------------------------------------------


def test_read_failure_stops_and_keeps_the_marker_before_it(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    first = _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    _squash_pr(gh, origin, 12, {"c.txt": "c\n"})
    gh.fail_on.add("issues/12/timeline")
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["read-failure"]
    assert gh.refs["vergil/agent-audit/develop"] == first.merge_commit


def test_gate_lookup_failure_is_a_read_failure(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates
) -> None:
    _mark(gh, origin)
    marker = gh.refs["vergil/agent-audit/develop"]
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    gates.result = GateResult(
        ok=False, reasons=("merge review: lookup failed for x...y: HTTP 502",), read_failure=True
    )
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["read-failure"]
    assert gh.refs["vergil/agent-audit/develop"] == marker


def test_marker_write_failure_is_reported(
    origin: Origin, clone: Path, gh: FakeGitHub, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})

    def refuse(method: str, endpoint: str, body: dict[str, object]) -> None:
        raise subprocess.CalledProcessError(1, ("gh",), "", "Reference update failed")

    monkeypatch.setattr(github, "write_json", refuse)
    results = agent_audit.audit(clone, REPO, AUTHORS)
    assert _kinds(results) == ["read-failure"]


# --- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli(clone: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(clone)
    monkeypatch.setattr(vrg_agent_audit, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(vrg_agent_audit, "require_remote_is", lambda repo, command: None)
    return clone


def test_cli_clean_exits_0(
    origin: Origin, cli: Path, gh: FakeGitHub, gates: Gates, capsys: pytest.CaptureFixture[str]
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    assert vrg_agent_audit.main(["--repo", REPO]) == 0
    out = capsys.readouterr().out
    assert "o/r develop: 0 finding(s)" in out


def test_read_failure_exits_1(
    origin: Origin, cli: Path, gh: FakeGitHub, gates: Gates, capsys: pytest.CaptureFixture[str]
) -> None:
    _mark(gh, origin)
    _squash_pr(gh, origin, 11, {"b.txt": "b\n"})
    gh.fail_on.add("commits/")
    assert vrg_agent_audit.main(["--repo", REPO]) == 1
    assert "read-failure: o/r develop" in capsys.readouterr().out


def test_cli_missing_marker_exits_1(
    origin: Origin, cli: Path, gh: FakeGitHub, gates: Gates, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vrg_agent_audit.main(["--repo", REPO]) == 1
    out = capsys.readouterr().out
    assert "missing-marker: o/r develop: vergil/agent-audit/develop" in out
    assert "missing-marker: o/r main: vergil/agent-audit/main" in out
    assert gh.refs == {}


def test_cli_init_exits_0(
    origin: Origin, cli: Path, gh: FakeGitHub, gates: Gates, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vrg_agent_audit.main(["--repo", REPO, "--init"]) == 0
    assert "vergil/agent-audit/develop created at" in capsys.readouterr().out
    assert set(gh.refs) == set(agent_audit.MARKER_BRANCHES)


def test_missing_trusted_authors_configuration_refuses(
    origin: Origin,
    cli: Path,
    gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mark(gh, origin)

    def missing() -> TrustedAuthors:
        raise TrustedAuthorsNotConfiguredError("no trusted authors configured")

    monkeypatch.setattr(vrg_agent_audit, "load_trusted_authors", missing)
    assert vrg_agent_audit.main(["--repo", REPO]) == 1
    assert "trusted-authors configuration" in capsys.readouterr().err
    assert gh.writes == []


@pytest.mark.parametrize("humans", [(), ("human", "other")])
def test_trusted_authors_must_name_one_human(
    origin: Origin,
    cli: Path,
    gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    humans: tuple[str, ...],
) -> None:
    _mark(gh, origin)
    authors = TrustedAuthors(human=humans, agents=AUTHORS.agents, orgs=AUTHORS.orgs)
    monkeypatch.setattr(vrg_agent_audit, "load_trusted_authors", lambda: authors)
    assert vrg_agent_audit.main(["--repo", REPO]) == 1


def test_cli_refuses_a_shallow_clone(
    origin: Origin,
    tmp_path: Path,
    gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin.bare}", str(shallow))
    monkeypatch.chdir(shallow)
    monkeypatch.setattr(vrg_agent_audit, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(vrg_agent_audit, "require_remote_is", lambda repo, command: None)
    assert vrg_agent_audit.main(["--repo", REPO]) == 1
    assert "shallow" in capsys.readouterr().err


def test_cli_usage_error_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_agent_audit.main(["--repo", "not-a-repo"])
    assert exc.value.code == 2
