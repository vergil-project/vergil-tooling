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
**before any other durable artifact** (release branch, PR, tag). Any failure
*after* the issue exists is resumable; any failure *before* it leaves nothing
behind and is handled by a plain fresh re-run.

This moves issue creation earlier than today. Currently `prepare` (stage 3)
creates the issue, while `preflight` (stage 2) already creates the release
branch/worktree — so a `prepare` failure could strand a branch with no issue.
Under this design the issue + checklist is created as the first durable step,
immediately after `audit` (which is read-only and leaves nothing behind):
`audit` is recorded as `[x]` at creation time, and `preflight` onward tick into
the existing issue. The exact refactor — where issue creation lands relative to
`preflight`'s branch/worktree creation — is for the implementation plan; the
invariant it must satisfy is *issue first, everything else after*.

### Version-skew guard

On resume the checklist's stage names are validated against the current
`build_stages()`. If they do not match, resume refuses rather than guessing —
a mismatch signals the checklist was written by a different tooling version.

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
| prepare | skip if the release PR exists; rebuild `release_pr_url` from it | `pr list --head release/X.Y.Z` |
| merge-release | skip if release PR `MERGED`; else re-run the merge | PR state |
| confirm-main | re-run (pure verification, #1611-hardened) | — |
| back-merge-bump | skip if back-merge PR merged; adopt an existing open `release/post-X.Y.Z` | `pr list --head release/post-X.Y.Z` |
| teardown-worktree | remove-if-present | worktree exists |
| confirm-develop | re-run (verification) | — |
| promote | already a force-update; optional skip if `vX.Y` already there | tag compare |
| close-finalize | cleanup is idempotent; **close the issue last** | (issue is open — that is why we are here) |
| consumer-refresh | re-run (display only) | — |

Three points:

1. **The core change is `preflight` → adopt-or-create.** Today's hard "branch
   already exists" abort is exactly what blocks resume. The *fresh* path keeps
   that abort (the lock); the *resume* path adopts the existing branch and
   worktree instead. `back-merge-bump` needs the same adopt-existing treatment
   for `release/post-X.Y.Z`.
2. **The manual-repair fallback is not for idempotency gaps** — every stage
   above is covered cheaply. It is for genuine *work* failures that recur (a
   merge conflict, a red CD): fix the underlying cause, then re-run `--resume`,
   which only has to get the broken stage across the line and proceed.
3. **`close-finalize` closes the GitHub issue as its final act** (after
   cleanup), because closing the issue ends resumability. `consumer-refresh`
   runs after it and is display-only, so nothing is lost.

## CLI surface and edges

- **`vrg-release`** (fresh): unchanged — creates the issue/checklist, aborts if
  an open `release: X.Y.Z` issue exists.
- **`vrg-release --resume`**: adopts the open release issue, reconstructs
  context (version from the issue title, `release_pr_url` from `pr list --head`,
  the rest derived), and enters at the first unchecked box. No `--continue`
  alias — one spelling.

**Which release does `--resume` adopt?** Look for open `release: X.Y.Z` issues:

- **Zero** → error: "no in-flight release to resume."
- **One** → adopt it.
- **Multiple** → anomaly (releases are serialized); refuse and require
  `vrg-release --resume X.Y.Z` to name it.

**Flag interactions:** `--resume` together with a `{minor,major}` bump argument
is an error — the version is locked by the issue. Other flags (`--no-promote`,
`--skip-audit`) apply to the resumed run normally.

## Context reconstruction

On resume, `ReleaseContext` is rebuilt before the pipeline runs:

- `version` ← parsed from the issue title (`release: X.Y.Z`).
- `issue_number` ← the adopted open issue.
- `release_pr_url` ← `gh pr list --head release/X.Y.Z`.
- `release branch` / `worktree` ← `preflight` adopt-or-create.
- tags and the back-merge PR ← derived/probed by the stages that need them.

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
- **Context reconstruction (unit):** issue title + `pr list` → version, issue
  number, `release_pr_url`.
- **CLI (unit):** `--resume` with zero / one / multiple open release issues;
  `--resume` + `{minor,major}` → error; fresh run with an existing issue → lock
  abort.
- **Resume-from-each-stage (integration, parametrized):** seed a checklist with
  stages `0..N-1` checked, run `--resume`, assert it enters at N, no-ops the
  checked ones, and runs N onward.
- **The two drift cases (adversarial):** box-checked-but-not-actually-done →
  the probe re-does it; box-unchecked-but-work-done (crash after work) → the
  probe skips it. These are the safety net, so they get explicit tests.
