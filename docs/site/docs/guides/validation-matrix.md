# Validation Matrix

This page maps every validation check to the tool that runs it and
where it runs. No git hooks are installed: every check runs inside a
`vrg-*` tool, in the Claude Code hook guard, or in PR CI.

## Check Summary

| Check | Run by | Local | PR CI |
| ----- | ------ | ----- | ----- |
| Raw `git` / `gh` blocked | Claude Code hook guard (`vrg-hook-guard`) | Agent sessions | -- |
| Branch name format at creation | `vrg-git` | Yes | -- |
| Worktree placement and naming | `vrg-git worktree add` | Yes | -- |
| Branch switch in the main worktree | `vrg-git checkout` / `switch` | Yes | -- |
| Branch and commit context | `vrg-commit` | Yes | -- |
| No auto-close keyword in the commit body | `vrg-commit` | Yes | -- |
| PR body issue linkage | `vrg-submit-pr` | Yes | -- |
| Repository profile | `vrg-repo-profile` (common checks) | Yes | `quality / common` |
| Versions resolve against the window; toolchain selection; language floor | `vrg-validate` (config check, before any stage) | Yes | Every `vrg-validate` job |
| Markdown, shell, YAML, Dockerfile, workflow and Ansible lint | `vrg-validate` (common checks) | Yes | `quality / common` |
| Lint and typecheck | `vrg-validate` | Yes (primary version) | `quality / lint / <version>`, `quality / typecheck / <version>` |
| Tests | `vrg-validate` | Yes (primary version; every version with `--all-versions`, Python only) | `test / unit / <version>` |
| Dependency audit | `vrg-validate` | Yes (primary version) | `audit / dependencies / <version>` |
| Custom validator | `vrg-validate` (custom stage) | Yes (primary version) | `quality / custom` (once, primary version) |
| Security scans (CodeQL, Trivy, Semgrep) | `ci-security.yml` | -- | `security / *` |
| Version bump | `ci-version-bump.yml` | -- | `version / version-bump` |

"Local" for `vrg-validate` means `vrg-container-run -- vrg-validate`,
which runs one container on the resolved primary version. PR CI
runs the same commands through the `vergil-actions` reusable workflows
on every resolved version (see
[Version resolution](ci-architecture.md#version-resolution)). See
[CI Architecture](ci-architecture.md) for the jobs and the required
checks.

The config check runs on every `vrg-validate` invocation, whatever
`--check` selects. It fails when the repo's versions do not resolve
against the published window (a pin outside the window, an app pin of
more than one version, an unreadable window) or when a file selects the
toolchain behind Vergil's back (for Python, `.python-version`). A
declared floor that falls below the window's oldest version, or an
upper bound that excludes its newest (for Python, `requires-python`),
is printed as a warning and does not fail. See
[Python version management](../standards/development/python/version-management.md#toolchain-selection-in-the-repo).

## Agent and Git Wrappers

### Claude Code hook guard

**Trigger:** every `Bash` tool call in a Claude Code session, in a
repository that has a `vergil.toml`.

`.claude/hooks/guard.sh`, wired in `.claude/settings.json`, calls
`vrg-hook-guard`. It blocks raw `git` and `gh` commands and lets
`vrg-git`, `vrg-gh` and the other `vrg-*` tools through. See the
[Claude Code Hook Guard reference](../reference/hooks/pre-commit.md).

### vrg-git

**Trigger:** every `vrg-git` command.

- `vrg-git worktree add` must create the worktree as a direct child of
  `<project-root>/.worktrees/`. When `-b` creates a `feature/`,
  `bugfix/`, `hotfix/` or `chore/` branch, the branch must be
  `<type>/<N>-<slug>` and the directory must be `issue-<N>-<slug>`.
- `vrg-git checkout -b` and `vrg-git switch -c` apply the same branch
  name format.
- On all three creation paths, issue `<N>` must be open and must not be
  labelled `epic` or `archive`. A failed issue read refuses the branch.
- When `.worktrees/` exists, switching branches in the main worktree is
  blocked, except to a protected branch.

### vrg-commit

**Trigger:** every `vrg-commit`.

| Check | Error message | Fix |
| ----- | ------------- | --- |
| Detached HEAD | `detached HEAD is not allowed for commits` | Create a branch |
| Protected branch (`develop`, `release`, `main`) | `direct commits to protected branches are forbidden` | Create a feature branch |
| Prefix not allowed by `[project].branching-model` | `branch name must use ...` | Rename the branch |
| Missing issue number | `branch name must include a repo issue number` | Rename to `<type>/<N>-<slug>` |
| Work-branch commit from the main worktree when `.worktrees/` exists | `feature-branch commits from the main worktree are forbidden` | Commit from the worktree |
| Auto-close keyword in `--body` | `commit body contains a GitHub auto-close keyword` | Remove it; linkage goes on the PR |

`vrg-commit` also refuses a commit on a branch frozen by
`vrg-pr-workflow report-ready`, and a commit with nothing staged unless
`--allow-empty` is given. The [Git Workflow](git-workflow.md) guide
covers the branching models and the per-change cycle.

### vrg-submit-pr

**Trigger:** PR creation.

PR body issue linkage is checked when `vrg-submit-pr` builds the body.
`Ref #N` and `Closes #N` are accepted; `Fixes` and `Resolves` are
banned. There is no separate CI check for it.

## vrg-validate Checks

**Trigger:** `vrg-container-run -- vrg-validate` locally, and the
reusable CI workflows on every pull request.

### Common checks

`vrg-validate --check common` runs, in order:

1. `vrg-repo-profile`: `vergil.toml` is present and valid, and
   `README.md` follows the structural conventions. See the
   [vrg-repo-profile reference](../reference/lint/repo-profile.md).
2. markdownlint on `docs/site/**/*.md` and `README.md`, with the
   canonical config bundled in vergil-tooling, then on `epics/` with a
   config that relaxes line length. See the
   [Markdown Validation](../reference/lint/markdown-standards.md)
   reference.
3. shellcheck on shell scripts under `scripts/`.
4. yamllint on YAML under `.github/` and `docs/`, with the bundled
   config.
5. hadolint on `Dockerfile*` at the repository root.
6. actionlint on `.github/workflows/`.
7. ansible-lint, when the repository has Ansible content.

The first failing check stops the run. In CI the common checks run once,
as `quality / common`.

### Language checks

`vrg-validate` then runs the lint, typecheck, test and audit commands
for `[project].primary-language`. Pass `--check lint`, `typecheck`,
`test` or `audit` to run one stage.

### Custom validator

Last, `vrg-validate` runs the repository's custom validator, when it has
one: `scripts/bin/validate-custom` or a `vrg-validate-custom` on `PATH`.
`--check custom` runs only that stage, after the language's install
stage so the validator sees the dev dependencies. With no custom
validator it prints `custom: no custom validator (scripts/bin/validate-custom
or vrg-validate-custom) — nothing to run` and exits 0. A validator that
is present but not an executable regular file fails the stage.
`--check custom --all-versions` is a usage error.

PR CI runs the same stage once per run, as `quality / custom`, on the
primary-version image that `quality / common` uses. Its transcript is
`quality-custom.log` in the `ci-evidence-quality` artifact, and the
`quality / evidence` gate needs the job, so a failed or skipped custom
stage fails that gate. `quality / custom` is not a required check of its
own, but every repository requires `quality / evidence` (see
[CI gates](ci-architecture.md#ci-gates)), so a failing custom validator
blocks the merge whatever the repository's language. A repository's
custom validator therefore runs both locally and in PR CI.

### All versions (Python)

`vrg-validate --all-versions` adds one `test (python X.Y)` stage per
resolved CI version after the test stage, so the
`--cov-branch --cov-fail-under=100` gate runs on every interpreter PR-CI
tests, not only the dev container's. Combine it with `--check test` to
run only the tests. Each stage has `uv` install the version as a managed
interpreter and sync the `dev` group into its own environment under the
container's temp directory, so the project `.venv` is untouched. The
container interpreter's own version is skipped: the plain test stage
already covers it. Its coverage and JUnit reports go beside that
environment, so the workspace's `coverage.xml` and `junit.xml` stay the
container interpreter's.

It needs network access to download the interpreters, and it costs one
extra suite run per extra version: on a Lima VM a full
`vrg-container-run -- vrg-validate --all-versions` for this repo's four
versions takes about 1.5 minutes, against about 20 seconds without it.
The default `vrg-validate` is unchanged. Other languages are refused: their
toolchains are not uv-managed, and their PR-CI matrix stays CI-only.

## Exit Codes

Every tool above exits 0 on success and non-zero when a check fails.
`vrg-repo-profile` distinguishes the two failure cases:

| Code | Meaning |
| ---- | ------- |
| 0 | `vergil.toml` and `README.md` are valid |
| 1 | `vergil.toml` is invalid, or `README.md` breaks the structural conventions |
| 2 | `vergil.toml` not found |
