# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in
this repository.

<!-- vergil:template:claude-md:begin -->
## Memory management

Memory is allowed with human approval. The authoritative policy is in
the user's global `~/.claude/CLAUDE.md` — agents must propose memory
writes and suggest a destination (repo memory, global CLAUDE.md, or
plugin/skill issue) before writing. See that file for the full
workflow.

Available skills:
- `/vergil:memory-init` — set up or update the policy header
  in a project's `MEMORY.md`.
- `/vergil:memory-audit` — structured collaborative review
  of memory files.

## Parallel AI agent development

This repository supports running multiple Claude Code agents in parallel via
git worktrees. The convention keeps parallel agents' working trees isolated
while preserving shared project memory (which Claude Code derives from the
session's starting CWD).

**Canonical spec:**
[`vergil-tooling/docs/specs/worktree-convention.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md)
— full rationale, trust model, failure modes, and memory-path implications.
The canonical text lives in `vergil-tooling`; this section is the local
on-ramp.

### Structure

```text
<project-root>/                              ← sessions ALWAYS start here
  .git/
  CLAUDE.md, …                               ← main worktree (usually `develop`)
  .worktrees/                                ← container for parallel worktrees
    issue-<N>-<short-slug>/                  ← worktree on feature/<N>-<short-slug>
    …
```

### Rules

1. **Sessions always start at the project root.**
   Never start Claude from inside `.worktrees/<name>/`. This keeps the
   memory-path slug stable and shared.
2. **Each parallel agent is assigned exactly one worktree.** The session
   prompt names the worktree (see Agent prompt contract below).
   - For Read / Edit / Write tools: use the worktree's absolute path.
   - For Bash commands that touch files: `cd` into the worktree first,
     or use absolute paths.
3. **The main worktree is read-only.** All edits flow through a worktree
   on a feature branch — the logical endpoint of the standing
   "no direct commits to develop" policy.
4. **One worktree per issue.** Don't stack in-flight issues. When a
   branch lands, remove the worktree before starting the next.
5. **Naming: `issue-<N>-<short-slug>`.** `<N>` is the GitHub issue
   number; `<short-slug>` is 2–4 kebab-case tokens.

### Agent prompt contract

When launching a parallel-agent session, use this template (fill in the
placeholders):

```text
You are working on issue #<N>: <issue title>.

Your worktree is: <project-root>/.worktrees/issue-<N>-<slug>/
Your branch is:   feature/<N>-<slug>

Rules for this session:
- Do all git operations from inside your worktree:
    cd <absolute-worktree-path> && vrg-git <command>
- For Read / Edit / Write tools, use the absolute worktree path.
- For Bash commands that touch files, cd into the worktree first
  or use absolute paths.
- Do not edit files at the project root. The main worktree is
  read-only — all changes flow through your worktree on your
  feature branch.
- When you need to run validation, run it from inside your worktree
  (vrg-container-run mounts the current directory).
```

All fields are required.

## Shell command policy

Use `vrg-git` instead of `git` for all git operations. Use `vrg-gh`
instead of `gh` for all GitHub CLI operations. These wrappers enforce
subcommand allowlists, flag deny lists, and credential selection.

Raw `git` and `gh` are denied by the permission model. If a command
is not available through the wrappers, explain the situation to the
human who can run it directly via `! <command>` in the prompt.

## Validation

```bash
vrg-container-run -- vrg-validate
```

This is the **only** validation command. Do not run individual linters,
formatters, or other tools outside of `vrg-validate`. If a tool is not
invoked by `vrg-validate`, it is not part of the validation pipeline.
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

**Agents must not run `vrg-submit-pr`.** PR submission, merge, and
finalization are human actions. The PR handoff is:

1. The agent records the PR metadata with `vrg-pr-workflow report-ready
   --issue <N> --title --summary --notes` (optional `--linkage`), which writes
   it to `.vergil/pr-workflow.json`. `title`, `summary`, and `notes` are
   required and non-empty. `linkage` defaults to `Ref`; leave it there.
   `vrg-submit-pr` auto-selects the keyword at submit time — a **managed task**
   (an issue with an `epic`-labeled parent) links with `Closes` so it
   auto-closes on merge, and its parent epic rolls up via the `on: issues.closed`
   Action; a legacy issue (no epic parent) keeps `Ref` and stays open for manual
   close. `Fixes`/`Resolves` remain banned so `Closes` is the one close keyword.
   This is safe because a task is exactly one PR: once it is in develop it is
   done, and any later change is a new follow-up issue, never a reopening (epic
   vergil-project/.github#75).
2. The human runs `vrg-submit-pr` with no arguments, which reads the
   state file, previews the PR, and submits after confirmation.
3. The human merges and runs post-merge cleanup (`vrg-finalize-pr`).

**Once you run `report-ready`, the branch is frozen.** `report-ready`
records the branch as the single, finished deliverable for its issue, so
until the human submits it must not change. Enforcement is at two
chokepoints — `vrg-commit` refuses a further commit and the `vrg-git`
push path refuses a further push — both printing an actionable refusal
(and a loud DRIFT warning if HEAD has already advanced past the reported
commit, the reused-branch straggler of epic #146 / issue #1719). The rule
follows directly from "a task is exactly one PR": more work is a **new
follow-up issue**, never a change to this branch. Two things stay allowed:

- **Correcting the PR prose** — re-running `report-ready` overwrites the
  recorded title/summary/notes. That is metadata, not code, so it is not
  frozen.
- **Deliberately reopening the branch** — `vrg-pr-workflow unfreeze` drops
  the workflow back to `implementing` (keeping the recorded metadata) so
  commits/pushes are allowed again. This is the *only* sanctioned way to
  lift the freeze; it is a distinct, explicit action precisely so
  reopening a branch is never a silent side effect. An **already-submitted**
  branch cannot be unfrozen — its PR exists, so further work is a new
  follow-up issue, full stop.

When the human finalizes, `vrg-finalize-pr` will not silently strand a
merged worktree it cannot remove (dirty tree, or a reused branch name with
unmerged commits): it surfaces every such worktree **prominently after the
pipeline** with the reason. For the common Mac case — a merged worktree
dirtied only by un-gitignored build/validation output — `--clean-dirty` is
an opt-in that clears exactly that after showing the untracked paths and
confirming; it never touches modified tracked files or a reused-branch
straggler's unmerged commits. The cleaner fix is to gitignore that output
in the first place, so the worktree is never dirtied and sweeps
automatically — consuming repos should keep validation/build artifacts out
of the tree.

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
commits, pushes the feature branch to origin, and runs `report-ready`,
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
a local `.venv-host`:

- **`.venv`** — Created inside dev containers. Shebang paths reference
  `/workspace/.venv/...` and do not work on the host.
- **`.venv-host`** — Dev-tree override venv for testing unreleased
  code on the host. Not the normal install mechanism.

```bash
# Dev-tree override (vergil-tooling development only)
UV_PROJECT_ENVIRONMENT=.venv-host uv sync --group dev
export PATH="$(pwd)/.venv-host/bin:$PATH"

```

After host tools are available, use `vrg-container-run` to run all
commands inside the dev container. See [Validation](#validation)
above.

### Two-Tier CI Model

Testing is split across two tiers with increasing scope and cost:

**Tier 1 — Local pre-commit (seconds):** The single entry point
`vrg-container-run -- vrg-validate` runs everything
(lint, typecheck, tests, audit, common checks) inside one dev
container. (Here that transparently expands to `uv run vrg-validate`
via the `[validation]` override in `vergil.toml` — see
[Validation](#validation).)

**Tier 2 — PR CI (~5-8 min):** Triggers on `pull_request`. Python 3.12,
all quality checks, security scanners (CodeQL, Trivy, Semgrep), standards
compliance, and release gates.
Workflow: `.github/workflows/ci.yml`.

Push-CI was retired once `vrg-validate` reached parity with PR-CI.
See `docs/site/docs/guides/ci-architecture.md` for the full rationale and
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
  registry.

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
  commits with co-author resolution
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
- **`vrg-release`** — Mechanized end-to-end release workflow (develop to main)
- **`vrg-resolve-tracking-issue`** — Extract tracking issue number from a merge commit's PR linkage
- **`vrg-finalize-pr`** — Merge a PR and run post-merge cleanup (branch/worktree deletion, remote pruning)
- **`vrg-validate`** — Unified validation driver (runs inside dev container)
- **`vrg-ensure-label`** — Idempotent GitHub label creation
- **`vrg-hook-guard`** — Claude Code PreToolUse hook guard (blocks raw git/gh)
- **`vrg-whoami`** — Canonical identity-mode and platform resolver
  (`--mode` for a scripting token, `--explain` to report the resolving
  signal and warn on signal disagreement, `--platform` for the empirical
  fail-closed `physical-host`/`local-vm`/`cloud-vm` token)
- **`vrg-container-run`** — Run arbitrary commands inside a dev container
- **`vrg-container-test`** — Run repo test suite inside a dev container

Shared libraries under `src/vergil_tooling/lib/`:

- **`git.py`** — Git subprocess wrappers
- **`github.py`** — gh CLI subprocess wrappers
- **`config.py`** — Parse `vergil.toml`
- **`release/`** — Mechanized release workflow (preflight, prepare, merge,
  bump, confirm, finalize, handoff, orchestrator)

### Docker Dev Images

Dev container images (Dockerfiles, build script, publish workflow) are
maintained in [vergil-containers](https://github.com/vergil-project/vergil-containers).

The `vrg-container-test` entry point auto-detects the project
language (Gemfile, pyproject.toml, go.mod, pom.xml/mvnw) and runs the test
suite inside the appropriate container. Consuming repos call it directly or wrap
it in a thin `scripts/dev/test.sh`. Environment overrides:

- `DOCKER_DEV_IMAGE` — override the container image
- `DOCKER_TEST_CMD` — override the test command
- `DOCKER_NETWORK` — join a Docker network (e.g., for integration tests)

Env-var passthrough is configured per-repo via `[container].env-prefixes`
in `vergil.toml` (see `docs/specs/2026-05-25-configurable-container-env-passthrough-design.md`).

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

`vergil-tooling` has two coordinated deployment targets (see
`docs/specs/host-level-tool.md` — historical spec, written under the
old `standard-tooling`/`st-*` naming):

| Target | Install mechanism | Who uses it |
|---|---|---|
| **Developer host** | `uv tool install` from git URL | Host-side commands: `vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-release`, `vrg-finalize-pr` |
| **Container runtime** (all languages) | `vrg-container-run` cache-first install per `vergil.toml` | `vrg-*` inside the container for all consumers |

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
