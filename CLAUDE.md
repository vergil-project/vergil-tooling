# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in
this repository.

## Parallel AI agent development

This repository supports running multiple Claude Code agents in parallel via
git worktrees. The convention keeps parallel agents' working trees isolated
while preserving shared project memory (which Claude Code derives from the
session's starting CWD).

**Canonical spec:**
[`docs/specs/worktree-convention.md`](docs/specs/worktree-convention.md) —
full rationale, trust model, failure modes, and memory-path implications.

### Structure

```text
~/dev/github/standard-tooling/           ← sessions ALWAYS start here
  .git/
  CLAUDE.md, src/, docs/, …              ← main worktree (usually `develop`)
  .worktrees/                            ← container for parallel worktrees
    issue-258-worktree-convention/       ← worktree on feature/258-worktree-convention
    …
```

### Rules

1. **Sessions always start at the project root.**
   `cd ~/dev/github/standard-tooling && claude` — never from inside
   `.worktrees/<name>/`. This keeps the memory-path slug stable and shared.
2. **Each parallel agent is assigned exactly one worktree.** The session
   prompt names the worktree (see Agent prompt contract below).
   - For Read / Edit / Write tools: use the worktree's absolute path.
   - For Bash commands that touch files: `cd` into the worktree first,
     or use absolute paths.
3. **The main worktree is read-only.** All edits flow through a worktree
   on a feature branch — the logical endpoint of the standing
   "no direct commits to develop" policy. `st-commit` itself enforces
   this: when a `.worktrees/` directory is present, it refuses commits
   on `feature/**`, `bugfix/**`, `hotfix/**`, or `chore/**` branches
   that originate from the main worktree. The `.githooks/pre-commit`
   gate then refuses any raw `git commit` that bypasses `st-commit`.
4. **One worktree per issue.** Don't stack in-flight issues. When a
   branch lands, remove the worktree before starting the next.
5. **Naming: `issue-<N>-<short-slug>`.** `<N>` is the GitHub issue
   number; `<short-slug>` is 2–4 kebab-case tokens.

### Agent prompt contract

When launching a parallel-agent session, use this template (fill in the
placeholders):

```text
You are working on issue #<N>: <issue title>.

Your worktree is: /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-<N>-<slug>/
Your branch is:   feature/<N>-<slug>

Rules for this session:
- Do all git operations from inside your worktree:
    cd <absolute-worktree-path> && git <command>
- For Read / Edit / Write tools, use the absolute worktree path.
- For Bash commands that touch files, cd into the worktree first
  or use absolute paths.
- Do not edit files at the project root. The main worktree is
  read-only — all changes flow through your worktree on your
  feature branch.
- When you need to run validation, run it from inside your worktree
  (st-docker-run mounts the current directory).
```

All fields are required.

## Project Overview

This is a Python package providing shared development tooling for all managed
repositories: CLI tools for commits, PRs, releases, and validation; bash
validators and git hooks consumed via PATH from a sibling checkout (local) or
CI checkout (GitHub Actions).

**Project name**: standard-tooling

**Status**: Stable (v1.x)

**Standards reference**: <https://github.com/wphillipmoore/standards-and-conventions>
— historical reference; active standards documentation lives in this
repository under `docs/`.

## Memory management

Memory is allowed with human approval. The authoritative policy is in
the user's global `~/.claude/CLAUDE.md` — agents must propose memory
writes and suggest a destination (repo memory, global CLAUDE.md, or
plugin/skill issue) before writing. See that file for the full
workflow.

Available skills:
- `/standard-tooling:memory-init` — set up or update the policy header
  in a project's `MEMORY.md`.
- `/standard-tooling:memory-audit` — structured collaborative review
  of memory files.

## Development Commands

### Environment Setup

Host-side `st-*` tools are installed via `uv tool install` (see
[Consumption Model](#consumption-model)). For developing
standard-tooling itself, there is also a **dev-tree override** using
a local `.venv-host`:

- **`.venv`** — Created inside dev containers. Shebang paths reference
  `/workspace/.venv/...` and do not work on the host.
- **`.venv-host`** — Dev-tree override venv for testing unreleased
  code on the host. Not the normal install mechanism.

```bash
# Dev-tree override (standard-tooling development only)
UV_PROJECT_ENVIRONMENT=.venv-host uv sync --group dev
export PATH="$(pwd)/.venv-host/bin:$PATH"

# Enable the pre-commit gate (refuses raw `git commit`; admits st-commit)
git config core.hooksPath .githooks
```

After host tools are available, use `st-docker-run` to run all
commands inside the dev container. See [Validation](#validation)
below.

### Validation

```bash
st-docker-run -- uv run st-validate-local              # Runs all checks
```

This is the **only** validation command. Do not run individual linters,
formatters, or other tools outside of `st-validate-local`. If a tool is not
invoked by `st-validate-local`, it is not part of the validation pipeline.

### Two-Tier CI Model

Testing is split across two tiers with increasing scope and cost:

**Tier 1 — Local pre-commit (seconds):** The single entry point
`st-docker-run -- uv run st-validate-local` runs everything
(lint, typecheck, tests, audit, common checks) inside one dev
container. Enforced via the `.githooks` pre-commit gate on every commit.

**Tier 2 — PR CI (~5-8 min):** Triggers on `pull_request`. Python 3.12,
all quality checks, security scanners (CodeQL, Trivy, Semgrep), standards
compliance, and release gates.
Workflow: `.github/workflows/ci.yml`.

Push-CI was retired once `st-validate-local` reached parity with PR-CI.
See `docs/site/docs/guides/ci-architecture.md` for the full rationale and
wphillipmoore/standard-actions#176 for the parity audit.

### Docker-First Testing

Docker is the only host prerequisite. The validation stack uses
exactly one container per run:

- **Outer layer**: `st-docker-run` launches the dev container once
  and runs the validation driver inside.
- **Inner layer**: `scripts/dev/{lint,test,typecheck,audit}.sh`
  are tiny, container-local scripts. They assume they are already
  running inside the dev container and invoke tooling directly
  (`uv run ruff check`, `uv run pytest`, etc.). They do **not**
  re-containerize.

Dev container images are maintained in
[standard-tooling-docker](https://github.com/wphillipmoore/standard-tooling-docker).

```bash
# Build the dev image (one-time)
cd ../standard-tooling-docker && docker/build.sh

# Run the full validation pipeline in one container
st-docker-run -- uv run st-validate-local
```

If you need to tweak what validation runs for this repo, edit
`scripts/dev/*.sh` — those scripts are the per-repo customization
point. Keep them container-local (no `st-docker-run`, no
`st-docker-test`, no `DOCKER_*` env vars).

## Architecture

### Python Package (`src/standard_tooling/`)

CLI tools installed as `st-*` console scripts:

- **`st-commit`** — Construct standards-compliant conventional
  commits with co-author resolution
- **`st-submit-pr`** — Create standards-compliant PRs (manual merge)
- **`st-merge-when-green`** — Wait for a PR's checks, then merge it
  (release-workflow use only; normal PRs stay on the honor-system
  manual-merge policy)
- **`st-prepare-release`** — Automate release preparation (branch, changelog, PR)
- **`st-finalize-repo`** — Post-merge cleanup (branch deletion, remote pruning)
- **`st-validate-local`** — Driver for pre-PR local validation
- **`st-ensure-label`** — Idempotent GitHub label creation
- **`st-docker-run`** — Run arbitrary commands inside a dev container
- **`st-docker-test`** — Run repo test suite inside a dev container

Shared libraries under `src/standard_tooling/lib/`:

- **`git.py`** — Git subprocess wrappers
- **`github.py`** — gh CLI subprocess wrappers
- **`config.py`** — Parse `standard-tooling.toml`

### Docker Dev Images

Dev container images (Dockerfiles, build script, publish workflow) are
maintained in [standard-tooling-docker](https://github.com/wphillipmoore/standard-tooling-docker).

The `st-docker-test` entry point auto-detects the project
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
  with `ST_COMMIT_CONTEXT=1` (set by `st-commit`) and admits derived
  workflows (`amend`, `cherry-pick`, `revert`, `rebase*`, `merge*`).
  Rejects raw `git commit -m "..."`. The five branch / context checks
  (detached HEAD, protected branches, branch prefix, issue number,
  worktree convention) live in `st-commit` itself, not the hook.

### Consumption Model

`standard-tooling` has two coordinated deployment targets (see
`docs/specs/host-level-tool.md` for the full spec):

| Target | Install mechanism | Who uses it |
|---|---|---|
| **Developer host** | `uv tool install` from git URL | Host-side commands: `st-docker-run`, `st-commit`, `st-submit-pr`, `st-prepare-release`, `st-finalize-repo` |
| **Container runtime** (all languages) | `st-docker-run` cache-first install per `standard-tooling.toml` | `st-*` inside the container for all consumers |

**Host install** (canonical):

```bash
uv tool install 'standard-tooling @ git+https://github.com/wphillipmoore/standard-tooling@v1.4'
```

**Git hooks** (any consuming repo): each repo checks in its own
`.githooks/pre-commit` (an env-var gate that admits `st-commit` and
rejects raw `git commit`) and enables it once per clone:

```bash
git config core.hooksPath .githooks
```

**CI (GitHub Actions)**: All repos use the cache-first runtime path
via `st-docker-run`, which reads `standard-tooling.toml` for the
version tag and builds a per-branch cached image with
standard-tooling pre-installed.

### Key Constraints

- **Portability**: Scripts must work on both macOS and Linux
- **shellcheck clean**: All bash scripts must pass shellcheck
- **No repo-specific logic**: Scripts must work in any consuming repository
