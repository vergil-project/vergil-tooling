"""Tests for the GitHub harvest layer in vergil_tooling.lib.ci_evidence.

All GitHub I/O is mocked at the ``vergil_tooling.lib.github`` boundary (``read_json`` /
``read_json_paginated`` / ``read_output`` / ``run``), mirroring the ``test_github.py``
monkeypatch style: the harvest functions are pure orchestration over those
wrappers, so the tests assert the selection/filter logic, never the network.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib import ci_evidence, github, trust
from vergil_tooling.lib.ci_evidence import (
    NoQualifyingRunError,
    ReleasePrUnresolvedError,
    download_evidence_artifacts,
    read_gate_conclusions,
    resolve_release_pr,
)
from vergil_tooling.lib.github_config import GHAS_INTEGRATION_ID, GITHUB_ACTIONS_INTEGRATION_ID
from vergil_tooling.lib.trust import PRRef, Role, Skipped

if TYPE_CHECKING:
    from pathlib import Path


# The head repository of a workflow run built from the release repo itself.
_OWN = {"full_name": "o/r"}
_MERGED = "2026-07-12T00:00:00Z"
_SPAM = "SPAM-MARKER-TEXT see https://example.invalid/x"


# --- resolve_release_pr -------------------------------------------------


def _rest_pr(number: int, branch: str, *, merged: bool = True) -> dict[str, Any]:
    """A ``/commits/{sha}/pulls`` entry: the fields the resolver reads."""
    return {
        "number": number,
        "merged_at": _MERGED if merged else None,
        "head": {"ref": branch},
        "title": _SPAM,
    }


def _pr_ref(number: int, branch: str) -> PRRef:
    """A merged same-repo trusted PR, as ``trust.prs_for_branch`` returns it."""
    return PRRef(
        number=number,
        url=f"https://github.com/o/r/pull/{number}",
        title="chore(release): 1.2.3",
        head=branch,
        head_oid="a" * 40,
        base="develop",
        author_role=Role.AGENTS,
        state="MERGED",
    )


def _branches(
    monkeypatch: pytest.MonkeyPatch,
    by_branch: dict[str, tuple[list[PRRef], list[Skipped]]],
) -> list[tuple[str, str, str]]:
    """Serve ``trust.prs_for_branch`` from *by_branch*; return the calls made."""
    calls: list[tuple[str, str, str]] = []

    def _lookup(repo: str, branch: str, *, state: str) -> tuple[list[PRRef], list[Skipped]]:
        calls.append((repo, branch, state))
        return by_branch.get(branch, ([], []))

    monkeypatch.setattr(trust, "prs_for_branch", _lookup)
    return calls


def test_resolve_release_pr_takes_the_trusted_merged_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            _rest_pr(10, "feature/10-x", merged=False),
            _rest_pr(42, "release/1.2.3"),
        ],
    )
    calls = _branches(monkeypatch, {"release/1.2.3": ([_pr_ref(42, "release/1.2.3")], [])})
    assert resolve_release_pr("o/r", "abc") == 42
    assert calls == [("o/r", "release/1.2.3", "merged")]


def test_resolve_release_pr_reads_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #3162: the merged PR on page 2 of ``/commits/{sha}/pulls`` wins."""
    calls: list[tuple[str, ...]] = []

    def fake_read_output(*args: str, **_: object) -> str:
        calls.append(args)
        page1 = [_rest_pr(n, f"feature/{n}", merged=False) for n in range(1, 31)]
        page2 = [_rest_pr(77, "release/1.2.3")]
        return json.dumps(page1) + json.dumps(page2)

    monkeypatch.setattr(github, "read_output", fake_read_output)
    _branches(monkeypatch, {"release/1.2.3": ([_pr_ref(77, "release/1.2.3")], [])})
    assert resolve_release_pr("o/r", "abc") == 77
    assert calls == [("api", "--paginate", "repos/o/r/commits/abc/pulls?per_page=100")]


def test_evidence_has_no_first_pr_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only unmerged PRs: an error naming them, never the first (GHSA-r4j2-cwcp-xg56)."""
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [_rest_pr(7, "feature/7", merged=False), _rest_pr(8, "x", merged=False)],
    )
    calls = _branches(monkeypatch, {})
    with pytest.raises(ReleasePrUnresolvedError, match=r"only unmerged PR\(s\) #7, #8"):
        resolve_release_pr("o/r", "abc")
    assert calls == []


def test_evidence_ignores_fork_pr_for_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merged fork PR is never the release PR, and it is reported, metadata only."""
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [_rest_pr(6, "release/1.2.3")]
    )
    fork = Skipped("o/r", 6, "pr", "drive-by", _MERGED, "head", trust.CROSS_REPOSITORY)
    _branches(monkeypatch, {"release/1.2.3": ([], [fork])})
    with pytest.raises(ReleasePrUnresolvedError, match="#6 are not same-repo PRs"):
        resolve_release_pr("o/r", "abc")
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: o/r#6 pr drive-by at {_MERGED} on head (cross-repository)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_resolve_release_pr_never_takes_another_number_on_the_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trusted PR with another number on the same branch never stands in for the fork's."""
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [_rest_pr(6, "release/1.2.3")]
    )
    _branches(monkeypatch, {"release/1.2.3": ([_pr_ref(5, "release/1.2.3")], [])})
    with pytest.raises(ReleasePrUnresolvedError, match="are not same-repo PRs"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_several_trusted_merged_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [_rest_pr(41, "release/a"), _rest_pr(42, "release/b")],
    )
    _branches(
        monkeypatch,
        {
            "release/a": ([_pr_ref(41, "release/a")], []),
            "release/b": ([_pr_ref(42, "release/b")], []),
        },
    )
    with pytest.raises(ReleasePrUnresolvedError, match="several trusted merged PRs #41, #42"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_uses_subject_when_api_has_no_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(
        github,
        "read_output",
        lambda *a, **k: "Merge pull request #55 from o/release/x\n\nbody",
    )
    seen: list[tuple[str, ...]] = []

    def _read_json(*args: str) -> dict[str, Any]:
        seen.append(args)
        return _rest_pr(55, "release/x")

    monkeypatch.setattr(github, "read_json", _read_json)
    _branches(monkeypatch, {"release/x": ([_pr_ref(55, "release/x")], [])})
    assert resolve_release_pr("o/r", "abc") == 55
    assert seen == [("api", "repos/o/r/pulls/55")]


def test_resolve_release_pr_uses_squash_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "chore(release): prepare (#99)")
    monkeypatch.setattr(github, "read_json", lambda *a: _rest_pr(99, "release/y"))
    _branches(monkeypatch, {"release/y": ([_pr_ref(99, "release/y")], [])})
    assert resolve_release_pr("o/r", "abc") == 99


def test_resolve_release_pr_subject_pr_must_be_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "chore(release): prepare (#99)")
    monkeypatch.setattr(github, "read_json", lambda *a: _rest_pr(99, "release/y"))
    outsider = Skipped("o/r", 99, "pr", "drive-by", _MERGED, "author", trust.UNTRUSTED)
    _branches(monkeypatch, {"release/y": ([], [outsider])})
    with pytest.raises(ReleasePrUnresolvedError, match="#99 are not same-repo PRs"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_raises_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "")
    with pytest.raises(ReleasePrUnresolvedError, match="cannot resolve release PR"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_merged_pr_without_branch_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [{"number": 42, "merged_at": _MERGED}],
    )
    with pytest.raises(ValueError, match="PR #42 has no head branch"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_pr_without_number_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [{"number": "42", "merged_at": _MERGED, "head": {"ref": "x"}}],
    )
    with pytest.raises(ValueError, match="a PR of commit abc has no usable number"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_subject_pr_not_an_object_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "chore(release): prepare (#99)")
    monkeypatch.setattr(github, "read_json", lambda *a: [])
    with pytest.raises(ValueError, match="PR #99 of o/r is not a JSON object"):
        resolve_release_pr("o/r", "abc")


def test_resolve_release_pr_trust_read_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [_rest_pr(42, "release/1.2.3")]
    )

    def _fail(*_a: Any, **_k: Any) -> Any:
        raise trust.TrustReadError("prs_for_branch o/r release/1.2.3", "the GraphQL call failed")

    monkeypatch.setattr(trust, "prs_for_branch", _fail)
    with pytest.raises(trust.TrustReadError):
        resolve_release_pr("o/r", "abc")


# --- select_passed_attempt (issue #5153) --------------------------------
#
# Modelled on vergil-fixture-ts-lib's release 0.1.10: the release PR's CI run
# 45505436055 passed at attempt 1, and attempt 2, a re-run after the release
# PR merged, failed its version-bump check. The run list reports only a run's
# latest attempt, so attempt 1 is read from the attempts endpoint.

_HEAD = "ff154258a8e3c09d5d591cb0b65e51ac08efa7db"
_RUN_URL = "https://github.com/o/r/actions/runs/45505436055"


def _ci_run(
    run_id: int, attempt: object, conclusion: str | None, started: str, **extra: Any
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "CI",
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "head_repository": _OWN,
        "head_sha": _HEAD,
        "run_attempt": attempt,
        "run_started_at": started,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "check_suite_id": _SUITE,
        **extra,
    }


def _attempts_api(
    monkeypatch: pytest.MonkeyPatch,
    runs: list[dict[str, Any]],
    attempts: dict[tuple[int, int], object],
) -> list[tuple[str, ...]]:
    """Serve *runs* as the head-SHA run list and *attempts* by (run id, attempt)."""
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: runs)
    calls: list[tuple[str, ...]] = []

    def read_json(*args: str) -> object:
        calls.append(args)
        assert args[0] == "api"
        parts = args[1].split("/")
        assert parts[:4] == ["repos", "o", "r", "actions"]
        assert parts[6] == "attempts"
        return attempts[(int(parts[5]), int(parts[7]))]

    monkeypatch.setattr(github, "read_json", read_json)
    return calls


def test_select_passed_attempt_latest_attempt_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _ci_run(45505436055, 1, "success", "2026-11-24T22:55:49Z")
    calls = _attempts_api(monkeypatch, [run], {})
    found = ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert found == ci_evidence.PassedAttempt(
        run_id=45505436055, attempt=1, latest_attempt=1, url=_RUN_URL, check_suite_id=_SUITE
    )
    assert found.is_latest
    assert found.next_started_at is None
    assert calls == []


def test_select_passed_attempt_skips_a_cancelled_run_for_a_passing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = _ci_run(1, 1, "cancelled", "2026-07-12T00:00:00Z")
    passing = _ci_run(42, 1, "success", "2026-07-12T00:05:00Z")
    _attempts_api(monkeypatch, [cancelled, passing], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD).run_id == 42


def test_select_passed_attempt_picks_the_latest_passing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _ci_run(1, 1, "success", "2026-07-12T00:00:00Z")
    newer = _ci_run(2, 1, "success", "2026-07-12T00:09:00Z")
    _attempts_api(monkeypatch, [older, newer], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD).run_id == 2


def test_select_passed_attempt_ignores_other_workflows_and_in_progress_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nightly = _ci_run(1, 1, "success", "2026-07-12T00:09:00Z", name="Nightly")
    running = _ci_run(2, 1, None, "2026-07-12T00:08:00Z")
    passing = _ci_run(3, 1, "success", "2026-07-12T00:00:00Z")
    _attempts_api(monkeypatch, [nightly, running, passing], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD).run_id == 3


def test_select_passed_attempt_honors_the_workflow_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _ci_run(7, 1, "success", "t", name="Release")
    _attempts_api(monkeypatch, [release], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD, workflow="Release").run_id == 7


def test_select_passed_attempt_ignores_a_fork_run_of_the_same_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork's newer green CI run of the same commit never attests the release."""
    own = _ci_run(1, 1, "success", "2026-07-12T00:00:00Z")
    fork = _ci_run(2, 1, "success", "2026-07-12T00:09:00Z")
    fork["head_repository"] = {"full_name": "drive-by/r"}
    _attempts_api(monkeypatch, [own, fork], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD).run_id == 1


def test_select_passed_attempt_skips_a_run_without_head_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no head repository (its fork deleted) is skipped, never a stop."""
    own = _ci_run(1, 1, "success", "2026-07-12T00:00:00Z")
    gone = _ci_run(3, 1, "success", "2026-07-12T00:09:00Z", head_repository=None)
    bare = {k: v for k, v in gone.items() if k != "head_repository"} | {"id": 4}
    _attempts_api(monkeypatch, [own, gone, bare], {})
    assert ci_evidence.select_passed_attempt("o/r", _HEAD).run_id == 1


def test_select_passed_attempt_refuses_a_passing_run_with_no_check_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = {k: v for k, v in _ci_run(9, 1, "success", "t").items() if k != "check_suite_id"}
    _attempts_api(monkeypatch, [run], {})
    with pytest.raises(ValueError, match="has no check suite"):
        ci_evidence.select_passed_attempt("o/r", _HEAD)


def test_select_passed_attempt_takes_the_attempt_that_passed_before_a_failed_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latest attempt failed (version-bump, after the merge); attempt 1 passed."""
    latest = _ci_run(45505436055, 2, "failure", "2026-11-24T23:09:08Z")
    first = _ci_run(45505436055, 1, "success", "2026-11-24T22:55:49Z")
    first["html_url"] = f"{_RUN_URL}/attempts/1"
    calls = _attempts_api(monkeypatch, [latest], {(45505436055, 1): first})
    found = ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert found == ci_evidence.PassedAttempt(
        run_id=45505436055,
        attempt=1,
        latest_attempt=2,
        url=f"{_RUN_URL}/attempts/1",
        check_suite_id=_SUITE,
        next_started_at="2026-11-24T23:09:08Z",
    )
    assert not found.is_latest
    assert calls == [("api", "repos/o/r/actions/runs/45505436055/attempts/1")]


def test_select_passed_attempt_takes_the_most_recent_passing_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = _ci_run(7, 4, "failure", "2026-11-24T23:30:00Z")
    attempts: dict[tuple[int, int], object] = {
        (7, 3): _ci_run(7, 3, "failure", "2026-11-24T23:20:00Z"),
        (7, 2): _ci_run(7, 2, "success", "2026-11-24T23:10:00Z"),
        (7, 1): _ci_run(7, 1, "success", "2026-11-24T23:00:00Z"),
    }
    calls = _attempts_api(monkeypatch, [latest], attempts)
    found = ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert (found.run_id, found.attempt, found.latest_attempt) == (7, 2, 4)
    # The window of attempt 2 ends where attempt 3, not the latest, began.
    assert found.next_started_at == "2026-11-24T23:20:00Z"
    # It stops at the newest attempt that passed and reads no older one.
    assert [c[1].rsplit("/", 1)[1] for c in calls] == ["3", "2"]


def test_select_passed_attempt_prefers_the_newest_across_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _ci_run(1, 1, "success", "2026-11-24T22:00:00Z")
    newer = _ci_run(2, 2, "failure", "2026-11-24T23:30:00Z")
    attempts: dict[tuple[int, int], object] = {
        (2, 1): _ci_run(2, 1, "success", "2026-11-24T23:00:00Z"),
    }
    _attempts_api(monkeypatch, [older, newer], attempts)
    found = ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert (found.run_id, found.attempt) == (2, 1)


def test_select_passed_attempt_no_attempt_passed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = _ci_run(9, 2, "failure", "2026-11-24T23:09:08Z")
    attempts: dict[tuple[int, int], object] = {
        (9, 1): _ci_run(9, 1, "failure", "2026-11-24T22:55:49Z"),
    }
    _attempts_api(monkeypatch, [latest], attempts)
    with pytest.raises(NoQualifyingRunError) as exc:
        ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert exc.value.visible is True
    assert exc.value.head_sha == _HEAD


def test_select_passed_attempt_no_own_ci_run_is_not_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fork = _ci_run(3, 1, "success", "t", head_repository={"full_name": "drive-by/r"})
    other = _ci_run(4, 1, "success", "t", name="Docs")
    _attempts_api(monkeypatch, [fork, other], {})
    with pytest.raises(NoQualifyingRunError) as exc:
        ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert exc.value.visible is False


def test_select_passed_attempt_never_reads_a_fork_runs_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork's run is never read for attempts, so its passing attempt never counts."""
    fork = _ci_run(3, 2, "failure", "t", head_repository={"full_name": "drive-by/r"})
    own = _ci_run(5, 1, "failure", "t")
    calls = _attempts_api(monkeypatch, [fork, own], {})
    with pytest.raises(NoQualifyingRunError):
        ci_evidence.select_passed_attempt("o/r", _HEAD)
    assert calls == []


def test_select_passed_attempt_refuses_an_attempt_of_another_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = _ci_run(9, 2, "failure", "t")
    stray = _ci_run(9, 1, "success", "t", head_sha="0" * 40)
    _attempts_api(monkeypatch, [latest], {(9, 1): stray})
    with pytest.raises(ValueError, match="attempt 1 of run 9 is for head"):
        ci_evidence.select_passed_attempt("o/r", _HEAD)


@pytest.mark.parametrize("attempt", [None, "2", True, 0])
def test_select_passed_attempt_refuses_a_run_with_no_attempt_number(
    monkeypatch: pytest.MonkeyPatch, attempt: object
) -> None:
    run = _ci_run(9, attempt, "failure", "t")
    _attempts_api(monkeypatch, [run], {})
    with pytest.raises(ValueError, match="no usable run_attempt"):
        ci_evidence.select_passed_attempt("o/r", _HEAD)


def test_select_passed_attempt_refuses_a_non_object_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = _ci_run(9, 2, "failure", "t")
    _attempts_api(monkeypatch, [latest], {(9, 1): ["not", "an", "object"]})
    with pytest.raises(ValueError, match="attempt 1 of run 9 is not a JSON object"):
        ci_evidence.select_passed_attempt("o/r", _HEAD)


# --- attempt_scope / attempt-scoped harvest (issue #5153) ---------------
#
# Attempt 1 of vergil-fixture-ts-lib's CI run 45505436055 (release 0.1.10),
# as its attempt-jobs list reports it (a subset, real times); attempt 2, a
# re-run after the release PR merged, began at 23:09:08 and failed
# "version / version-bump".

_NEXT_START = "2026-11-24T23:09:08Z"
_EARLIER = ci_evidence.PassedAttempt(
    run_id=45505436055,
    attempt=1,
    latest_attempt=2,
    url=f"{_RUN_URL}/attempts/1",
    check_suite_id=501,
    next_started_at=_NEXT_START,
)


def _job(
    name: str, started: str | None, completed: str, conclusion: str = "success", **extra: Any
) -> dict[str, Any]:
    return {
        "id": abs(hash(name)) % 10**9,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": _HEAD,
        "started_at": started,
        "completed_at": completed,
        **extra,
    }


_ATTEMPT_1_JOBS = [
    _job("audit / matrix", "2026-11-24T22:55:52Z", "2026-11-24T22:56:09Z"),
    _job("security / codeql", "2026-11-24T22:57:52Z", "2026-11-24T22:59:05Z"),
    _job("security / evidence", "2026-11-24T22:59:40Z", "2026-11-24T22:59:56Z"),
    _job("version / version-bump", "2026-11-24T22:58:11Z", "2026-11-24T22:59:00Z"),
    _job("test / unit / node-26", "2026-11-24T22:59:51Z", "2026-11-24T23:00:28Z"),
]


def _serve_pages(monkeypatch: pytest.MonkeyPatch, pages: dict[str, list[Any]]) -> list[str]:
    """Serve ``read_json_paginated`` by endpoint prefix; record each endpoint read."""
    seen: list[str] = []

    def _read(endpoint: str, key: str | None = None, **_k: Any) -> list[Any]:
        seen.append(endpoint)
        for prefix, items in pages.items():
            if endpoint.startswith(prefix):
                return items
        msg = f"unexpected endpoint {endpoint}"
        raise AssertionError(msg)

    monkeypatch.setattr(github, "read_json_paginated", _read)
    return seen


_JOBS_ENDPOINT = "repos/o/r/actions/runs/45505436055/attempts/1/jobs"


def test_attempt_scope_reads_the_attempts_own_jobs_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    seen = _serve_pages(monkeypatch, {_JOBS_ENDPOINT: _ATTEMPT_1_JOBS})
    scope = ci_evidence.attempt_scope("o/r", _HEAD, _EARLIER)
    assert seen == [_JOBS_ENDPOINT]
    # Attempt 1's version-bump passed, whatever attempt 2's check run says.
    assert scope.job_conclusions["version / version-bump"] == "success"
    assert scope.start == datetime(2026, 11, 24, 22, 55, 52, tzinfo=UTC)
    assert scope.end == datetime(2026, 11, 24, 23, 9, 8, tzinfo=UTC)
    assert scope.holds(datetime(2026, 11, 24, 23, 0, 0, tzinfo=UTC))
    assert not scope.holds(scope.end)
    assert not scope.holds(datetime(2026, 11, 24, 22, 55, 0, tzinfo=UTC))


def test_attempt_scope_keeps_a_carried_over_jobs_own_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run of failed jobs lists the jobs it carried over with their first start."""
    from datetime import UTC, datetime

    carried = _job("docs / docs", "2026-11-24T23:01:02Z", "2026-11-24T23:01:40Z")
    rerun = _job("release / release", "2026-11-24T23:27:32Z", "2026-11-24T23:28:00Z")
    passed = replace(_EARLIER, next_started_at="2026-11-24T23:40:00Z")
    _serve_pages(monkeypatch, {_JOBS_ENDPOINT: [carried, rerun]})
    scope = ci_evidence.attempt_scope("o/r", _HEAD, passed)
    assert scope.start == datetime(2026, 11, 24, 23, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("jobs", "match"),
    [
        ([], "lists no jobs"),
        (["job"], "not an object"),
        ([_job("", "2026-11-24T22:56:00Z", "2026-11-24T22:57:00Z")], "with no name"),
        (
            [_job("a", "2026-11-24T22:56:00Z", "2026-11-24T22:57:00Z", head_sha="0" * 40)],
            "is for head",
        ),
        (
            [_job("a", "2026-11-24T22:56:00Z", "2026-11-24T22:57:00Z", status="in_progress")],
            "not completed",
        ),
        (
            [_job("a", "2026-11-24T22:56:00Z", "2026-11-24T23:09:08Z")],
            "after the next attempt began",
        ),
        ([_job("a", "2026-11-24T22:56:00Z", "soon")], "unreadable timestamp"),
        ([_job("a", "2026-11-24T22:56:00Z", "2026-11-24T22:57:00")], "no time zone"),
        ([_job("a", None, "2026-11-24T22:57:00Z")], "has no timestamp"),
        (
            [
                _job("a", "2026-11-24T22:56:00Z", "2026-11-24T22:57:00Z"),
                _job("a", "2026-11-24T22:58:00Z", "2026-11-24T22:59:00Z"),
            ],
            "more than once",
        ),
    ],
)
def test_attempt_scope_refuses_a_job_it_cannot_tie_to_the_attempt(
    monkeypatch: pytest.MonkeyPatch, jobs: list[Any], match: str
) -> None:
    _serve_pages(monkeypatch, {_JOBS_ENDPOINT: jobs})
    with pytest.raises(ci_evidence.AttemptScopeError, match=match):
        ci_evidence.attempt_scope("o/r", _HEAD, _EARLIER)


def test_attempt_scope_refuses_a_latest_attempt() -> None:
    latest = replace(_EARLIER, attempt=2, next_started_at=None)
    with pytest.raises(ci_evidence.AttemptScopeError, match="latest attempt"):
        ci_evidence.attempt_scope("o/r", _HEAD, latest)


def test_attempt_scope_error_is_an_evidence_artifact_error() -> None:
    """The CLI reports it as a one-line substantive failure, like its parent."""
    assert issubclass(ci_evidence.AttemptScopeError, ci_evidence.EvidenceArtifactError)


def _scanner(
    name: str, started: str, conclusion: str = "success", *, run_id: int = 1
) -> dict[str, Any]:
    """A GitHub Advanced Security scanner check run on the commit."""
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "started_at": started,
        "app": {"id": GHAS_INTEGRATION_ID},
        "check_suite": {"id": 777},
    }


def _scope(monkeypatch: pytest.MonkeyPatch) -> ci_evidence.AttemptScope:
    _serve_pages(monkeypatch, {_JOBS_ENDPOINT: _ATTEMPT_1_JOBS})
    return ci_evidence.attempt_scope("o/r", _HEAD, _EARLIER)


def test_read_attempt_gate_conclusions_reads_the_attempt_not_the_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    scope = _scope(monkeypatch)
    check_runs = [
        # Attempt 2's failed version-bump check run is the latest on the commit.
        {**_check("version / version-bump", "failure"), "started_at": "2026-11-24T23:10:00Z"},
        # The newest in the window wins, in whatever order they are listed.
        _scanner("CodeQL", "2026-11-24T22:59:15Z", run_id=138840877068),
        _scanner("CodeQL", "2026-11-24T22:59:10Z", run_id=138840877066),
        _scanner("CodeQL", "2026-11-24T22:59:20Z", "neutral", run_id=138840877070),
        # A scanner check run of attempt 2 is out of the window.
        _scanner("Trivy", "2026-11-24T23:10:30Z", "failure", run_id=138846500000),
        _scanner("Trivy", "2026-11-24T22:58:30Z", run_id=138840418634),
        # A check run with a scanner's name from another App never counts.
        {**_check("Semgrep OSS", "success", app_id=999), "started_at": "2026-11-24T22:59:00Z"},
    ]
    seen = _serve_pages(monkeypatch, {"repos/o/r/commits/": check_runs})
    required = (
        EvidenceGate(name="security", checks=("security / codeql", "CodeQL", "Trivy")),
        EvidenceGate(name="version", checks=("version / version-bump",)),
    )
    result = ci_evidence.read_attempt_gate_conclusions("o/r", _HEAD, scope, required)
    assert seen == [f"repos/o/r/commits/{_HEAD}/check-runs?filter=all"]
    assert result["version / version-bump"] == "success"
    assert result["CodeQL"] == "neutral"
    assert result["Trivy"] == "success"
    assert "Semgrep OSS" not in result


def test_read_attempt_gate_conclusions_refuses_a_required_check_out_of_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    scope = _scope(monkeypatch)
    later = [_scanner("Semgrep OSS", "2026-11-24T23:10:30Z")]
    _serve_pages(monkeypatch, {"repos/o/r/commits/": later})
    required = (EvidenceGate(name="security", checks=("security / codeql", "Semgrep OSS")),)
    with pytest.raises(
        ci_evidence.AttemptScopeError,
        match=r"required checks \['Semgrep OSS'\] cannot be tied to attempt 1 of CI run",
    ):
        ci_evidence.read_attempt_gate_conclusions("o/r", _HEAD, scope, required)


def test_download_evidence_artifacts_in_scope_takes_the_attempts_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attempt 2's newer copy of a gate's artifact is never consumed for attempt 1."""
    from vergil_tooling.lib.github_config import EvidenceGate

    scope = _scope(monkeypatch)
    artifacts = [
        {"name": "ci-evidence-security", "id": 11, "created_at": "2026-11-24T22:59:50Z"},
        {"name": "ci-evidence-security", "id": 22, "created_at": "2026-11-24T23:10:40Z"},
        {"name": "ci-evidence-version", "id": 33, "created_at": "2026-11-24T22:59:01Z"},
    ]
    _serve_pages(monkeypatch, {"repos/o/r/actions/runs/45505436055/artifacts": artifacts})
    calls = _fake_downloads(monkeypatch)
    required = (
        EvidenceGate(name="security", checks=()),
        EvidenceGate(name="version", checks=()),
    )
    result = download_evidence_artifacts(
        "o/r", 45505436055, tmp_path / "gates", required, scope=scope
    )
    assert result == [tmp_path / "gates" / "security", tmp_path / "gates" / "version"]
    assert calls == [
        "repos/o/r/actions/artifacts/11/zip",
        "repos/o/r/actions/artifacts/33/zip",
    ]


def test_download_evidence_artifacts_in_scope_refuses_a_gate_with_no_copy_of_the_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    scope = _scope(monkeypatch)
    artifacts = [{"name": "ci-evidence-version", "id": 44, "created_at": "2026-11-24T23:10:40Z"}]
    _serve_pages(monkeypatch, {"repos/o/r/actions/runs/45505436055/artifacts": artifacts})
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="version", checks=()),)
    with pytest.raises(
        ci_evidence.AttemptScopeError,
        match=r"gate 'version' has evidence artifacts \(ids \[44\]\), but none was created",
    ):
        download_evidence_artifacts("o/r", 45505436055, tmp_path / "gates", required, scope=scope)
    assert calls == []


def test_download_evidence_artifacts_in_scope_refuses_a_copy_with_no_creation_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    scope = _scope(monkeypatch)
    artifacts = [{"name": "ci-evidence-version", "id": 55}]
    _serve_pages(monkeypatch, {"repos/o/r/actions/runs/45505436055/artifacts": artifacts})
    _fake_downloads(monkeypatch)
    with pytest.raises(ci_evidence.AttemptScopeError, match="artifact 55 has no timestamp"):
        download_evidence_artifacts(
            "o/r",
            45505436055,
            tmp_path / "gates",
            (EvidenceGate(name="version", checks=()),),
            scope=scope,
        )


def test_run_harvest_of_an_earlier_attempt_reads_it_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The 0.1.10 case end to end: attempt 1's checks and artifacts, named in the state."""
    from vergil_tooling.lib.github_config import EvidenceGate

    required = (EvidenceGate(name="version", checks=("version / version-bump",)),)
    monkeypatch.setattr(ci_evidence, "resolve_release_pr", lambda _repo, _sha: 98)
    monkeypatch.setattr(github, "head_sha", lambda _pr: _HEAD)
    monkeypatch.setattr(ci_evidence, "wait_for_passed_attempt", lambda *_a, **_k: _EARLIER)
    monkeypatch.setattr(ci_evidence, "resolve_required_gates", lambda _repo, _ref: required)

    def _latest_only(*_a: Any, **_k: Any) -> Any:
        msg = "the latest attempt's check runs must not be read"
        raise AssertionError(msg)

    monkeypatch.setattr(ci_evidence, "read_gate_conclusions", _latest_only)
    _serve_pages(
        monkeypatch,
        {
            _JOBS_ENDPOINT: _ATTEMPT_1_JOBS,
            "repos/o/r/commits/": [],
            "repos/o/r/actions/runs/45505436055/artifacts": [
                {"name": "ci-evidence-version", "id": 33, "created_at": "2026-11-24T22:59:01Z"},
                {"name": "ci-evidence-version", "id": 66, "created_at": "2026-11-24T23:10:40Z"},
            ],
        },
    )
    archive = _zip_bytes(
        {
            "evidence.json": json.dumps({"gate": "version", "tools": [], "metrics": {}}).encode(),
            "version.txt": b"0.1.10",
        }
    )
    calls = _fake_downloads(monkeypatch, {"repos/o/r/actions/artifacts/33/zip": archive})

    with caplog.at_level("WARNING", logger="vergil_tooling.lib.ci_evidence"):
        state = ci_evidence.run_harvest("o/r", "10586fe76d3f", tmp_path / "staging")

    assert calls == ["repos/o/r/actions/artifacts/33/zip"]
    assert state.ci_run_urls == (f"{_RUN_URL}/attempts/1",)
    assert state.gate_conclusions == {"version": "success"}
    assert state.checks["version / version-bump"] == "success"
    assert "latest attempt, 2, did not pass; harvesting attempt 1" in caplog.text


def test_run_harvest_of_a_latest_attempt_reads_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run whose latest attempt passed is harvested exactly as it was."""
    from vergil_tooling.lib.github_config import EvidenceGate

    latest = replace(_EARLIER, latest_attempt=1, url=_RUN_URL, next_started_at=None)
    required = (EvidenceGate(name="version", checks=("version / version-bump",)),)
    monkeypatch.setattr(ci_evidence, "resolve_release_pr", lambda _repo, _sha: 98)
    monkeypatch.setattr(github, "head_sha", lambda _pr: _HEAD)
    monkeypatch.setattr(ci_evidence, "wait_for_passed_attempt", lambda *_a, **_k: latest)
    monkeypatch.setattr(ci_evidence, "resolve_required_gates", lambda _repo, _ref: required)
    suites: list[int] = []

    def _conclusions(_repo: str, _sha: str, *, check_suite_id: int) -> dict[str, str]:
        suites.append(check_suite_id)
        return {"version / version-bump": "success"}

    def _scoped(*_a: Any, **_k: Any) -> Any:
        msg = "a latest attempt is never scoped"
        raise AssertionError(msg)

    monkeypatch.setattr(ci_evidence, "read_gate_conclusions", _conclusions)
    monkeypatch.setattr(ci_evidence, "attempt_scope", _scoped)
    _serve_pages(
        monkeypatch,
        {
            "repos/o/r/actions/runs/45505436055/artifacts": [
                {"name": "ci-evidence-version", "id": 66, "created_at": "2026-11-24T23:10:40Z"},
            ],
        },
    )
    archive = _zip_bytes({"evidence.json": b'{"gate": "version"}', "version.txt": b"0.1.10"})
    calls = _fake_downloads(monkeypatch, {"repos/o/r/actions/artifacts/66/zip": archive})
    state = ci_evidence.run_harvest("o/r", "10586fe76d3f", tmp_path / "staging")
    assert suites == [501]
    assert calls == ["repos/o/r/actions/artifacts/66/zip"]
    assert state.ci_run_urls == (_RUN_URL,)


# --- wait_for_passed_attempt (issue #3434) ------------------------------

_SUCCESS_RUN = {
    "id": 9,
    "name": "CI",
    "status": "completed",
    "conclusion": "success",
    "head_repository": _OWN,
    "run_started_at": "t",
    "run_attempt": 1,
    "html_url": "https://github.com/o/r/actions/runs/9",
    "check_suite_id": 501,
}


class _FakeClock:
    """A monotonic clock that advances only when the fake sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


def _pages(monkeypatch: pytest.MonkeyPatch, *pages: list[dict[str, Any]]) -> list[int]:
    """Serve each lookup the next page (the last repeats); return the call counter."""
    calls = [0]

    def _read(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        page = pages[min(calls[0], len(pages) - 1)]
        calls[0] += 1
        return page

    monkeypatch.setattr(github, "read_json_paginated", _read)
    return calls


def test_wait_for_passed_attempt_returns_at_once_when_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _pages(monkeypatch, [_SUCCESS_RUN])
    fake = _FakeClock()
    passed = ci_evidence.wait_for_passed_attempt("o/r", "abc", sleep=fake.sleep, clock=fake.clock)
    assert passed.run_id == 9
    assert calls[0] == 1
    assert fake.sleeps == []


def test_wait_for_passed_attempt_passes_once_lagging_run_appears(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run the head-SHA index shows only on a later query passes the gate."""
    calls = _pages(monkeypatch, [], [], [_SUCCESS_RUN])
    fake = _FakeClock()
    passed = ci_evidence.wait_for_passed_attempt(
        "o/r", "abc", grace=300, sleep=fake.sleep, clock=fake.clock
    )
    assert passed.run_id == 9
    assert calls[0] == 3
    assert len(fake.sleeps) == 2
    retries = [r.getMessage() for r in caplog.records if "not visible yet" in r.getMessage()]
    assert len(retries) == 2
    assert "CI run for abc not visible yet — GitHub index may lag" in retries[0]
    assert "up to 300s total" in retries[0]


def test_wait_for_passed_attempt_fails_loudly_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that never appears fails after the bounded grace, naming the wait."""
    calls = _pages(monkeypatch, [])
    fake = _FakeClock()
    with pytest.raises(NoQualifyingRunError) as exc:
        ci_evidence.wait_for_passed_attempt(
            "o/r", "abc", grace=30, sleep=fake.sleep, clock=fake.clock
        )
    assert exc.value.head_sha == "abc"
    assert exc.value.visible is False
    assert str(exc.value) == (
        "no completed+success CI run for head SHA abc"
        " (no run became visible within 30s; GitHub's run index may lag)"
    )
    # Bounded: the last sleep is clamped to land exactly on the deadline.
    assert fake.now == pytest.approx(30)
    assert calls[0] == len(fake.sleeps) + 1


def test_wait_for_passed_attempt_zero_grace_fails_on_first_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _pages(monkeypatch, [])
    fake = _FakeClock()
    with pytest.raises(NoQualifyingRunError):
        ci_evidence.wait_for_passed_attempt(
            "o/r", "abc", grace=0, sleep=fake.sleep, clock=fake.clock
        )
    assert calls[0] == 1
    assert fake.sleeps == []


def test_wait_for_passed_attempt_visible_failed_run_fails_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible but failed run is a real verdict, not index lag: no retry."""
    failed = {**_SUCCESS_RUN, "conclusion": "failure"}
    calls = _pages(monkeypatch, [failed])
    fake = _FakeClock()
    with pytest.raises(NoQualifyingRunError) as exc:
        ci_evidence.wait_for_passed_attempt("o/r", "abc", sleep=fake.sleep, clock=fake.clock)
    assert exc.value.visible is True
    assert "may lag" not in str(exc.value)
    assert calls[0] == 1
    assert fake.sleeps == []


def test_wait_for_passed_attempt_defaults_to_real_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without injection it uses ``time.sleep``/``time.monotonic``."""
    _pages(monkeypatch, [], [_SUCCESS_RUN])
    slept: list[float] = []
    monkeypatch.setattr(ci_evidence.time, "sleep", slept.append)
    assert ci_evidence.wait_for_passed_attempt("o/r", "abc").run_id == 9
    assert len(slept) == 1


def test_select_passed_attempt_issues_a_get_not_a_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runs query must reach ``GET /actions/runs`` — never a POST (issue #2335).

    ``gh api`` switches to POST the instant any ``-f``/``-F`` field is present,
    and ``GET /actions/runs`` is GET-only, so a ``-f head_sha=…`` field produced
    an HTTP 404 that silently sank the whole harvest. This asserts the *call
    shape* — no ``-f``/``-F`` fields (unless paired with an explicit ``-X GET``)
    and the filter carried in the URL — so the regression cannot recur behind a
    mocked return value.
    """
    seen: dict[str, tuple[str, ...]] = {}

    def _record(*args: str, **_kwargs: Any) -> list[Any]:
        seen["args"] = args
        return [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "head_repository": _OWN,
                "run_started_at": "t",
                "id": 5,
                "run_attempt": 1,
                "html_url": "https://github.com/o/r/actions/runs/5",
                "check_suite_id": 501,
            },
        ]

    monkeypatch.setattr(github, "read_json_paginated", _record)
    assert ci_evidence.select_passed_attempt("o/r", "abc").run_id == 5

    args = seen["args"]
    field_flags = {"-f", "-F", "--field", "--raw-field"}
    uses_field = any(flag in field_flags for flag in args)
    forces_get = any(
        flag in ("-X", "--method") and args[i + 1].upper() == "GET"
        for i, flag in enumerate(args[:-1])
    )
    assert not uses_field or forces_get, (
        f"select_passed_attempt must not POST to a GET-only endpoint: {args!r}"
    )
    # The head_sha filter must ride the URL, otherwise it was dropped entirely.
    assert any("head_sha=abc" in str(arg) for arg in args), args


# --- download_evidence_artifacts ----------------------------------------


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    """An in-memory zip archive holding *members* (name → content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _fake_downloads(
    monkeypatch: pytest.MonkeyPatch, archives: dict[str, bytes] | None = None
) -> list[str]:
    """Stub :func:`github.download_api_binary`; record each endpoint fetched.

    Each download writes the archive registered for its endpoint in *archives*,
    or by default a zip holding an ``evidence.json`` that names the endpoint.
    """
    endpoints: list[str] = []

    def _download(endpoint: str, dest: Path) -> None:
        endpoints.append(endpoint)
        default = _zip_bytes({"evidence.json": json.dumps({"from": endpoint}).encode()})
        dest.write_bytes((archives or {}).get(endpoint, default))

    monkeypatch.setattr(github, "download_api_binary", _download)
    return endpoints


def test_download_evidence_artifacts_uses_newest_of_duplicate_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A re-run's second same-named artifact: the newest is fetched once, by id (#3171).

    The 2.1.235 release harvest downloaded ``ci-evidence-quality`` twice by name
    into one directory and aborted on ``evidence.json: file exists``.
    """
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-quality", "id": 5, "created_at": "2026-10-01T10:00:00Z"},
            {"name": "ci-evidence-quality", "id": 9, "created_at": "2026-10-01T11:00:00Z"},
            {"name": "ci-evidence-quality", "id": 7, "created_at": "2026-10-01T09:00:00Z"},
        ],
    )
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="quality", checks=("quality / evidence",)),)
    dest = tmp_path / "gates"

    with caplog.at_level("WARNING", logger="vergil_tooling.lib.ci_evidence"):
        result = download_evidence_artifacts("o/r", 99, dest, required)

    assert result == [dest / "quality"]
    assert calls == ["repos/o/r/actions/artifacts/9/zip"]
    evidence = json.loads((dest / "quality" / "evidence.json").read_text())
    assert evidence == {"from": "repos/o/r/actions/artifacts/9/zip"}
    assert "[5, 7, 9]" in caplog.text
    assert "id 9" in caplog.text


def test_download_evidence_artifacts_tie_on_created_at_breaks_by_highest_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-test", "id": 4, "created_at": "2026-10-01T10:00:00Z"},
            {"name": "ci-evidence-test", "id": 3, "created_at": "2026-10-01T10:00:00Z"},
        ],
    )
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    download_evidence_artifacts("o/r", 1, tmp_path, required)
    assert calls == ["repos/o/r/actions/artifacts/4/zip"]


def test_download_evidence_artifacts_single_artifact_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [{"name": "ci-evidence-test", "id": 4, "created_at": "t"}],
    )
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    with caplog.at_level("WARNING", logger="vergil_tooling.lib.ci_evidence"):
        assert download_evidence_artifacts("o/r", 1, tmp_path, required) == [tmp_path / "test"]
    assert calls == ["repos/o/r/actions/artifacts/4/zip"]
    assert caplog.text == ""


def test_download_evidence_artifacts_skips_expired_newest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An expired newest copy falls back to the newest unexpired one (#3171)."""
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-test", "id": 1, "created_at": "2026-10-01T09:00:00Z"},
            {"name": "ci-evidence-test", "id": 2, "created_at": "2026-10-01T10:00:00Z"},
            {
                "name": "ci-evidence-test",
                "id": 3,
                "created_at": "2026-10-01T11:00:00Z",
                "expired": True,
            },
        ],
    )
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    download_evidence_artifacts("o/r", 1, tmp_path, required)
    assert calls == ["repos/o/r/actions/artifacts/2/zip"]


def test_download_evidence_artifacts_all_expired_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-test", "id": 2, "created_at": "b", "expired": True},
            {"name": "ci-evidence-test", "id": 1, "created_at": "a", "expired": True},
        ],
    )
    calls = _fake_downloads(monkeypatch)
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    with pytest.raises(ci_evidence.EvidenceArtifactError, match=r"'test' has expired.*\[1, 2\]"):
        download_evidence_artifacts("o/r", 1, tmp_path, required)
    assert calls == []


@pytest.mark.parametrize("member", ["../escape.json", "/abs/evidence.json", "a/../../x"])
def test_download_evidence_artifacts_rejects_unsafe_zip_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, member: str
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [{"name": "ci-evidence-test", "id": 1}]
    )
    archive = _zip_bytes({"evidence.json": b"{}", member: b"pwned"})
    _fake_downloads(monkeypatch, {"repos/o/r/actions/artifacts/1/zip": archive})
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    dest = tmp_path / "gates"
    with pytest.raises(ci_evidence.EvidenceArtifactError, match="unsafe member path"):
        download_evidence_artifacts("o/r", 1, dest, required)
    # Validation precedes extraction: nothing from the archive was written.
    assert not (dest / "test" / "evidence.json").exists()
    assert not (tmp_path / "escape.json").exists()


def test_download_evidence_artifacts_extracts_nested_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [{"name": "ci-evidence-test", "id": 1}]
    )
    archive = _zip_bytes({"evidence.json": b"{}", "reports/junit.xml": b"<x/>"})
    _fake_downloads(monkeypatch, {"repos/o/r/actions/artifacts/1/zip": archive})
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    download_evidence_artifacts("o/r", 1, tmp_path, required)
    assert (tmp_path / "test" / "reports" / "junit.xml").read_bytes() == b"<x/>"


def test_download_evidence_artifacts_filters_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-test", "id": 1},
            {"name": "ci-evidence-security", "id": 2},
            {"name": "build-logs", "id": 3},
        ],
    )
    calls = _fake_downloads(monkeypatch)

    required = (
        EvidenceGate(name="test", checks=("test / unit",)),
        EvidenceGate(name="security", checks=("CodeQL",)),
    )
    dest = tmp_path / "artifacts"
    result = download_evidence_artifacts("o/r", 99, dest, required)

    assert result == [dest / "security", dest / "test"]
    assert (dest / "test").is_dir()
    assert (dest / "security").is_dir()
    assert not (dest / "build-logs").exists()
    # Each required gate's artifact is fetched by id from the repo.
    assert sorted(calls) == [
        "repos/o/r/actions/artifacts/1/zip",
        "repos/o/r/actions/artifacts/2/zip",
    ]
    assert (dest / "test" / "evidence.json").is_file()


def test_download_evidence_artifacts_ignores_security_sarif_partials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The security gate's SARIF partials (no ``evidence.json``) must be skipped.

    Regression for issue #2340: the harvest previously globbed ``ci-evidence-*``
    and treated ``ci-evidence-security-part-codeql`` as a gate, crashing on the
    absent ``gates/security-part-codeql/evidence.json``. Scoping to the exact
    required-gate names consumes only the consolidated ``ci-evidence-security``.
    """
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": "ci-evidence-security", "id": 10},
            {"name": "ci-evidence-security-part-codeql", "id": 11},
            {"name": "ci-evidence-security-part-trivy", "id": 12},
            {"name": "ci-evidence-security-part-semgrep", "id": 13},
        ],
    )
    calls = _fake_downloads(monkeypatch)

    required = (EvidenceGate(name="security", checks=("CodeQL",)),)
    dest = tmp_path / "artifacts"
    result = download_evidence_artifacts("o/r", 99, dest, required)

    # Only the consolidated gate directory is created; no partial is materialized.
    assert result == [dest / "security"]
    assert not (dest / "security-part-codeql").exists()
    assert not (dest / "security-part-trivy").exists()
    assert not (dest / "security-part-semgrep").exists()
    assert calls == ["repos/o/r/actions/artifacts/10/zip"]


def test_download_evidence_artifacts_omits_genuinely_absent_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A required gate with no artifact is left out — never fabricated or skipped.

    Its absence surfaces downstream as an :class:`IncompleteEvidenceError` at
    completeness validation (issue #2340), not a silent pass.
    """
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [{"name": "ci-evidence-security", "id": 1}]
    )
    _fake_downloads(monkeypatch)

    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),  # no artifact in the run
    )
    result = download_evidence_artifacts("o/r", 1, tmp_path, required)

    assert result == [tmp_path / "security"]
    with pytest.raises(ci_evidence.IncompleteEvidenceError, match="test"):
        ci_evidence.validate_completeness(
            required,
            {"security": ci_evidence.GateEvidence("security", "success", (), {}, ())},
        )


def test_download_evidence_artifacts_none_matching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    monkeypatch.setattr(
        github, "read_json_paginated", lambda *a, **k: [{"name": "coverage", "id": 1}]
    )
    assert _fake_downloads(monkeypatch) == []
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    assert download_evidence_artifacts("o/r", 1, tmp_path, required) == []


def _paged_gh_output(key: str, items: list[dict[str, Any]], page_size: int = 30) -> str:
    """Render *items* as ``gh api --paginate`` does: one envelope per page, back to back."""
    pages = [items[i : i + page_size] for i in range(0, len(items), page_size)]
    return "".join(json.dumps({"total_count": len(items), key: page}) for page in pages)


def test_download_evidence_artifacts_harvests_required_gate_on_page_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A required gate's artifact past the first page is still harvested (issue #3160).

    The 2.1.234 release PR's CI run produced 31 artifacts; the unpaginated
    listing returned only the first 30, so ``ci-evidence-security`` (31st) read
    as "no evidence artifact" and the release gate failed closed. The listing is
    driven through the real :func:`github.read_json_paginated` so the multi-page
    ``gh api --paginate`` output shape is exercised end to end.
    """
    import subprocess

    from vergil_tooling.lib.github_config import EvidenceGate

    artifacts: list[dict[str, Any]] = [{"name": "ci-evidence-test", "id": 1}]
    artifacts += [{"name": f"package-reports-test-{i}", "id": 100 + i} for i in range(29)]
    artifacts.append({"name": "ci-evidence-security", "id": 31})  # 31st: lands on page 2
    assert len(artifacts) == 31
    stdout = _paged_gh_output("artifacts", artifacts)
    seen: list[tuple[str, ...]] = []

    def _fake_run(argv: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    monkeypatch.setattr("vergil_tooling.lib.retry.subprocess.run", _fake_run)
    downloads = _fake_downloads(monkeypatch)

    required = (
        EvidenceGate(name="test", checks=("test / unit",)),
        EvidenceGate(name="security", checks=("CodeQL",)),
    )
    dest = tmp_path / "artifacts"
    result = download_evidence_artifacts("o/r", 99, dest, required)

    assert result == [dest / "security", dest / "test"]
    assert sorted(downloads) == [
        "repos/o/r/actions/artifacts/1/zip",
        "repos/o/r/actions/artifacts/31/zip",
    ]
    assert seen == [("gh", "api", "--paginate", "repos/o/r/actions/runs/99/artifacts?per_page=100")]


# --- read_gate_conclusions ----------------------------------------------

# The check suite of the selected CI run.
_SUITE = 501


def _check(
    name: str,
    conclusion: str | None = "success",
    *,
    app_id: int = GITHUB_ACTIONS_INTEGRATION_ID,
    suite: int = _SUITE,
) -> dict[str, Any]:
    """A ``/commits/{sha}/check-runs`` entry: the fields the harvest reads."""
    return {
        "name": name,
        "conclusion": conclusion,
        "app": {"id": app_id},
        "check_suite": {"id": suite},
    }


def test_read_gate_conclusions_includes_check_runs_past_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check runs beyond the first page still reach the conclusion map (issue #3160)."""
    import subprocess

    check_runs = [_check(f"test / unit / {i}") for i in range(30)]
    check_runs.append(_check("security / evidence", "failure"))
    stdout = _paged_gh_output("check_runs", check_runs)

    def _fake_run(argv: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "--paginate" in argv
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    monkeypatch.setattr("vergil_tooling.lib.retry.subprocess.run", _fake_run)

    result = read_gate_conclusions("o/r", "abc", check_suite_id=_SUITE)

    assert len(result) == 31
    assert result["security / evidence"] == "failure"


def test_read_gate_conclusions_maps_name_to_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [_check("test / unit / 3.14"), _check("security / codeql", None)],
    )
    result = read_gate_conclusions("o/r", "abc", check_suite_id=_SUITE)
    assert result == {"test / unit / 3.14": "success", "security / codeql": ""}


def test_check_runs_matched_by_app_and_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check run with a gate's name from another app or workflow never counts."""
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            _check("test / unit / 3.14", "failure"),
            # The same name from another App, and from another workflow's suite.
            _check("test / unit / 3.14", "success", app_id=999),
            _check("quality / evidence", "success", suite=777),
            _check("quality / evidence", "failure"),
            # GitHub Advanced Security's scanner checks count, any other name of its does not.
            _check("CodeQL", "success", app_id=GHAS_INTEGRATION_ID, suite=900),
            _check("Semgrep OSS", "success", app_id=GHAS_INTEGRATION_ID, suite=901),
            _check("Trivy", "success", app_id=GHAS_INTEGRATION_ID, suite=902),
            _check("test / unit / 3.13", "success", app_id=GHAS_INTEGRATION_ID),
            # A scanner check's name from another App never counts.
            _check("Trivy", "failure", app_id=999),
        ],
    )
    result = read_gate_conclusions("o/r", "abc", check_suite_id=_SUITE)
    assert result == {
        "test / unit / 3.14": "failure",
        "quality / evidence": "failure",
        "CodeQL": "success",
        "Semgrep OSS": "success",
        "Trivy": "success",
    }


def test_read_gate_conclusions_check_run_without_app_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [{"id": 3, "name": "test / unit", "conclusion": "success"}],
    )
    with pytest.raises(ValueError, match="check run 3 has no creating app"):
        read_gate_conclusions("o/r", "abc", check_suite_id=_SUITE)


def test_read_gate_conclusions_actions_run_without_suite_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = {"id": 4, "name": "test / unit", "app": {"id": GITHUB_ACTIONS_INTEGRATION_ID}}
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [run])
    with pytest.raises(ValueError, match="check run 4 has no check suite"):
        read_gate_conclusions("o/r", "abc", check_suite_id=_SUITE)


def test_check_suite_id_of_a_run_must_be_present() -> None:
    assert ci_evidence._check_suite_id({"id": 1, "check_suite_id": _SUITE}) == _SUITE
    with pytest.raises(ValueError, match="workflow run 1 has no check suite"):
        ci_evidence._check_suite_id({"id": 1})


def test_qualifying_run_predicate_is_reusable() -> None:
    run: dict[str, Any] = {"name": "CI", "status": "completed", "conclusion": "success"}
    assert ci_evidence._is_qualifying_run(run, "CI")
    assert not ci_evidence._is_qualifying_run({**run, "conclusion": "cancelled"}, "CI")


# --- resolve_required_gates ---------------------------------------------


def test_resolve_required_gates_wires_config_and_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from vergil_tooling.lib.github_config import EvidenceGate

    cfg = SimpleNamespace(
        project="PROJECT", ci="CI", publish=SimpleNamespace(docs=False), packages=()
    )
    captured: dict[str, Any] = {}

    def fake_config(repo: str, ref: str) -> Any:
        captured["config_at"] = (repo, ref)
        return cfg

    monkeypatch.setattr(ci_evidence, "require_config_at_ref", fake_config)

    def fake_integration(config: Any) -> tuple[str, ...]:
        captured["integration_config"] = config
        return ("3.14",)

    monkeypatch.setattr(ci_evidence, "resolved_integration_versions", fake_integration)

    expected_gates = (EvidenceGate(name="test", checks=("test / unit",)),)

    def fake_required(
        project: Any,
        ci: Any,
        *,
        docs: bool,
        package: bool,
        integration_versions: tuple[str, ...],
    ) -> Any:
        captured["derivation_args"] = (project, ci, docs, package, integration_versions)
        return expected_gates

    monkeypatch.setattr(ci_evidence, "required_evidence_gates", fake_required)

    result = ci_evidence.resolve_required_gates("o/r", "cafef00d")

    assert result is expected_gates
    # The config is the --repo target's, fetched at the given ref (#3226).
    assert captured["config_at"] == ("o/r", "cafef00d")
    # docs is wired from config.publish.docs (here False), and package from
    # the presence of a [package] section (here absent).
    assert captured["derivation_args"] == ("PROJECT", "CI", False, False, ("3.14",))
    # The integration checks come from the resolver, for the same config.
    assert captured["integration_config"] is cfg


def test_package_section_makes_harvest_download_ci_evidence_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``[package]`` repo requires the ``package`` gate, so the harvest pulls
    ``ci-evidence-package`` (epic vergil-project/.github#356, spec 8.1).

    Runs the real derivation (no stub of ``required_evidence_gates``) so the
    config -> required-gate -> artifact-name chain is exercised end to end.
    """
    from types import SimpleNamespace

    from vergil_tooling.lib.config import CiConfig, ProjectConfig

    project = ProjectConfig(
        repository_type="library",
        versioning_scheme="semver",
        branching_model="library-release",
        release_model="tagged-release",
        primary_language="python",
    )
    cfg = SimpleNamespace(
        project=project,
        ci=CiConfig(versions=["3.14"], integration_tests=False),
        publish=SimpleNamespace(docs=True),
        packages=(SimpleNamespace(builder="python"),),
    )
    monkeypatch.setattr(ci_evidence, "require_config_at_ref", lambda _repo, _ref: cfg)

    required = ci_evidence.resolve_required_gates("o/r", "cafef00d")
    assert "package" in {g.name for g in required}

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": f"ci-evidence-{g.name}", "id": i} for i, g in enumerate(required)
        ],
    )
    calls = _fake_downloads(monkeypatch)

    dest = tmp_path / "artifacts"
    result = download_evidence_artifacts("o/r", 7, dest, required)

    assert dest / "package" in result
    package_id = [g.name for g in required].index("package")
    assert f"repos/o/r/actions/artifacts/{package_id}/zip" in calls


def _images_repo_required_gates(monkeypatch: pytest.MonkeyPatch, *, images: bool) -> Any:
    """The real required-gate derivation for a repo with ``[ci].images`` set as given."""
    from types import SimpleNamespace

    from vergil_tooling.lib.config import CiConfig, ProjectConfig

    project = ProjectConfig(
        repository_type="tooling",
        versioning_scheme="semver",
        branching_model="library-release",
        release_model="tagged-release",
        primary_language=None,
    )
    cfg = SimpleNamespace(
        project=project,
        ci=CiConfig(versions=["latest"], integration_tests=False, images=images),
        publish=SimpleNamespace(docs=True),
        packages=(),
    )
    monkeypatch.setattr(ci_evidence, "require_config_at_ref", lambda _repo, _ref: cfg)
    return ci_evidence.resolve_required_gates("o/r", "cafef00d")


def test_ci_images_makes_harvest_download_ci_evidence_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``[ci].images`` repo requires the ``images`` gate, so the harvest pulls
    ``ci-evidence-images`` (vergil-project/vergil-tooling#4132)."""
    required = _images_repo_required_gates(monkeypatch, images=True)
    gates = {g.name: g for g in required}
    assert gates["images"].checks == ("images / evidence",)

    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [
            {"name": f"ci-evidence-{g.name}", "id": i} for i, g in enumerate(required)
        ],
    )
    calls = _fake_downloads(monkeypatch)

    result = download_evidence_artifacts("o/r", 7, tmp_path / "artifacts", required)

    assert tmp_path / "artifacts" / "images" in result
    images_id = [g.name for g in required].index("images")
    assert f"repos/o/r/actions/artifacts/{images_id}/zip" in calls


def test_ci_images_release_without_images_artifact_fails_completeness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``[ci].images`` release whose CI run uploaded no ``ci-evidence-images``
    fails the harvest naming the gate, never a silent pass."""
    required = _images_repo_required_gates(monkeypatch, images=True)
    others = [g for g in required if g.name != "images"]
    monkeypatch.setattr(
        github,
        "read_json_paginated",
        lambda *a, **k: [{"name": f"ci-evidence-{g.name}", "id": i} for i, g in enumerate(others)],
    )
    _fake_downloads(monkeypatch)

    gate_dirs = download_evidence_artifacts("o/r", 7, tmp_path, required)
    harvested = {
        d.name: ci_evidence.GateEvidence(d.name, "success", (), {}, (d / "report.json",))
        for d in gate_dirs
    }

    with pytest.raises(ci_evidence.IncompleteEvidenceError) as exc:
        ci_evidence.validate_completeness(required, harvested)
    assert exc.value.missing == ["images"]


def test_no_ci_images_never_requires_images_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``[ci].images`` the release never asks for ``ci-evidence-images``."""
    required = _images_repo_required_gates(monkeypatch, images=False)
    assert "images" not in {g.name for g in required}


# --- _gate_conclusion ---------------------------------------------------


def test_gate_conclusion_all_success() -> None:
    assert ci_evidence._gate_conclusion(("a", "b"), {"a": "success", "b": "success"}) == "success"


def test_gate_conclusion_reports_first_non_success() -> None:
    assert ci_evidence._gate_conclusion(("a", "b"), {"a": "success", "b": "failure"}) == "failure"


def test_gate_conclusion_null_conclusion_is_unknown() -> None:
    assert ci_evidence._gate_conclusion(("a",), {"a": ""}) == "unknown"


def test_gate_conclusion_no_checks_is_success() -> None:
    assert ci_evidence._gate_conclusion((), {}) == "success"


# --- load_gate_evidence / load_harvested_gates --------------------------


def _write_gate_dir(parent: Path, gate: str, fragment: dict[str, Any]) -> Path:
    gate_dir = parent / gate
    gate_dir.mkdir(parents=True)
    (gate_dir / "evidence.json").write_text(json.dumps(fragment), encoding="utf-8")
    (gate_dir / f"{gate}.report").write_text("report-data", encoding="utf-8")
    return gate_dir


def test_load_gate_evidence_parses_fragment_and_files(tmp_path: Path) -> None:
    gate_dir = _write_gate_dir(
        tmp_path,
        "security",
        {"gate": "security", "tools": [{"name": "codeql", "version": "1"}], "metrics": {"high": 0}},
    )
    evidence = ci_evidence.load_gate_evidence(gate_dir, conclusion="success")

    assert evidence.name == "security"
    assert evidence.conclusion == "success"
    assert evidence.tools == ({"name": "codeql", "version": "1"},)
    assert evidence.metrics == {"high": 0}
    assert {path.name for path in evidence.files} == {"evidence.json", "security.report"}


def test_load_gate_evidence_defaults_absent_tools_and_metrics(tmp_path: Path) -> None:
    gate_dir = _write_gate_dir(tmp_path, "quality", {"gate": "quality"})
    evidence = ci_evidence.load_gate_evidence(gate_dir, conclusion="success")

    assert evidence.tools == ()
    assert evidence.metrics == {}


def test_load_harvested_gates_derives_conclusions_and_maps_by_name(tmp_path: Path) -> None:
    from vergil_tooling.lib.github_config import EvidenceGate

    test_dir = _write_gate_dir(tmp_path, "test", {"gate": "test"})
    security_dir = _write_gate_dir(tmp_path, "security", {"gate": "security"})
    extra_dir = _write_gate_dir(tmp_path, "extra", {"gate": "extra"})

    required = (
        EvidenceGate(name="test", checks=("test / unit",)),
        EvidenceGate(name="security", checks=("CodeQL",)),
    )
    conclusions = {"test / unit": "success", "CodeQL": "failure"}

    result = ci_evidence.load_harvested_gates(
        [test_dir, security_dir, extra_dir], required, conclusions
    )

    assert set(result) == {"test", "security", "extra"}
    assert result["test"].conclusion == "success"
    assert result["security"].conclusion == "failure"
    # a downloaded gate with no matching required gate falls back to an empty
    # check set -> success (not dropped).
    assert result["extra"].conclusion == "success"
