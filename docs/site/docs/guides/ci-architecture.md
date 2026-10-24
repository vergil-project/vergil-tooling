# CI Architecture

This guide explains the continuous integration model used by every
Vergil-managed repository, and how a repository gets it.

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
| 1 | Manual (before commit) | Seconds to minutes | No |
| 2 | Pull request | Minutes | Yes |

- **Tier 1**: Single version, full local validation, dockerized
- **Tier 2**: Full version matrix, all checks, security uploads

The goal is fast local feedback for the developer and comprehensive gated
checks on the PR itself. Tier 1 is a discipline, not a gate: `vrg-commit`
checks the branch and commit context but does not run validation, so run
Tier 1 yourself before each commit. If you do, the PR should already be
green when it opens.

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
vrg-container-run -- vrg-validate
```

This is the single Tier 1 command. `vrg-container-run` starts one
container and runs `vrg-validate` inside it, with the repository
bind-mounted. The image is
`ghcr.io/vergil-project/<prefix>-<language>:<version>`: the prefix is
`prod` unless you pass `--prefix dev`, the language comes from
`[project].primary-language`, and the version is the resolver's
primary version: `[ci].primary-version` when it is set, otherwise the
newest resolved version (see [Version resolution](#version-resolution)).
C++ and TypeScript carry a family in the image
name instead (see [Dev container images](#dev-container-images)), and a
repository with no language uses the base image. On top of that image,
`vrg-container-run` builds a per-branch cached image with
vergil-tooling installed. Set `DOCKER_DEV_IMAGE` to use a different
image.

`vrg-validate` first runs the common checks: `vrg-repo-profile`,
markdownlint, shellcheck, yamllint, hadolint, actionlint, and
ansible-lint when the repository has Ansible content. It then runs the
language's lint, typecheck, test and audit commands. To run one stage,
pass `--check common`, `lint`, `typecheck`, `test` or `audit`. The
[Validation Matrix](validation-matrix.md) lists every check and where
it runs.

A repository can change the in-container command with
`[validation].container-command` in `vergil.toml`. vergil-tooling
uses this to run its own unreleased code (`uv run vrg-validate`).

!!! note "Python tests run in parallel by default"
    For Python repos the validation gate's test stage runs in parallel via
    pytest-xdist's work-stealing scheduler — the shared command appends
    `-n <workers> --dist worksteal` to the coverage-gate argv when
    pytest-xdist is installed in the project's test environment. This is
    the fleet-wide default and materially cuts test-stage wall-clock —
    roughly 3.2× on this repo's own suite (epic
    [vergil-project/.github#333](https://github.com/vergil-project/.github/issues/333)).
    The worker count defaults to `auto` and is tunable per validation
    context (`ci`, `local-vm`, …) with `[test].workers` and
    `[test.<context>]`. A repo with an order-dependent suite opts out with
    `[test].parallel = false` in its `vergil.toml`; see
    [Test Config (`[test]`)](../reference/test-config.md) for the knobs and
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

**What runs** (each item is one job in `ci.yml`, calling one
`vergil-actions` reusable workflow):

- `audit` (`ci-audit.yml`): dependency audit across the full version
  matrix, for languages that have an audit command
- `quality` (`ci-quality.yml`): the common checks once (`vrg-validate
  --check common`), then lint and typecheck across the full version
  matrix
- `security` (`ci-security.yml`): CodeQL, Trivy and Semgrep
- `test` (`ci-test.yml`): the language's test command across the full
  version matrix
- `version` (`ci-version-bump.yml`): fails when the PR's version equals
  the version on `main`. It is a no-op before the first release, and it
  passes when `[project].release-model` is `none`
- `docs` (`ci-docs.yml`): builds the docs site, only in repos with
  `[publish].docs` enabled
- `package`: binary package build and install-test (`ci-package.yml`, only in repos
  whose `vergil.toml` has `[package]`). Feature PRs run the **reduced**
  tier: every build cell, but one install-test cell per format (the oldest
  selected release, amd64 first). Release PRs (`release/*` into `main`)
  and non-PR events run the **full** matrix. See
  [Tiers: full and reduced](../reference/package-config.md#tiers-full-and-reduced).

The workflow file is `.github/workflows/ci.yml`. It runs on
`pull_request` and is also exposed through `workflow_call` (see
[Thin-caller pattern](#thin-caller-pattern)).

!!! warning "Local validate is not full coverage parity"
    Tier 1 runs one dev container on a single Python interpreter (currently
    3.15, the newest window version), so its `--cov-fail-under=100` gate proves 100% coverage on that
    one interpreter only. PR-CI re-runs the test-and-coverage gate
    **independently in a separate container per resolved matrix version**
    (currently 3.12, 3.13, 3.14 and 3.15: this repo is a `library` with no
    `[ci].versions` pin, so it tests the whole version window). Because
    branch coverage (`--cov-branch`) can
    legitimately differ across CPython versions — a branch reachable on one
    version may be dead on another, or a version-guarded code path may only
    execute on some interpreters — code that measures 100% locally can still
    fall below 100% on a 3.12, 3.13 or 3.14 leg and fail CI. The default
    local validate covers a single interpreter, trading the matrix away for
    speed. For Python, `vrg-container-run -- vrg-validate --all-versions`
    closes the gap on demand: inside the same one container it runs the
    gate on every resolved version, each on a uv-managed interpreter (see
    [All versions](validation-matrix.md#all-versions-python)). For other
    languages the multi-version matrix stays a **PR-CI-only** gate.

## Architecture

### Thin-caller pattern

A consuming repo's `ci.yml` is a **thin caller**: each job simply `uses:` a
`vergil-actions` reusable workflow (`ci-audit`, `ci-quality`, `ci-security`,
`ci-test`, `ci-version-bump`, `ci-docs`, `ci-package`) pinned to the repo's
vergil series tag (for example `@v2.1`; `vrg-github-repo-init` uses the
`vX.Y` series of the installed vergil-tooling). Apart from the scope
inputs below, it passes only `language:` and `container-suffix:`, and
`ci-docs` and `ci-package` take no inputs. It does **not** pass a version matrix
or a container tag — the reusable workflows run `vrg-ci-resolve` against the
repo's `vergil.toml` at run time and take both the matrix and the
primary-version container from it (epics
[vergil-project/.github#338](https://github.com/vergil-project/.github/issues/338)
and [vergil-project/.github#407](https://github.com/vergil-project/.github/issues/407)).
A window change or a `[ci].versions` change therefore takes effect with no
edit to `ci.yml`.

`ci.yml` also declares `workflow_call` with one boolean input,
`run-security`, defaulting to `true`. On a `pull_request` run the input
is empty, so the generated `ci.yml` compares it with `!= 'false'` and it
stays on. `run-security` is forwarded to `ci-security.yml`. The security
job passes no `container-suffix:`, because `ci-security.yml` ignores it.
The generated `ci.yml` no longer declares or forwards `run-release`: the
version gate in `ci-version-bump.yml` follows `[project].release-model`
instead.

### Shared security workflow

The security scanners live in the reusable workflow
`vergil-project/vergil-actions/.github/workflows/ci-security.yml`. It
runs three scanner jobs and one aggregate. Because the caller's job is
named `security`, the checks report as:

- `security / codeql`
- `security / trivy`
- `security / semgrep`
- `security / evidence`

vergil-tooling's own `ci.yml` calls it like this:

```yaml
security:
  uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@v2.1
  permissions:
    contents: read
    security-events: write
    actions: read
  with:
    language: python
    run-security: ${{ inputs.run-security != 'false' }}
    container-suffix: python
```

CodeQL is skipped when `run-codeql` is `false` (set for languages
CodeQL does not support) or when `upload-sarif` is `false` (set for
private repositories without GitHub Advanced Security).
`container-suffix` is a deprecated no-op here, and the old
standards-compliance job is gone: PR issue linkage is now checked by
`vrg-submit-pr` when it builds the PR body.

!!! tip "Semgrep rulesets"
    `vrg-semgrep-scan` maps the language to a registry ruleset:
    `python`, `java` and `rust` map to `p/<language>`, and `go`
    maps to `p/golang`. It adds `p/dockerfile` when the repository has
    Dockerfiles and `p/github-actions` when it has workflows.
    `ci-security.yml` adds `p/c` for `cpp` and `p/typescript` for
    `typescript`.

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

### Version resolution

The matrix is **not** embedded in the workflow or passed as an input. The
supported versions of each language live in one place: the version manifest in
[vergil-containers](https://github.com/vergil-project/vergil-containers), which
publishes them as `docker/versions/window.json` at every Vergil ref (the
*version window*). The lifecycle job in vergil-containers adds versions to the
window and drops them from it as they enter and leave support; consuming repos
do not edit anything when the window moves.

One resolver (`src/vergil_tooling/lib/versions/resolve.py`) turns a repo's
`vergil.toml` and the window into the versions it builds. It reads the window at
the repo's `[dependencies].vergil` ref (for example `v2.1`), so a window change
reaches the repo without a tooling release. The dev container, `vrg-validate`,
the required-check computation and the CI setup step (through
[`vrg-ci-resolve`](../reference/cli-tools-overview.md)) all call it. The rules:

- `[project].repository-type = "library"` resolves as a **library** and builds
  every window version. Every other type resolves as an **app** and builds one
  version, the newest in the window.
- `[ci].versions` is an optional pin. Every pinned version must be in the
  window, and an app pin must name exactly one version; either violation is an
  error. C++ and TypeScript versions carry a family (`clang-20`, `node-24`), so
  those repos must pin.
- The **primary version** is `[ci].primary-version` when it is set (it must name
  a resolved version), otherwise the newest resolved version.

Each matrixed reusable workflow (`ci-audit`, `ci-quality`, `ci-test`) fans out
over the resolved versions. Single-container jobs (`ci-security`,
`ci-version-bump`, `ci-docs`) run on the primary version. Nothing is
hand-maintained in `ci.yml`, so the matrix cannot drift from the window.
Set `VRG_VERSION_WINDOW=<path>` to resolve against a local `window.json` for
deliberate offline work; a window that cannot be fetched is an error, never a
silent default.

## Implementation guide

A repository does not write its CI by hand. `vrg-github-repo-init`
generates it, for a new repository or for an existing one with
`--adopt`:

- **`ci.yml`** is rendered as the thin caller described above. Its jobs
  follow the language: `audit` only when the language has an audit
  command, `test` only when it has a test command or
  `[ci].integration-tests` is on, `docs` only when `[publish].docs` is
  on, `version` only when `[project].release-model` is not `none`, and
  `package` only when `vergil.toml` has `[package]`.
- **The `CI gates` ruleset** is applied to `main` and `develop` with
  the required checks listed in [CI gates](#ci-gates). Before it is
  applied, the tool checks that the generated `ci.yml` produces every
  required check and fails if it does not.

After that, `vrg-github-repo-config` keeps the repository in line:
`audit` reports drift, `diff` shows it, and `apply` reconciles the
GitHub side, including the ruleset. `vrg-container-run -- vrg-validate`
is the local command. Nothing else needs to be added to the repository.

## CI gates

The `CI gates` ruleset requires these checks. The list is computed from
`vergil.toml`:

| Check | Required when |
| ----- | ------------- |
| `quality / common` | Always |
| `security / trivy`, `security / semgrep` | Always |
| `docs / docs` | `[publish].docs` is on |
| `audit / evidence` | The language has an audit command |
| `quality / evidence` | The language has a lint or typecheck command |
| `test / evidence` | The language has a unit test command |
| `version / version-bump` | `[project].release-model` is not `none` |
| `package / evidence` | `vergil.toml` has `[package]` |
| `security / codeql`, `CodeQL` | GHAS is available and CodeQL supports the language |
| `Trivy`, `Semgrep OSS` | GHAS is available |
| `test / integration / <version>` | `[ci].integration-tests` is on (one per resolved version) |

GHAS (GitHub Advanced Security) is taken from `[project].ghas` when it
is set. Otherwise it counts as available for any repository that is not
private. The bare `CodeQL`, `Trivy` and `Semgrep OSS` checks come from
GitHub code scanning, not from a workflow job. Without GHAS those checks
never report, so they are not required.

### Every check that runs is a gate

There is no tier of checks that runs on a PR but does not block it. The
required set above is pinned by a test so it cannot drift silently.

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

Published to `ghcr.io/vergil-project/<prefix>-<language>:<version>` from
the [vergil-containers](https://github.com/vergil-project/vergil-containers)
repository. The `dev-` images are built from its `develop` branch and
the `prod-` images from `main`. `vrg-container-run` and the CI
workflows use `prod-` by default.

### Available images

This page does not repeat the published versions of each image. The
generated version table in the `vergil-containers`
[README](https://github.com/vergil-project/vergil-containers/blob/develop/README.md#available-images)
is the single source of truth.

**`dev-python`**
:   Base: `python:<v>-slim`. Includes git, curl, uv.

**`dev-java`**
:   Base: `eclipse-temurin:<v>-jdk`. Includes git, curl.

**`dev-go`**
:   Base: `golang:<v>`. Includes golangci-lint,
    govulncheck, go-licenses, gocyclo.

**`dev-cpp-clang`**
:   Base: `debian:trixie-slim` + `apt.llvm.org`. Includes clang,
    clang-tidy, clang-format, lld, llvm (`llvm-cov`), the sanitizer
    runtime, CMake, Conan 2, cppcheck, and gcovr.

**`dev-cpp-gcc`**
:   Base: `debian:trixie-slim`. Includes g++, gcov, CMake, Conan 2,
    cppcheck, and gcovr.

**`dev-ts-node`**
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
family rides the `[ci].versions` tag prefix (`clang-20`, `gcc-14`), so a
C++ repo must pin `[ci].versions`. Only a `library` can pin more than one
entry; an app builds exactly one.

### Building locally

```bash
cd ../vergil-containers
docker/build.sh
```

This builds all images. Individual images can be built with:

```bash
docker/generate.sh python
docker build --build-arg PYTHON_VERSION=3.14 \
  -t dev-python:3.14 docker/python/
```

### Publishing

Images are published automatically on push to `develop` (`dev-`) or
`main` (`prod-`) in the `vergil-containers` repository: its `cd.yml`
calls the reusable `.github/workflows/cd-docker-publish.yml` workflow.

### Design principles

- **Thin images** — language runtime + package manager + git/curl
- **Project-managed dependencies** — tools come from lockfiles at
  container startup (e.g., `uv sync`, `go install`)
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
