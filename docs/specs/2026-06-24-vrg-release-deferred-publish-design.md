# Defer Artifact-Publish Failures in vrg-release

**Issue:** [#1853](https://github.com/vergil-project/vergil-tooling/issues/1853)
**Date:** 2026-06-24

## Problem

`vrg-release` aborts the entire release pipeline when **any** CD job fails at
the `confirm-main` gate — including artifact-publish jobs (`docker-publish`,
`docs`) that have no bearing on the integrity of the release itself.

The `confirm-main` stage runs in `fail_fast` mode and sits **before**
`back-merge-bump` in the pipeline:

```
prepare → merge-release → confirm-main (fail_fast) → back-merge-bump → ...
```

So when a publish job fails, `confirm-main` raises and the run aborts **before**
`develop` is back-merged and version-bumped. The result is the recurring mess:
`develop` left behind `main` (no back-merge), no rolling `vX.Y` tag, and the
release tracking issue left open — each occurrence requiring a manual
back-merge + bump + tag-promote cleanup.

This bites `vergil-docker` routinely: its `docker-publish` job fails the Trivy
CRITICAL/HIGH gate whenever the upstream language base images carry un-triaged
CVEs, which is a near-constant background condition.

### Root cause

`confirm_main` (in `lib/release/confirm.py`) gates on two things:

1. `_watch_cd(ctx, branch="main", check_status=True)` — fails if the
   **overall run** status is not success. A `docker-publish` failure turns the
   whole `cd.yml` run red, so this is what trips.
2. `_verify_jobs(ctx, run_id, _MAIN_EXPECTED_JOBS=("docs", "release"))` — a
   per-job check that currently treats `docs` as hard-required and does not
   look at `docker-publish` at all.

The overall-run-status gate (1) is the over-broad veto: it aborts the release
on *any* job failure, even a re-publishable artifact job.

## Principle

The git tag + GitHub Release is the only **irreversible** commitment in a
release. Every published artifact — docs, Docker images, packages — is
**re-publishable** after the fact, without re-cutting the release. Therefore an
artifact-publish failure must never abort the release bookkeeping (back-merge,
develop bump, tag promote).

This holds for every repository, so the new behavior is a **fleet-wide
default** with **no configuration**. We lower the release-integrity bar for no
one; we simply stop letting retryable downstream work veto it.

## Solution

`confirm-main` hard-fails on exactly one thing — the release itself — and
defers everything else.

### Hard gate (unchanged severity, narrowed scope)

`confirm-main` raises (fatal, `fail_fast`) only when one of these is true:

- the `release` job did not conclude `success`, or
- a release artifact is missing: the `vX.Y.Z` tag, the GitHub Release for that
  tag, or the `develop-vX.Y.Z` boundary tag.

If the `release` job failed, or the tag/Release cannot be cut, the release is
genuinely broken and must stop immediately — same as today.

### Deferred jobs (the change)

Every **other** job in the `cd.yml` run that did not conclude `success` —
`docs`, `docker-publish`, and anything added later — is recorded as a
**deferred failure** rather than raising:

- `confirm-main` does **not** raise for it; the stage completes, so the pipeline
  proceeds through `back-merge-bump` and the remaining stages. `develop` is
  back-merged and bumped, the rolling `vX.Y` tag is promoted — the bookkeeping
  always completes.
- The deferred failure is surfaced at the end of the run: a non-zero exit, a
  clear summary line naming each failed publish job, and the release tracking
  issue is **left open** with a "republish" to-do rather than closed.

`confirm-develop`'s `docs` check defers the same way, for consistency.

### Mechanics

1. **Stop vetoing on overall run status.** `confirm_main` watches the run to a
   terminal state but no longer treats overall-run failure as fatal
   (`_watch_cd(..., check_status=False)` for the main-branch confirm). The
   run-level red is expected when a deferrable job fails.
2. **Classify per job.** After the run settles, fetch all jobs. Hard-verify the
   `release` job (`conclusion == "success"`) and the artifacts. For every other
   job whose `conclusion != "success"`, append a structured entry to a **new**
   context field `ctx.deferred_publish_failures` (job name + conclusion + run
   URL) and print a warning. Do not raise.

   A separate field is used rather than the existing `ctx.deferred_failures`
   because the latter is owned by the `_tracked` wrapper, which appends **stage
   names** on stage exceptions. Mixing job names into it would conflate the two.
3. **Surface the deferral** via two independent consumers:
   - `close-finalize` must **not** close the tracking issue when
     `ctx.deferred_publish_failures` is non-empty; instead it posts a comment
     listing the deferred publish jobs and the remediation (re-run the publish /
     nightly `no-cache` rebuild) and leaves the issue open.
   - A new terminal stage **`publish-status`** (mode `fail_defer`, last in the
     pipeline) raises a `ReleaseError` iff `ctx.deferred_publish_failures` is
     non-empty. Being `fail_defer`, it does not abort anything (every prior
     stage has already run) but it marks the run failed, so the existing
     `lib/progress.py` summary renders the deferred `PipelineError … exit 1`
     naming the unpublished artifacts. When the field is empty the stage is a
     no-op and the run exits `0`.

### Why not a `vergil.toml` opt-in

An earlier iteration scoped this per-repo via `vergil.toml`. It was dropped:
the principle (only the release commit is irreversible) is universal, so a
config knob would only ever be set one way. Per YAGNI, no config is introduced.
If a future repo genuinely needs a publish to be fatal, a config key can be
added then, against a concrete need.

## Components touched

- `lib/release/confirm.py` — `confirm_main` (narrow the hard gate, classify and
  record deferred jobs); `confirm_develop` (defer `docs`); the `_verify_jobs` /
  `_watch_cd` helpers.
- `lib/release/finalize.py` — `close_and_finalize` honors a non-empty
  `ctx.deferred_failures` (leave the issue open, post a remediation comment).
- `lib/release/context.py` — add `deferred_publish_failures` (job name,
  conclusion, run URL), separate from the `_tracked`-owned `deferred_failures`.
- `lib/release/orchestrator.py` — append one terminal `publish-status` stage
  (mode `fail_defer`) that raises iff `ctx.deferred_publish_failures` is
  non-empty. `confirm-main` keeps mode `fail_fast` (its *hard gate* is still
  fatal); the deferral itself is data on the context, surfaced by the new stage.

## Data flow

```
confirm-main  (fail_fast)
  ├─ watch cd.yml run on main → terminal (no overall-status veto)
  ├─ release job success?  ── no ─▶ raise (fatal)
  ├─ artifacts present?    ── no ─▶ raise (fatal)
  └─ each other failed job ─▶ ctx.deferred_publish_failures.append(job)  (no raise)
        │
        ▼
back-merge-bump … promote … (all run normally; develop ⊇ main, vX.Y promoted)
        │
        ▼
close-finalize  (fail_defer)
  └─ ctx.deferred_publish_failures non-empty?
        ├─ yes ─▶ leave tracking issue OPEN + remediation comment
        └─ no  ─▶ close tracking issue
        │
        ▼
publish-status  (fail_defer, terminal)
  └─ ctx.deferred_publish_failures non-empty? ── yes ─▶ raise ─▶ summary exit 1
                                              └─ no  ─▶ no-op  ─▶ exit 0
```

## Error handling

- A genuinely-broken release (release job failed, or tag/Release/boundary tag
  missing) still aborts immediately at `confirm-main`, before any back-merge —
  unchanged from today.
- A transient jobs-API lag is already handled by `_settled_run_jobs`; the
  classification reuses it so a still-settling job is not misread as failed.
- If the CD run itself never appears or never reaches terminal, that remains a
  hard `confirm-main` failure (we cannot confirm the release at all).

## Testing

- Unit: `confirm_main` with a jobs fixture where `docker-publish` =
  `failure`, `release` = `success`, artifacts present → no raise,
  `ctx.deferred_publish_failures == ["docker-publish"]`.
- Unit: `release` = `failure` → raises (fatal), regardless of other jobs.
- Unit: artifacts missing (`release` success but no tag) → raises (fatal).
- Unit: `docs` = `failure` on develop → `confirm_develop` defers, no raise.
- Unit: `close_and_finalize` with non-empty `deferred_publish_failures` → issue
  left open, remediation comment posted; with empty → issue closed.
- Unit: `publish-status` stage raises (deferred) when
  `deferred_publish_failures` is non-empty, no-op when empty.
- Integration: full pipeline with a stubbed CD run where only `docker-publish`
  failed → all stages run, `develop ⊇ main`, rolling tag promoted, exit 1,
  tracking issue open.

## Scope

In scope: `confirm-main` / `confirm-develop` job classification, the
deferred-failure surfacing, and the `close-finalize` issue-handling change.

Out of scope: the `vergil-docker` Trivy CVE triage itself (a separate,
recurring `.trivyignore` task); changing what the `cd.yml` jobs do; any
`vergil.toml` configuration.

## Limitations

- Defer-by-default means a future *non-publish* critical job added to CD would
  also defer unless it is the `release` job. This is acceptable: the deferred
  failure is still surfaced (exit 1, issue open) — it just no longer strands
  `develop`. If such a job ever needs hard-fail semantics, it can be added to
  the hard gate explicitly at that time.
