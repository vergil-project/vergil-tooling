# Getting Started

A five-to-ten minute quickstart for wiring up a new repository to
use standard-tooling. For the detailed walkthrough with rationale,
CI config, and troubleshooting, see
[Consuming Repo Setup](guides/consuming-repo-setup.md).

## Prerequisites

Install these on your host:

- **Docker** — the dev container engine
- **uv** — Python package manager
  ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **`gh` CLI** — GitHub CLI, authenticated
  (`gh auth login`)
- **macOS or Linux** (Bash)

Everything else — language runtimes, linters, test frameworks, and
most `st-*` tools — lives inside the dev container. The host-side
`st-*` tools (`st-docker-run`, `st-commit`, `st-submit-pr`, etc.)
are installed via `uv tool install`.

## 1. Install standard-tooling on the host

```bash
uv tool install 'standard-tooling @ git+https://github.com/wphillipmoore/standard-tooling@v1.4'
```

This installs all `st-*` console scripts into `~/.local/bin/`,
which `uv`'s installer already puts on `PATH`.

```bash
which st-docker-run    # should resolve to ~/.local/bin/st-docker-run
st-docker-run --help   # should print usage
```

## 2. Configure git hooks in your consuming repo

Every managed repo checks in a `.githooks/pre-commit` env-var gate
(see [Consuming Repo Setup](guides/consuming-repo-setup.md) for
the gate content). Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

## 3. Enable the Claude Code plugin

Create `.claude/settings.json` in your repo:

```json
{
  "extraKnownMarketplaces": {
    "standard-tooling-marketplace": {
      "source": {
        "source": "github",
        "repo": "wphillipmoore/standard-tooling-plugin"
      }
    }
  },
  "enabledPlugins": {
    "standard-tooling@standard-tooling-marketplace": true
  }
}
```

Commit this file — it's part of the repo's reproducible setup.

!!! note "Plugin install is a known rough edge"
    The install/update flow for the plugin itself is tracked in
    [standard-tooling-plugin#46](https://github.com/wphillipmoore/standard-tooling-plugin/issues/46).
    For now, this settings.json entry is enough for Claude Code to
    discover and enable the plugin on the next session restart.

## 4. Create your repository profile

Create `docs/repository-standards.md` with the six required
attributes (and AI co-author entries if you'll use them):

```markdown
# Repository Standards

## Table of Contents

- [AI co-authors](#ai-co-authors)
- [Repository profile](#repository-profile)

## AI co-authors

- Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## Repository profile

- repository_type: library
- versioning_scheme: semver
- branching_model: library-release
- release_model: tagged-release
- supported_release_lines: 1
- primary_language: python
```

Pick the values that match your repo. See
[Consuming Repo Setup](guides/consuming-repo-setup.md) for the full
attribute reference.

## 5. Adopt the worktree convention

Add `.worktrees/` to your `.gitignore`:

```bash
echo '.worktrees/' >> .gitignore
```

Add a "Parallel AI agent development" section to your `CLAUDE.md`
describing the convention. Every managed repo already has one you
can copy from; the canonical source is
[the worktree convention spec][worktree-spec].

## 6. Verify

```bash
# Host tooling reachable
st-docker-run --help

# Repo profile validates (runs inside the container)
st-docker-run -- uv run st-repo-profile

# Git hook blocks raw git commit
git commit --allow-empty -m "test"    # should be blocked by the gate
```

If all three behave as expected, you're wired up correctly.

## Next steps

- **[Consuming Repo Setup](guides/consuming-repo-setup.md)** —
  detailed walkthrough including CI workflow, plugin nuances, and
  troubleshooting.
- **[Git Workflow](guides/git-workflow.md)** — branching, commit /
  PR / finalize cycle, two-layer enforcement, worktrees in practice.
- **[Worktree convention spec][worktree-spec]**
  — full rationale for the parallel-agent convention, failure
  modes, memory-path implications.

[worktree-spec]: https://github.com/wphillipmoore/standard-tooling/blob/develop/docs/specs/worktree-convention.md
