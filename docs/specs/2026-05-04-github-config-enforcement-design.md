# GitHub configuration enforcement and validation refactor

Ref: https://github.com/wphillipmoore/standard-tooling/issues/173

## Problem

During the mq-rest-admin-python v1.2.1 publish, a release PR
auto-merged into main before CI completed because integration tests
were not required status checks on the main branch. A fleet-wide
audit of 13 repos across projects 3 (mq-rest-admin) and 4
(standards) revealed:

- Two repos had CI gates targeting only `~DEFAULT_BRANCH` (develop),
  leaving main unprotected
- One repo (mq-rest-admin-template) had no rulesets at all
- One repo had stale classic branch protection alongside rulesets
- Three repos had incorrect GitHub Actions permissions
- CI gate names were inconsistent across repos (bespoke jobs left
  over from incremental standardization)
- No tooling existed to enforce or audit GitHub-level configuration

The ecosystem evolved incrementally. Individual repos were
standardized at different times, and the trailing cleanup was never
completed. The result is an inconsistent fleet with no mechanism to
enforce or verify correctness.

## Decision

Centrally controlled GitHub configuration, enforced by tooling.

`standard-tooling.toml` is the single source of truth for each
repo's identity. A derivation engine in standard-tooling computes
the complete desired GitHub configuration from that identity. Two
new tools enforce and validate this:

- **`st-github-config`** — audit, diff, and apply GitHub API-level
  configuration (repo settings, rulesets, security, Actions
  permissions, labels)
- **`st-validate`** — renamed from `st-validate-local`, extended
  with version-matrix awareness and check filtering

The standard is defined centrally in standard-tooling's code. Per-repo
configuration is minimal (identity + version matrix). Exceptions are
explicit overrides in `standard-tooling.toml`, visible and intentional.

## Architecture

### Layered configuration model

The configuration for any repo is computed as:

```
central defaults (in tool code, keyed by repo type + language)
  + per-repo identity (standard-tooling.toml [project] + [ci])
  + per-repo overrides (standard-tooling.toml [github], rare)
  = desired state
```

Central defaults encode the standard. Per-repo identity drives
derivation (a Python library with three versions gets different CI
gates than a shell tooling repo with one). Overrides exist only for
genuine exceptions (e.g., a template repo that intentionally has no
rulesets). The presence of a `[github]` override section is itself a
signal that warrants review.

### standard-tooling.toml extensions

The existing `[project]` fields (`repository-type`,
`versioning-scheme`, `branching-model`, `release-model`,
`primary-language`) are unchanged and drive the derivation. The
existing `[dependencies]` and `[markdownlint]` sections are also
unchanged.

New `[ci]` section (required for all repos):

```toml
[ci]
versions = ["3.12", "3.13", "3.14"]
integration-tests = true
```

- **`versions`** — language version matrix. Consumed by CI workflows
  (for matrix strategy) and by `st-validate` (for per-version local
  validation via Docker). Single-element list for apps/tooling repos
  that target one version.
- **`integration-tests`** — whether this repo has integration tests.
  Drives CI workflow structure and the check names in rulesets.

New `[github]` section (optional, overrides only):

```toml
[github]
skip-rulesets = true
```

This section is absent or empty for the vast majority of repos. Its
presence signals a non-standard configuration that should be
reviewed. The set of supported override keys will be defined during
implementation; the principle is that overrides are explicit,
minimal, and visible.

## Canonical GitHub configuration standard

### Repo settings

Uniform across all repos. No derivation, no exceptions.

| Setting | Required value |
|---|---|
| `default_branch` | `develop` |
| `allow_auto_merge` | `false` |
| `delete_branch_on_merge` | `true` |
| `allow_merge_commit` | `true` |
| `allow_squash_merge` | `true` |
| `allow_rebase_merge` | `true` |
| `has_issues` | `true` |
| `has_projects` | `true` |
| `has_wiki` | `true` |

### Security settings

Uniform across all repos. No derivation, no exceptions.

| Setting | Required value | Rationale |
|---|---|---|
| `secret_scanning` | `enabled` | |
| `secret_scanning_push_protection` | `enabled` | |
| `vulnerability_alerts` | `disabled` | Vulnerability scanning is handled by Trivy, Semgrep, and language-specific audit tools (pip-audit, govulncheck, etc.) in CI. GitHub vulnerability alerts would duplicate this coverage without adding value. Subject to reevaluation — see below. |
| `dependabot_security_updates` | `disabled` | Dependabot security updates apply patches unsupervised. All dependency changes must go through the standard PR and CI pipeline. Subject to reevaluation — see below. |

**Reevaluation note:** A separate issue will be created to
holistically evaluate GitHub's security offerings (vulnerability
alerts, Dependabot, and any other features) against the current
Trivy/Semgrep/language-audit toolchain to determine whether any
would add coverage.

### Actions permissions

Uniform across all repos. No derivation, no exceptions.

| Setting | Required value |
|---|---|
| `default_workflow_permissions` | `read` |
| `can_approve_pull_request_reviews` | `false` |
| `allowed_actions` | `selected` |
| `patterns_allowed` | `actions/*`, `github/*`, `docker/*`, `ruby/*`, `actions-rust-lang/*`, `astral-sh/*`, `pypa/*`, `wphillipmoore/*` |

The `patterns_allowed` list restricts which GitHub Actions can run
in workflows to the set of action owners actually in use across the
fleet. This is enforced rather than `allowed_actions: all` to
prevent unauthorized actions from being introduced. The list is
maintained as part of the canonical standard and updated when a new
action owner is adopted.

### Labels

Synced from the canonical registry
(`src/standard_tooling/data/labels.json`). This already works via
`st-ensure-label` and is unchanged.

### Rulesets

Three rulesets per repo. The first two are uniform; the third is
derived from repo identity.

#### Ruleset 1: Branch protection

Identical across all repos. No derivation needed.

- **Target:** `branch`
- **Applies to:** `refs/heads/main`, `refs/heads/develop`
- **Enforcement:** `active`
- **Bypass actors:** none (`current_user_can_bypass: never`)
- **Rules:**
  - `deletion` — prevent branch deletion
  - `non_fast_forward` — prevent force push
  - `pull_request`:
    - `required_approving_review_count: 0`
    - `dismiss_stale_reviews_on_push: true`
    - `require_code_owner_review: false`
    - `require_last_push_approval: false`
    - `required_review_thread_resolution: false`
    - `allowed_merge_methods: [merge, squash, rebase]`

#### Ruleset 2: Tag protection

Identical across all repos. No derivation needed.

- **Target:** `tag`
- **Applies to:** `refs/tags/v*.*.*`
- **Enforcement:** `active`
- **Bypass actors:** RepositoryRole 5 (admin), `bypass_mode: always`
- **Rules:**
  - `deletion` — prevent tag deletion
  - `non_fast_forward` — prevent force push
  - `update` — prevent tag mutation

#### Ruleset 3: CI gates

Structure is fixed; the required check list varies by repo identity.

- **Target:** `branch`
- **Applies to:** `refs/heads/main`, `refs/heads/develop`
  (explicitly — never `~DEFAULT_BRANCH`)
- **Enforcement:** `active`
- **Bypass actors:** none (`current_user_can_bypass: never`)
- **Rules:**
  - `required_status_checks`:
    - `strict_required_status_checks_policy: true`
    - Check list derived from repo identity (see canonical check
      name registry and derivation rules below)
    - All checks pinned to `integration_id: 15368` (GitHub Actions)

## Canonical check name registry

**TODO:** This section must be defined before implementation begins.
It is the linchpin connecting rulesets, CI workflows, and
`st-validate --only/--skip`.

Define here:

1. Every canonical check name
2. The CI job structure that produces each name (individual job vs.
   step within a consolidated job — this determines what GitHub
   registers as a status check)
3. The naming convention for version-matrix expansions (e.g., how
   `unit` + version `3.14` maps to a check name)
4. The mapping between these names and `st-validate --only/--skip`
   filter values

The check names must be consistent across all repos, CI workflows,
rulesets, and the `st-validate` filter interface. One namespace
everywhere.

### CI gates check list derivation

The tool computes the required check list from `[project]` +
`[ci]`, using the canonical check names defined above.

**Always present (every repo):**

- Standards compliance (from reusable workflow)
- Security: Trivy, Semgrep
- Security: CodeQL (where `primary-language` is CodeQL-supported)
- Common quality checks — shellcheck, hadolint, actionlint,
  yamllint, markdownlint, repo-profile validation. These run on
  every repo and are no-ops when no relevant files exist. Whether
  these are individual CI jobs (each a separate required status
  check) or steps within a consolidated validation job is resolved
  as part of the check name registry definition above.

**Present when `release-model != "none"`:**

- Release gates

**Present for each entry in `[ci].versions`:**

- Unit test check (one per version)

**Present when `[ci].integration-tests = true`:**

- Integration test check (one per version)

**Present based on `primary-language`:**

- Dependency audit (pip-audit, govulncheck, etc.)
- Language-specific quality checks not subsumed by common checks

**Bespoke gates that exist today and will be removed:**

- `ci: type-check` — subsumed by the per-version quality/test gate
- `ci: python` — replaced by the standard validation gate
- `ci: shellcheck` (as standalone CI job) — subsumed by common
  validator
- `ci: hadolint` (as standalone CI job) — subsumed by common
  validator
- `ci: actionlint` (as standalone CI job) — subsumed by common
  validator
- `ci: mkdocs-build` (as standalone CI job) — subsumed by common
  validator

### Classic branch protection

The standard requires no classic branch protection rules. All
protection is via rulesets. The tool flags and optionally removes
any classic branch protection it finds (e.g., the stale rules on
standard-tooling-plugin).

## Tools

### st-github-config

GitHub configuration enforcement tool. Three modes:

**`st-github-config audit [--repo OWNER/REPO | --owner OWNER --project N]`**

Reads `standard-tooling.toml` from the target repo(s), computes
desired GitHub config, fetches actual config via `gh api`, compares,
and reports diffs. Exit 0 if compliant, exit 1 if not. Project mode
uses the same repo discovery as `st-ensure-label`.

For project-wide audit, the tool clones or fetches each repo's
`standard-tooling.toml` from the default branch (no full clone
needed — just the config file via `gh api`).

**`st-github-config apply [--repo OWNER/REPO | --owner OWNER --project N] [--yes]`**

Same computation as audit, but writes corrections via `gh api`.
Reports what it changed. Requires confirmation unless `--yes`.

**Safety gate:** `apply` refuses to act unless `audit` confirms
that the current CI workflows already produce the expected check
names. This prevents applying rulesets that reference check names
the workflows don't produce yet, which would block merges
fleet-wide.

**`st-github-config diff [--repo OWNER/REPO | --owner OWNER --project N]`**

Outputs a structured diff of what would change without changing
anything. Useful for review before apply and for potential CI
integration.

**Deployment target:** Host-level tool. Runs on the developer's
machine (or CI runner) using `gh` CLI directly — not inside a dev
container. Same deployment model as `st-commit`, `st-submit-pr`,
`st-ensure-label`.

**Implementation location:**

- `src/standard_tooling/bin/github_config.py` — CLI entry point
- `src/standard_tooling/lib/github_config.py` — derivation engine

The derivation engine is the core. It takes a parsed `StConfig` and
returns a complete desired-state object covering repo settings,
security settings, Actions permissions, rulesets (including the
computed check list), and labels. This object can be compared
against actual state for auditing, or serialized into `gh api`
calls for applying.

### st-validate

Renamed from `st-validate-local`. The name reflects its role as THE
validation tool — same checks whether run locally or in CI.

**Execution model:**

`st-validate` is a host-side tool. It orchestrates validation by
calling `st-docker-run` to execute checks inside dev containers.
This is a change from the previous `st-validate-local` design,
which ran inside a single container. The version matrix (multiple
language versions requiring different container images) makes
single-container execution untenable.

The container guard added in v1.4.14 (`_in_dev_container()` check)
is removed as part of this work.

**Version-matrix awareness:**

Reads `[ci].versions` from `standard-tooling.toml`. Runs
language-specific validation once per version using the
corresponding Docker image
(`ghcr.io/wphillipmoore/dev-<lang>:<version>`).

Execution model when run locally:

1. Common checks (shellcheck, hadolint, actionlint, yamllint,
   markdownlint, repo-profile) — run once via `st-docker-run` in
   the base dev container
2. Language-specific checks (lint, typecheck, tests, audit) — run
   once per version in the matrix, each via `st-docker-run` with
   the matching dev container image
3. Results aggregated and reported

When run in CI, the workflow may parallelize across the matrix using
workflow matrix strategy. `st-validate` runs sequentially when
invoked directly (local use), but CI can invoke it per-version as
separate jobs.

**Per-language command registry:**

`st-validate` owns the per-language commands for lint, typecheck,
test, and audit centrally. These are not configurable per repo.
Fleet audit confirmed all repos use the same commands per language:

| Language | Lint | Typecheck | Test | Audit |
|---|---|---|---|---|
| Python | `ruff check` + `ruff format --check` | `mypy src/` | `pytest` (with coverage) | `uv sync --check` + `uv lock --check` |
| Go | `golangci-lint run` + `gocyclo` | (included in lint) | `go test` (with coverage) | `govulncheck` |
| Java | `mvnw spotless:check checkstyle:check` | (included in lint) | `mvnw verify` | (included in test) |
| Ruby | `rubocop` | (N/A) | `rake` | (N/A) |
| Rust | `cargo fmt --check` + `cargo clippy` | (included in lint) | `cargo llvm-cov` (with coverage) | `cargo audit` |

This replaces the `scripts/dev/{lint,typecheck,test,audit}.sh`
scripts that previously existed in every repo. Those scripts were
either trivial container-local invocations or identical boilerplate
wrapping `st-docker-test` — the per-language variation was fully
derivable from `standard-tooling.toml`. The scripts are deleted
from all repos as part of this work.

`st-docker-test` is also retired — its function is subsumed by
`st-validate` calling `st-docker-run` directly.

Per-repo customization of the four standard checks is explicitly
not supported in this version. If a future need arises, the
customization mechanism will be designed fresh on top of the new
architecture rather than inheriting the shell script interface.

**Check filtering (`--only` / `--skip`):**

```bash
st-validate --only markdown,shellcheck
st-validate --skip integration
st-validate --only "unit(3.14)"
```

The check names used in `--only` and `--skip` are the same
canonical names used in CI gate definitions and rulesets. One
namespace everywhere. This allows targeted local validation (e.g.,
re-running just the markdown checks after editing docs) and allows
CI workflows to invoke `st-validate --only unit` per matrix entry
for parallelization.

**Migration path:**

- `st-validate-local` becomes a thin wrapper that prints a
  deprecation warning and calls `st-validate`
- `st-validate-local-common` and `st-validate-local-<lang>` are
  similarly wrapped during transition
- Old entry points stay in `pyproject.toml` for one minor version
  cycle, then are removed
- `st-docker-test` entry point similarly deprecated and removed

## Scope and boundaries

### In scope

1. The canonical GitHub configuration standard (repo settings,
   security, Actions permissions, rulesets)
2. The `standard-tooling.toml` extensions (`[ci]` section,
   `[github]` override section)
3. The canonical check name registry (names, job structure, naming
   conventions)
4. The `st-github-config` tool (audit/apply/diff)
5. The `st-validate` tool (rename, host-orchestrated execution,
   version-matrix awareness, per-language command registry,
   `--only`/`--skip` filtering)
6. The CI gate naming standard (canonical names, derivation rules)
7. Cleanup of `scripts/dev/*.sh` across all repos
8. Retirement of `st-docker-test`
9. Consolidation of common checks (actionlint, hadolint, etc.)
   into the common validator so they run on all repos

### Out of scope

- CI workflow file generation or templating — workflow YAML remains
  hand-maintained but must conform to the standard names and consume
  `standard-tooling.toml` for the version matrix
- Publishing workflows (`publish.yml`)
- Documentation site build and deploy (mkdocs)
- MQ integration test infrastructure
- Per-repo customization of lint/typecheck/test/audit commands

### Implementation order

Steps 1-4 can ship independently and immediately fix the rulesets.
Steps 5-8 are the larger CI/validation refactor. Step 9 ties them
together with a safety gate.

1. Define the canonical check name registry (names, job structure,
   matrix naming convention)
2. Extend `standard-tooling.toml` schema (`[ci]` section)
3. Build the GitHub config derivation engine
4. Build `st-github-config` (audit first, then apply, then diff)
5. Rename `st-validate-local` to `st-validate` with deprecation
   wrappers; change execution model to host-orchestrated
6. Add version-matrix support, per-language command registry, and
   `--only`/`--skip` filtering
7. Consolidate common checks (actionlint, hadolint, etc.) into
   the common validator
8. Refactor CI workflows across all repos to consume
   `standard-tooling.toml` and use canonical check names. Phased
   rollout:
   - Define the per-repo migration sequence (start with
     standard-tooling itself, then standards-project repos, then
     mq-rest-admin repos)
   - Validate each repo's CI produces the expected check names
     before moving to the next
   - Run `st-github-config audit` per repo to confirm alignment
     before proceeding to step 9
9. Run `st-github-config apply` across all repos to enforce
   rulesets. The `apply` safety gate requires `audit` to pass
   first — rulesets cannot be applied until CI workflows produce
   the expected check names.
10. Remove `scripts/dev/{lint,typecheck,test,audit}.sh` from all
    repos, remove `st-docker-test`, and remove deprecated
    `st-validate-local*` entry points

### Key constraint

The CI gate design is authoritative. `st-validate` is subordinate
to it — it mirrors the CI gates for local execution, minus checks
that do not make sense locally (e.g., PR issue linkage, which
requires a PR to exist). Docker execution is mandatory for local
validation to match CI (same images, same tooling).
