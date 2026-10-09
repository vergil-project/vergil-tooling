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
    matched the checks push-CI ran; the push-CI workflow added no coverage
    that PR-CI didn't already provide and created a concurrency-group
    deadlock with `ci.yml`. Integration-test coverage at push-time was
    deliberately dropped and is tracked separately as future work on local
    integration testing. See vergil-project/vergil-actions#176 for the
    parity audit and removal rationale. The "parity with PR-CI" framing is
    precise only for the single-interpreter checks Tier 1 runs — see the
    coverage-parity caveat under [Tier 2: PR CI](#tier-2-pr-ci) below for
    the one deliberate gap.

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

!!! note "Python tests run in parallel by default"
    For Python repos the validation gate's test stage runs in parallel via
    pytest-xdist's work-stealing scheduler — the shared command appends
    `-n auto --dist worksteal` to the coverage-gate argv. This is the
    fleet-wide default (the Python dev image ships `pytest-xdist`, so the
    flag resolves everywhere) and materially cuts test-stage wall-clock —
    roughly 3.2× on this repo's own suite (epic
    [vergil-project/.github#333](https://github.com/vergil-project/.github/issues/333)).
    A repo with an order-dependent suite opts out with `[test].parallel =
    false` in its `vergil.toml`; see
    [Test Config (`[test]`)](../reference/test-config.md) for the knob and
    how the flag is computed. Parallelism changes only *how fast* the suite
    runs — the `--cov-branch --cov-fail-under=100` gate is present in every
    computed command, serial or parallel, and measures the identical set.

Two adjacent levers were evaluated for this epic and **dropped**: the
`sys.monitoring` coverage backend (`COVERAGE_CORE=sysmon`) is inert under
`--cov-branch` — coverage.py silently falls back to the C tracer — and
`--import-mode=importlib` gave no measured speedup and was not fleet-safe.
Neither ships; parallelism is the one universal win. See
[`epics/333-python-test-perf/evidence/baseline.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/epics/333-python-test-perf/evidence/baseline.md)
for the measured evidence.

## Tier 2: PR CI

Triggers on `pull_request` events. Runs the full validation suite.

**What runs:**

- Unit tests across the full version matrix
- Integration tests across the full version matrix
- Security scanners (CodeQL, Trivy, Semgrep) via shared reusable workflow
- Standards compliance
- Dependency audit
- Release gates (version divergence, format validation)
- Binary package build and install-test (`ci-package.yml`, only in repos
  whose `vergil.toml` has `[package]`). Feature PRs run the **reduced**
  tier: every build cell, but one install-test cell per format (the oldest
  selected release, amd64 first). Release PRs (`release/*` into `main`)
  and non-PR events run the **full** matrix. See
  [Tiers: full and reduced](../reference/package-config.md#tiers-full-and-reduced).

The workflow file is `.github/workflows/ci.yml`, which runs directly on
`pull_request` and is also exposed as a reusable workflow via
`workflow_call` for any specialized callers (release pipelines, etc.).

!!! warning "Local validate is not full coverage parity"
    Tier 1 runs one dev container on a single Python interpreter (currently
    3.14), so its `--cov-fail-under=100` gate proves 100% coverage on that
    one interpreter only. PR-CI re-runs the test-and-coverage gate
    **independently in a separate container per `[ci].versions` entry**
    (3.12, 3.13, 3.14). Because branch coverage (`--cov-branch`) can
    legitimately differ across CPython versions — a branch reachable on one
    version may be dead on another, or a version-guarded code path may only
    execute on some interpreters — code that measures 100% locally can still
    fall below 100% on a 3.12 or 3.13 leg and fail CI. The multi-version
    coverage matrix is therefore a **PR-CI-only** gate: local validate covers
    a single interpreter and cannot reproduce it. This is a known, accepted
    limitation, not a bug — closing it would mean running the full version
    matrix locally, which Tier 1's one-container model deliberately trades
    away for speed.

## Architecture

### Thin-caller pattern

A consuming repo's `ci.yml` is a **thin caller**: each job simply `uses:` a
`vergil-actions` reusable workflow (`ci-audit`, `ci-quality`, `ci-security`,
`ci-test`, `ci-version-bump`, `ci-docs`) at the pinned `@v2.1` tag and passes
only `language:` and `container-suffix:`. It does **not** pass a version matrix
or a container tag — the reusable workflows read `[ci].versions` from the repo's
`vergil.toml` at run time and derive both the matrix and the primary-version
container themselves (epic
[vergil-project/.github#338](https://github.com/vergil-project/.github/issues/338)).
A `[ci].versions` change therefore takes effect fleet-wide with no edit to
`ci.yml`.

`ci.yml` still exposes `workflow_call` with two scope inputs so specialized
callers (e.g. release pipelines) can constrain it — `run-security` (enable the
security scanners) and `run-release` (enable the release/version gates). When
triggered directly by `pull_request` both default on, producing the full
Tier 2 behavior.

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

#### Suppressing a reviewed finding in source

The SARIF gate (`vrg-sarif-evaluate`, and the `vrg-semgrep-scan` /
`vrg-trivy-scan` wrappers, all backed by `src/vergil_tooling/lib/sarif.py`)
honors SARIF result **suppressions**. A result does **not** fail the gate
when its `suppressions` array holds an entry with:

- `kind` of `inSource` (an in-code comment) or `external` (an out-of-band
  record), **and**
- `status` absent or `accepted`.

Every other case fails closed and still counts against the gate: a
`status` of `underReview` or `rejected`, an unknown or missing `kind`, or an
empty or malformed `suppressions` array.

Suppressed findings are **reported, not hidden**. They are listed in a
separate "suppressed finding(s)" table in the job summary (rule, level,
file, line, suppression kind, and justification). The scan wrappers also
emit a `suppressed_count` step output next to `finding_count`.

GitHub code scanning does not apply the same rule: it raises an alert for
every SARIF result, suppressed or not. So the SARIF that is **uploaded to
code scanning** omits accepted-suppressed results, while the **CI-evidence
bundle keeps the full, unfiltered SARIF**. The filtered copy comes from
`vrg-sarif-filter`:

```bash
vrg-sarif-filter INPUT OUTPUT
```

It drops every result whose suppression is in effect by exactly the rule
above (the same `effective_suppression()` logic as the gate), from every run
and at any severity. Everything else is kept: the tool, rules, invocations,
and every unsuppressed or fail-closed result. It prints
`vrg-sarif-filter: removed N suppressed result(s) from INPUT -> OUTPUT`.
`INPUT` and `OUTPUT` may be the same path, because the output is written
atomically. Unreadable or invalid SARIF (bad JSON, or no `runs` list) is a
hard error with exit code 1.

For semgrep, write the suppression as a `nosemgrep` comment that names the
**exact rule ID**, and put the justification and a tracking reference in the
same comment, after the rule ID:

```yaml
    secrets: inherit  # nosemgrep: yaml.github-actions.security.secrets-inherit.secrets-inherit -- reusable workflow needs the release token; reviewed in #123
```

Semgrep also accepts the comment on the line **above** the finding:

```python
# nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true -- fixed command string, no user input; see #456
subprocess.call(CMD, shell=True)
```

Semgrep emits both forms in SARIF as `"suppressions": [{"kind": "inSource"}]`.
It does **not** carry the comment text into SARIF, so the summary's
justification column stays empty for semgrep. The justification lives in the
code, where review sees it. Rules for writing a suppression:

- **Always name the rule ID.** A bare `# nosemgrep` silences every rule on
  the line, including ones nobody reviewed.
- **Always give a reason and a tracking issue.** An unexplained suppression
  is not a reviewed exception.
- **Treat suppressions as reviewable code.** A suppression is visible in the
  diff and in the scan summary, so reviewers can challenge it like any other
  change.

Other analyzers are honored as long as their SARIF carries a `suppressions`
entry. Trivy's `.trivyignore` works differently: it drops the finding before
SARIF is written, so ignored findings never appear in the summary at all.

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

**Exception: repos with `[package]`.** A repo whose `vergil.toml` has a
`[package]` section MUST call `cd-release` with `secrets: inherit`,
suppressed inline:

```yaml
    secrets: inherit  # nosemgrep: yaml.github-actions.security.secrets-inherit.secrets-inherit
```

Its `package-sign` job reads `PACKAGE_SIGNING_KEY` and
`PACKAGE_SIGNING_PASSPHRASE` from the caller's `package-signing`
environment. Environment secrets reach a job in a cross-repo reusable
workflow only through `inherit`: an explicit secrets map leaves
`package-sign` with an empty key (proven by the probe in
[vergil-project/packages#6](https://github.com/vergil-project/packages/issues/6)).
The callee is first-party, so the blanket form is an accepted, reviewed
exception. `vrg-repo-init` generates this caller for `[package]` repos
([vergil-project/vergil-tooling#3144](https://github.com/vergil-project/vergil-tooling/issues/3144)).
See the [CD section of the package config reference](../reference/package-config.md#cd)
for the full caller.

### Dynamic matrix from `vergil.toml`

The matrix is **not** embedded in the workflow or passed as an input. Each
matrixed reusable workflow (`ci-audit`, `ci-quality`, `ci-test`) reads
`[ci].versions` from the consuming repo's `vergil.toml` at run time and fans its
matrix out over that list. Single-container jobs (`ci-security`,
`ci-version-bump`, `ci-docs`) run on the **primary version** —
`[ci].primary-version` if it is set, otherwise the highest entry of
`[ci].versions` (so `3.14` for `["3.12", "3.13", "3.14"]`). `vergil.toml` is the
single stored source of the version set; nothing is hand-maintained in `ci.yml`,
so the matrix cannot drift from `[ci].versions`.

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

The docs-build job is a universal reusable-CI job emitted on every
managed repo with no `if:` guard, so it always runs and surfaces the
`docs / docs` check.

### Every gate is required — there are no optional PR gates

Every check that can gate a PR is configured as a **required status
check** on the target branch; there is no tier of checks that runs but
does not block. `docs / docs` is required alongside the tests, audit,
release gates, and the security checks — the desired required-check set is
complete, and it is pinned by a test so it cannot silently drift.

For the matrixed kinds — audit, quality (lint + typecheck), and unit tests —
the required check is the **stable, version-agnostic `<kind> / evidence`
aggregate** each reusable workflow emits (`audit / evidence`,
`quality / evidence`, `test / evidence`), **not** the per-version legs
(`audit / dependencies / 3.12`, `quality / lint / 3.13`, …). Each `evidence`
job `needs` the whole version matrix, so a single required context covers every
version. Because the required-check *names* no longer carry a version, a matrix
change — including a *reduction* — merges through the same gate. This closes the
old deadlock, where branch protection required a per-version leg that a reduced
matrix could never produce, leaving the PR "expected, never reported" and
permanently blocked with no `--admin` escape (epic
[vergil-project/.github#338](https://github.com/vergil-project/.github/issues/338)).
Non-matrixed checks (the security scanners, `quality / common`, the version-bump
gate, `docs / docs`) keep their fixed, version-free names. A repo whose
`vergil.toml` has a `[package]` section also requires `package / evidence`, the
one stable gate `ci-package.yml` emits over its whole target matrix; repos
without `[package]` neither call `ci-package.yml` nor require the gate.

`vrg-github-repo-config audit` **hard-fails on required-set drift**: if a
repo's configured required checks diverge from the desired set, the audit
fails rather than reporting a warning. `vrg-release` gates on that audit,
so a repo whose required-check set has drifted cannot release until the
set is reconciled.

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

**`dev-cpp-clang`** (19, 20)
:   Base: `debian:trixie-slim` + `apt.llvm.org`. Includes clang,
    clang-tidy, clang-format, lld, llvm (`llvm-cov`), the sanitizer
    runtime, CMake, Conan 2, cppcheck, and gcovr.

**`dev-cpp-gcc`** (13, 14)
:   Base: `debian:trixie-slim`. Includes g++, gcov, CMake, Conan 2,
    cppcheck, and gcovr.

**`dev-ts-node`** (22, 24)
:   Base: `debian:trixie-slim` + prebuilt Node from NodeSource (never
    built from source). Includes Node.js, npm, TypeScript (`tsc`),
    ESLint + typescript-eslint, Prettier, Vitest, `@vitest/coverage-v8`,
    and license-checker. The runtime family rides the `ts-node` image
    suffix and the Node major is the tag (`node-24` → `prod-ts-node:24`);
    `node-24` is the primary, `node-22` the second (epic
    [vergil-project/.github#284](https://github.com/vergil-project/.github/issues/284)).

C++ is the one language with a compiler-family axis: it ships **two**
image families (`dev-cpp-clang` / `dev-cpp-gcc`, and matching `prod-`
images) so TYPECHECK and TEST run per compiler and per version. The
family rides the `[ci].versions` tag prefix (`clang-20`, `gcc-14`).

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

### Repo-specific system packages

The shared base images stay language-generic on purpose — a system dependency is
treated as a property of the **language**, not the repo, which is what keeps the
image cache sound. A repo that genuinely needs a Debian binary its language image
does not carry (for example a repo whose render/e2e tests shell out to LilyPond)
declares it once in `vergil.toml`, **without** touching the shared base image:

```toml
[container]
system-packages = ["lilypond"]
```

The full key reference — trust model, the names-only surface, cache-key
behaviour, and the fail-closed error — lives in
[Container Config Reference → `system-packages`](../reference/container-config.md#system-packages).
Two things matter for the CI/container model here:

- **Two paths, one declaration.** Locally the packages are **baked** into the
  per-branch cached dev image, so they are present for every check in that one
  image. In CI they are **installed per run** by a composite setup step
  (vergil-actions#807) on the base-image container, before the tests. The two
  paths agree on *what* is installed (the same names, from the same Debian
  sources) even though they differ on *when* — baked once locally vs installed
  per run in CI.
- **Test-runtime contract.** In CI the install runs **only on the jobs that
  execute the repo's tests**, never on lint or typecheck — these binaries are
  needed at test time only. Locally the single cached image carries them for
  every check as a convenience of the one-image model, but that does **not**
  license depending on a system package during lint or typecheck. If a package
  has no install candidate for the build architecture, the build (local) and the
  CI step both **fail closed**, naming the package and the architecture — no
  silent skip.

### Repo-specific build steps

For a dependency that is **not** an apt package — a JS library a test driver
consumes at runtime, say — a repo declares a `build-command` (with optional
`build-cache-files`) instead of `system-packages`:

```toml
[container]
build-command = "npm install -g @coderline/alphatab"
build-cache-files = ["package-lock.json"]
```

The full key reference — the out-of-workspace contract, the `NODE_PATH` handling
for baked npm libraries, the trust model, and the cache-key behaviour — lives in
[Container Config Reference → `build-command`](../reference/container-config.md#build-command).
It follows the **same two-paths, one-declaration** shape as `system-packages`:

- **Two paths, one declaration.** Locally the command is **baked** into the
  per-branch cached dev image, so its artifact is present for every check in that
  one image. In CI the command is **run per job** on the test jobs — the job
  obtains it with `vrg-container-build-command --script` and runs it against the
  base-image container before the tests. The two paths agree on *what* is
  provisioned (the same command string) even though they differ on *when* — baked
  once locally vs run per test job in CI.
- **Fail-closed.** A non-zero exit from the command **fails the build** (local)
  and the CI step, rather than producing an image whose missing dependency only
  surfaces mysteriously inside a test.
