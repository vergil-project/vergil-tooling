# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in
this repository.

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
  (vrg-docker-run mounts the current directory).
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
vrg-docker-run -- vrg-validate
```

This is the **only** validation command. Do not run individual linters,
formatters, or other tools outside of `vrg-validate`. If a tool is not
invoked by `vrg-validate`, it is not part of the validation pipeline.

> **Note:** This repository uses
> `vrg-docker-run -- uv run vrg-validate` because it runs its own
> unreleased code rather than the pre-installed version.

## Project Overview

This is a Python package providing shared development tooling for all managed
repositories: CLI tools for commits, PRs, releases, and validation; bash
validators and git hooks consumed via PATH from a sibling checkout (local) or
CI checkout (GitHub Actions).

**Project name**: vergil-tooling

**Status**: Stable (v1.x)

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

# Enable the pre-commit gate (refuses raw `git commit`; admits vrg-commit)
git config core.hooksPath .githooks
```

After host tools are available, use `vrg-docker-run` to run all
commands inside the dev container. See [Validation](#validation)
above.

### Two-Tier CI Model

Testing is split across two tiers with increasing scope and cost:

**Tier 1 — Local pre-commit (seconds):** The single entry point
`vrg-docker-run -- uv run vrg-validate` runs everything
(lint, typecheck, tests, audit, common checks) inside one dev
container. Enforced via the `.githooks` pre-commit gate on every commit.

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

- **Outer layer**: `vrg-docker-run` launches the dev container once
  and runs `vrg-validate` inside.
- **Inner layer**: `vrg-validate` reads `primary_language` from
  `vergil.toml` and runs common checks (markdownlint,
  shellcheck, yamllint, hadolint, actionlint), then language-specific
  checks (lint, typecheck, test, audit) from the built-in command
  registry.

Dev container images are maintained in
[vergil-docker](https://github.com/vergil-project/vergil-docker).

```bash
# Build the dev image (one-time)
cd ../vergil-docker && docker/build.sh

# Run the full validation pipeline in one container
vrg-docker-run -- uv run vrg-validate
```

## Architecture

### Python Package (`src/vergil_tooling/`)

CLI tools installed as `vrg-*` console scripts:

- **`vrg-commit`** — Construct standards-compliant conventional
  commits with co-author resolution
- **`vrg-submit-pr`** — Create standards-compliant PRs (manual merge)
- **`vrg-merge-when-green`** — Wait for a PR's checks, then merge it
  (release-workflow use only; normal PRs stay on the honor-system
  manual-merge policy)
- **`vrg-prepare-release`** — Automate release preparation (branch, changelog, PR)
- **`vrg-resolve-tracking-issue`** — Extract tracking issue number from a merge commit's PR linkage
- **`vrg-finalize-repo`** — Post-merge cleanup (branch deletion, remote pruning)
- **`vrg-validate`** — Unified validation driver (runs inside dev container)
- **`vrg-ensure-label`** — Idempotent GitHub label creation
- **`vrg-docker-run`** — Run arbitrary commands inside a dev container
- **`vrg-docker-test`** — Run repo test suite inside a dev container

Shared libraries under `src/vergil_tooling/lib/`:

- **`git.py`** — Git subprocess wrappers
- **`github.py`** — gh CLI subprocess wrappers
- **`config.py`** — Parse `vergil.toml`

### Docker Dev Images

Dev container images (Dockerfiles, build script, publish workflow) are
maintained in [vergil-docker](https://github.com/vergil-project/vergil-docker).

The `vrg-docker-test` entry point auto-detects the project
language (Gemfile, pyproject.toml, go.mod, pom.xml/mvnw) and runs the test
suite inside the appropriate container. Consuming repos call it directly or wrap
it in a thin `scripts/dev/test.sh`. Environment overrides:

- `DOCKER_DEV_IMAGE` — override the container image
- `DOCKER_TEST_CMD` — override the test command
- `DOCKER_NETWORK` — join a Docker network (e.g., for MQ integration tests)
- `MQ_*` env vars are automatically passed through to the container

### Git Hooks (`.githooks/`)

Consumed via `git config core.hooksPath .githooks`:

- `pre-commit` — Env-var-plus-`GIT_REFLOG_ACTION` gate. Admits commits
  with `VRG_COMMIT_CONTEXT=1` (set by `vrg-commit`) and admits derived
  workflows (`amend`, `cherry-pick`, `revert`, `rebase*`, `merge*`).
  Rejects raw `git commit -m "..."`. The five branch / context checks
  (detached HEAD, protected branches, branch prefix, issue number,
  worktree convention) live in `vrg-commit` itself, not the hook.

### Consumption Model

`vergil-tooling` has two coordinated deployment targets (see
`docs/specs/host-level-tool.md` for the full spec):

| Target | Install mechanism | Who uses it |
|---|---|---|
| **Developer host** | `uv tool install` from git URL | Host-side commands: `vrg-docker-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-prepare-release`, `vrg-finalize-repo` |
| **Container runtime** (all languages) | `vrg-docker-run` cache-first install per `vergil.toml` | `vrg-*` inside the container for all consumers |

**Host install** (canonical):

```bash
uv tool install --python 3.14 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v1.4'
```

**Git hooks** (any consuming repo): each repo checks in its own
`.githooks/pre-commit` (an env-var gate that admits `vrg-commit` and
rejects raw `git commit`) and enables it once per clone:

```bash
git config core.hooksPath .githooks
```

**CI (GitHub Actions)**: All repos use the cache-first runtime path
via `vrg-docker-run`, which reads `vergil.toml` for the
version tag and builds a per-branch cached image with
vergil-tooling pre-installed.

### Key Constraints

- **Portability**: Scripts must work on both macOS and Linux
- **shellcheck clean**: All bash scripts must pass shellcheck
- **No repo-specific logic**: Scripts must work in any consuming repository
