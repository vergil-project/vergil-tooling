# CI Architecture

This guide explains the continuous integration model used across all
`mq-rest-admin-*` repositories and the vergil-tooling ecosystem, and
how to implement it in new projects.

## Table of Contents

- [Overview](#overview)
- [Tier 1: Local pre-commit](#tier-1-local-pre-commit)
- [Tier 2: PR CI](#tier-2-pr-ci)
- [Architecture](#architecture)
- [Implementation guide](#implementation-guide)
- [CI gates](#ci-gates)
- [Dev container images](#dev-container-images)

## Overview

Testing is split into two tiers with increasing scope, cost, and feedback
latency:

| Tier | Trigger | Time | Security |
| ---- | ------- | ---- | -------- |
| 1 | Manual (before commit) | Seconds | No |
| 2 | Pull request | ~8-10 min | Yes |

- **Tier 1**: Single version, full local validation, dockerized
- **Tier 2**: Full version matrix, all checks, security uploads

The goal is fast local feedback for the developer and comprehensive gated
checks on the PR itself. The Claude Code hook guard and `vrg-commit`
enforce that Tier 1 runs before any commit lands, so by the time a PR
opens, it should already be green.

!!! note "Historical: three-tier CI"
    Earlier versions of this guide documented a third tier — push-CI — as
    a thin `workflow_call` wrapper that ran a subset of checks on every
    push to a feature branch. That tier was removed once `vrg-validate`
    reached parity with PR-CI; the push-CI workflow added no coverage that
    PR-CI didn't already provide and created a concurrency-group deadlock
    with `ci.yml`. Integration-test coverage at push-time was deliberately
    dropped and is tracked separately as future work on local integration
    testing. See vergil-project/vergil-actions#176 for the parity audit
    and removal rationale.

## Tier 1: Local pre-commit

Run in a dev container on the developer's machine. Docker is the only
host prerequisite.

```bash
./scripts/dev/test.sh        # Unit tests + linting
./scripts/dev/lint.sh        # Lint and formatting checks
./scripts/dev/audit.sh       # Dependency and license audit
```

Each script follows the same pattern:

1. Set `DOCKER_DEV_IMAGE` (default: `dev-<language>:<latevrg-version>`)
2. Set `DOCKER_TEST_CMD` (language-specific command)
3. Delegate to `vrg-container-test` if available, otherwise run `docker run`
   directly

Environment overrides:

- `DOCKER_DEV_IMAGE` — use a different container image
- `DOCKER_TEST_CMD` — override the test command

!!! tip
    Build the dev images locally before first use:
    `cd ../vergil-containers && docker/build.sh`

Running `vrg-container-run -- uv run vrg-validate` before each commit runs
common checks and per-language validation. The Claude Code hook guard
ensures agents use `vrg-commit` (which runs validation) rather than raw
`git commit`.

## Tier 2: PR CI

Triggers on `pull_request` events. Runs the full validation suite.

**What runs:**

- Unit tests across the full version matrix
- Integration tests across the full version matrix
- Security scanners (CodeQL, Trivy, Semgrep) via shared reusable workflow
- Standards compliance
- Dependency audit
- Release gates (version divergence, format validation)

The workflow file is `.github/workflows/ci.yml`, which runs directly on
`pull_request` and is also exposed as a reusable workflow via
`workflow_call` for any specialized callers (release pipelines, etc.).

## Architecture

### Reusable workflow pattern

`ci.yml` accepts `workflow_call` with inputs that control scope:

| Input | Type | Default |
| ----- | ---- | ------- |
| `versions` | string (JSON) | Full matrix |
| `integration-matrix` | string (JSON) | Full matrix |
| `run-security` | string | `"true"` |
| `run-release-gates` | string | `"true"` |

- `versions` — language versions to test
- `integration-matrix` — test entries with ports
- `run-security` — enable security scanners
- `run-release-gates` — enable release gate checks

When triggered directly by `pull_request`, all inputs are empty and
defaults produce the full Tier 2 behavior. The inputs remain in place so
specialized callers (e.g., release pipelines) can constrain scope when
needed.

!!! warning "String inputs, not booleans"
    Use `type: string` for gate inputs, not `type: boolean`. Boolean
    inputs are unreliable for job-level `if` conditions when the
    workflow is triggered directly (inputs are empty, not `false`).
    Use `!= 'false'` comparisons instead.

### Shared security workflow

Security scanners and standards compliance are factored into a shared
reusable workflow at
`vergil-project/vergil-actions/.github/workflows/ci-security.yml`.

This provides four jobs:

- `ci: standards-compliance`
- `security: codeql`
- `security: trivy`
- `security: semgrep`

Call it from `ci.yml`:

```yaml
security-and-standards:
  if: ${{ inputs.run-security != 'false' }}
  uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@develop
  with:
    language: ruby
    # For Go, also set: semgrep-language: golang
  permissions:
    contents: read
    security-events: write
```

!!! tip "Semgrep language names"
    Semgrep uses `p/<language>` rulesets. Most languages match their
    common name (`ruby`, `python`, `java`) but Go requires `golang`.
    Use the `semgrep-language` input to override when needed.

#### Fleet-excluded semgrep rules

The semgrep scanner (`vrg-semgrep-scan`, backed by
`src/vergil_tooling/lib/semgrep.py`) always excludes a small set of
fleet-default rules — `DEFAULT_EXCLUDED_RULES` — via semgrep's
`--exclude-rule`. Callers can add further exclusions with the repeatable
`--exclude-rule <RULE_ID>` flag; those are added **on top of** the fleet
defaults, never in place of them.

The one fleet default today is
`github-actions-mutable-action-tag`, which flags every `uses: …@vN`
action reference. It is exempted fleet-wide pending backlog
[vergil-project/.github#194](https://github.com/vergil-project/.github/issues/194)
(pin third-party action SHAs once pin-advancement tooling exists). Our
own `vergil-project/vergil-actions@v2.1` references are a **permanent**
exception — they are our release line, not a mutable third-party tag.
Every other semgrep rule stays enforced.

### CD: release-publishing secrets

The release-publishing workflow generated into a consuming repo's
`cd.yml` (by `repo_init`) forwards **explicit, least-privilege secrets**
to the reusable `cd-release` workflow per ecosystem — never a blanket
`secrets: inherit` (epic
[vergil-project/.github#189](https://github.com/vergil-project/.github/issues/189)).
The map lives in `_cd_release_secrets()`
(`src/vergil_tooling/lib/repo_init.py`) and mirrors exactly what each
publisher reads:

| Ecosystem | Secrets forwarded |
| --------- | ----------------- |
| python | *none* — PyPI OIDC trusted publishing |
| go | *none* — no publish token |
| rust | `CARGO_REGISTRY_TOKEN` |
| ruby | `RUBYGEMS_API_KEY` |
| java | `CENTRAL_USERNAME`, `CENTRAL_TOKEN`, `GPG_PRIVATE_KEY`, `GPG_PASSPHRASE` |

A language that needs no secret (python, go, or any non-publishing
language) gets **no `secrets:` block at all**, not `secrets: inherit`.
References to our own `vergil-actions@v2.1` reusable workflows are
unaffected — they are trusted first-party refs.

### Default matrix pattern

Use `fromJSON()` with a fallback to embed the full default matrix
directly in the workflow:

```yaml
strategy:
  fail-fast: false
  matrix:
    version: ${{ fromJSON(inputs.versions || '["3.2", "3.3", "3.4"]') }}
```

This avoids needing a separate job to compute the matrix.

## Implementation guide

### Step 1: Define ci.yml

Trigger on `pull_request` and (optionally) expose `workflow_call`
alongside it for specialized callers. Define inputs with string types
and sensible defaults.

### Step 2: Factor security into shared workflow

Replace inline CodeQL, Trivy, Semgrep, and standards-compliance jobs
with a single call to `ci-security.yml`.

### Step 3: Add dev scripts

Create `scripts/dev/test.sh`, `scripts/dev/lint.sh`, and
`scripts/dev/audit.sh` following the Docker-first pattern. See
[Dev container images](#dev-container-images) for image details.

### Step 4: Update CI gates

Update the repository ruleset to match new check names. Key changes:

- Remove `ci: docs-only` (no longer exists)
- Replace `ci: standards-compliance` with
  `security-and-standards / ci: standards-compliance`
- Replace `security: *` with `security-and-standards / security: *`

Use the GitHub API to update rulesets:

```bash
gh api repos/OWNER/REPO/rulesets/RULESET_ID -X PUT --input gates.json
```

### Step 5: Update CLAUDE.md

Add the two-tier CI model and Docker-first testing sections to the
repository's `CLAUDE.md`.

## CI gates

When security and standards jobs move into the shared reusable workflow,
their check names gain a `security-and-standards /` prefix:

Old names and their replacements:

- `ci: standards-compliance` →
  `security-and-standards / ci: standards-compliance`
- `security: codeql` →
  `security-and-standards / security: codeql`
- `security: trivy` →
  `security-and-standards / security: trivy`
- `security: semgrep` →
  `security-and-standards / security: semgrep`

Jobs that remain inline keep their names unchanged:

- `ci: dependency-audit`
- `release: gates`
- `test: unit (<version>)`
- `test: integration (<version>)`

## Dev container images

Published to `ghcr.io/vergil-project/dev-<language>:<version>` from the
[vergil-containers](https://github.com/vergil-project/vergil-containers)
repository.

### Available images

**`dev-ruby`** (3.2, 3.3, 3.4)
:   Base: `ruby:<v>-slim`. Includes build-essential,
    git, curl, bundler.

**`dev-python`** (3.12, 3.13, 3.14)
:   Base: `python:<v>-slim`. Includes git, curl, uv.

**`dev-java`** (17, 21)
:   Base: `eclipse-temurin:<v>-jdk`. Includes git, curl.

**`dev-go`** (1.25, 1.26)
:   Base: `golang:<v>`. Includes golangci-lint,
    govulncheck, go-licenses, gocyclo.

### Building locally

```bash
cd ../vergil-containers
docker/build.sh
```

This builds all images. Individual images can be built with:

```bash
docker build --build-arg RUBY_VERSION=3.4 -t dev-ruby:3.4 docker/ruby/
```

### Publishing

Images are published automatically on push to `develop` or `main` in
the `vergil-containers` repository via its
`.github/workflows/docker-publish.yml` workflow.

### Design principles

- **Thin images** — language runtime + package manager + git/curl
- **Project-managed dependencies** — tools come from lockfiles at
  container startup (e.g., `bundle install`, `uv sync`, `go install`)
- **No host requirements** — Docker is the only prerequisite for
  local development
