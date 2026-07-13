# CI Evidence Convention

This guide is the **contract** for the CI evidence archival mechanism: the
naming convention, file schemas, and bundle format that any release-publishing
repo — or any future CI gate — conforms to. It is the authoritative reference
for the `ci-evidence-<gate>` artifact, the `evidence.json` fragment, the
evidence-producing gate set, and the `manifest.json` bundle index.

For the motivation, the determinism analysis, and the full architecture, see
epic vergil-project/.github#140. This page documents the wire format only.

## Table of Contents

- [Overview](#overview)
- [Producer side: the `ci-evidence-<gate>` artifact](#producer-side-the-ci-evidence-gate-artifact)
- [The `evidence.json` fragment schema](#the-evidencejson-fragment-schema)
- [The evidence-producing gate set](#the-evidence-producing-gate-set)
- [Producer prerequisite: real report files](#producer-prerequisite-real-report-files)
- [The bundle tree](#the-bundle-tree)
- [The `manifest.json` schema (v1.0)](#the-manifestjson-schema-v10)
- [Verification: how a downstream auditor checks the bundle](#verification-how-a-downstream-auditor-checks-the-bundle)
- [Deployment lifecycle](#deployment-lifecycle)

## Overview

CI evidence archival makes each release's gate output **durable, complete, and
machine-verifiable**. At publish time the release harvests every CI gate's full
report, bundles it into a compressed, self-describing archive, cryptographically
attests the archive to the pipeline and the exact released commit, and attaches
it to the GitHub Release — where it lives permanently, independent of Actions
retention.

The mechanism has **two sides coupled only by a naming convention** — no direct
calls — which is what keeps the harvester language-agnostic and the whole
mechanism fleet-wide:

- **Producer side** (CI gates in `vergil-actions`). Each gate reusable workflow
  uploads its full report(s) as a workflow-run artifact named
  `ci-evidence-<gate>`, alongside a small `evidence.json` fragment.
- **Consumer side** (publish-time harvest in `vergil-tooling`). A single
  language-agnostic command, `vrg-ci-evidence`, downloads every
  `ci-evidence-*` artifact, validates completeness, assembles the bundle tree
  plus a `manifest.json` index with per-file `sha256`, and produces
  `v{version}-ci-evidence.tar.gz`.

The **only** cross-repo coupling is the `ci-evidence-*` artifact name and the
`evidence.json` shape. Everything else is derived. Adding a future gate requires
no harvester change: it emits the convention and is picked up automatically.

!!! note "Scope boundary"
    The evidence step lives inside `cd-release`, so it applies to — and only to
    — repos that publish releases through `cd-release`. Repos that do not cut
    package releases (`vergil-project/.github`, docs-only repos) never invoke
    it. "Fleet-wide" here means "every release-publishing repo," not literally
    every repository.

## Producer side: the `ci-evidence-<gate>` artifact

Each evidence-producing gate uploads exactly one workflow-run artifact:

- **Artifact name:** `ci-evidence-<gate>` — for example `ci-evidence-security`,
  `ci-evidence-test`, `ci-evidence-audit`, `ci-evidence-quality`.
- **Contents:** the gate's full report files (SARIF, coverage XML/HTML, JUnit
  XML, audit/license JSON, SBOM, …), plus an `evidence.json` fragment at the
  artifact root describing what the gate ran and found.

### One uniform evidence source — always artifacts

Every gate — **including the security scanners** — uploads its report(s) as a
`ci-evidence-<gate>` artifact **unconditionally**, decoupled from any
code-scanning upload. Under GitHub Advanced Security, CodeQL still drops its
SARIF as an artifact alongside its code-scanning upload; Trivy and Semgrep do
the same regardless of GHAS availability.

The harvester therefore reads **artifacts only** and never touches the
code-scanning API — one code path, no GHAS branching, one fewer external
dependency that can fail. Severity metrics come from each gate's `evidence.json`
fragment (computed from its own SARIF at gate time), never from a server API.

## The `evidence.json` fragment schema

The `evidence.json` fragment sits at the root of each `ci-evidence-<gate>`
artifact. It is the gate's self-description:

```json
{
  "gate": "security",
  "tools": [{ "name": "codeql", "version": "..." }],
  "metrics": { "findings_by_severity": { "critical": 0, "high": 0 } },
  "files": ["codeql.sarif", "trivy.sarif", "semgrep.sarif"]
}
```

| Field     | Type              | Meaning                                                        |
| --------- | ----------------- | ------------------------------------------------------------- |
| `gate`    | string            | The gate name — matches the `<gate>` in the artifact name.    |
| `tools`   | array of objects  | Each tool that ran, with `name` and `version`.                |
| `metrics` | object            | Gate-specific summary (e.g. `findings_by_severity`, `coverage_pct`, `tests`). |
| `files`   | array of strings  | The report file names present in the artifact.                |

If the fragment is absent, the harvester still bundles the raw files and records
the gate's conclusion from the check-runs API — but for a required gate an
absent fragment or an absent artifact counts against completeness (see the
[deployment lifecycle](#deployment-lifecycle)).

## The evidence-producing gate set

The set of gates that MUST emit evidence is **not a hand-maintained list.** It
is **derived from the same source of truth that drives branch protection**:
`lib/github_config.py:desired_ci_gates_ruleset()` computes a repo's required
status checks from its `VergilConfig` (language, `[ci]` versions, GHAS
availability). The evidence layer consumes that *same* computation, so the gates
that are **enforced to merge** and the gates that are **required to have
evidence** are provably the same set, with no drift.

This is the load-bearing invariant: management of the required gates and
collection of their auditing evidence come from **common configuration code**.

### Classification by check-name prefix

Required status checks are grouped into evidence gates by their check-name
prefix:

| Check name / prefix                                | Evidence gate | Evidence-producing? |
| -------------------------------------------------- | ------------- | ------------------- |
| `security / …`, plus GHAS `Trivy` / `Semgrep OSS` / `CodeQL` | `security`    | Yes                 |
| `test / …`                                         | `test`        | Yes                 |
| `audit / …`                                        | `audit`       | Yes                 |
| `quality / …` (lint, typecheck)                    | `quality`     | Yes                 |
| `version / …`                                      | —             | No (non-blocking)   |
| `docs`                                             | —             | No (low-signal)     |

The guiding principle: **any gate that can block the build is evidence worth
keeping.** Quality (lint/typecheck) sits alongside security, test, and audit as
first-class evidence. `version/` is a sanity check on version state, not
substantive evidence, and `docs` is low-signal; the absence of either does not
fail the release.

### Per-repo correctness for free

Because the set is *derived*, a repo with a different real profile — no GHAS
(CodeQL not required), a non-Python stack, or a gate legitimately disabled —
demands evidence for exactly the gates it actually gates on. It cannot
spuriously fail for a gate it never ran. Adding a future required gate
automatically pulls it into the evidence set via the shared config; the
harvester never changes.

## Producer prerequisite: real report files

The evidence layer only has value if the gates emit **machine-readable report
files with real data.** A bundle whose `test` / `audit` / `quality` entries
carry only an `evidence.json` envelope and no report is an **empty report — and
there is no point publishing empty reports.**

Producers must therefore emit real report files at the workspace-root paths the
producer composite globs for:

| Gate      | Required report file(s)                       |
| --------- | --------------------------------------------- |
| `test`    | `coverage.xml`, `junit.xml`                   |
| `audit`   | `pip-audit.json`, `licenses.json`             |
| `quality` | the quality tool's machine-readable output    |
| `security`| `codeql.sarif`, `trivy.sarif`, `semgrep.sarif`|

Historically the check registry ran `pytest --cov … --cov-fail-under=100` with
no `--cov-report=xml` / `--junitxml`, and `pip-audit` / license checks with no
`--output` — pass/fail only, no persisted report. Closing that is in scope and
blocking for the mechanism: **no bundle is attached until its reports carry real
data.** A producer that has not yet been updated to write these files is not
ready to contribute evidence.

## The bundle tree

The harvester assembles a single compressed archive,
`v{version}-ci-evidence.tar.gz`, designed to be consumed by a machine auditor —
self-describing and verifiable:

```text
v{version}-ci-evidence.tar.gz
└─ evidence/
   ├─ manifest.json          # top-level machine index (see below)
   ├─ checks.json            # raw check-runs snapshot (name, conclusion, timing, log URL)
   ├─ gates/
   │   ├─ security/          # codeql.sarif, trivy.sarif, semgrep.sarif, evidence.json
   │   ├─ test/              # coverage.xml, htmlcov/, junit.xml, evidence.json
   │   ├─ audit/             # pip-audit.json, licenses.json, evidence.json
   │   └─ sbom/              # sbom.cdx.json
   └─ README.md              # human orientation for the archive
```

- `manifest.json` is the curated, per-gate machine index (schema below).
- `checks.json` is the **raw** check-run snapshot — distinct from the manifest's
  curated view — capturing every check's name, conclusion, timing, and log URL.
- `gates/sbom/` is populated by copying the SBOM already built during publish
  (also a standalone Release asset) into the bundle at harvest time, so the
  archive stays fully self-contained.
- `README.md` is a fixed human orientation for the archive.

A standalone copy of `manifest.json` is also attached as a small, separate
Release asset, so a tool can read the summary without downloading the full
tarball.

## The `manifest.json` schema (v1.0)

`manifest.json` is the top-level machine index. It records the release identity,
the provenance anchor, and a per-gate summary with per-file `sha256`:

```json
{
  "schema_version": "1.0",
  "repo": "vergil-project/vergil-tooling",
  "release": {
    "version": "2.1.129",
    "tag": "v2.1.129",
    "released_commit": "<main merge SHA>"
  },
  "provenance": {
    "release_pr": 2281,
    "validated_head_sha": "<PR head SHA>",
    "ci_run_urls": ["https://github.com/.../actions/runs/123"]
  },
  "generated_at": "<ISO-8601, injected by CD>",
  "gates": [
    {
      "name": "security",
      "conclusion": "success",
      "tools": [{ "name": "codeql", "version": "..." }],
      "metrics": { "findings_by_severity": { "critical": 0, "high": 0 } },
      "files": [{ "path": "gates/security/codeql.sarif", "sha256": "..." }]
    },
    {
      "name": "test",
      "conclusion": "success",
      "metrics": { "coverage_pct": 100, "tests": 1423 },
      "files": [{ "path": "gates/test/coverage.xml", "sha256": "..." }]
    }
  ],
  "missing_gates": []
}
```

| Field            | Meaning                                                                 |
| ---------------- | ----------------------------------------------------------------------- |
| `schema_version` | Manifest schema version. This document specifies `"1.0"`.               |
| `repo`           | `owner/name` of the release-publishing repo.                            |
| `release`        | The released `version`, git `tag`, and `released_commit` (the `main` merge SHA). |
| `provenance`     | The evidence anchor: the `release_pr`, its `validated_head_sha`, and the `ci_run_urls` the evidence was harvested from. |
| `generated_at`   | ISO-8601 timestamp, injected by the CD environment.                     |
| `gates`          | Per-gate record: `name`, `conclusion`, `tools`, `metrics`, and `files` (each with `path` and `sha256`). |
| `missing_gates`  | Names of required gates that produced no evidence — recorded as data, never silently dropped. |

Two trust properties are load-bearing:

1. **Every file carries a `sha256`.** An auditor can prove the archive is intact
   and unmodified.
2. **`missing_gates` is explicit.** A required gate that produced no evidence is
   recorded as data, never silently dropped.

### Why the provenance anchor is the release PR, not the released commit

`develop → main` promotion uses a **merge commit**, and the security/audit/test
gates run on **pull requests**, not on pushes to `main`. The final `main`
merge-commit SHA therefore has no gate check-runs anchored to it. The gates *do*
run on the **release PR**, whose validated tree is identical to what lands on
`main`. Evidence is thus anchored to `provenance.validated_head_sha` (the release
PR head) and harvested at publish time while the PR's runs and artifacts are
still fresh. `released_commit` records what shipped; `validated_head_sha` records
what was validated — they name the same tree.

## Verification: how a downstream auditor checks the bundle

An auditor who pulls a release months later verifies the bundle in two steps:

**1. Verify the attestation (chain of custody).** After the bundle is assembled,
the pipeline produces a **build-provenance attestation over the bundle's
digest** (`actions/attest-build-provenance`), binding the archive to this
repository's workflow and the released commit. Verify it with:

```bash
gh attestation verify v2.1.129-ci-evidence.tar.gz \
  --repo vergil-project/vergil-tooling
```

A successful verification turns "here is a tarball" into "here is a tarball
cryptographically proven to be the genuine output of this pipeline for this
commit." This is the chain-of-custody core of the mechanism.

**2. Verify every file's integrity.** Unpack the archive and confirm each file's
`sha256` matches its `manifest.json` entry:

```bash
tar -xzf v2.1.129-ci-evidence.tar.gz
cd evidence
# For each gate file listed in manifest.json:
sha256sum gates/security/codeql.sarif
# compare against manifest.json → gates[].files[].sha256
```

Every file listed in the manifest carries a `sha256`, so an automated auditor
can confirm the archive is intact, then read `gates[].conclusion` and
`gates[].metrics` to confirm what ran, with what result, against which commit —
and that `missing_gates` is empty.

## Deployment lifecycle

The evidence step is a **hard gate**, introduced in **warning mode** and
promoted to **enforcing mode** only once it is proven reliable in production:

- **Warning mode (initial).** The step runs the full path — harvest → bundle →
  attest → attach — but on any failure it emits a loud warning and the release
  proceeds, attaching whatever evidence it gathered. It is timeout-bounded and
  never aborts a release.
- **Enforcing mode (end state).** A single, global, human-gated flag flip
  promotes the gate: evidence-first ordering, substantive incompleteness is
  terminal, and nothing publishes without complete attested evidence.

Warning mode is a temporary **deployment state of a hard gate — not a permanent
soft gate.** A permanent soft/report-only gate remains rejected. For the full
lifecycle, the bake-in window, and the human-gated flip, see epic
vergil-project/.github#140 (§9.2, §14.1).

This safety property is load-bearing on the **all-hard-gates model**: bundling
everything onto a public Release asset is safe precisely because every gate is a
hard gate, so a bundle by definition reflects a passing scan with no
unremediated findings to expose. For why every check that matters is a hard,
asserting gate — and there are no report-only/warning gates — see the
all-hard-gates principle in the `vergil-actions` documentation.
