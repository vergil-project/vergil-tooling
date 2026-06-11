# Resumable releases via the tracking issue as durable state

- **Issue:** [#1612](https://github.com/vergil-project/vergil-tooling/issues/1612)
- **Status:** Design
- **Date:** 2026-06-11

## Problem

When `vrg-release` fails partway through its pipeline — usually at a
verification step, often from a transient GitHub glitch — the release is left
half-complete. Some of {tag/publish, rolling-tag promote, back-merge-bump,
develop-CD confirm, close-finalize} are done and the rest are not. There is no
resume path: `preflight` hard-aborts if the release branch or the tracking
issue already exists, so re-running `vrg-release` cannot pick up where it left
off. The only ways forward today are to abandon the in-flight version and bump
to a new one (leaving orphaned tags, branches, and an open tracking issue as
cruft), or to complete the remaining stages by hand. This happens a few times
a week and each manual recovery is a slog.

This is the motivating incident behind the 2.1.27–2.1.30 recovery, where a
`confirm-main` race (now fixed under #1611) aborted an already-succeeded
release and the manual recovery then compounded into further mistakes.

## Goal

`vrg-release --resume` detects an existing open `release: X.Y.Z` tracking issue
and continues the interrupted release from the first incomplete stage, without
bumping the version or leaving orphaned artifacts. Re-running is safe: a stage
that already ran is skipped or re-done harmlessly.

Non-goal for v1: a hard concurrency mutex beyond the issue-as-lock. `vrg-release`
is a human-driven, serialized command; two simultaneous `--resume` runs on the
same issue are user error, not worth a locking subsystem.

## Architecture

The tracking issue becomes the single durable artifact for an in-flight
release:

- **Title** carries the version: `release: X.Y.Z`.
- **Body** holds a canonical **stage checklist** in a delimited block — the
  resume cursor.
- **Comments** are the chronological log: each phase-complete, each failure
  (with its error), and a marker each time `--resume` adopts the issue.

`ReleaseContext` stays in-memory but becomes **reconstructible**: the resume
path rebuilds it from the issue (version, issue number) plus a few cheap repo
probes (the release PR URL, the back-merge PR, tags). Both entry modes then run
the *same* pipeline:

- **Fresh** (`vrg-release`): creates the issue + checklist; still aborts if an
  open `release: X.Y.Z` issue already exists (the lock).
- **Resume** (`vrg-release --resume`): adopts the existing open issue,
  reconstructs context, and enters the pipeline at the first unchecked box.
  Each stage optionally probes reality to skip, does its work, then ticks its
  box.

## State model

### The checklist

The checklist lives in a delimited block in the issue body so writes never
clobber the human-written summary:

```markdown
<!-- vrg-release:progress -->
- [x] audit
- [x] preflight
- [x] prepare
- [ ] merge-release
- [ ] confirm-main
- [ ] back-merge-bump
- [ ] teardown-worktree
- [ ] confirm-develop
- [ ] promote
- [ ] close-finalize
- [ ] consumer-refresh
<!-- /vrg-release:progress -->
```

- **Source of truth for the list** is `build_stages()`; the checklist is
  *generated* from it, so the stage names and order cannot drift from the real
  pipeline.
- **Writing**: on completion each stage rewrites just the delimited block
  (flip its `[ ]` → `[x]`) via an issue-body edit. The issue-as-lock means only
  one `vrg-release` touches it, so there are no races. `[x]` vs `[ ]` is the
  only thing parsed; any annotations (timestamps) are cosmetic.
- **The box is ticked at the *end* of a stage**, after its real work. Ticking
  and the work are not one atomic operation, so a crash between them leaves the
  box unchecked and resume re-runs the stage. Re-doing beats skipping.
- **All stages appear** in the checklist, including ones that may no-op (e.g.
  `promote` under `--no-promote` still ticks as a completed no-op). Uniform and
  simple.

### Ordering invariant: the issue comes before any other artifact

The tracking issue is the durable state *and* the lock, so it must be created
**after all read-only validation, but before the first durable artifact**
(release branch, PR, tag). Any failure *after* the issue exists is resumable;
any failure *before* it leaves nothing behind and is handled by a plain fresh
re-run.

The placement matters in both directions. Today `prepare` (stage 3) creates the
issue while `preflight` (stage 2) already creates the release branch/worktree,
so a `prepare` failure could strand a branch with no issue — the gap this fixes.
But the issue must not be created *too* early either: `preflight` does two
things, **read-only validation** (on `develop`? clean tree? version not already
tagged? branch exists?) and **branch/worktree creation**. Creating the issue
before the validations would manufacture a tracking issue for every release that
*correctly* fails preflight ("version already tagged", "develop behind origin")
— cruft for a release that should never have started.

So the sequence is: `audit` → preflight **validations** → **create issue +
checklist** → preflight **branch/worktree** (adopt-or-create) → `prepare`.
Validation failures still leave nothing behind; only branch-creation-and-onward
is resumable. `audit` and the preflight validations are recorded as `[x]` at
creation time. This likely means splitting today's `preflight` into a validation
part and a branch part so issue creation can sit between them — a detail for the
implementation plan, but the invariant it must satisfy is *issue after all
read-only checks, before the first durable write*.

### Version-skew guard

On resume the checklist's stage names are validated against the current
`build_stages()`. If they do not match, resume refuses rather than guessing —
a mismatch signals the checklist was written by a different tooling version.

The refusal must be **actionable**, not a dead end: the error explains the cause
and the two ways forward — complete the release with the tooling version that
started it (its checklist matches), or finish the remaining stages manually.
Auto-migrating an old checklist to the new stage
list is deliberately *not* offered: stage semantics may have changed between
versions, and silently remapping is exactly the guessing the guard exists to
prevent. A `--force`-style override could be added later if skew ever proves
common, but it is YAGNI for v1 — the skew is rare and the manual path bounds it.

### The log

Comments remain the log. The existing `comment_phase_complete` /
`comment_phase_failed` helpers already capture per-phase progress and the prior
error. Resume adds one **"▶ Resuming at `<stage>`"** marker comment each time
`--resume` adopts the issue, so the timeline reads cleanly.

### Manual override

The checklist is a real GitHub task list. If an operator fixes a stage by hand,
they tick its box in the GitHub UI; resume then treats it as done and proceeds.
Manual override is built into the medium — no special "skip stage" flag. The
version-skew guard still protects against a checklist from a different version.

## Per-stage idempotency

Idempotency is best-effort by cost: where a "already done?" check is cheap, the
stage probes and skips; where it would be expensive, the stage simply re-runs.
Surveying all eleven stages, every needed probe turns out to be cheap (a PR
state, a tag or branch lookup) or the stage is naturally idempotent
(verification, display, read-only), so overhead is negligible.

| Stage | On resume | Cheap probe |
|---|---|---|
| audit | re-run (read-only) | — |
| **preflight** | **adopt-or-create**: reuse the existing `release/X.Y.Z` branch/worktree instead of aborting | branch/worktree exists |
| prepare | **sub-step idempotent**: complete only the missing part of {changelog commit, push, PR}; rebuild `release_pr_url` | `pr list --head` + branch log |
| merge-release | skip if release PR `MERGED`; else re-run the merge | PR state |
| confirm-main | re-run (pure verification, #1611-hardened) | — |
| back-merge-bump | skip if back-merge PR merged; adopt an existing open `release/post-X.Y.Z` | `pr list --head release/post-X.Y.Z` |
| teardown-worktree | remove-if-present | worktree exists |
| confirm-develop | re-run (verification) | — |
| promote | already a force-update; optional skip if `vX.Y` already there | tag compare |
| close-finalize | cleanup is idempotent; **close the issue only if no fail-defer error is pending** | deferred-error state |
| consumer-refresh | re-run (display only) | — |

Four points:

1. **The core change is `preflight` → adopt-or-create.** Today's hard "branch
   already exists" abort is exactly what blocks resume. The *fresh* path keeps
   that abort (the lock); the *resume* path adopts the existing branch and
   worktree instead. `back-merge-bump` needs the same adopt-existing treatment
   for `release/post-X.Y.Z`.
2. **"Skip" never means "no-op" — it means *hydrate from reality*.** A stage
   that is already done still re-derives the `ReleaseContext` fields it would
   have set (see [Context reconstruction](#context-reconstruction)) by probing,
   then skips only the *work*. Otherwise a later stage entered on resume finds
   `None` where it expected an upstream stage's output.
3. **`close-finalize` closes the issue only when the release truly succeeded.**
   The tail (`teardown-worktree`, `confirm-develop`, `promote`, `close-finalize`)
   is `fail_defer`: a failure there is recorded and the pipeline keeps going, so
   without care `close-finalize` would close the issue *despite* a failed
   `promote`, leaving an unchecked box on a closed issue that `--resume` (which
   scans only open issues) can never recover. So `close-finalize` closes the
   tracking issue **only if no prior fail-defer stage has a pending error** — it
   reads the runner's accumulated deferred-error state. Any deferred failure →
   the issue stays open → resumable. `consumer-refresh` runs after and is just a
   reminder, so it does not gate the close.
4. **The manual-repair fallback is not for idempotency gaps** — every stage
   above is covered cheaply. It is for genuine *work* failures that recur (a
   merge conflict, a red CD): fix the underlying cause, then re-run `--resume`,
   which only has to get the broken stage across the line and proceed.

## CLI surface and edges

- **`vrg-release`** (fresh): unchanged — creates the issue/checklist, aborts if
  an open `release: X.Y.Z` issue exists.
- **`vrg-release --resume`**: adopts the open release issue, reconstructs the
  minimal context anchors (version, issue number) up front while each skipped
  stage hydrates its own outputs (see [Context
  reconstruction](#context-reconstruction)), and enters at the first unchecked
  box. No `--continue` alias — one spelling.

**Which release does `--resume` adopt?** Look for open `release: X.Y.Z` issues:

- **Zero** → error: "no in-flight release to resume."
- **One** → adopt it.
- **Multiple** → anomaly (releases are serialized); refuse and require
  `vrg-release --resume X.Y.Z` to name it.

**Flag interactions:** `--resume` together with a `{minor,major}` bump argument
is an error — the version is locked by the issue. Other flags (`--no-promote`,
`--skip-audit`) apply to the resumed run normally.

## Context reconstruction

`ReleaseContext` is in-memory, so on resume it must be rebuilt — and this is the
subtle part, because most fields are populated by stages that resume *skips*.
Downstream stages read far more than the obvious few: `close-finalize`'s summary
alone consumes `bump_pr_url`, `tag`, `develop_tag`, `release_url`, `cd_run_url`,
and `develop_cd_run_url`, all set by earlier stages. If those stages are skipped
and their fields left `None`, everything after them breaks.

The resolution is the **hydrate** principle (per-stage point 2): a skipped stage
re-derives its own `ctx` outputs from reality, rather than no-opping. So context
reconstruction is *not* one big up-front rebuild (which would duplicate every
stage's output-derivation in a second place that can drift); it is split:

- **Up front, before the pipeline:** the minimal anchors —
  - `version` ← parsed from the issue title (`release: X.Y.Z`).
  - `issue_number` ← the adopted open issue.
- **Per stage, on its skip/hydrate path:** that stage re-derives the fields it
  owns by probing — e.g. `prepare` → `release_pr_url` from `pr list --head`;
  `confirm-main` → `tag` / `develop_tag` / `release_url` / `cd_run_url`;
  `back-merge-bump` → `bump_pr_url` / `next_version`; `preflight` → the branch /
  worktree via adopt-or-create.

Each stage thus stays the single source of how its outputs are derived, on both
the do-the-work path and the skip path.

## Testing strategy

Built test-first, to the repo's 100% coverage bar:

- **Checklist block (unit):** write/parse round-trip; flipping one box
  preserves the rest of the body; the version-skew guard refuses a block whose
  stage names do not match `build_stages()`.
- **Resume cursor (unit):** first-unchecked detection; all-checked → nothing
  left to do.
- **Per-stage probes (unit):** each cheap probe (PR merged? tag at version?
  branch/worktree exists?) returns skip-vs-run correctly, with `gh`/`git`
  mocked.
- **Context reconstruction / hydrate (unit):** each stage's skip path populates
  the `ctx` fields it owns from probes (e.g. resume entered at `close-finalize`
  still has `tag`, `bump_pr_url`, the CD/release URLs), not `None`.
- **Deferred-error gating (unit):** `close-finalize` closes the issue when no
  fail-defer stage errored, and **leaves it open** when one did (e.g. a failed
  `promote`) so the release stays resumable.
- **`prepare` partial states (unit):** branch-with-commit-but-no-PR, and
  branch-pushed-but-no-PR, each complete only the missing sub-step without
  re-committing the changelog.
- **Issue-creation ordering (unit):** a preflight *validation* failure leaves no
  tracking issue; a failure after the branch is created leaves a resumable one.
- **CLI (unit):** `--resume` with zero / one / multiple open release issues;
  `--resume` + `{minor,major}` → error; fresh run with an existing issue → lock
  abort.
- **Resume-from-each-stage (integration, parametrized):** seed a checklist with
  stages `0..N-1` checked, run `--resume`, assert it enters at N, no-ops the
  checked ones, and runs N onward.
- **The two drift cases (adversarial):** box-checked-but-not-actually-done →
  the probe re-does it; box-unchecked-but-work-done (crash after work) → the
  probe skips it. These are the safety net, so they get explicit tests.
