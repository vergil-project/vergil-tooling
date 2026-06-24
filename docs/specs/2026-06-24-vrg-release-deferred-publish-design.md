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

The `release` job is the single load-bearing assertion, so it is matched
**exactly** (the reusable-workflow leaf name `release / release`, via a named
constant), **not** by the loose substring matching `_find_job` uses elsewhere.
Exact matching avoids a future `release`-containing job (e.g. `release-notes`)
binding the hard gate to the wrong job. If the release job is ever renamed
upstream, the exact lookup returns nothing and confirm-main hard-fails
"release job not found" — fail-closed, which is the safe direction. The
substring `_find_job` is retained only for the deferred-job sweep.

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

### `confirm-develop` — unified reporting, not a behavior change

`confirm-develop` is **already** `mode="fail_defer"`, so a `docs` failure there
already does not abort the pipeline — that behavior needs no change. The only
change is **reporting**: instead of letting `confirm_develop` raise (which would
record the generic stage name `"confirm-develop"` into the `_tracked`-owned
`deferred_failures` and surface a bare stage error), it records the failed
`docs` job into the **same** `ctx.deferred_publish_failures` list that
`confirm-main` uses, and does not raise. That way every deferred publish
failure — docker-on-main, docs-on-main, docs-on-develop — flows through one
surface: the `publish-status` summary, the open tracking issue, and the same
remediation message. Without this, a docs-on-develop failure would report
differently and would not trigger the "leave the issue open + how to republish"
handling (which keys off `deferred_publish_failures`).

### Mechanics

1. **Stop vetoing on overall run status.** `confirm_main` watches the run to a
   terminal state but no longer treats overall-run failure as fatal
   (`_watch_cd(..., check_status=False)` for the main-branch confirm). The
   run-level red is expected when a deferrable job fails.
2. **Classify per job.** After the run settles, fetch all jobs. Hard-verify the
   `release` job and the artifacts. For every other job whose conclusion is
   neither `success` nor `skipped`, append its **family** to a **new** context
   field `ctx.deferred_publish_failures: list[str]`, and print a warning. Do not
   raise. The family is the part of a reusable-workflow leaf name before
   `" / "` (e.g. `docker-publish / publish: prod-base:latest` → `docker-publish`),
   so a matrix of failed `docker-publish` leaves collapses to a single
   `docker-publish` entry; the field holds ordered-unique families. The run URL
   for the remediation comes from `ctx.cd_run_url` (already set), so no per-job
   URL or conclusion is stored — `list[str]` is enough.

   A separate field is used rather than the existing `ctx.deferred_failures`
   because the latter is owned by the `_tracked` wrapper, which appends **stage
   names** on stage exceptions. Mixing job families into it would conflate the
   two — see the precedence rule under *Surface the deferral*.
3. **Surface the deferral** via three consumers:
   - `close-finalize` must **not** close the tracking issue when
     `ctx.deferred_publish_failures` is non-empty; instead it posts a comment
     and leaves the issue open. The comment names the **concrete** remediation,
     because the intuitive next step is a trap: the release is already tagged
     and `develop` already bumped, so `vrg-release --resume` does **not** apply
     (it is for a *halted* release, not a *completed-with-deferral* one) and
     will error on the existing tag. The correct remediation is a **CD-side
     re-run** of the publish — `gh workflow run cd.yml` / Actions → CD → Run
     workflow, or the nightly `no-cache` `ops.yml` rebuild — once the blocker
     (e.g. CVE triage) is cleared. The comment states this explicitly and warns
     against `--resume`. A first-class `vrg-republish` helper is tracked
     separately as a follow-up (#1854).

     **Precedence (both lists non-empty).** A genuine fail-defer *stage* error
     (in `ctx.deferred_failures`, e.g. `promote` blew up) and a publish deferral
     (in `ctx.deferred_publish_failures`) can co-occur. They have *opposite*
     remediations — a stage error means the release is halted and **resumable**
     (`--resume`), a publish deferral means it is **complete** (do *not*
     `--resume`). `deferred_failures` therefore **takes precedence**:
     `close-finalize` checks it first and short-circuits to the resume path,
     leaving the issue open without the publish-deferral comment (the publish
     deferral rides along in the same open issue). Emitting both would produce
     contradictory `--resume` / don't-`--resume` guidance. This precedence is a
     rule, not an accident of statement order — do not reorder the checks.
   - `consumer-refresh` must **guard** on `ctx.deferred_publish_failures`: when
     it is non-empty, it prints a **hold-warning** ("artifacts pending
     republish — do not advertise vX.Y.Z to consumers until republished")
     instead of the normal upgrade guidance. Otherwise the run would print
     "consumers can upgrade now" immediately before the deferred-failure
     summary says the artifacts never shipped — contradictory output that
     invites advertising a stale release.
   - A new terminal stage **`publish-status`** (mode `fail_defer`, last in the
     pipeline) raises a `ReleaseError` iff `ctx.deferred_publish_failures` is
     non-empty. Being `fail_defer`, it does not abort anything (every prior
     stage has already run) but it marks the run failed, so the existing
     `lib/progress.py` summary renders the deferred `PipelineError … exit 1`
     naming the unpublished artifacts. When the field is empty the stage is a
     no-op and the run exits `0`. **It is wired as a bare `Stage`, not via the
     `_tracked` wrapper** (like `teardown-worktree`): `_tracked` appends the
     stage name to `deferred_failures` and posts a "phase failed" tracking-issue
     comment on any exception, so wrapping `publish-status` would both pollute
     `deferred_failures` (re-conflating the two lists) and spam the issue. Its
     raise must reach `run_pipeline` directly.

### Why not a `vergil.toml` opt-in

An earlier iteration scoped this per-repo via `vergil.toml`. It was dropped:
the principle (only the release commit is irreversible) is universal, so a
config knob would only ever be set one way. Per YAGNI, no config is introduced.
If a future repo genuinely needs a publish to be fatal, a config key can be
added then, against a concrete need.

## Components touched

- `lib/release/confirm.py` — `confirm_main`: watch with `check_status=False`,
  exact-match the `release` job for the hard gate, classify every other failed
  job into `ctx.deferred_publish_failures`. `confirm_develop`: record a failed
  `docs` job into the same list (do not raise). Add an exact-match helper (named
  constant for `release / release`); keep substring `_find_job` for the sweep.
- `lib/release/tracking.py` — add `comment_publish_deferred(ctx, jobs)`, which
  posts the deferred-publish remediation comment via the existing `_comment`
  helper (names the CD re-run, warns against `--resume`).
- `lib/release/finalize.py` — `close_and_finalize` honors a non-empty
  `ctx.deferred_publish_failures`: call `comment_publish_deferred` and leave the
  issue open (but still run finalize-pr cleanup — the release is complete).
  Checks `deferred_failures` **first** (the resume precedence above).
- `lib/release/handoff.py` — `consumer_refresh` guards on
  `ctx.deferred_publish_failures`: print a hold-warning instead of upgrade
  guidance when non-empty.
- `lib/release/context.py` — add `deferred_publish_failures: list[str]`
  (ordered-unique failed job families), separate from the `_tracked`-owned
  `deferred_failures`.
- `lib/release/orchestrator.py` — append one terminal `publish-status` stage
  (mode `fail_defer`) that raises iff `ctx.deferred_publish_failures` is
  non-empty. Wired as a **bare `Stage`, not `_tracked`** (see the publish-status
  note above). `confirm-main` keeps mode `fail_fast` (its *hard gate* is still
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
        ├─ yes ─▶ leave issue OPEN + comment: CD re-run remediation, NOT --resume
        └─ no  ─▶ close tracking issue
        │
        ▼
consumer-refresh  (fail_defer)
  └─ ctx.deferred_publish_failures non-empty?
        ├─ yes ─▶ hold-warning ("pending republish; do not advertise vX.Y.Z")
        └─ no  ─▶ normal upgrade guidance
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
- `check_status=False` does **not** make the watch return early: `watch_workflow`
  runs `gh run watch <run_id>`, which blocks until the run is terminal
  regardless; `check_status` only toggles `--exit-status` (whether a red run
  *propagates* as a failure). So the per-job classification never races a
  still-running job, and `_settled_run_jobs` guards the conclusion-settle window
  on top of that.

## Testing

- Unit: `confirm_main` with a jobs fixture where `docker-publish` =
  `failure`, `release` = `success`, artifacts present → no raise,
  `ctx.deferred_publish_failures == ["docker-publish"]`.
- Unit: `release` = `failure` → raises (fatal), regardless of other jobs.
- Unit: artifacts missing (`release` success but no tag) → raises (fatal).
- Unit: a job named `release-notes` present alongside `release / release` →
  the hard gate binds to `release` exactly, not the substring match.
- Unit: `docs` = `failure` on develop → `confirm_develop` records it into
  `ctx.deferred_publish_failures` and does not raise.
- Unit: `close_and_finalize` with non-empty `deferred_publish_failures` → issue
  left open; the comment names the CD re-run and warns against `--resume`; with
  empty → issue closed.
- Unit: `consumer_refresh` with non-empty `deferred_publish_failures` → prints
  the hold-warning, not upgrade guidance; with empty → normal guidance.
- Unit: `publish-status` stage raises (deferred) when
  `deferred_publish_failures` is non-empty, no-op when empty.
- Integration: full pipeline with a stubbed CD run where only `docker-publish`
  failed → all stages run, `develop ⊇ main`, rolling tag promoted, consumer
  hold-warning shown, exit 1, tracking issue open with remediation comment.

## Scope

In scope: `confirm-main` / `confirm-develop` job classification; the new
`deferred_publish_failures` context field and `publish-status` terminal stage;
the `close-finalize` issue-handling + remediation comment; and the
`consumer-refresh` hold-warning guard.

Out of scope: the `vergil-docker` Trivy CVE triage itself (a separate,
recurring `.trivyignore` task); changing what the `cd.yml` jobs do; any
`vergil.toml` configuration.

## Limitations

- Defer-by-default means a future *non-publish* critical job added to CD would
  also defer unless it is the `release` job. This is acceptable: the deferred
  failure is still surfaced (exit 1, issue open) — it just no longer strands
  `develop`. If such a job ever needs hard-fail semantics, it can be added to
  the hard gate explicitly at that time.
