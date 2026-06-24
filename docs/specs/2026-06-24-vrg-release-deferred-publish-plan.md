# Defer Artifact-Publish Failures in vrg-release — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vrg-release` hard-fail only on the release itself (the `release` job + tag/Release/boundary-tag) and defer every other CD-job failure, so a publish failure never strands `develop`.

**Architecture:** `confirm-main` stops vetoing on overall-run status, hard-verifies the `release` job exactly, and records every other failed job into a new `ctx.deferred_publish_failures`. The pipeline runs to completion (back-merge, bump, promote, finalize); a new terminal `publish-status` `fail_defer` stage turns a non-empty list into `exit 1`, `close-finalize` leaves the tracking issue open with a CD-re-run remediation, and `consumer-refresh` prints a hold-warning.

**Tech Stack:** Python (vergil-tooling), `dataclasses`, `pytest`, `unittest.mock`. GitHub interaction via the existing `github`/`git` wrappers.

## Global Constraints

- Repo: `vergil-tooling`; worktree `.worktrees/issue-1853-defer-publish`, branch `feature/1853-defer-publish`.
- **Fleet-wide default, no `vergil.toml` config** — do not add any config key.
- The `release` job is matched **exactly** by the constant `"release / release"`; the loose substring `_find_job` is kept only for the deferred sweep.
- New context field is **`deferred_publish_failures`** (separate from the `_tracked`-owned `deferred_failures`).
- Run tests with `uv run pytest <path>::<test> -v`.
- Commit with `vrg-commit --type <t> --scope release --message "<m>" --body "...\n\nRef #1853"` (raw `git commit` is denied). Stage with `vrg-git add`.
- Final validation: `vrg-container-run -- vrg-validate` must be green.

---

### Task 1: Add `deferred_publish_failures` to the release context

**Files:**
- Modify: `src/vergil_tooling/lib/release/context.py:54`
- Test: `tests/vergil_tooling/test_release_context.py`

**Interfaces:**
- Produces: `ReleaseContext.deferred_publish_failures: list[str]` (default `[]`) — list of failed CD job *families* (e.g. `["docker-publish", "docs"]`), consumed by Tasks 3–8.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_release_context.py
def test_deferred_publish_failures_defaults_empty() -> None:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext

    ctx = ReleaseContext(
        repo="o/r", version="2.1.0", repo_root=Path("/tmp/r"), version_override=None  # noqa: S108
    )
    assert ctx.deferred_publish_failures == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_release_context.py::test_deferred_publish_failures_defaults_empty -v`
Expected: FAIL — `AttributeError: 'ReleaseContext' object has no attribute 'deferred_publish_failures'`

- [ ] **Step 3: Add the field**

In `context.py`, immediately after the `deferred_failures` field (line 54), add:

```python
    # Families of CD publish jobs that did not succeed (e.g. "docker-publish",
    # "docs"). The release itself is valid (tag + Release published); these are
    # re-publishable. Surfaced by the publish-status stage (exit 1), the open
    # tracking issue, and the consumer-refresh hold-warning. Separate from
    # deferred_failures, which the _tracked wrapper fills with failed STAGE
    # names (#1853).
    deferred_publish_failures: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_release_context.py::test_deferred_publish_failures_defaults_empty -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/context.py tests/vergil_tooling/test_release_context.py
vrg-commit --type feat --scope release --message "add deferred_publish_failures to ReleaseContext" --body "Separate list for failed CD publish job families, distinct from the _tracked-owned deferred_failures (stage names).

Ref #1853"
```

---

### Task 2: Classification helpers in `confirm.py` (exact release gate + deferred sweep)

**Files:**
- Modify: `src/vergil_tooling/lib/release/confirm.py` (add constant + two helpers near the other `_verify_*` helpers, ~line 156)
- Test: `tests/vergil_tooling/test_release_confirm.py`

**Interfaces:**
- Consumes: the existing `_job(name, status, conclusion)` test helper and `ReleaseError`.
- Produces:
  - `_RELEASE_JOB_NAME = "release / release"` (str constant)
  - `_verify_release_job(jobs: list[dict[str, Any]]) -> None` — raises `ReleaseError` if the exact release job is missing or did not conclude `success`; else returns.
  - `_collect_deferred_publish(jobs: list[dict[str, Any]]) -> list[str]` — ordered-unique families of non-release jobs whose conclusion is neither `success` nor `skipped`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_release_confirm.py — add these
from vergil_tooling.lib.release.confirm import (
    _RELEASE_JOB_NAME,
    _collect_deferred_publish,
    _verify_release_job,
)


def test_verify_release_job_success_returns() -> None:
    _verify_release_job([_job("release / release")])  # no raise


def test_verify_release_job_failure_raises() -> None:
    with pytest.raises(ReleaseError, match="did not succeed"):
        _verify_release_job([_job("release / release", conclusion="failure")])


def test_verify_release_job_missing_raises() -> None:
    with pytest.raises(ReleaseError, match="not found"):
        _verify_release_job([_job("docs / docs")])


def test_verify_release_job_is_exact_not_substring() -> None:
    # a "release-notes" job must NOT satisfy the hard gate
    with pytest.raises(ReleaseError, match="not found"):
        _verify_release_job([_job("release-notes / build")])


def test_collect_deferred_publish_collapses_families() -> None:
    jobs = [
        _job("release / release"),
        _job("docker-publish / publish: prod-base:latest", conclusion="failure"),
        _job("docker-publish / publish: prod-python:3.14", conclusion="failure"),
        _job("docs / docs", conclusion="failure"),
    ]
    assert _collect_deferred_publish(jobs) == ["docker-publish", "docs"]


def test_collect_deferred_publish_ignores_success_and_skipped() -> None:
    jobs = [
        _job("release / release"),
        _job("docs / docs"),  # success
        _job("codeql / analyze", conclusion="skipped"),
    ]
    assert _collect_deferred_publish(jobs) == []


def test_release_job_name_constant() -> None:
    assert _RELEASE_JOB_NAME == "release / release"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -k "verify_release_job or collect_deferred or release_job_name" -v`
Expected: FAIL — `ImportError: cannot import name '_RELEASE_JOB_NAME'`

- [ ] **Step 3: Add the constant and helpers**

In `confirm.py`, after `_verify_jobs` (ends ~line 180), add:

```python
_RELEASE_JOB_NAME = "release / release"


def _verify_release_job(jobs: list[dict[str, Any]]) -> None:
    """Hard gate: the release job must exist and have concluded ``success``.

    Matched EXACTLY (the reusable-workflow leaf ``release / release``), not by
    the substring ``_find_job`` uses for the deferred sweep — the release job is
    the single load-bearing assertion, so a future ``release``-prefixed job must
    not satisfy it. A renamed/absent release job fails closed (#1853).
    """
    for job in jobs:
        if job.get("name") == _RELEASE_JOB_NAME:
            conclusion = job.get("conclusion")
            if conclusion != "success":
                raise ReleaseError(
                    phase="confirm-main",
                    command=f"verify job '{_RELEASE_JOB_NAME}'",
                    message=(
                        f"Release job did not succeed (conclusion: '{conclusion}')."
                    ),
                )
            return
    raise ReleaseError(
        phase="confirm-main",
        command=f"verify job '{_RELEASE_JOB_NAME}'",
        message=f"Release job '{_RELEASE_JOB_NAME}' not found in the workflow run.",
    )


def _collect_deferred_publish(jobs: list[dict[str, Any]]) -> list[str]:
    """Ordered-unique families of non-release jobs that did not succeed.

    A job "did not succeed" when its conclusion is neither ``success`` nor
    ``skipped`` (a skipped job — e.g. codeql — is not a failure). Reusable
    leaves are ``<family> / <job>``; collapse to the family so a matrix of
    failed ``docker-publish`` leaves reports once as ``docker-publish``.
    """
    families: list[str] = []
    for job in jobs:
        name = job.get("name", "")
        if name == _RELEASE_JOB_NAME:
            continue
        if job.get("conclusion") in ("success", "skipped"):
            continue
        family = name.split(" / ", 1)[0]
        if family and family not in families:
            families.append(family)
    return families
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -k "verify_release_job or collect_deferred or release_job_name" -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/confirm.py tests/vergil_tooling/test_release_confirm.py
vrg-commit --type feat --scope release --message "add exact release-job gate and deferred-publish sweep helpers" --body "_verify_release_job hard-checks the exact 'release / release' job; _collect_deferred_publish returns unique non-release failed job families.

Ref #1853"
```

---

### Task 3: Rewire `confirm_main` to defer non-release job failures

**Files:**
- Modify: `src/vergil_tooling/lib/release/confirm.py:27-36` (`confirm_main`)
- Test: `tests/vergil_tooling/test_release_confirm.py`

**Interfaces:**
- Consumes: `_verify_release_job`, `_collect_deferred_publish` (Task 2); `_watch_cd`, `_settled_run_jobs`, `_verify_artifacts` (existing); `ctx.deferred_publish_failures` (Task 1).
- Produces: `confirm_main(ctx)` populates `ctx.deferred_publish_failures` and never raises for a non-release job failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_release_confirm.py — add these
def test_confirm_main_defers_docker_publish_failure() -> None:
    ctx = _ctx()
    jobs = [
        _job("release / release"),
        _job("docker-publish / publish: prod-base:latest", conclusion="failure"),
    ]
    with (
        patch(_MOD + "._watch_cd", return_value=("123", "https://run/123")),
        patch(_MOD + "._settled_run_jobs", return_value=jobs),
        patch(_MOD + "._verify_artifacts"),
    ):
        confirm_main(ctx)  # must NOT raise
    assert ctx.deferred_publish_failures == ["docker-publish"]
    assert ctx.cd_run_id == "123"


def test_confirm_main_release_failure_still_raises() -> None:
    ctx = _ctx()
    jobs = [_job("release / release", conclusion="failure")]
    with (
        patch(_MOD + "._watch_cd", return_value=("123", "https://run/123")),
        patch(_MOD + "._settled_run_jobs", return_value=jobs),
        patch(_MOD + "._verify_artifacts"),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_main(ctx)
    assert ctx.deferred_publish_failures == []


def test_confirm_main_clean_run_defers_nothing() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + "._watch_cd", return_value=("123", "https://run/123")),
        patch(_MOD + "._settled_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + "._verify_artifacts"),
    ):
        confirm_main(ctx)
    assert ctx.deferred_publish_failures == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -k confirm_main_defers -v`
Expected: FAIL — current `confirm_main` calls `_verify_jobs` (raises on the docker-publish-failed overall run) / does not set `deferred_publish_failures`.

- [ ] **Step 3: Rewrite `confirm_main`**

Replace the body of `confirm_main` (lines 27–36) with:

```python
def confirm_main(ctx: ReleaseContext) -> None:
    """Watch CD on main: hard-verify the release, defer other publish jobs."""
    run_id, run_url = _watch_cd(ctx, branch="main", check_status=False)
    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, ("release",))
    _verify_release_job(jobs)

    deferred = _collect_deferred_publish(jobs)
    if deferred:
        ctx.deferred_publish_failures.extend(
            d for d in deferred if d not in ctx.deferred_publish_failures
        )
        print(f"  Publish deferred (release is valid): {', '.join(deferred)}")
    else:
        print("  All CD jobs succeeded.")

    _verify_artifacts(ctx)
    print(f"Release v{ctx.version} confirmed.")
```

(Note: `check_status=True` → `False`; `_verify_jobs(...)` is replaced. `_MAIN_EXPECTED_JOBS` / `_verify_jobs` stay defined for now — Task 4 removes the last `_verify_jobs` caller; leave the unused-symbol cleanup to Task 4's commit.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -k confirm_main -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/confirm.py tests/vergil_tooling/test_release_confirm.py
vrg-commit --type feat --scope release --message "defer non-release CD job failures in confirm-main" --body "Watch with check_status=False; hard-verify the release job exactly; record other failed job families into ctx.deferred_publish_failures instead of aborting. Artifacts still hard-verified.

Ref #1853"
```

---

### Task 4: Rewire `confirm_develop` to defer a docs failure (unified path)

**Files:**
- Modify: `src/vergil_tooling/lib/release/confirm.py:39-46` (`confirm_develop`); remove now-unused `_MAIN_EXPECTED_JOBS`, `_DEVELOP_EXPECTED_JOBS`, `_verify_jobs` if no callers remain.
- Test: `tests/vergil_tooling/test_release_confirm.py` (update the two `_*_EXPECTED_JOBS` assertions — they are being removed)

**Interfaces:**
- Consumes: `_collect_deferred_publish` (Task 2); `_watch_cd`, `_settled_run_jobs`; `ctx.deferred_publish_failures`.
- Produces: `confirm_develop(ctx)` records a failed `docs` job into `ctx.deferred_publish_failures` and never raises for it.

- [ ] **Step 1: Write the failing test + delete obsolete assertions**

Delete `test_main_expected_jobs` and `test_develop_expected_jobs` (they assert constants being removed). Add:

```python
def test_confirm_develop_defers_docs_failure() -> None:
    ctx = _ctx()
    jobs = [_job("docs / docs", conclusion="failure")]
    with (
        patch(_MOD + "._watch_cd", return_value=("9", "https://run/9")),
        patch(_MOD + "._settled_run_jobs", return_value=jobs),
    ):
        confirm_develop(ctx)  # must NOT raise
    assert ctx.deferred_publish_failures == ["docs"]
    assert ctx.develop_cd_run_id == "9"


def test_confirm_develop_clean_defers_nothing() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + "._watch_cd", return_value=("9", "https://run/9")),
        patch(_MOD + "._settled_run_jobs", return_value=_DEVELOP_JOBS_OK),
    ):
        confirm_develop(ctx)
    assert ctx.deferred_publish_failures == []
```

Remove the now-unused imports `_DEVELOP_EXPECTED_JOBS, _MAIN_EXPECTED_JOBS` from the test's import block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -k confirm_develop -v`
Expected: FAIL — current `confirm_develop` calls `_verify_jobs`, which raises on the docs failure.

- [ ] **Step 3: Rewrite `confirm_develop` and drop dead code**

Replace `confirm_develop` (lines 39–46) with:

```python
def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop; defer a docs publish failure rather than raise."""
    run_id, run_url = _watch_cd(ctx, branch="develop", check_status=False)
    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, ("docs",))
    deferred = _collect_deferred_publish(jobs)
    if deferred:
        ctx.deferred_publish_failures.extend(
            d for d in deferred if d not in ctx.deferred_publish_failures
        )
        print(f"  Publish deferred on develop: {', '.join(deferred)}")
    else:
        print("Develop CD verified.")
```

Then delete the now-unused `_MAIN_EXPECTED_JOBS`, `_DEVELOP_EXPECTED_JOBS` constants (lines 17–18) and the `_verify_jobs` function (no remaining callers).

- [ ] **Step 4: Run the full confirm test module**

Run: `uv run pytest tests/vergil_tooling/test_release_confirm.py -v`
Expected: PASS (all, including Tasks 2–3 tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/confirm.py tests/vergil_tooling/test_release_confirm.py
vrg-commit --type feat --scope release --message "defer docs failure in confirm-develop via unified path" --body "confirm_develop records a failed docs job into ctx.deferred_publish_failures (already a fail_defer stage); drop the now-unused _verify_jobs and _*_EXPECTED_JOBS.

Ref #1853"
```

---

### Task 5: Add the terminal `publish-status` stage

**Files:**
- Modify: `src/vergil_tooling/lib/release/orchestrator.py` (add `_publish_status_stage`; append the `Stage` in `build_stages`)
- Test: `tests/vergil_tooling/test_release_orchestrator.py`

**Interfaces:**
- Consumes: `ReleaseState.ctx.deferred_publish_failures`; `ReleaseError` (already imported).
- Produces: `_publish_status_stage(state: ReleaseState) -> None` (raises when the list is non-empty); a `Stage("publish-status", _publish_status_stage, mode="fail_defer")` as the **last** stage.

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_release_orchestrator.py
from pathlib import Path

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import (
    ReleaseState,
    _publish_status_stage,
    build_stages,
)


def _state() -> ReleaseState:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/r"), promote=True)  # noqa: S108
    state.ctx = ReleaseContext(
        repo="o/r", version="2.1.2", repo_root=Path("/tmp/r"), version_override=None  # noqa: S108
    )
    return state


def test_publish_status_raises_when_deferred() -> None:
    state = _state()
    state.ctx.deferred_publish_failures = ["docker-publish"]
    with pytest.raises(ReleaseError, match="docker-publish"):
        _publish_status_stage(state)


def test_publish_status_noop_when_clean() -> None:
    state = _state()
    _publish_status_stage(state)  # no raise


def test_publish_status_is_terminal_fail_defer() -> None:
    stages = build_stages()
    assert stages[-1].name == "publish-status"
    assert stages[-1].mode == "fail_defer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_release_orchestrator.py -k publish_status -v`
Expected: FAIL — `ImportError: cannot import name '_publish_status_stage'`

- [ ] **Step 3: Implement the stage**

In `orchestrator.py`, add after `_promote_phase` (~line 172):

```python
def _publish_status_stage(state: ReleaseState) -> None:
    """Terminal fail_defer: surface deferred artifact-publish failures as exit 1.

    Runs last, after every bookkeeping stage. Raising here marks the run failed
    (fail_defer → exit 1 with the deferred summary) without aborting anything —
    the release is already complete. No-op when nothing deferred.
    """
    ctx = state.ctx
    if ctx is None or not ctx.deferred_publish_failures:
        return
    jobs = ", ".join(ctx.deferred_publish_failures)
    raise ReleaseError(
        phase="publish-status",
        command="publish-status",
        message=(
            f"Release v{ctx.version} is published (tag + GitHub Release), but these "
            f"CD publish jobs did not succeed: {jobs}. Re-run the CD publish to "
            "deliver the artifacts — do NOT use vrg-release --resume (the version "
            "is already tagged)."
        ),
    )
```

In `build_stages()`, append as the final stage (after `consumer-refresh`). Wire
it as a **bare `Stage`, NOT `_tracked(...)`** (like `teardown-worktree`):
`_tracked` appends the stage name to `deferred_failures` and posts a
"phase failed" issue comment on any exception, so wrapping `publish-status`
would re-conflate the two lists and spam the tracking issue.

```python
        Stage("publish-status", _publish_status_stage, mode="fail_defer"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_release_orchestrator.py -k publish_status -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/orchestrator.py tests/vergil_tooling/test_release_orchestrator.py
vrg-commit --type feat --scope release --message "add terminal publish-status fail_defer stage" --body "Raises when ctx.deferred_publish_failures is non-empty so a deferred publish ends the run exit 1 with a clear summary, after all bookkeeping has run.

Ref #1853"
```

---

### Task 6: `comment_publish_deferred` remediation helper

**Files:**
- Modify: `src/vergil_tooling/lib/release/tracking.py` (add public helper using `_comment`)
- Test: `tests/vergil_tooling/test_release_tracking.py`

**Interfaces:**
- Consumes: `ctx.deferred_publish_failures`, `ctx.cd_run_url`, `ctx.tag`, `ctx.version`; existing `_comment(ctx, body)`.
- Produces: `comment_publish_deferred(ctx: ReleaseContext, jobs: list[str]) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_release_tracking.py
def test_comment_publish_deferred_names_jobs_and_warns_resume() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()  # reuse the module's existing ctx builder
    ctx.tag = "v2.1.2"
    ctx.cd_run_url = "https://run/55"
    with patch("vergil_tooling.lib.release.tracking._comment") as comment:
        comment_publish_deferred(ctx, ["docker-publish", "docs"])
    body = comment.call_args[0][1]
    assert "docker-publish" in body and "docs" in body
    assert "--resume" in body  # explicitly steers away from it
    assert "https://run/55" in body
```

(If `test_release_tracking.py` has no `_ctx()` builder, copy the 6-line builder from `test_release_confirm.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_release_tracking.py::test_comment_publish_deferred_names_jobs_and_warns_resume -v`
Expected: FAIL — `ImportError: cannot import name 'comment_publish_deferred'`

- [ ] **Step 3: Implement the helper**

In `tracking.py`, after `comment_phase_failed` (~line 126), add:

```python
def comment_publish_deferred(ctx: ReleaseContext, jobs: list[str]) -> None:
    """Post a remediation comment for a deferred artifact publish.

    The release is complete (tag + GitHub Release published); these CD publish
    jobs must be re-run. ``vrg-release --resume`` does NOT apply — the version is
    already tagged — so the comment says so explicitly (#1853).
    """
    job_list = ", ".join(jobs)
    run = ctx.cd_run_url or "the CD run"
    lines = [
        "<!-- vrg-release:publish:deferred -->",
        "",
        f"**Artifact publish deferred** for `{ctx.version}`.",
        "",
        f"The release tag `{ctx.tag}` and the GitHub Release are published, but "
        f"these CD publish jobs did not succeed: **{job_list}** ({run}).",
        "",
        "**To finish delivery** once the blocker is cleared: re-run the CD publish "
        "— `gh workflow run cd.yml` (Actions → CD → Run workflow), or the nightly "
        "`no-cache` ops rebuild.",
        "",
        "**Do not** run `vrg-release --resume`: the version is already tagged; "
        "`--resume` is for a *halted* release, not a *completed-with-deferral* one.",
    ]
    _comment(ctx, "\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_release_tracking.py::test_comment_publish_deferred_names_jobs_and_warns_resume -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/tracking.py tests/vergil_tooling/test_release_tracking.py
vrg-commit --type feat --scope release --message "add comment_publish_deferred remediation helper" --body "Posts a tracking-issue comment naming the deferred publish jobs, the CD re-run remediation, and an explicit warning against --resume.

Ref #1853"
```

---

### Task 7: `close-finalize` leaves the issue open on a deferred publish

**Files:**
- Modify: `src/vergil_tooling/lib/release/finalize.py:12` (import) and `:37-54` (`close_and_finalize`)
- Test: `tests/vergil_tooling/test_release.py` (or `test_release_finalize.py` if present)

**Interfaces:**
- Consumes: `ctx.deferred_failures` (existing), `ctx.deferred_publish_failures` (Task 1), `comment_publish_deferred` (Task 6), `close_tracking_issue`, `_build_summary`.
- Produces: `close_and_finalize` with three branches — (a) `deferred_failures` → resume path (unchanged); (b) `deferred_publish_failures` → comment + leave open, then still run cleanup; (c) clean → close + cleanup.

- [ ] **Step 1: Write the failing tests**

```python
# in the finalize test module
def test_close_and_finalize_publish_deferred_leaves_open() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    ctx.deferred_publish_failures = ["docker-publish"]
    with (
        patch.object(finalize, "comment_publish_deferred") as comment,
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress"),
    ):
        finalize.close_and_finalize(ctx)
    comment.assert_called_once()
    close.assert_not_called()  # issue stays open


def test_close_and_finalize_clean_closes_issue() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    with (
        patch.object(finalize, "comment_publish_deferred") as comment,
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress"),
    ):
        finalize.close_and_finalize(ctx)
    close.assert_called_once()
    comment.assert_not_called()


def test_close_and_finalize_stage_failure_short_circuits() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    ctx.deferred_failures = ["confirm-main"]
    with (
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress") as prog,
    ):
        finalize.close_and_finalize(ctx)
    close.assert_not_called()
    prog.run.assert_not_called()  # cleanup skipped — resumable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_release.py -k close_and_finalize -v`
Expected: FAIL — current code only branches on `deferred_failures`; `comment_publish_deferred` not imported.

- [ ] **Step 3: Update the import and `close_and_finalize`**

Change the tracking import (line 12) to:

```python
from vergil_tooling.lib.release.tracking import close_tracking_issue, comment_publish_deferred
```

Replace `close_and_finalize`'s body (lines 45–54, the part before "Running vrg-finalize-pr") with:

```python
    if ctx.deferred_failures:
        print(
            "Leaving the tracking issue open and skipping cleanup — earlier "
            f"stages failed ({', '.join(ctx.deferred_failures)}). Fix the cause "
            "and resume with vrg-release --resume."
        )
        return

    if ctx.deferred_publish_failures:
        comment_publish_deferred(ctx, ctx.deferred_publish_failures)
        print(
            "Leaving the tracking issue open — artifact publish deferred "
            f"({', '.join(ctx.deferred_publish_failures)}). The release is "
            "complete; re-run the CD publish to deliver the artifacts (NOT "
            "--resume)."
        )
    else:
        summary = _build_summary(ctx)
        close_tracking_issue(ctx, summary)
        print("Tracking issue closed.")
```

**Precedence is intentional, not incidental:** `deferred_failures` is checked
**first** and short-circuits to the resume path. A genuine stage failure means
the release is halted/resumable (`--resume`), which is the *opposite*
remediation from a publish deferral (do *not* `--resume`); emitting both would
contradict. When both lists are non-empty the resume path wins and the publish
deferral rides along in the same still-open issue. Do not reorder these checks.

(The existing "Running vrg-finalize-pr..." block stays as-is and now runs for both the close branch and the publish-deferred branch — the release is complete in both, so the branches must be pruned.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_release.py -k close_and_finalize -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/finalize.py tests/vergil_tooling/test_release.py
vrg-commit --type feat --scope release --message "leave tracking issue open on deferred publish in close-finalize" --body "Distinct from the deferred_failures resume path: a deferred publish posts the CD-rerun remediation comment and leaves the issue open, but still runs finalize-pr cleanup (the release is complete).

Ref #1853"
```

---

### Task 8: `consumer-refresh` hold-warning guard

**Files:**
- Modify: `src/vergil_tooling/lib/release/handoff.py:15-41` (`consumer_refresh`)
- Test: `tests/vergil_tooling/test_release.py` (or `test_release_handoff.py` if present)

**Interfaces:**
- Consumes: `ctx.deferred_publish_failures`.
- Produces: `consumer_refresh` prints a hold-warning and sets `ctx.consumer_refresh_commands = None` when the list is non-empty, before reading any config.

- [ ] **Step 1: Write the failing test**

```python
def test_consumer_refresh_holds_when_publish_deferred() -> None:
    from vergil_tooling.lib.release.handoff import consumer_refresh

    ctx = _ctx()
    ctx.deferred_publish_failures = ["docker-publish"]
    consumer_refresh(ctx)  # must not read config / must not raise
    assert ctx.consumer_refresh_commands is None
    assert "held" in (ctx.consumer_refresh_message or "").lower()
    assert "2.1.0" in (ctx.consumer_refresh_message or "")  # version named
```

(The `_ctx()` builder sets `version="2.1.0"`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_release.py::test_consumer_refresh_holds_when_publish_deferred -v`
Expected: FAIL — current code calls `config.read_config` unconditionally; no hold-warning.

- [ ] **Step 3: Add the guard**

At the top of `consumer_refresh` (immediately after the docstring, before `cfg = config.read_config(...)`), insert:

```python
    if ctx.deferred_publish_failures:
        message = (
            "⚠ Consumer refresh held: artifact publish was deferred "
            f"({', '.join(ctx.deferred_publish_failures)}). Do NOT advertise "
            f"v{ctx.version} to consumers until the CD publish is re-run and the "
            "artifacts are delivered."
        )
        ctx.consumer_refresh_message = message
        ctx.consumer_refresh_commands = None
        print()
        print(message)
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_release.py::test_consumer_refresh_holds_when_publish_deferred -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/handoff.py tests/vergil_tooling/test_release.py
vrg-commit --type feat --scope release --message "hold consumer-refresh when publish deferred" --body "Print a hold-warning instead of upgrade guidance when ctx.deferred_publish_failures is non-empty, so the run never tells consumers to upgrade to a version whose artifacts did not ship.

Ref #1853"
```

---

### Task 9: Full validation and PR readiness

**Files:** none (verification only)

- [ ] **Step 1: Run the full release test suite**

Run: `uv run pytest tests/vergil_tooling/ -k release -v`
Expected: PASS — all release tests green, including the deleted-constant tests are gone and new ones pass.

- [ ] **Step 2: Run the complete suite (catch cross-module fallout)**

Run: `uv run pytest -q`
Expected: PASS — no other module imported `_verify_jobs` / `_MAIN_EXPECTED_JOBS` (grep to confirm: `grep -rn "_verify_jobs\|_MAIN_EXPECTED_JOBS\|_DEVELOP_EXPECTED_JOBS" src tests` returns only intended sites).

- [ ] **Step 3: Lint, types, and the repo gate**

Run: `vrg-container-run -- vrg-validate`
Expected: green (ruff, mypy/ty, markdownlint of the spec/plan docs).

- [ ] **Step 4: Confirm the design's acceptance criteria by inspection**

- A `docker-publish`/`docs` failure no longer aborts: `confirm_main`/`confirm_develop` do not raise for them (Tasks 3–4).
- A `release`-job failure or missing artifact still hard-fails (Task 2 `_verify_release_job`, existing `_verify_artifacts`).
- Deferred publish ends exit 1 (Task 5), issue stays open with remediation (Tasks 6–7), consumer-refresh holds (Task 8).
- No `vergil.toml` key was added (grep `vergil.toml` / `read_config` shows no new release-gate config).

- [ ] **Step 5: Final commit if any validation fixups were needed**

```bash
vrg-git add -A
vrg-commit --type test --scope release --message "validation fixups for deferred-publish feature" --body "Ref #1853"
```

(Skip if nothing changed in Steps 1–4.)

---

## Self-Review

**Spec coverage:** Hard gate narrowed to release job + artifacts (Tasks 2–3); exact-match release job (Task 2); defer every other job into `deferred_publish_failures` (Tasks 3–4); new field (Task 1); `publish-status` terminal `fail_defer` stage → exit 1 (Task 5); `close-finalize` leaves issue open + remediation, warns off `--resume` (Tasks 6–7); `consumer-refresh` hold-warning (Task 8); `confirm-develop` unified into the same list (Task 4); fleet-wide, no config (Global Constraints + Task 9 Step 4). Watch-blocks-until-terminal is relied on by `check_status=False` (Task 3) and was verified in the spec.

**Placeholder scan:** none — every code/test step carries full content.

**Type consistency:** `deferred_publish_failures: list[str]` used identically across Tasks 1, 3–8; `_RELEASE_JOB_NAME` / `_verify_release_job` / `_collect_deferred_publish` signatures match between Task 2 (definition) and Tasks 3–4 (callers); `comment_publish_deferred(ctx, jobs)` matches between Task 6 (def) and Task 7 (call); `_publish_status_stage(state)` matches Task 5 def and the `Stage` wiring.
