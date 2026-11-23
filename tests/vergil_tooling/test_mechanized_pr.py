"""Tests for vergil_tooling.lib.mechanized_pr.

A local bare repo stands in for ``origin``: the clone is a real
``clone_develop`` checkout of it, and the push is a real ``git push``. The
GitHub API is a fake whose branch list and branch deletes act on that bare repo,
so a branch an earlier run left is really there, and really gone once deleted.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import (
    auto_merge,
    develop_clone,
    github,
    mechanized_pr,
    pr_create,
    pr_merge,
    retry,
    trust,
)
from vergil_tooling.lib.develop_clone import clone_develop
from vergil_tooling.lib.mechanized_pr import (
    AUTOMATION_BRANCH_PREFIX,
    MechanizedPRError,
    PRNotMergedError,
    SyncResult,
    branch_name,
    open_sync_pr,
)

if TYPE_CHECKING:
    from collections.abc import Generator

REPO = "o/r"
JOB = "repo-config-sync"
RUN = "4242"
BRANCH = f"automation/{JOB}/{RUN}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class _PR:
    number: int
    head: str
    cross: bool = False
    owner: str = "o"
    name: str = "r"
    state: str = "open"
    title: str = "chore: sync"
    head_oid: str = ""

    @property
    def url(self) -> str:
        return f"https://github.com/{REPO}/pull/{self.number}"


@dataclass
class FakeGitHub:
    """The GitHub API calls ``mechanized_pr`` makes, backed by the bare *origin*."""

    origin: Path
    prs: list[_PR] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    mutations: list[dict[str, object]] = field(default_factory=list)
    auto_merge: dict[str, str] | None = None
    confirm_auto_merge: bool = True
    auto_merge_refusal: BaseException | None = None
    merges: list[tuple[str, str, str | None]] = field(default_factory=list)
    waits: list[tuple[str, str, int]] = field(default_factory=list)
    wait_failure: BaseException | None = None
    # Errors the next ``gh pr create`` calls raise, in turn; with *create_lands*
    # the first one is raised after GitHub opened the PR (a lost response).
    create_errors: list[BaseException] = field(default_factory=list)
    create_lands: bool = False
    create_calls: int = 0
    # The head commit a landed PR has, when it is not the pushed one.
    landed_oid: str | None = None

    def branches(self) -> list[str]:
        out = _git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return [line for line in out.splitlines() if line]

    def read_json_paginated(self, endpoint: str) -> list[object]:
        match = re.fullmatch(rf"repos/{REPO}/git/matching-refs/heads/(.+)", endpoint)
        assert match, endpoint
        prefix = match.group(1)
        return [
            {"ref": f"refs/heads/{b}", "object": {"sha": "x"}}
            for b in self.branches()
            if b.startswith(prefix)
        ]

    def read_json(self, *args: str) -> object:
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            assert args[args.index("--repo") + 1] == REPO
            head = args[args.index("--head") + 1]
            base = args[args.index("--base") + 1] if "--base" in args else None
            return [
                {
                    "number": pr.number,
                    "url": pr.url,
                    "title": pr.title,
                    "headRefName": pr.head,
                    "baseRefName": "develop",
                    "headRefOid": pr.head_oid or "0" * 40,
                    "isCrossRepository": pr.cross,
                    "headRepository": {"name": pr.name},
                    "headRepositoryOwner": {"login": pr.owner},
                }
                for pr in self.prs
                if pr.head == head and pr.state == "open" and base in {None, "develop"}
            ]
        if args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1]
            if fields == "id":
                return {"id": f"PR_{args[2].rsplit('/', 1)[1]}"}
            if fields == "state":
                merged = any(pr == args[2] for pr, _strategy, _head in self.merges)
                return {"state": "MERGED" if merged else "OPEN"}
            return {"autoMergeRequest": self.auto_merge}
        raise AssertionError(args)

    def run(self, *args: str) -> None:
        self.calls.append(args)
        assert args[:2] == ("pr", "close"), args
        number = int(args[2])
        for pr in self.prs:
            if pr.number == number:
                pr.state = "closed"

    def delete_if_exists(self, endpoint: str) -> bool:
        branch = endpoint.removeprefix(f"repos/{REPO}/git/refs/heads/")
        if branch not in self.branches():
            return False
        _git(self.origin, "update-ref", "-d", f"refs/heads/{branch}")
        return True

    def create_pr(self, **kwargs: str | bool) -> str:
        repo, head, title = str(kwargs["repo"]), str(kwargs["head"]), str(kwargs["title"])
        self.calls.append(("pr", "create", "--repo", repo, "--head", head))
        self.create_calls += 1
        assert kwargs["retry"] is False, "the retry is pr_create's, never gh's"
        assert kwargs["base"] == "develop"
        assert Path(str(kwargs["body_file"])).read_text() == "the body"
        if self.create_errors and not self.create_lands:
            raise self.create_errors.pop(0)
        pr = _PR(
            number=100 + len(self.prs),
            head=head,
            title=title,
            head_oid=self.landed_oid or _git(self.origin, "rev-parse", head),
        )
        self.prs.append(pr)
        if self.create_errors:
            self.create_lands = False
            raise self.create_errors.pop(0)
        return pr.url

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert "enablePullRequestAutoMerge" in query
        self.mutations.append({"query": query, **variables})
        if self.auto_merge_refusal is not None:
            raise self.auto_merge_refusal
        if self.confirm_auto_merge:
            self.auto_merge = {"mergeMethod": str(variables["method"])}
        return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 1}}}

    def merge(self, pr: str, *, strategy: str, match_head: str | None = None) -> None:
        self.calls.append(("pr", "merge", pr))
        self.merges.append((pr, strategy, match_head))

    def wait_until_merged(self, pr: str, *, strategy: str, deadline_secs: int) -> None:
        """Stands in for ``pr_merge.wait_until_merged`` (tested in test_pr_merge)."""
        assert self.auto_merge is not None, "the wait starts only after auto-merge is on"
        self.waits.append((pr, strategy, deadline_secs))
        if self.wait_failure is not None:
            raise self.wait_failure

    def prs_for_branch(
        self, repo: str, branch: str, *, state: str
    ) -> tuple[list[trust.PRRef], list[trust.Skipped]]:
        """The trust layer's open PRs on *branch*: a fork is skipped, never found."""
        assert (repo, state) == (REPO, "open")
        self.calls.append(("trust", "prs_for_branch", branch))
        found: list[trust.PRRef] = []
        skipped: list[trust.Skipped] = []
        for pr in self.prs:
            if pr.head != branch or pr.state != "open":
                continue
            if pr.cross:
                skipped.append(
                    trust.Skipped(
                        repo, pr.number, "pr", pr.owner, "t", "head", trust.CROSS_REPOSITORY
                    )
                )
                continue
            found.append(
                trust.PRRef(
                    number=pr.number,
                    url=pr.url,
                    title=pr.title,
                    head=pr.head,
                    head_oid=pr.head_oid or "0" * 40,
                    base="develop",
                    author_role=trust.Role.AUTOMATION,
                    state="OPEN",
                )
            )
        return found, skipped

    def pr_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "pr"]


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare origin with one ``develop`` commit; ``clone_develop`` clones it."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GITHUB_RUN_ID", RUN)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _git(seed, "checkout", "-q", "-b", "develop")
    (seed / "vergil.toml").write_text("v1\n")
    _git(seed, "add", "vergil.toml")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "push", "-q", "origin", "develop")
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: bare.as_uri())
    return bare


@pytest.fixture
def fake(origin: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub(origin)
    for name in (
        "read_json_paginated",
        "read_json",
        "run",
        "delete_if_exists",
        "create_pr",
        "merge",
    ):
        monkeypatch.setattr(github, name, getattr(api, name))
    monkeypatch.setattr(github, "graphql", api.graphql)
    monkeypatch.setattr(pr_merge, "wait_until_merged", api.wait_until_merged)
    monkeypatch.setattr(trust, "prs_for_branch", api.prs_for_branch)
    monkeypatch.delenv("VRG_MECHANIZED_PR_DEADLINE", raising=False)
    return api


@pytest.fixture
def clone(origin: Path) -> Generator[Path]:  # noqa: ARG001 - clones it
    with clone_develop(REPO) as path:
        yield path


def _leave_branch(origin: Path, branch: str) -> None:
    """Push *branch* to *origin*, as an earlier run of a job would have."""
    develop = _git(origin, "rev-parse", "develop")
    _git(origin, "update-ref", f"refs/heads/{branch}", develop)


def _sync(clone: Path) -> SyncResult:
    return open_sync_pr(REPO, clone, job=JOB, title="chore: sync", body="the body")


def test_prefix_and_branch_name() -> None:
    assert AUTOMATION_BRANCH_PREFIX == "automation/"
    assert branch_name(JOB, "99") == "automation/repo-config-sync/99"


def test_run_id_is_github_run_id_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    assert mechanized_pr.run_id() == "777"


def test_run_id_is_utc_stamp_and_hex_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    first = mechanized_pr.run_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", first)
    branch_name(JOB, first)


@pytest.mark.parametrize("job", ["", "Repo", "a/b", "-x", "a b"])
def test_invalid_job_name_raises(job: str) -> None:
    with pytest.raises(MechanizedPRError, match="invalid automation job name"):
        branch_name(job, "1")


@pytest.mark.parametrize("run_id", ["", "a/b", "../x", "-1"])
def test_invalid_run_id_raises(run_id: str) -> None:
    with pytest.raises(MechanizedPRError, match="invalid automation run ID"):
        branch_name(JOB, run_id)


def test_clean_clone_closes_earlier_pr_and_reports_in_sync(
    fake: FakeGitHub, origin: Path, clone: Path
) -> None:
    earlier = f"automation/{JOB}/1"
    _leave_branch(origin, earlier)
    fake.prs.append(_PR(7, earlier))

    result = _sync(clone)

    assert result == SyncResult(status="in_sync", pr_url=None, closed=[fake.prs[0].url])
    assert fake.prs[0].state == "closed"
    assert fake.branches() == ["develop"]
    assert not fake.mutations


def test_changes_pushed_on_new_branch_and_pr_opened(
    fake: FakeGitHub, origin: Path, clone: Path
) -> None:
    develop = _git(clone, "rev-parse", "HEAD")
    (clone / "vergil.toml").write_text("v2\n")
    shim = clone / ".claude" / "hooks" / "shim"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n")
    shim.chmod(0o755)

    result = _sync(clone)

    assert result == SyncResult(status="merged", pr_url=fake.prs[0].url, closed=[])
    assert sorted(fake.branches()) == [BRANCH, "develop"]
    assert _git(origin, "rev-parse", f"{BRANCH}^") == develop
    assert _git(origin, "log", "-1", "--format=%s", BRANCH) == "chore: sync"
    assert _git(origin, "show", f"{BRANCH}:vergil.toml") == "v2"
    mode = _git(origin, "ls-tree", BRANCH, ".claude/hooks/shim").split()[0]
    assert mode == "100755"
    assert fake.prs[0].head == BRANCH


def test_earlier_open_pr_closed_before_new_one_opened(
    fake: FakeGitHub, origin: Path, clone: Path
) -> None:
    earlier = f"automation/{JOB}/1"
    _leave_branch(origin, earlier)
    fake.prs.append(_PR(7, earlier))
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    assert result.closed == [fake.prs[0].url]
    assert fake.prs[0].state == "closed"
    assert result.pr_url == fake.prs[1].url
    kinds = [call[1] for call in fake.pr_calls() if call[1] in {"close", "create"}]
    assert kinds == ["close", "create"]
    assert earlier not in fake.branches()


def test_branch_without_pr_under_own_prefix_deleted(
    fake: FakeGitHub, origin: Path, clone: Path
) -> None:
    """A failed earlier attempt with the same run ID left its branch and no PR."""
    _leave_branch(origin, BRANCH)
    _leave_branch(origin, f"automation/{JOB}/other")
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    assert result.status == "merged"
    assert result.closed == []
    assert sorted(fake.branches()) == [BRANCH, "develop"]
    assert _git(origin, "show", f"{BRANCH}:vergil.toml") == "v2"


def test_mechanized_uses_helper_squash(
    fake: FakeGitHub, origin: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str, str]] = []
    helper = auto_merge.enable_auto_merge

    def spy(pr_url: str, *, head: str, method: auto_merge.MergeMethod) -> object:
        seen.append((pr_url, head, method))
        return helper(pr_url, head=head, method=method)

    monkeypatch.setattr(auto_merge, "enable_auto_merge", spy)
    (clone / "vergil.toml").write_text("v2\n")

    _sync(clone)

    head = _git(origin, "rev-parse", BRANCH)
    assert seen == [(fake.prs[0].url, head, "SQUASH")]
    [mutation] = fake.mutations
    assert mutation["method"] == "SQUASH"
    assert mutation["head"] == head
    assert mutation["id"] == f"PR_{fake.prs[0].number}"


def test_mechanized_auto_merge_refusal_is_mechanized_error(fake: FakeGitHub, clone: Path) -> None:
    fake.auto_merge_refusal = github.GitHubAPIError(
        1,
        ("gh", "api", "graphql"),
        "",
        "GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)",
    )
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError) as excinfo:
        _sync(clone)

    assert not isinstance(excinfo.value, auto_merge.AutoMergeError)
    message = str(excinfo.value)
    assert message.startswith(f"mechanized PR for {REPO}: enabling auto-merge failed: ")
    assert fake.prs[0].url in message
    assert "Auto merge is not allowed" in message
    assert fake.merges == []
    assert fake.waits == []


def test_mechanized_already_mergeable_merges_directly(
    fake: FakeGitHub, origin: Path, clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.auto_merge_refusal = github.GitHubAPIError(
        1,
        ("gh", "api", "graphql"),
        "",
        "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)",
    )
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    url = fake.prs[0].url
    assert result.status == "merged"
    assert result.pr_url == url
    assert fake.merges == [(url, "squash", _git(origin, "rev-parse", BRANCH))]
    assert fake.waits == []
    assert f"{REPO}: {url} was already mergeable and merged directly" in capsys.readouterr().out


def test_auto_merge_not_confirmed_raises(fake: FakeGitHub, clone: Path) -> None:
    fake.confirm_auto_merge = False
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="auto-merge is not confirmed"):
        _sync(clone)


def test_auto_merge_mutation_failure_raises(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_query: str, **_variables: object) -> dict[str, object]:
        msg = "GraphQL errors: auto-merge is not allowed"
        raise RuntimeError(msg)

    monkeypatch.setattr(github, "graphql", refuse)
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="enabling auto-merge"):
        _sync(clone)


def test_push_failure_raises_naming_step(fake: FakeGitHub, clone: Path, tmp_path: Path) -> None:
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match=r"o/r: git push failed"):
        _sync(clone)

    assert not fake.prs


def test_commit_failure_raises_naming_step(fake: FakeGitHub, clone: Path) -> None:
    (clone / "vergil.toml").write_text("v2\n")
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    with pytest.raises(MechanizedPRError, match="git commit failed"):
        _sync(clone)

    assert fake.branches() == ["develop"]


def test_pr_opened_for_named_repo_from_unrelated_cwd(
    fake: FakeGitHub, origin: Path, clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    earlier = f"automation/{JOB}/1"
    _leave_branch(origin, earlier)
    fake.prs.append(_PR(7, earlier))
    (clone / "vergil.toml").write_text("v2\n")

    _sync(clone)

    kinds = {call[1] for call in fake.pr_calls()}
    assert {"create", "list", "close", "view"} <= kinds
    for call in fake.pr_calls():
        if "--repo" in call:
            assert call[call.index("--repo") + 1] == REPO, call
        else:
            assert call[2].startswith(f"https://github.com/{REPO}/pull/"), call


def test_only_own_job_prefix_closed(fake: FakeGitHub, origin: Path, clone: Path) -> None:
    other = "automation/other-job/1"
    longer = f"automation/{JOB}-extra/1"
    for branch in (other, longer):
        _leave_branch(origin, branch)
    fake.prs += [_PR(7, other), _PR(8, longer)]

    result = _sync(clone)

    assert result.closed == []
    assert all(pr.state == "open" for pr in fake.prs)
    assert sorted(fake.branches()) == sorted(["develop", other, longer])


def test_fork_pr_on_automation_prefix_ignored(fake: FakeGitHub, origin: Path, clone: Path) -> None:
    """A fork's PR whose branch has our name is neither closed nor touched."""
    name = f"automation/{JOB}/x"
    _leave_branch(origin, name)
    fake.prs.append(_PR(9, name, cross=True, owner="mallory", name="r"))

    result = _sync(clone)

    assert result.closed == []
    assert fake.prs[0].state == "open"
    assert not [call for call in fake.pr_calls() if call[1] == "close"]


def test_same_repo_flag_contradicting_head_repo_raises(
    fake: FakeGitHub, origin: Path, clone: Path
) -> None:
    name = f"automation/{JOB}/x"
    _leave_branch(origin, name)
    fake.prs.append(_PR(9, name, cross=False, owner="mallory", name="r"))

    with pytest.raises(MechanizedPRError, match="not cross-repository"):
        _sync(clone)

    assert fake.prs[0].state == "open"


def test_ref_outside_prefix_from_api_raises(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        github, "read_json_paginated", lambda _endpoint: [{"ref": "refs/heads/develop"}]
    )

    with pytest.raises(MechanizedPRError, match="outside"):
        _sync(clone)


def test_api_failure_clearing_earlier_runs_raises(
    fake: FakeGitHub, origin: Path, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _leave_branch(origin, f"automation/{JOB}/1")

    def broken(endpoint: str) -> bool:
        raise github.GitHubAPIError(1, ("gh", "api", endpoint), "", "HTTP 403")

    monkeypatch.setattr(github, "delete_if_exists", broken)

    with pytest.raises(MechanizedPRError, match="clearing earlier runs failed"):
        _sync(clone)


_GOOD_ENTRY: dict[str, object] = {
    "number": 7,
    "url": "https://github.com/o/r/pull/7",
    "isCrossRepository": False,
    "headRepository": {"name": "r"},
    "headRepositoryOwner": {"login": "o"},
}


@pytest.mark.parametrize(
    ("listing", "match"),
    [
        ({"not": "a list"}, "not a list"),
        (["text"], "not an object"),
        ([{**_GOOD_ENTRY, "isCrossRepository": None}], "no isCrossRepository flag"),
        ([{**_GOOD_ENTRY, "headRepository": None}], "no head repository"),
        ([{**_GOOD_ENTRY, "headRepositoryOwner": {}}], "no head repository"),
        ([{**_GOOD_ENTRY, "number": True}], "no number or URL"),
        ([{**_GOOD_ENTRY, "url": None}], "no number or URL"),
    ],
)
def test_malformed_pr_list_raises(
    fake: FakeGitHub,
    origin: Path,
    clone: Path,
    monkeypatch: pytest.MonkeyPatch,
    listing: object,
    match: str,
) -> None:
    _leave_branch(origin, f"automation/{JOB}/1")

    def read_json(*args: str) -> object:
        if args[:2] == ("pr", "list"):
            return listing
        return fake.read_json(*args)

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(MechanizedPRError, match=match):
        _sync(clone)

    assert fake.prs == []


def test_same_repo_match_ignores_case(fake: FakeGitHub, origin: Path, clone: Path) -> None:
    name = f"automation/{JOB}/1"
    _leave_branch(origin, name)
    fake.prs.append(_PR(7, name, owner="O", name="R"))

    assert _sync(clone).closed == [fake.prs[0].url]


def _api_error(*args: object) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", *map(str, args)), "", "HTTP 502")


@pytest.fixture
def sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The retry backoff of a failed ``gh pr create``, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(pr_create.time, "sleep", slept.append)
    return slept


def test_create_pr_failure_raises(fake: FakeGitHub, clone: Path, sleep: list[float]) -> None:
    """A create that really failed, with no PR open, is retried and then fails the run."""
    fake.create_errors = [_api_error("pr", "create")] * (retry.MAX_RETRIES + 1)
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="opening the PR failed"):
        _sync(clone)

    assert fake.create_calls == retry.MAX_RETRIES + 1
    assert len(sleep) == retry.MAX_RETRIES
    assert fake.prs == []
    assert fake.mutations == []
    assert BRANCH in fake.branches()


def test_create_that_really_failed_once_is_retried(
    fake: FakeGitHub, clone: Path, sleep: list[float]
) -> None:
    fake.create_errors = [_api_error("pr", "create")]
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    assert fake.create_calls == 2
    assert len(sleep) == 1
    assert result.status == "merged"
    assert result.pr_url == fake.prs[0].url


def test_create_that_landed_is_adopted_and_auto_merged(
    fake: FakeGitHub, origin: Path, clone: Path, sleep: list[float]
) -> None:
    """GitHub opened the PR but the response was lost: it is adopted, then auto-merged (#4699)."""
    fake.create_errors = [github.GitHubAPIError(1, ("gh",), "", "HTTP 504: Gateway Timeout")]
    fake.create_lands = True
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    [pr] = fake.prs
    assert fake.create_calls == 1
    assert sleep == []
    assert result == SyncResult(status="merged", pr_url=pr.url, closed=[])
    [mutation] = fake.mutations
    assert mutation["id"] == f"PR_{pr.number}"
    assert mutation["head"] == _git(origin, "rev-parse", BRANCH)
    assert fake.waits == [(pr.url, "squash", 2400)]


def test_open_pr_that_is_not_this_runs_is_refused_by_name(
    fake: FakeGitHub, clone: Path, sleep: list[float]
) -> None:
    """An open PR from the branch whose head is not the pushed commit is never adopted."""
    exists = github.GitHubAPIError(1, ("gh",), "", "a pull request for branch already exists")
    fake.create_errors = [exists]
    fake.create_lands = True
    fake.landed_oid = "f" * 40
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="opening the PR failed") as exc:
        _sync(clone)

    message = str(exc.value)
    assert fake.prs[0].url in message
    assert "it was not adopted" in message
    assert f"its head commit is '{'f' * 40}'" in message
    assert fake.create_calls == 1
    assert sleep == []
    assert fake.mutations == []


def test_create_pr_without_url_raises(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "create_pr", lambda **_kwargs: "")
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="printed no URL"):
        _sync(clone)

    assert fake.branches()


@pytest.mark.parametrize(
    ("view", "match"),
    [
        ("raise", "reading the PR's node ID failed"),
        (["a list"], "gh pr view returned"),
        ({"id": ""}, "has no node ID"),
    ],
)
def test_pr_view_failures_raise(
    fake: FakeGitHub,
    clone: Path,
    monkeypatch: pytest.MonkeyPatch,
    view: object,
    match: str,
) -> None:
    def read_json(*args: str) -> object:
        if args[:2] != ("pr", "view"):
            return fake.read_json(*args)
        if view == "raise":
            raise _api_error(*args)
        return view

    monkeypatch.setattr(github, "read_json", read_json)
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match=match):
        _sync(clone)

    assert not fake.mutations


def test_auto_merge_with_other_method_not_confirmed(fake: FakeGitHub, clone: Path) -> None:
    fake.confirm_auto_merge = False
    fake.auto_merge = {"mergeMethod": "MERGE"}
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="auto-merge is not confirmed"):
        _sync(clone)


# --- Wait until merged (vergil-tooling#4370) ---------------------------------


def test_opened_pr_is_watched_until_merged_with_the_default_deadline(
    fake: FakeGitHub, clone: Path
) -> None:
    (clone / "vergil.toml").write_text("v2\n")

    result = _sync(clone)

    assert result.status == "merged"
    assert fake.waits == [(fake.prs[0].url, "squash", 2400)]


def test_in_sync_clone_waits_for_nothing(fake: FakeGitHub, clone: Path) -> None:
    assert _sync(clone).status == "in_sync"
    assert fake.waits == []


def test_deadline_follows_the_env_override(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VRG_MECHANIZED_PR_DEADLINE", "600")
    (clone / "vergil.toml").write_text("v2\n")

    _sync(clone)

    assert fake.waits == [(fake.prs[0].url, "squash", 600)]


def test_caller_deadline_overrides_the_env(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VRG_MECHANIZED_PR_DEADLINE", "600")
    (clone / "vergil.toml").write_text("v2\n")

    open_sync_pr(REPO, clone, job=JOB, title="chore: sync", body="the body", deadline_secs=6600)

    assert fake.waits == [(fake.prs[0].url, "squash", 6600)]


def test_no_caller_deadline_keeps_the_env_rule(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VRG_MECHANIZED_PR_DEADLINE", "900")
    (clone / "vergil.toml").write_text("v2\n")

    open_sync_pr(REPO, clone, job=JOB, title="chore: sync", body="the body", deadline_secs=None)

    assert fake.waits == [(fake.prs[0].url, "squash", 900)]


@pytest.mark.parametrize("secs", [0, -1, True])
def test_bad_caller_deadline_raises_before_any_pr(fake: FakeGitHub, clone: Path, secs: int) -> None:
    (clone / "vergil.toml").write_text("v2\n")
    with pytest.raises(MechanizedPRError, match="deadline must be a positive whole number"):
        open_sync_pr(REPO, clone, job=JOB, title="t", body="the body", deadline_secs=secs)
    assert fake.prs == []


def test_commit_parent_is_the_clone_head(fake: FakeGitHub, origin: Path, clone: Path) -> None:
    older = _git(clone, "rev-parse", "HEAD")
    seed = origin.parent / "seed"
    (seed / "later.txt").write_text("later\n")
    _git(seed, "add", "later.txt")
    _git(seed, "commit", "-qm", "later")
    _git(seed, "push", "-q", "origin", "develop")
    # The clone stays on the older develop commit; the PR's commit builds on it.
    (clone / "vergil.toml").write_text("v2\n")

    _sync(clone)

    assert _git(origin, "rev-parse", f"{BRANCH}^") == older
    assert _git(origin, "rev-parse", "develop") != older


def test_log_lines_name_closed_opened_and_wait(
    fake: FakeGitHub, origin: Path, clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    earlier = f"automation/{JOB}/1"
    _leave_branch(origin, earlier)
    fake.prs.append(_PR(7, earlier))
    (clone / "vergil.toml").write_text("v2\n")

    _sync(clone)

    out = capsys.readouterr().out
    new_url = fake.prs[1].url
    assert f"{REPO}: closed the earlier run's PR {fake.prs[0].url}" in out
    assert f"{REPO}: opened {new_url}; waiting up to 2400s for it to merge" in out
    assert re.search(rf"{REPO}: the wait for {re.escape(new_url)} ended after \d+s: merged", out)


def test_log_line_names_a_wait_that_did_not_merge(
    fake: FakeGitHub, clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.wait_failure = pr_merge.MergeAbortError("Checks failed on PR x: ci / test")
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(PRNotMergedError):
        _sync(clone)

    assert "ended after 0s: not merged (Checks failed on PR x: ci / test)" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("raw", ["0", "-5", "forty", "1.5"])
def test_malformed_deadline_raises_before_any_pr(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VRG_MECHANIZED_PR_DEADLINE", raw)
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(MechanizedPRError, match="VRG_MECHANIZED_PR_DEADLINE must be a positive"):
        _sync(clone)

    assert fake.prs == []
    assert fake.branches() == ["develop"]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (pr_merge.MergeAbortError("Checks failed on PR x: ci / test"), "Checks failed on PR x"),
        (
            pr_merge.MergeDeadlineError("PR x did not merge within the 2400s deadline"),
            "within the 2400s deadline",
        ),
        (
            github.GitHubAPIError(1, ("gh", "pr", "view"), "", "HTTP 401: Bad credentials"),
            "the wait until merged failed: ",
        ),
    ],
    ids=["abort", "deadline", "api-error"],
)
def test_pr_that_does_not_merge_raises_naming_it(
    fake: FakeGitHub, clone: Path, failure: BaseException, reason: str
) -> None:
    fake.wait_failure = failure
    (clone / "vergil.toml").write_text("v2\n")

    with pytest.raises(PRNotMergedError) as excinfo:
        _sync(clone)

    url = fake.prs[0].url
    assert excinfo.value.pr_url == url
    assert reason in excinfo.value.reason
    assert str(excinfo.value).startswith(f"mechanized PR for {REPO}: {url} did not merge: ")
    assert isinstance(excinfo.value, MechanizedPRError)
