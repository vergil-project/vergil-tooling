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


## Validation in this repo

This repo validates its own unreleased code: the `[validation]` override in
`vergil.toml` (`container-command = "uv run vrg-validate"`) expands
`vrg-container-run -- vrg-validate` to `uv run vrg-validate` in the container.

## Identity

Agent sessions run as `user` or `audit`. Query the resolved role with
`vrg-whoami`; never infer identity from `VRG_IDENTITY_MODE` alone. It is only
the first of five fallback signals, and unset means "fall through", not "human".
Flags: [`vrg-whoami`](docs/site/docs/reference/cli-tools-overview.md#vrg-whoami).

## PR mechanics

The handoff and freeze rules are in the template region above. The full
mechanics are in [Git Workflow](docs/site/docs/guides/git-workflow.md) and the
[`vrg-pr-workflow` reference](docs/site/docs/reference/cli-tools-overview.md#vrg-pr-workflow).

## Project overview

vergil-tooling is the Python package behind the `vrg-*` CLI tools that every
managed repository uses for commits, PRs, releases, validation and agent VMs.

**Status**: Stable (v2.x)

## Development

### Environment setup

Host-side `vrg-*` tools come from `uv tool install` (see
[Deployment targets](docs/site/docs/guides/releasing.md#deployment-targets)).
To test unreleased code on the host, use the dev-tree override:

```bash
uv sync --group dev
export PATH="$(pwd)/.venv/bin:$PATH"
```

The dev container masks the host `.venv` with an anonymous volume (#2486), so
one `.venv` is safe. Run everything else through `vrg-container-run`.

### Coverage on every Python version

Local validation tests one interpreter, but PR CI gates 100% branch coverage on
every version in the window, and coverage can differ between versions. Run
`vrg-container-run -- vrg-validate --all-versions` before pushing to catch that
(see [CI Architecture](docs/site/docs/guides/ci-architecture.md)).

### Correcting a commit message

Interactive rebase is blocked. Reword a branch-local commit with
[`vrg-reword`](docs/site/docs/reference/cli-tools-overview.md#vrg-reword),
only before report-ready (a reported-ready branch is frozen).

## Reference

- [CLI Tools Overview](docs/site/docs/reference/cli-tools-overview.md): every
  `vrg-*` command (`[project.scripts]` in `pyproject.toml` is the full list).
- [Package Architecture](docs/site/docs/reference/package-architecture.md):
  the `src/vergil_tooling/` layout and shared libraries.
- [CI Architecture](docs/site/docs/guides/ci-architecture.md) and the
  [Validation Matrix](docs/site/docs/guides/validation-matrix.md).
- [Releasing](docs/site/docs/guides/releasing.md): the release workflow and
  the deployment targets.
- [Claude config ownership](docs/site/docs/guides/claude-config-ownership.md):
  what belongs in this file.

## Key Constraints

- **Portability**: Scripts must work on both macOS and Linux
- **shellcheck clean**: All bash scripts must pass shellcheck
- **No repo-specific logic**: Scripts must work in any consuming repository
- **Release before consuming**: consumers pin a vergil-tooling release, so
  merge to `main` and release before updating a consuming repo to use a change
