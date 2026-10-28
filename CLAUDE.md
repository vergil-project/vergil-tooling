# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in
this repository.

<!-- vergil:template:claude-md:begin -->
## Memory

Never write, edit or delete memory files without explicit human approval.
Propose the entry and where it belongs first, then wait for the decision.

## Worktrees

- Sessions always start at the project root, never inside `.worktrees/`.
- One worktree per issue: `.worktrees/issue-<N>-<slug>/` on branch
  `feature/<N>-<slug>`. Remove it once its branch lands.
- The main worktree is read-only; every edit goes through a worktree.
- Read, Edit and Write use the worktree's absolute path. Bash commands `cd`
  into the worktree first or use absolute paths.
- The full convention: <https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md>.
- Brief each parallel sub-agent with its [Agent prompt contract](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md#agent-prompt-contract).

## Git and GitHub

Use `vrg-git` and `vrg-gh` only; the hook guard blocks raw `git` and `gh`. If a
command is not available through the wrappers, explain why and ask the human to
run it with `! <command>` in the prompt.

## Validation

`vrg-container-run -- vrg-validate` is the only validation command. Don't run
linters, formatters or tests outside it; a tool it doesn't invoke is not part
of the pipeline.

## PR handoff

- Agents never run `vrg-submit-pr`. Submitting, merging and finalizing a PR are
  human actions.
- Hand off with `vrg-pr-workflow report-ready --issue <N> --title --summary
  --notes`. It pushes the branch itself. Leave `--linkage` at its default (`Ref`).
- A delta touching `.github/workflows/**` makes GitHub refuse the agent push.
  That is expected: report-ready records the state anyway and the human's
  `vrg-submit-pr` pushes the branch.
- After report-ready the branch is frozen: no new commits and no push that
  advances it; more work is a new follow-up issue. Re-running report-ready to
  fix the prose is allowed. `vrg-pr-workflow unfreeze` is the only way to lift it.
- The report-ready title, summary and notes are public (the relay ref is
  world-readable): never put secrets in them.

## Local settings

`.claude/settings.local.json` must stay absent or exactly `{}`; the agent tools
refuse otherwise. See the [ownership model](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/site/docs/guides/claude-config-ownership.md).

## Engineering principles

- Correctness over cost over speed. Never ship known-broken code with a
  follow-up fix; if something is wrong, fix it now.
- No silent failures: no swallowed exceptions, no `except: return ""`, no
  fallbacks that hide errors. A failure hidden in code becomes a hallucination
  at the AI layer.

## paad

paad usage policy and deviations:
<https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/paad-policy.md>.
<!-- vergil:template:claude-md:end -->

> **Note:** The command above works as-is here, even though this repo
> dogfoods its own unreleased code (the cached dev image deliberately
> skips `uv tool install`, so `vrg-validate` is not on `PATH` and must be
> run via `uv run`). This repo declares a `[validation]` override in
> `vergil.toml` (`container-command = "uv run vrg-validate"`), and
> `vrg-container-run` reads it from the target repo at execution time, so
> `vrg-container-run -- vrg-validate` is transparently expanded to
> `vrg-container-run -- uv run vrg-validate`. The override lives in
> `vergil.toml` (not just here) so cross-repo agents pick it up regardless
> of which `CLAUDE.md` their session loaded (issues #1430, #1433).

## Identity modes and PR submission

Identity-aware tools (`vrg-git`, `vrg-gh`, `vrg-submit-pr`) read
`VRG_IDENTITY_MODE` (`human`, `user`, or `audit`; see
`src/vergil_tooling/lib/identity_mode.py`). Agent sessions run as
`user` or `audit`.

To query the resolved role, use `vrg-whoami` — never infer identity
from `VRG_IDENTITY_MODE` alone. That env var is only the first of five
fallback steps (env var → mode file → `app.pem` → `VRG_APP_ID` →
human); an unset value means "fall through," not "default to human."
`vrg-whoami --mode` emits a single token for scripting, and
`vrg-whoami --explain` reports the resolving signal and warns when
signals disagree. `vrg-whoami --platform` resolves a second, orthogonal
axis — `physical-host` / `local-vm` / `cloud-vm` — from empirical signals
(fail-closed: an unconfirmed VM resolves to `cloud-vm`, never
`physical-host`); `--explain` also cross-checks the platform against the
identity and warns when the two disagree.

The PR handoff and the post-report-ready freeze are in the template region
above. The full mechanics (linkage auto-selection, push outcomes, the
explicit push at the recorded `head_sha`, `unfreeze` limits, and
`vrg-finalize-pr --clean-dirty`) are in
[Git Workflow](docs/site/docs/guides/git-workflow.md) and the
[`vrg-pr-workflow` reference](docs/site/docs/reference/cli-tools-overview.md#vrg-pr-workflow).

### Cloud session prompt contract (off-platform VMs)

The PR handoff above no longer needs a shared filesystem. `report-ready`
**always** mirrors the recorded ready-state onto a reserved git ref,
`refs/vergil/pr-workflow/<branch>` (the *relay ref*), in addition to the
local `.vergil/pr-workflow.json`. The push is unconditional — no config
key, no off-platform detection — so a cloud x86 VM's `report-ready` is
visible to the Mac even though the two never share a disk. The write is a
pure ref update built out-of-band with git plumbing; it never advances the
feature branch, so it stays freeze-neutral (the post-`report-ready` freeze
still holds).

Because the metadata now rides GitHub, **a cloud VM can do PR-development
end-to-end** — not just triage. A cloud agent implements the issue,
commits, and runs `report-ready` (which pushes the feature branch to origin),
exactly as it would under Lima. The old "cloud x86 VMs are triage-only /
not for PR-development" boundary is retired, along with the "until the
relay lands" framing — the relay
([#1858](https://github.com/vergil-project/vergil-tooling/issues/1858),
Deliverable B) shipped.

**Only submission and merge stay human-on-Mac.** From the Mac's main
worktree, the human runs `vrg-submit-pr` **worktree-free** with an explicit
branch list:

```text
vrg-submit-pr <branch> [<branch> …]
```

Each branch's ready-state is resolved from a local worktree's
`pr-workflow.json` when one exists, else fetched from the relay ref; the
tip of `origin/<branch>` is verified against the recorded `head_sha`, and
the PR is opened **without pushing** (the branch already rode GitHub, so
`--head` just names it). Merge and cleanup stay human actions:
`vrg-finalize-pr` deletes the branch's relay ref alongside the branch on
cleanup, and a swept safety net prunes any relay ref whose branch no longer
exists, so a cloud-handoff ref never outlives its work.

**The relay ref is world-readable on a public repo.** Anyone can read
`refs/vergil/pr-workflow/<branch>`, so the `report-ready` `--title`,
`--summary`, and `--notes` must carry **no secrets** — treat them as public
the moment they are recorded.

This does not loosen the "agents must not run `vrg-submit-pr`" policy: the
cloud agent stops at `report-ready`, exactly like a Lima agent, and the
human submits and merges on the Mac. What changed is only *where the
development can happen* — the relay removed the shared-disk requirement, so
a cloud VM is now a full PR-development environment, not a triage-only one.

## Project Overview

This is a Python package providing shared development tooling for all managed
repositories: CLI tools for commits, PRs, releases, and validation; bash
validators and git hooks consumed via PATH from a sibling checkout (local) or
CI checkout (GitHub Actions).

**Project name**: vergil-tooling

**Status**: Stable (v2.x)

**Standards reference**: <https://github.com/wphillipmoore/standards-and-conventions>
— historical reference; active standards documentation lives in this
repository under `docs/`.

## Development Commands

### Environment Setup

Host-side `vrg-*` tools are installed via `uv tool install` (see
[Consumption Model](#consumption-model)). For developing
vergil-tooling itself, there is also a **dev-tree override** using
a local `.venv` for testing unreleased code on the host:

```bash
# Dev-tree override (vergil-tooling development only)
uv sync --group dev
export PATH="$(pwd)/.venv/bin:$PATH"

```

A single `.venv` is safe because the dev container never touches the
host `.venv` — it is masked by an anonymous volume (#2486).

After host tools are available, use `vrg-container-run` to run all
commands inside the dev container. See [Validation](#validation)
above.

### Two-Tier CI Model

Testing is split across two tiers with increasing scope and cost:

**Tier 1 — Local pre-commit (seconds):** The single entry point
`vrg-container-run -- vrg-validate` runs everything
(common checks, lint, typecheck, tests, audit, and the repo's custom
validator when it has one) inside one dev container. (Here that
transparently expands to `uv run vrg-validate` via the `[validation]` override in `vergil.toml` — see
[Validation](#validation).)

**Tier 2 — PR CI (~5-8 min):** Triggers on `pull_request`. Runs every
quality check across the full Python version matrix (currently 3.12, 3.13,
3.14, 3.15), security scanners (CodeQL, Trivy, Semgrep), and release gates. Workflow: `.github/workflows/ci.yml`. `ci.yml` is a **thin
caller** of the `vergil-actions` reusable workflows, passing only
`language:`/`container-suffix:`; the matrix is resolved at run time by
`vrg-ci-resolve` from the published version window. This repo sets
`repository-type = "library"` and has no `[ci].versions` pin, so it tests
the whole window (epic vergil-project/.github#407). Branch protection
requires the stable, version-agnostic `audit / evidence`,
`quality / evidence`, and `test / evidence` gates rather than per-version
checks — so a matrix change needs no edit to `ci.yml` or the ruleset (epic
vergil-project/.github#338).

Push-CI was retired once `vrg-validate` matched the checks push-CI ran.
Every `vrg-validate` stage now has a PR-CI job, including the custom stage
(`vrg-validate --check custom`, run once as `quality / custom` and folded
into `quality / evidence`, which every repo requires; epic
vergil-project/.github#488).
Note one deliberate gap in the "parity with PR-CI" framing: local
`vrg-container-run -- vrg-validate` runs a single dev container on one
Python interpreter (currently 3.15), so its `--cov-fail-under=100` gate
proves 100% coverage on that interpreter only. PR-CI re-runs the
test-and-coverage gate independently in a separate container per
resolved matrix version (currently 3.12, 3.13, 3.14, 3.15). Because branch
coverage (`--cov-branch`) can legitimately differ across CPython versions, code
at 100% locally can still fall below 100% on a 3.12, 3.13 or 3.14 leg and fail
CI. Run `vrg-container-run -- vrg-validate --all-versions` (Python only) to
catch that before pushing: it re-runs the gate on every resolved version, each
on a uv-managed interpreter in the same container (about 1.5 minutes here,
against about 20 seconds). See
`docs/site/docs/guides/ci-architecture.md` for the full rationale and
vergil-project/vergil-actions#176 for the parity audit.

### Docker-First Testing

Docker is the only host prerequisite. The validation stack uses
exactly one container per run:

- **Outer layer**: `vrg-container-run` launches the dev container once
  and runs `vrg-validate` inside.
- **Inner layer**: `vrg-validate` reads `primary_language` from
  `vergil.toml` and runs common checks (markdownlint,
  shellcheck, yamllint, hadolint, actionlint), then language-specific
  checks (lint, typecheck, test, audit) from the built-in command
  registry, and last the repo's custom validator
  (`scripts/bin/validate-custom` or `vrg-validate-custom`) when present.

Dev container images are maintained in
[vergil-containers](https://github.com/vergil-project/vergil-containers).

```bash
# Build the dev image (one-time)
cd ../vergil-containers && docker/build.sh

# Run the full validation pipeline in one container
# (the [validation] override in vergil.toml expands this to `uv run vrg-validate`)
vrg-container-run -- vrg-validate
```

## Architecture

### Python Package (`src/vergil_tooling/`)

CLI tools installed as `vrg-*` console scripts:

- **`vrg-commit`** — Construct standards-compliant conventional
  commits (no AI co-author trailer; Claude Code's attribution is turned
  off in `.claude/settings.json`, #3200)
- **`vrg-reword`** — Reword a branch-local commit's message via a scripted,
  non-interactive rebase; the agent-safe path to correct a branch-local
  commit message (raw interactive rebase is blocked repo-wide). Bounded:
  refuses shared/merged history and protected branches, refuses a foreign
  author without `--allow-foreign-author`, and pushes the rewrite with
  `--force-with-lease`
- **`vrg-submit-pr`** — Create standards-compliant PRs (manual merge;
  human-run — agents hand off via `.vergil/pr-workflow.json`)
- **`vrg-pr-fix-body`** — Regenerate a PR body from corrected fields via
  the validated builder; the agent-safe path to fix body-level standards
  failures on its own PR during pr-watch (pushes an empty commit to
  re-trigger CI)
- **`vrg-release`** — Mechanized end-to-end release workflow (develop to main),
  ending with the `update-deps` stage (`--without-deps` skips it); a fresh
  start refuses while a `dependency-update` PR is open
  (`--allow-pending-deps` bypasses, logged on the tracking issue)
- **`vrg-update-deps`** — Mechanized dependency update within declared bounds:
  built-in updaters (`python`, `vergil`, `images`) plus the
  `[dependency-update].hook`, one validation, a `dependency-update`-labelled
  PR merged when green, a fix task on any failure (validation, merge, or
  an updater/hook failing before any PR exists), and the
  held-back report (`--only`/`--skip`/`--vergil X.Y`). Human-only
- **`vrg-upstream-refresh`** — Run the repo's `[upstream-refresh].hook`
  (separate from `[dependency-update]`) and open, refresh or close the one
  `upstream-refresh` PR; never merges. Run by the scheduled
  `ops-upstream-refresh` workflow under the App token, or by a human;
  refuses agent identities
- **`vrg-resolve-tracking-issue`** — Extract tracking issue number from a merge commit's PR linkage
- **`vrg-finalize-pr`** — Merge a PR and run post-merge cleanup (branch/worktree deletion, remote pruning)
- **`vrg-branch-sweep`** — Report (dry run) or delete (`--apply`) a repo's
  finished remote branches: newest PR merged or closed, none open, tip
  unchanged. The safety net behind the delete-on-merge repo setting and the
  delete-on-close rule (#3332); never touches protected, reserved or no-PR
  branches
- **`vrg-intake-create`** — Capture an uncurated item into the intake queue,
  linked under its queue home's `Epic (intake): <name>` as it is created
  (`<org>/.github` for a public repo; a private repo self-homes). No kind
  label; `vrg-triage-create` is a one-release deprecated alias that ignores
  `--kind`
- **`vrg-intake-route`** — Route one intake item (`drop`, `defer`, `one-off`,
  `planning`, `split`); owns transfer-or-recreate, reparent, re-link and
  close-with-pointer for the `intake-review` skill; a `security` item's
  advisories are retargeted on transfer, recreate and split (a split of an
  item naming several advisories is refused)
- **`vrg-security-record`** — Record a security finding as a private draft
  advisory in `<org>/.github` paired with a neutral `security` intake issue
  (`create` with attach and resume, `list`, `show`, `retarget`); advisory text
  is printed only by `show` and `list --details` (epic vergil-project/.github#517)
- **`vrg-queue`** — Read-only view of the `intake` queue (oldest first) or the
  `planning` queue (waiting / in-progress / ready-to-close; a planning item
  still tracked by a security advisory is held at in-progress)
- **`vrg-epic-audit`** — Epic/task drift and invariant report (read-only);
  `--close` (human, or the scheduled `ops-epic-sweep` with `VRG_EPIC_SWEEP=1`)
  closes drift, drains standing-epic archives, then runs the orphan pass that
  attaches every unparented issue to its home's intake epic (epic
  vergil-project/.github#176) and the security-records pass that syncs each
  paired advisory's state to its issues and reports broken pairs (epic
  vergil-project/.github#517)
- **`vrg-validate`** — Unified validation driver (runs inside dev container)
- **`vrg-ensure-label`** — Idempotent GitHub label creation
- **`vrg-hook-guard`** — Claude Code PreToolUse hook guard (blocks raw git/gh)
- **`vrg-whoami`** — Canonical identity-mode and platform resolver
  (`--mode` for a scripting token, `--explain` to report the resolving
  signal and warn on signal disagreement, `--platform` for the empirical
  fail-closed `physical-host`/`local-vm`/`cloud-vm` token)
- **`vrg-container-run`** — Run arbitrary commands inside a dev container
- **`vrg-container-test`** — Run repo test suite inside a dev container
- **`vrg-package`** — Build, install-test and index binary OS packages
  (`.deb`/`.rpm`) from `[package]` in `vergil.toml`: `matrix`, `build`,
  `check-unpublished` (refuse re-publishing a released package version),
  `check-runtime` (refuse a release whose packages mix runtimes),
  `install-test`, and `index` (the signed apt/dnf site)
- **`vrg-sarif-filter`** — Copy a SARIF file without its
  accepted-suppressed results, for upload to GitHub code scanning
- **`vrg-vm`** — Create, start, update, rebuild and enter Lima/cloud agent
  VMs (`vrg-vm update` reinstalls vergil-tooling in a running VM)
- **`vrg-host`** — Observe and manage the macOS host dependencies declared
  in the packaged, never repo-overridable manifest
  `src/vergil_tooling/data/host_dependencies.toml`: `status` (default;
  `--fix` runs check remediations), `upgrade <dep>… | --all [--force]`,
  `pin`/`unpin` (epic #155 doctrine; pins live in the user's
  `~/.config/vergil/host-pins.toml`). Runs unprivileged — never escalates
  privilege (`sudo` etc. are refused)

This list highlights the main tools; `[project.scripts]` in
`pyproject.toml` is the complete set of entry points.

Shared libraries under `src/vergil_tooling/lib/`:

- **`git.py`** — Git subprocess wrappers
- **`github.py`** — gh CLI subprocess wrappers
- **`config.py`** — Parse `vergil.toml`
- **`epics.py`** — Epic/task model: the `StandingKind` table (ad-hoc, intake,
  planning and deferred standing epics and their per-quarter archives), epic
  and queue homes, sub-issue linking, rollup, and the parent-based queue-item
  check that `report-ready` / `vrg-submit-pr` use to refuse intake, planning
  and deferred items; `epic_audit.py` (drift report and orphan pass), `intake_route.py`,
  and `queue_view.py` build on it
- **`release/`** — Mechanized release workflow (preflight, prepare, merge,
  bump, confirm, finalize, handoff, orchestrator, package-index wait, and
  the `update-deps` stage); preflight holds the dependency-update gate
- **`update_deps/`** — `vrg-update-deps` pipeline (preflight, orchestrator,
  PR and fix task, failure output, held-back report, registry and package
  index lookups) and its updaters under `updaters/`
- **`package/`** — Binary package builds (matrix, python and staged
  builders, nFPM, install-test, repository bootstrap, `index/` for the
  apt/dnf site)
- **`host_*.py`** — `vrg-host` manifest loading (`host_deps`), install-method
  handlers (`host_methods`), host pins (`host_pins`), classification and
  rendering (`host_status`), and version ordering (`host_versions`)

### Docker Dev Images

Dev container images (Dockerfiles, build script, publish workflow) are
maintained in [vergil-containers](https://github.com/vergil-project/vergil-containers).

The `vrg-container-test` entry point takes the project language from
`[project].primary-language` in `vergil.toml` (falling back to package-manager
markers such as pyproject.toml, go.mod, Cargo.toml or pom.xml/mvnw) and runs
the test suite inside the appropriate container. Consuming repos call it
directly. Environment overrides:

- `DOCKER_DEV_IMAGE` — override the container image
- `DOCKER_TEST_CMD` — override the test command
- `DOCKER_NETWORK` — join a Docker network (e.g., for integration tests)

Env-var passthrough is configured per-repo via `[container].env-prefixes`
in `vergil.toml` (see `docs/specs/2026-05-25-configurable-container-env-passthrough-design.md`).
The sibling `[container]` keys — `system-packages` (apt names) and
`build-command`/`build-cache-files` (a non-apt provisioning step) — are documented
in `docs/site/docs/reference/container-config.md`.

### Claude Code Hook Guard

Raw `git` and `gh` commands are blocked by a Claude Code `PreToolUse`
hook. The enforcement has two layers:

- **Per-repo hook wiring** (`.claude/settings.json`) — calls
  `.claude/hooks/guard.sh`, a thin shell shim that execs
  `vrg-hook-guard` when vergil-tooling is installed, or falls back
  to a `jq`-based hard deny for git/gh commands.
- **Per-developer plugin** (`vergil-claude-plugin`) — provides the
  same guard via the plugin's hook system.

Both layers call `vrg-hook-guard`, which uses regex matching to
detect raw `git`/`gh` invocations while allowing `vrg-git`/`vrg-gh`
wrappers through. Only active in repos with a `vergil.toml`.

### Consumption Model

`vergil-tooling` has three coordinated deployment targets (see
`docs/specs/host-level-tool.md` — historical spec, written under the
old `standard-tooling`/`st-*` naming):

| Target | Install mechanism | Who uses it |
|---|---|---|
| **Developer host** (macOS) | `uv tool install` from git URL | Host-side commands: `vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-release`, `vrg-finalize-pr`, `vrg-host` |
| **Container runtime** (all languages) | `vrg-container-run` cache-first install per `vergil.toml` | `vrg-*` inside the container for all consumers |
| **Agent VM** (Lima/cloud) | Signed `vergil-tooling` package via `apt` from <https://vergil-project.github.io/packages>, pinned to the identity's release line or version | Agent sessions inside the VM |

On an agent VM, a dev ref (`vrg-vm update --tag develop`, or any non-release
ref) is an explicit `uv tool install` from git that shadows the packaged copy,
and every `vrg-vm` command that touches the VM prints a `DEV tooling` banner
while it is in place. A plain `vrg-vm update` returns the VM to the package.
See `docs/site/docs/reference/vm-spec.md`.

**Host install** (canonical):

```bash
uv tool install --python 3.14 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

**Claude Code hook** (any consuming repo): each repo ships a thin
shell shim at `.claude/hooks/guard.sh` that calls `vrg-hook-guard`
to block raw `git`/`gh` commands in agent sessions.

**CI (GitHub Actions)**: All repos use the cache-first runtime path
via `vrg-container-run`, which reads `vergil.toml` for the
version tag and builds a per-branch cached image with
vergil-tooling pre-installed.

### Key Constraints

- **Portability**: Scripts must work on both macOS and Linux
- **shellcheck clean**: All bash scripts must pass shellcheck
- **No repo-specific logic**: Scripts must work in any consuming repository
