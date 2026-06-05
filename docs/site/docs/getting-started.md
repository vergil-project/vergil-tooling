# Getting Started

A five-to-ten minute quickstart for wiring up a new repository to
use vergil-tooling. For the detailed walkthrough with rationale,
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
most `vrg-*` tools — lives inside the dev container. The host-side
`vrg-*` tools (`vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, etc.)
are installed via `uv tool install`.

## 1. Install vergil-tooling on the host

```bash
uv tool install 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

This installs all `vrg-*` console scripts into `~/.local/bin/`,
which `uv`'s installer already puts on `PATH`.

```bash
which vrg-container-run    # should resolve to ~/.local/bin/vrg-container-run
vrg-container-run --help   # should print usage
```

## 2. Configure the Claude Code hook guard

Every managed repo ships a `.claude/hooks/guard.sh` shim that blocks
raw `git` and `gh` commands in agent sessions. The shim is wired via
`.claude/settings.json` and requires no per-clone configuration. See
[Consuming Repo Setup](guides/consuming-repo-setup.md) for the full
setup.

## 3. Enable the Claude Code plugin

Create `.claude/settings.json` in your repo:

```json
{
  "extraKnownMarketplaces": {
    "vergil-tooling-marketplace": {
      "source": {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin"
      }
    }
  },
  "enabledPlugins": {
    "vergil-tooling@vergil-tooling-marketplace": true
  }
}
```

Commit this file — it's part of the repo's reproducible setup.

!!! note "Plugin install is a known rough edge"
    The install/update flow for the plugin itself is tracked in
    [vergil-claude-plugin#46](https://github.com/vergil-project/vergil-claude-plugin/issues/46).
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
vrg-container-run --help

# Repo profile validates (runs inside the container)
vrg-container-run -- uv run vrg-repo-profile

# Hook guard shim is present and executable
ls -la .claude/hooks/guard.sh
```

If all three behave as expected, you're wired up correctly. The hook
guard fires on Claude Code `Bash` tool invocations — to test it end
to end, have Claude Code try a raw `git commit` in a session.

## Next steps

- **[Consuming Repo Setup](guides/consuming-repo-setup.md)** —
  detailed walkthrough including CI workflow, plugin nuances, and
  troubleshooting.
- **[Git Workflow](guides/git-workflow.md)** — branching, commit /
  PR / finalize cycle, two-layer enforcement, worktrees in practice.
- **[Worktree convention spec][worktree-spec]**
  — full rationale for the parallel-agent convention, failure
  modes, memory-path implications.

[worktree-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md
