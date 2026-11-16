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
| Repository profile | `vrg-validate` (repo-profile, common checks) | Yes | `quality / common` |
| Versions resolve against the window; toolchain selection; language floor | `vrg-validate` (config check, before any stage) | Yes | Every `vrg-validate` job |
| Markdown, shell, YAML, Dockerfile, workflow and Ansible lint | `vrg-validate` (common checks) | Yes | `quality / common` |
| Lint and typecheck | `vrg-validate` | Yes (primary version) | `quality / lint / <version>`, `quality / typecheck / <version>` |
| Tests | `vrg-validate` | Yes (primary version; every version with `--all-versions`, Python only) | `test / unit / <version>` |
| Dependency audit | `vrg-validate` | Yes (primary version) | `audit / dependencies / <version>` |
| Custom validator | `vrg-validate` (custom stage) | Yes (primary version) | `quality / custom` (once, primary version) |
| Semgrep scan | `vrg-validate` (semgrep stage) | Yes | `security / semgrep` |
| Trivy scan | `vrg-validate` (trivy stage) | Yes | `security / trivy` |
| CodeQL scan | `ci-security.yml` | -- (CI-only) | `security / codeql` |
| GitHub code scanning results | GitHub, from the uploaded SARIF | -- (CI-only) | `Trivy`, `Semgrep OSS`, `CodeQL` |
| Strict docs build | `vrg-validate` (docs stage) | Yes | `docs / docs` |
| Version bump | `ci-version-bump.yml` | -- (CI-only) | `version / version-bump` |
| Package build and install tests | `ci-package.yml` | -- (CI-only) | `package / evidence` |
| Image build and scan (vergil-containers) | vergil-containers CI | -- (CI-only) | `images / evidence` |

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
toolchain behind Vergil's back (for Python, `.python-version`; for
Rust, `rust-toolchain.toml` or the legacy `rust-toolchain`). A
declared floor that falls below the window's oldest version, or an
upper bound that excludes its newest (for Python, `requires-python`;
for Go, the `go.mod` `go` directive; for Rust, the root `Cargo.toml`'s
`rust-version`), is printed as a warning and does not fail. See
[Python version management](../standards/development/python/version-management.md#toolchain-selection-in-the-repo)
and the Rust standards'
[Toolchain Selection](../standards/development/rust/overview.md#toolchain-selection).

## PR CI Gates Not Run Locally

Each required PR CI check (see [CI gates](ci-architecture.md#ci-gates))
was compared with what `vrg-container-run -- vrg-validate` runs, in
vergil-tooling#4003. Every gap has a decision: it runs locally, it is
planned to run locally in a follow-up, or it stays CI-only for the
reason given. A gap that stays CI-only is a failure an agent cannot see
before the PR is opened.

Already run locally: `quality / common`, `quality / custom`,
`security / semgrep` (the [Semgrep scan](#semgrep-scan) stage),
`security / trivy` (the [Trivy scan](#trivy-scan) stage, #4445),
`docs / docs` (the [Docs build](#docs-build) stage, #4446), and
lint, typecheck, tests and audit on the primary version. The
repo-specific gates run locally too: vergil-containers'
`quality / hadolint` (vergil-containers#761) and vergil-actions' action
shell suites (vergil-actions#1108).

| PR CI check | Decision | Reason |
| ----------- | -------- | ------ |
| `security / codeql` | CI-only | It needs GitHub code scanning, its CLI is not in the dev images, and building its database is heavy. |
| `Trivy`, `Semgrep OSS`, `CodeQL` | CI-only | GitHub computes them from the uploaded SARIF for the changed lines, so they cannot run locally. The local semgrep and trivy stages cover the findings behind them. |
| `version / version-bump` | CI-only | It compares the branch's version with `main`'s, so its result depends on where the release cycle stands, not on the change. |
| `package / evidence` | CI-only | It builds the `.deb` and `.rpm` packages and install-tests them across the distro matrix, which needs nested containers and a long run. |
| `images / evidence` | CI-only | It builds and scans the changed images on each architecture's own runner. |
| Non-primary versions of lint, typecheck, tests and audit | CI-only | Locally only the primary version runs, plus every version's tests with `--all-versions` (Python only). Lint and typecheck rarely differ between versions. |

Two follow-ups came out of the same review and change no gate's
decision. The semgrep stage repeats the `p/c` and `p/typescript`
rulesets that `ci-security.yml` adds; #4447 makes `vrg-semgrep-scan`
the one source of that mapping. The CI gates require
`test / integration / <version>` when `[ci].integration-tests` is on,
but no reusable workflow runs an integration job yet, so such a repo
could not merge. The job stays deferred (#2721); since #4448
`vrg-github-repo-init` refuses the setting before it pushes anything,
and the CI-gates guard names it as the cause. No fleet repo turns it on
today.

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

Docker is the only host prerequisite, and a local run uses exactly one
container. The outer layer, `vrg-container-run`, launches the dev
container once and runs `vrg-validate` inside it. The inner layer,
`vrg-validate`, reads `[project].primary-language` from `vergil.toml` and
runs the common checks, then the language's checks from the built-in
command registry, then the semgrep scan, then the Trivy scan, then
the strict docs build,
then the repository's custom validator, as below. The
dev images come from
[vergil-containers](https://github.com/vergil-project/vergil-containers)
(see [Dev container images](ci-architecture.md#dev-container-images)).

### Common checks

`vrg-validate --check common` runs, in order:

1. repo-profile: `vergil.toml` is present and valid, and
   `README.md` follows the structural conventions. See the
   [repo-profile reference](../reference/lint/repo-profile.md).
2. markdownlint on `docs/site/**/*.md`, `README.md` and `epics/`, in one
   pass with the canonical config bundled in vergil-tooling. See the
   [Markdown Validation](../reference/lint/markdown-standards.md)
   reference.
3. shellcheck on shell scripts under `scripts/` and on every `*.sh`
   file elsewhere in the repository.
4. yamllint on YAML under `.github/` and `docs/`, with the bundled
   config.
5. hadolint on `Dockerfile*` at the repository root.
6. actionlint on `.github/workflows/`.
7. runner-labels: no value in a `.github/workflows/*.yml` or `*.yaml`
   file (a `runs-on`, a matrix value, a `with:` input) may be
   `ubuntu-latest`. Pin GitHub-hosted runners to `ubuntu-24.04`; the
   check names each hit by path and line.
8. ansible-lint, when the repository has Ansible content.

The first failing check stops the run. In CI the common checks run once,
as `quality / common`.

### Language checks

`vrg-validate` then runs the lint, typecheck, test and audit commands
for `[project].primary-language`. Pass `--check lint`, `typecheck`,
`test` or `audit` to run one stage.

A repository with none of its language's manifests is not bootstrapped
yet, such as a Rust repository that `vrg-github-repo-init` created with
no `Cargo.toml`. The manifests are the ones the dev image's warmup needs:

| Language | Manifests |
|---|---|
| Python | `pyproject.toml`, `uv.lock` |
| Rust | `Cargo.toml` |
| Go | `go.mod` |
| C++ | `conanfile.txt` or `conanfile.py`, `CMakeLists.txt`, `conan.lock` |
| TypeScript | `package.json`, `package-lock.json` |

For such a repository the install, lint, typecheck, test and audit
stages do not run. Each is recorded as a warning (⚠) whose reason names
the missing manifests, for example `skipped: no Cargo.toml (rust repo
not bootstrapped yet)`, on its row and in the run summary. The common
checks and the custom validator still run. A warning counts as passed,
so the run can reach merge review. The skip applies only when every
manifest is missing: a repository with some of them, such as a
`pyproject.toml` with no `uv.lock`, runs the stages and fails on the
gap, as does a `Cargo.toml` that `cargo fetch` cannot resolve.

The skip also needs the repository to hold no source files of its
language (`.rs` for Rust, `.py` for Python, and so on), tracked or
untracked, ignored files left out. A repository with sources but none of
its manifests, such as a branch that deletes `Cargo.toml` from an
existing crate or adds Rust sources without one, has lost its manifest
rather than never had one: its install stage fails, naming the missing
manifests and the first sources found, and no language stage runs.

### Semgrep scan

After the language checks, `vrg-validate` runs the scan of PR CI's
`security / semgrep` gate, so a finding fails locally before the PR
is opened. `--check semgrep` runs only this stage. It resolves the
rulesets the CI job does: the language's registry ruleset (see
[Shared security workflow](ci-architecture.md#shared-security-workflow)), `p/dockerfile`
when the repository has a `Dockerfile*` anywhere, `p/github-actions`
when `.github/workflows/` holds a `*.yml` or `*.yaml`, and the
`p/c` (C++) or `p/typescript` (TypeScript) ruleset that
`ci-security.yml` adds. The files it looks at are the tracked and
untracked ones that `.gitignore` does not exclude. The
fleet-excluded rules stay excluded, and a finding with an in-source
`nosemgrep` suppression is listed but does not fail the stage, as in
CI.

Semgrep downloads the rulesets from its registry, so the stage
needs network access, like the audit stage. Its usage metrics are
off (`--metrics off`). The SARIF goes to a temporary directory, never
the working tree. A scan that errors or writes no SARIF fails the
stage. A repository with no ruleset to run (no language ruleset, no
Dockerfile and no workflow) records the stage as a warning (⚠). For
this repository the scan takes about 15 seconds.

### Trivy scan

After the semgrep scan, `vrg-validate` runs the vulnerability scan of
PR CI's `security / trivy` gate, so a CRITICAL or HIGH vulnerability
in a dependency fails locally before the PR is opened. `--check trivy`
runs only this stage. It runs the commands of vergil-actions'
`actions/shared/security/trivy/scan.sh` with the gate's inputs:
`trivy fs --scanners vuln --severity CRITICAL,HIGH` writes the JSON
report (skipping pip's vendored `bom.cdx.json`), `trivy convert`
prints the findings table, and `trivy convert --exit-code 1` writes
the SARIF report and fails on a finding. As in CI, Trivy reads the
repository's `.trivyignore` and `trivy.yaml` from the directory it
scans. The scan runs on a temporary copy of the tracked and untracked
files that `.gitignore` does not exclude, which stands in for CI's
fresh checkout: an ignored `.venv`, `node_modules`, `target` or
`build` directory cannot add a finding CI would not report. The
reports go to the same temporary directory, never the working tree.

The stage needs network access to download Trivy's vulnerability
database. The database is cached between runs, never in the working
tree: `vrg-container-run` mounts the `vergil-trivy-cache` volume
for it (see
[Container Config](../reference/container-config.md#variables-vrg-container-run-always-sets)).
`vrg-container-run` points `VRG_TRIVY_CACHE_DIR` at the volume; in
a container started without it, the cache is `vergil/trivy` under
`$XDG_CACHE_HOME` or `~/.cache`, which lasts only as long as the
container. A cache directory inside the working tree fails the stage. Runs that share the cache take turns, so parallel validations
never download into it at once. A scan that writes no report, such as
one whose database download failed, is retried twice, after 15 and 30
seconds, as in CI, and then fails the stage as a scan error, never as
a pass. With the database cached, the scan of this repository takes
about a second.

### Docs build

After the Trivy scan, `vrg-validate` runs the strict mkdocs build of
PR CI's `docs / docs` gate (`ci-docs.yml`), so a broken internal link
or a nav entry for a missing page fails locally before the PR is
opened. `--check docs` runs only this stage. A repository with no
`docs/site/mkdocs.yml` has no docs build: the stage prints a notice and
passes, as the CI job does.

The stage first stages the sources as CI's docs job does: it copies
`CHANGELOG.md` and the release notes under `releases/` into
`docs/site/docs/`, and patches the release versions into the nav. The
nav patch goes to `docs/site/mkdocs.staged.yml`, a copy of the config,
never the tracked `mkdocs.yml`, as `vrg-container-docs` does. Every
path it writes must be gitignored (the managed `.gitignore` covers
them); if one is not, the stage fails before writing anything.
Staging is offline: CI also adds a CI-evidence download link to each
release page, an external link a strict build does not check, so the
stage leaves it out and needs no `gh` authentication.

It then runs `mkdocs build --strict` on the staged config, with
`NO_MKDOCS_2_WARNING=1` as in CI. A Python repository runs it as
`uv run --frozen mkdocs`, as CI runs `uv run mkdocs`, so a `mkdocs`
in the project's environment is used when there is one; `--frozen`
keeps `uv.lock` unchanged. Any other repository, and a Python one
with no `mkdocs` in its environment, uses the dev image's
mkdocs-material. The site is built in a temporary directory, never the
working tree.

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
The repo-profile check distinguishes the two failure cases:

| Code | Meaning |
| ---- | ------- |
| 0 | `vergil.toml` and `README.md` are valid |
| 1 | `vergil.toml` is invalid, or `README.md` breaks the structural conventions |
| 2 | `vergil.toml` not found |
