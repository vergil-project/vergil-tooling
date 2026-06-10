# Mechanized dependency update — `vrg-update-deps`

**Issue:** #1379
**Date:** 2026-06-10

## Table of Contents

- [Problem](#problem)
- [Goals and non-goals](#goals-and-non-goals)
- [Overview](#overview)
- [Invocation and evolution](#invocation-and-evolution)
- [Architecture](#architecture)
  - [Generic driver](#generic-driver)
  - [Updater interface](#updater-interface)
  - [Updater registry and applicability](#updater-registry-and-applicability)
- [The updaters](#the-updaters)
  - [Language libraries](#language-libraries)
  - [Third-party GitHub Actions to SHA](#third-party-github-actions-to-sha)
  - [Vergil ecosystem dependency](#vergil-ecosystem-dependency)
  - [Repo-specific extensions](#repo-specific-extensions)
  - [Warn-only dependencies](#warn-only-dependencies)
- [Configuration: the `[dependency-update]` section](#configuration-the-dependency-update-section)
- [Run semantics: no-op, idempotency, abort](#run-semantics-no-op-idempotency-abort)
- [Error handling](#error-handling)
- [Validation and merge gates](#validation-and-merge-gates)
- [Relationship to `vrg-release` and the core version file](#relationship-to-vrg-release-and-the-core-version-file)
- [Identity and human-only invocation](#identity-and-human-only-invocation)
- [Testing](#testing)
- [Open decision points](#open-decision-points)
- [Work breakdown](#work-breakdown)

## Problem

After a `vrg-release` ships, the first action of the next development cycle is
to catch up to the latest acceptable dependencies on `develop`, run a full
integration test, and only then begin new work. This was previously a
high-level **`dependency-update` plugin skill** (retired 2026-06-04) that an AI
agent followed by hand: branch off `develop`, update each category at its
source of truth, regenerate derived artifacts, run validation, and submit a PR.

The skill left every step to agent judgment. Per the Vergil 2.1 design
principle "anything fully deterministic leaves the AI entirely" (#1376), this
work mechanizes the whole flow into a deterministic, human-run CLI so it no
longer consumes agent provision and produces the correct result every run.

The dependency surface is wider than language libraries. Across the five vergil
repos alone, versions are specified in: `pyproject.toml`/`uv.lock` (and the
equivalents for the other four supported languages), `.github/workflows/*.yml`
`uses:` pins, Docker base-image `ARG`s and release-download tool pins, the VM
provisioning template, and the cross-repo vergil ecosystem pin in
`vergil.toml`. Each has a different source-of-truth format and a different
upgrade primitive.

## Goals and non-goals

**Goals**

- One deterministic, human-run command that updates a single repo's
  dependencies post-release and drives the entire PR lifecycle end-to-end
  (branch → update → validate → create → merge → finalize), modeled on
  `vrg-release`.
- An extensible **updater registry**: generic upgrade logic plus
  language-specific and repo-specific updaters, so new dependency mechanisms
  are added without reworking the driver.
- Mechanize the previously-manual **vergil ecosystem upgrade** (bump the
  ecosystem version this repo depends on and propagate it everywhere).
- Fail loud and legible: a broken upgrade aborts with enough detail for a
  human and agent to take over.
- Treat a **no-op run as a first-class outcome**: when nothing needs updating,
  do nothing and create no PR.

**Non-goals**

- **No dependency-anchor mechanism.** The anchor-record workflow
  (`docs/dependencies/*.md` + tracking issues, "fix-or-anchor" judgment) was an
  early, never-fully-implemented concept. Anchors and exceptions are two
  unresolved holes in the workflow; both are deferred. This tool carries
  neither. The lightweight, machine-readable pin/override metadata in
  [`[dependency-update]`](#configuration-the-dependency-update-section) is the
  starting replacement, with a schema derived iteratively.
- **No bisection or per-package isolation.** When an upgrade breaks
  validation, the tool aborts; it does not attempt to find the culprit package.
- **No AI in the tool.** Diagnosing and repairing a broken upgrade is the
  human-plus-agent job that begins after the tool aborts.
- **Not a cross-repo sweeper.** The tool runs in and updates exactly one repo.
- **No automatic cleanup of abandoned branches** on abort (deferred — see
  [Run semantics](#run-semantics-no-op-idempotency-abort)).

## Overview

`vrg-update-deps` is a human-run sibling of `vrg-release`. Because the human
runs it, the tool has human identity and access, so — like `vrg-release`
packaging up submit + merge + finalize — it mechanizes the **entire**
dependency-update PR lifecycle rather than handing off to `vrg-submit-pr`.

It is invoked from the repo root on a clean `develop` that is in sync with
`origin/develop`. Like every other automated workflow in this repo, it does
its work in a **managed git worktree** rather than switching branches in the
root checkout — so the root's `develop` is never left in-flight and a parallel
agent can safely branch off it while a sweep runs. Preflight creates the
worktree (off `develop`) and `chdir`s into it; all updates, commits, and
validation happen there; finalize removes it.

It detects which updaters apply, runs each as its own commit on the worktree's
`chore/dep-update-<date>` branch, runs `vrg-validate` once, and on success
drives the PR through to merge and finalize (removing the worktree). If no
updater produced a change, it removes the worktree and exits without pushing a
branch or opening a PR. On failure it aborts with the worktree, diff, and
captured validation output left in place for the human and agent.

## Invocation and evolution

The standalone command and any future `vrg-release` integration drive the
**same stage pipeline**. The integration is intentionally staged so the human
gains confidence before it becomes automatic:

1. **Now — standalone, human-decided.** Running `vrg-update-deps` is a
   deliberate human workflow decision. The **most common** moment is right
   after `vrg-release`, before serious work on the next cycle begins — but the
   tool is **not locked to that**. It can run at any point in a development
   cycle on a clean, synced `develop` (for example, when triaging a bug surfaces
   a dependency that needs bumping mid-cycle). There is **no "release boundary"
   precondition** — the only gate is a clean `develop` in sync with
   `origin/develop`. During rapid release bursts the human may simply choose not
   to run it yet.
2. **Next — opt-in `vrg-release` stage.** A `vrg-release --with-deps` flag runs
   this pipeline at the appropriate point in the release flow. It does **not**
   default to on.
3. **Eventually — default-on.** Flip the default so every release runs the
   sweep, with `--without-deps` as the skip. This is the most bulletproof form:
   the dependency update becomes part of the release's own validated CI path.

This spec delivers stage 1 and builds the pipeline so stages 2 and 3 are wiring
changes, not redesigns.

## Architecture

### Generic driver

A declarative stage pipeline mirroring `lib/release/orchestrator.py`
(`build_stages()` returning `Stage(name, fn, mode, skip_flag)` run through the
shared `lib/progress` framework):

```text
preflight → [applicable updaters…] → validate → prepare-pr → merge → finalize
```

- **preflight** — confirm invoked from the repo root, on `develop`, with a
  clean tree that is in sync with `origin/develop`; resolve `vergil.toml`;
  acquire/verify GitHub auth; create a **managed worktree** off `develop` on
  the `chore/dep-update-<date>` branch and `chdir` into it. Preconditions fail
  loud with a clear message (see
  [Run semantics](#run-semantics-no-op-idempotency-abort)).
- **updaters** — each applicable updater runs in registry order, inside the
  worktree, and commits its own category (e.g. `chore(deps): uv lock --upgrade`,
  `chore(deps): pin third-party actions to SHA`). Per-category commits cost
  nothing and make a failed run legible.
- **validate** — a single `vrg-container-run -- vrg-validate`, run inside the
  worktree (`vrg-container-run` mounts the cwd). All-or-nothing.
- **prepare-pr / merge / finalize** — reuse the release machinery
  (`pr_template`, `pr_body`, `pr_merge`, `pr_await`, `finalize`). Finalize
  removes the worktree. Phase 1 opens the PR with a `Ref` to the implementation
  issue; a per-run (or standing) deps tracking issue — which a managed worktree
  does not require — is a deferred refinement. Where the release pipeline can
  defer failures (`fail_defer`) to let the run complete as far as possible,
  this pipeline does the same.

The worktree is created and removed through a small **managed-worktree helper**
shared with the rest of the automated tooling — the deliberate replacement for
switching branches in the root checkout, which leaves the base branch in-flight
and can collide with a parallel agent. `vrg-update-deps` is the first adopter;
`vrg-release` is retrofitted onto the same helper as a tracked follow-up
([#1578](https://github.com/vergil-project/vergil-tooling/issues/1578)).

### Updater interface

Every updater is a small, independently testable module exposing a uniform
contract:

- `applies(ctx) -> bool` — does this repo have my surface? (manifest present,
  workflows present, repo opted in via config).
- `apply(ctx) -> UpdateResult` — perform the upgrade at the source of truth,
  regenerate derived artifacts, and report what changed (files touched,
  whether anything actually changed, a human-readable summary line for the
  commit and PR body, any warnings).

Updaters never run validation, never commit, and never touch git history —
those belong to the driver. An updater either succeeds with a result
(possibly an empty/no-change result) or raises with a clear message.

### Updater registry and applicability

The driver holds an ordered registry of built-in updaters. Applicability is
determined by:

1. **Detection** — manifest/lockfile presence (`pyproject.toml`, `go.mod`,
   `Gemfile`, `pom.xml`, `package.json`), `.github/workflows/*.yml` presence,
   `vergil.toml` `primary_language`.
2. **Config opt-in** — the `[dependency-update]` section of `vergil.toml`
   enables repo-specific extensions and declares warn-only entries, so the core
   tool stays generic and a downstream repo can opt into behavior without core
   changes.

## The updaters

### Language libraries

One updater per supported language, each a thin wrapper over the ecosystem's
native upgrade primitive, run inside the dev container:

| Language | Source of truth | Primitive |
| --- | --- | --- |
| Python | `pyproject.toml` + `uv.lock` | `uv lock --upgrade` |
| Ruby | `Gemfile` + `Gemfile.lock` | `bundle update` |
| Go | `go.mod` + `go.sum` | `go get -u ./... && go mod tidy` |
| Java | `pom.xml` | `mvn versions:use-latest-releases` |
| JavaScript | `package.json` + lockfile | `npm update` / `bun update` |

The native primitive is the wheel; the updater's job is to select it, run it in
the right container, regenerate any derived artifacts (e.g. exported
requirements), and report whether anything changed.

These primitives are **not uniform** in how they treat "latest" (respect
declared constraints vs. cross major boundaries), so each language updater is
specified and tested independently rather than assumed interchangeable. Only
Python is exercisable in the vergil repos; the other four are developed and
tested against the real-world MQRest-admin family (see
[Testing](#testing)). The language updater honors override metadata from the
`[dependency-update]` config (e.g. excluding a named dependency from upgrade);
that schema starts minimal and is derived iteratively.

### Third-party GitHub Actions to SHA

Industry best practice pins third-party GitHub Actions to a full commit SHA.
All vergil action references are currently **tag-pinned** (`@v6`); this updater
brings third-party references into compliance:

1. Parse every `uses: owner/repo@ref` in `.github/workflows/*.yml`.
2. Classify owner as **vergil-internal** or **third-party** (see next section
   for internal handling).
3. For each third-party reference, resolve the tag to the latest release's
   commit SHA and rewrite to `owner/repo@<sha> # vX.Y.Z` (SHA pinned, tag in a
   trailing comment for legibility).

This updater is the **most uncertain** part of the design: third-party actions
do not all follow one packaging or release convention, so the resolution logic
will be figured out iteratively against real references. Known cases the
implementation must handle, drawn from the current catalog, include:

- **Subdir actions** — `github/codeql-action/upload-sarif@v4`,
  `vergil-actions/actions/shared/security/trivy@v2.1` (path below the repo
  root; the SHA still belongs to the repo).
- **Actions referenced by branch** rather than tag.
- **Actions with no GitHub Release** (only git tags) — resolution must fall
  back to tags.
- **Authentication and rate limits** on the resolution API calls.

Because `vrg-validate` does not exercise GitHub workflows, action changes are
validated by the PR's own CI at the merge gate (see
[Validation and merge gates](#validation-and-merge-gates)).

### Vergil ecosystem dependency

The version of the vergil ecosystem a repo depends on is **a single logical
dependency whose source of truth is the `[dependencies].vergil` field in
`vergil.toml`** (today `vergil = "v2.1"`). Every other place that names the
ecosystem — `uses: vergil-*/...@vX.Y`, the setup-vergil action's tooling pin,
any git pins — is a *secondary reference that must match the source of truth.*
By policy these internal references are pinned to **major.minor** (`vX.Y`),
deliberately **not** SHA-pinned: patch releases flow to consumers
transparently, and a consumer who wants patch-level pinning takes on their own
upgrade responsibility. This is the sanctioned exception to the SHA-pinning best
practice and applies only to vergil-internal references.

This updater has two operations:

- **Normalize (routine, every run).** Force every secondary reference to equal
  the `[dependencies].vergil` value. This *is* the internal consistency check,
  and it is what catches and fixes drift such as the observed `vergil-vm`
  `ci-docs.yml@v2.0` pin while the repo declares `v2.1`. Normalize does **not**
  change the source-of-truth value.
- **Bump (deliberate, flag-gated, e.g. `--vergil 2.2`).** Raise the
  `[dependencies].vergil` value, then run normalize to propagate it to every
  secondary reference. This mechanizes the previously-manual, error-prone
  "upgrade vergil" procedure.

The set of secondary-reference locations is a single central list owned by this
updater. It begins with the known targets (`[dependencies].vergil`, workflow
`uses:` refs, the setup-vergil action pin) and is extended as we discover
additional places the ecosystem version is named — so "anything else an upgrade
must touch" is captured here over time rather than living in tribal knowledge.

### Repo-specific extensions

Some dependency surfaces are specific to one repo and require a module that
knows where each version string lives. These are built-in but gated by config
opt-in:

- **Docker (`vergil-docker`).** Base-image `ARG` defaults
  (`PYTHON_VERSION`, `GO_VERSION`, …), the language-version matrix in
  `docker/build.sh`, and release-download tool pins (trivy, shellcheck,
  shfmt, hadolint, actionlint, git-cliff, scorecard, markdownlint-cli, uv).
- **VM (`vergil-vm`).** Provisioning versions in `templates/agent.yaml`
  (Node.js, yq, gh, Lima minimum, `@anthropic-ai/claude-code`).

New extensions (for example, a future MQ-cluster repo) register through the
same seam.

### Warn-only dependencies

Some dependencies cannot be auto-pulled — e.g. a Red Hat VM build ISO/DVD that
is a manual download. For these the tool emits a **warning** in the run summary
and PR body and **never edits**. Where a machine-readable source exists, the
warning includes whether a newer version is available; for genuinely manual
sources with no API, the warning may only **remind** the human to check. These
are declared in the `[dependency-update]` config.

## Configuration: the `[dependency-update]` section

A new optional section in `vergil.toml`, distinct from the existing
`[dependencies]` section (which declares *what* the repo depends on). This
section configures *how the update tool behaves* for this repo, and is the
single home for all per-repo, checked-in (not per-developer) control:

- **Extension opt-ins** — enable the Docker or VM extension, or a future
  custom extension.
- **Warn-only entries** — declare manual-download dependencies and, where
  possible, how to check for a newer version.
- **Override / pin metadata** — the machine-readable replacement for the
  dropped anchor-doc workflow: tell the tool to skip or constrain a named
  dependency for this repo. **The full schema is explicitly TBD** and will be
  derived as real pin cases appear. The first implementation ships a minimal
  schema — at least "exclude this dependency from upgrade" — and grows from
  there. This is per-repo and version-aware ("override X for this version"),
  not a developer-local setting.

## Run semantics: no-op, idempotency, abort

This tool is **not** a fixed-point operation, and that is by design:

- **Non-deterministic by nature.** Like `uv lock --upgrade`, running it now vs.
  ten minutes later can pick up a freshly released patch. Different results
  across runs are correct, not a bug. The tool makes no assumption that a prior
  run "settled" the state.
- **No-op is first-class.** A run frequently finds nothing to change —
  especially during rapid release bursts where, say, no Python dependency moved
  between two runs. When **no updater produced a change**, the tool removes its
  worktree and exits cleanly — no PR, no tracking issue, no pushed branch. The
  root checkout's `develop` is exactly as it was.
- **Abort leaves the worktree.** On a red-validation abort the worktree, its
  `chore/dep-update-<date>` branch, and its per-category commits are left in
  place for the human and agent; the root checkout stays clean on `develop`. A
  re-run simply starts a fresh worktree (preflight fails loud if the prior
  branch still exists, prompting cleanup). Automatic reclamation of abandoned
  worktrees is a known wart but **out of scope here** — revisited when we
  decide how to handle it broadly.
- **Partial-failure tolerance.** Mirroring `vrg-release`, stages that can defer
  failure do so, letting the run complete as far as safely possible.
  Externally-induced breakage (e.g. GitHub failing mid-PR) is accepted as a
  manual-intervention case, not something the tool tries to repair.

## Error handling

All-or-nothing on validation. The tool either completes cleanly or aborts with
clear, detailed error output:

- If the final `vrg-validate` goes red, **abort.** Leave the
  `chore/dep-update-<date>` branch, its per-category commits, the diff, and the
  captured validation output in place. No bisection, no anchor, no auto-revert.
- An updater that cannot complete (e.g. a registry resolution failure) raises
  with a clear message and aborts the run before validation.
- The tool exposes enough detail — which updater, which command, captured
  stdout/stderr — that the cause is obvious without re-running.

## Validation and merge gates

Two kinds of change are validated by two different gates, both all-or-nothing:

- **Local validation** — `vrg-validate` covers language libraries, Docker tool
  versions, and anything exercised by lint/typecheck/test/audit in the dev
  container. Runs once, after all updaters, before the PR is created.
- **PR CI (merge gate)** — `vrg-validate` does not run GitHub workflows, so
  third-party action SHA bumps are validated by the PR's own CI. The merge
  stage reuses the release `wait_and_merge` path (waiting for required checks
  to reach successful terminal conclusions) against `develop`. If PR CI fails,
  the merge does not happen and the tool stops with the PR open for inspection.

**Audit gate.** The Vergil 2.1 `vergil-audit/approved` handshake is currently
experimental and **not required by default** (the agent default is being
flipped to not request audit unless asked). `vrg-update-deps` therefore merges
via the normal human path and does not depend on the audit cycle. If the audit
check ever becomes a mandatory gate on `develop`, the merge stage is the spot
to revisit — either routing these PRs through the audit handshake or using the
configured human bypass actor deliberately.

## Relationship to `vrg-release` and the core version file

`vrg-update-deps` is structurally a sibling of `vrg-release` and reuses its
pipeline, PR, merge, and finalize libraries. Its **most common** use is right
after a release, as the opening act of the next development cycle (the
standards rule that the dependency refresh is the first action after the PATCH
bump) — but it is runnable at any point on a clean, synced `develop`, not just
at a release boundary. The [invocation evolution](#invocation-and-evolution)
describes how the two commands relate over time (standalone → opt-in stage →
default stage). Both tools will share the managed-worktree mechanism; the
`vrg-release` retrofit is tracked in
[#1578](https://github.com/vergil-project/vergil-tooling/issues/1578).

The application's **own** version — managed by the release bump machinery via
the repository's version file — is conceptually distinct from the
**ecosystem-dependency** version (`[dependencies].vergil`) this tool normalizes
and bumps, even though in vergil's own repos the two numbers can look similar.
This spec draws a hard line: the vergil-ecosystem updater touches the
`[dependencies].vergil` pin and its secondary references, and must **not**
collide with the release version-bump file. Whether the core version file is
ever in this tool's scope, or stays purely the concern of `vrg-release`, is a
marked decision point ([Open decision points](#open-decision-points)) — it is
not silently merged here.

## Identity and human-only invocation

PR submission, merge, and finalization are human actions in the Vergil identity
model. `vrg-update-deps` drives all of them end-to-end and therefore runs under
human identity only, exactly like `vrg-release` and `vrg-submit-pr`. Agents are
blocked from invoking it. The tool resolves identity via `vrg-whoami` rather
than inferring from environment variables.

## Testing

- **Per-updater unit tests.** Given a manifest or workflow fixture, assert the
  command the updater issues or the exact text it rewrites, with native
  primitives and registry lookups mocked. Each updater is testable in
  isolation, including its no-change path.
- **Driver tests.** Run the stage pipeline with stubbed updaters to verify
  ordering, per-category commits, single-validation, abort-on-red, the no-op
  (discard-branch-no-PR) path, and the merge/finalize handoff — mirroring the
  existing `lib/release` test layout.
- **Real-world cross-language integration testbed.** Python is the only
  language exercisable in the vergil repos. The **MQRest-admin family** provides
  real repositories in every supported language — roughly eight instances (five
  language repos plus `.github`, the dev-environment repo, and the common
  repo). These currently sit on **vergil 2.0**, so they double as the first
  real exercise of the [vergil ecosystem upgrade](#vergil-ecosystem-dependency)
  (2.0 → 2.1), replacing the by-hand upgrade that was previously a
  chicken-and-egg chore. We develop the non-Python language updaters and the
  vergil-upgrade mechanism against these repos.

## Open decision points

1. **Third-party action resolution target** — latest release including major
   bumps (most aggressive "stay current") vs. latest within the current major.
   Starting position: latest release, SHA-pinned with a tag comment, with the
   PR CI gate as the safety net. Expected to iterate per action, alongside the
   subdir/branch/no-release edge cases above.
2. **Override / pin schema** — the `[dependency-update]` override metadata
   syntax is genuinely unknown until real pin cases appear. Ship minimal, grow
   iteratively.
3. **Core version file boundary** — confirm whether the application version
   file ever falls in this tool's scope or remains solely `vrg-release`'s
   concern.
4. **Abandoned-branch cleanup** — deferred. Decide later how (and whether) the
   tool reclaims branches left by aborted runs, ideally as part of a broader
   branch-hygiene effort.

## Work breakdown

Natural build/test order (each slice delivers independent value; the order is
driven by what is testable where):

1. Driver + stage pipeline + `[dependency-update]` config parsing + the updater
   interface and registry, including the no-op and abort semantics.
2. Python/uv language updater + PR-lifecycle wiring (prepare/merge/finalize)
   reusing release libraries — the first shippable, locally-testable slice.
3. Vergil ecosystem updater (normalize, then flag-gated bump), proven by
   upgrading the MQRest-admin family from 2.0 to 2.1.
4. The remaining four language updaters (Ruby, Go, Java, JavaScript), developed
   against the MQRest-admin language repos.
5. Third-party GitHub Actions SHA updater (iterative, per the edge cases above).
6. Repo-specific extensions (Docker, VM) and warn-only support.
7. Human-only identity gating throughout.
8. Later: `vrg-release --with-deps` integration (opt-in, then default).
