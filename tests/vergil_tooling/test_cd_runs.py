from __future__ import annotations

import json
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.cd_runs import (
    CDRunLookupError,
    classify_jobs,
    failed_job_labels,
    failed_job_names,
    find_run,
    is_docs_job,
    is_release_job,
    watch_run,
)

_MOD = "vergil_tooling.lib.cd_runs"
_SHA = "abc123def456"
_URL = "https://github.com/o/r/actions/runs/12345"


class _FakeGh:
    """A ``subprocess.run`` fake for ``gh run list`` and ``gh run view``.

    *lists* and *views* are served in order, one per call of that kind; each
    entry is a payload to serialize, or a ``CompletedProcess`` returned as is.
    """

    def __init__(
        self, *, lists: list[object] | None = None, views: list[object] | None = None
    ) -> None:
        self.lists = list(lists or [])
        self.views = list(views or [])
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> CompletedProcess:
        self.calls.append(command)
        queue = {"list": self.lists, "view": self.views}[command[2]]
        item = queue.pop(0)
        if isinstance(item, CompletedProcess):
            return item
        return CompletedProcess(args=command, returncode=0, stdout=json.dumps(item))


def _view(status: str, conclusion: str | None = None) -> dict[str, object]:
    return {"status": status, "conclusion": conclusion, "url": _URL}


def _run(sha: str = _SHA, run_id: int = 12345) -> dict[str, object]:
    return {"databaseId": run_id, "headSha": sha, "status": "queued", "url": _URL}


def _job(name: str, conclusion: str | None = "success", status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


# -- watch_run -----------------------------------------------------------------


def test_watch_waits_through_queued() -> None:
    fake = _FakeGh(
        views=[
            _view("queued"),
            _view("pending"),
            _view("in_progress"),
            _view("completed", "success"),
        ]
    )
    with patch(_MOD + ".subprocess.run", side_effect=fake), patch(_MOD + ".time.sleep") as sleep:
        assert watch_run("12345", timeout=3600) == "success"
    assert sleep.call_count == 3
    assert all(call[:4] == ["gh", "run", "view", "12345"] for call in fake.calls)


def test_watch_timeout_is_timed_out() -> None:
    fake = _FakeGh(views=[_view("in_progress")])
    with patch(_MOD + ".subprocess.run", side_effect=fake), patch(_MOD + ".time.sleep"):
        assert watch_run("12345", timeout=0) == "timed_out"


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("success", "success"),
        ("skipped", "success"),
        ("neutral", "success"),
        ("failure", "failed"),
        ("timed_out", "failed"),
        ("startup_failure", "failed"),
    ],
)
def test_watch_maps_conclusions(conclusion: str, state: str) -> None:
    fake = _FakeGh(views=[_view("completed", conclusion)])
    with patch(_MOD + ".subprocess.run", side_effect=fake):
        assert watch_run("12345", timeout=0) == state


@pytest.mark.parametrize(
    ("group", "state"), [("docker-publish-dev", "superseded"), (None, "cancelled")]
)
def test_watch_tells_a_supersede_from_another_cancel(group: str | None, state: str) -> None:
    fake = _FakeGh(views=[_view("completed", "cancelled")])
    with (
        patch(_MOD + ".subprocess.run", side_effect=fake),
        patch(_MOD + "._supersede_group", return_value=group) as lookup,
    ):
        assert watch_run("12345", timeout=0) == state
    lookup.assert_called_once_with("12345", _URL)


def test_watch_raises_when_a_cancel_cannot_be_checked() -> None:
    fake = _FakeGh(views=[_view("completed", "cancelled")])
    with (
        patch(_MOD + ".subprocess.run", side_effect=fake),
        patch(_MOD + "._supersede_group", side_effect=ValueError("no jobs list")),
        pytest.raises(ValueError, match="no jobs list"),
    ):
        watch_run("12345", timeout=0)


# -- find_run ------------------------------------------------------------------


def test_find_run_matches_by_sha_and_names_the_repo() -> None:
    mine = _run()
    fake = _FakeGh(lists=[[], [_run(sha="deadbeef"), mine]])
    with patch(_MOD + ".subprocess.run", side_effect=fake), patch(_MOD + ".time.sleep"):
        assert find_run("o/r", "develop", _SHA, dispatch_timeout=3600) == mine
    assert fake.calls[0][:5] == ["gh", "run", "list", "--repo", "o/r"]
    assert "develop" in fake.calls[0]


def test_find_run_none_when_no_run_registers() -> None:
    fake = _FakeGh(lists=[[_run(sha="deadbeef")]])
    with patch(_MOD + ".subprocess.run", side_effect=fake), patch(_MOD + ".time.sleep"):
        assert find_run("o/r", "develop", _SHA, dispatch_timeout=0) is None


@pytest.mark.parametrize(
    "response",
    [
        CompletedProcess(args=(), returncode=1, stdout="", stderr="HTTP 502"),
        CompletedProcess(args=(), returncode=0, stdout="not json"),
        CompletedProcess(args=(), returncode=0, stdout='{"runs": []}'),
    ],
)
def test_find_run_raises_when_runs_cannot_be_listed(response: CompletedProcess) -> None:
    fake = _FakeGh(lists=[response])
    with (
        patch(_MOD + ".subprocess.run", side_effect=fake),
        pytest.raises(CDRunLookupError, match="gh run list --repo o/r"),
    ):
        find_run("o/r", "develop", _SHA, dispatch_timeout=0)


# -- job classification -------------------------------------------------------


def test_classify_docs_publish_is_deferred() -> None:
    jobs = [_job("lint / lint"), _job("docs / docs", conclusion="failure")]
    assert classify_jobs(jobs) == ([], ["docs"])


def test_classify_other_failures_block() -> None:
    jobs = [
        _job("docs / docs", conclusion="failure"),
        _job("docker-publish / publish: dev-base", conclusion="failure"),
        _job("docker-publish / publish: dev-python", conclusion="cancelled"),
        _job("codeql / analyze", conclusion="skipped"),
    ]
    assert classify_jobs(jobs) == (["docker-publish"], ["docs"])


def test_classify_a_failed_release_job_blocks() -> None:
    jobs = [_job("release / release", conclusion="failure"), _job("docs / docs")]
    assert classify_jobs(jobs) == (["release / release"], [])


def test_classify_ignores_jobs_not_completed() -> None:
    jobs = [_job("docker-publish / x", conclusion=None, status="in_progress")]
    assert classify_jobs(jobs) == ([], [])


def test_is_docs_job_matches_reusable_leaf() -> None:
    assert is_docs_job("docs / docs")
    assert is_docs_job("cd-docs / deploy")
    assert not is_docs_job("docker-publish / publish: dev-base")


def test_is_release_job_matches_leaf_not_caller() -> None:
    # Both the reusable leaf and vergil-actions' inline job share the leaf.
    assert is_release_job("release / release")
    assert is_release_job("cd / release")
    # Decoys whose leaf is not exactly "release" must NOT match (#1853 guard).
    assert not is_release_job("release-notes / build")
    assert not is_release_job("docs / docs")
    assert not is_release_job("docker-publish / publish: prod-base:latest")


def test_failed_job_labels_excludes_inline_release_job() -> None:
    # The "cd / release" release job must not be collected as a publish family.
    jobs = [
        _job("cd / release"),
        _job("docs / docs", conclusion="failure"),
    ]
    assert failed_job_labels(jobs) == ["docs"]


def test_failed_job_names_keeps_each_full_name_once() -> None:
    jobs = [
        _job("release / release", conclusion="failure"),
        _job("docker-publish / publish: a", conclusion="failure"),
        _job("docker-publish / publish: b", conclusion="cancelled"),
        _job("docker-publish / publish: a", conclusion="failure"),
        _job("codeql / analyze", conclusion="skipped"),
        _job("docs / docs", conclusion=None, status="in_progress"),
        _job("pypi / publish"),
    ]
    assert failed_job_names(jobs) == [
        "docker-publish / publish: a",
        "docker-publish / publish: b",
    ]


def test_failed_job_labels_collapses_families() -> None:
    jobs = [
        _job("release / release"),
        _job("docker-publish / publish: prod-base:latest", conclusion="failure"),
        _job("docker-publish / publish: prod-python:3.14", conclusion="failure"),
        _job("docs / docs", conclusion="failure"),
    ]
    assert failed_job_labels(jobs) == ["docker-publish", "docs"]


def test_failed_job_labels_ignores_success_and_skipped() -> None:
    jobs = [
        _job("release / release"),
        _job("docs / docs"),  # success
        _job("codeql / analyze", conclusion="skipped"),
    ]
    assert failed_job_labels(jobs) == []


def test_failed_job_labels_names_release_family_job_in_full() -> None:
    """A failed job in the release job's own family is reported by full name,
    so the message cannot read as the (successful) release itself failing."""
    jobs = [
        _job("release / release"),
        _job("release / package-matrix", conclusion="failure"),
        _job("docker-publish / publish: prod-base:latest", conclusion="failure"),
    ]
    assert failed_job_labels(jobs) == ["release / package-matrix", "docker-publish"]
