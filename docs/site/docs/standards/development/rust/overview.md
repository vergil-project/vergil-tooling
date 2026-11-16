# Rust Development Standards Overview

## Purpose

Define consistent Rust standards that emphasize safety, readability,
maintainability, and long-term survivability across repositories.

## Core Principles

- The Rust API Guidelines and standard library conventions are the default and
  highest priority.
- Safety and correctness override cleverness or brevity.
- Readability overrides micro-optimization.
- Exceptions must be explicit, documented, and justified.

## Tooling Expectations

- Lockfile: `Cargo.lock` is committed and enforced, for libraries as well as
  applications, following Cargo's guidance since 2023. Every gate command that
  reads it (fetch, clippy, check, the coverage run, the doctests and the audit,
  and the release build and publish) runs with `--locked`, so it is used as it
  is and never re-resolved. `vrg-validate` fails a repository with no
  `Cargo.lock` at install, naming the file and how to create it
  (`cargo generate-lockfile` in the container, then commit it). A
  `Cargo.lock` that no longer matches `Cargo.toml` fails the install with
  cargo's own `--locked` error. The dev image's dependency warmup also runs
  with `--locked` and never writes `Cargo.lock`: with no lockfile it is
  skipped, and with a stale one it prints
  `warmup skipped: Cargo.lock does not match Cargo.toml` and the image builds
  unwarmed, so the lockfile can still be created or refreshed in the
  container.
- Formatting: rustfmt (canonical; any formatting configuration goes in
  `rustfmt.toml`).
- Linting: clippy (official Rust linter, 800+ lint rules), run on all targets
  (`cargo clippy --all-targets --locked -- -D warnings`), so the library, binaries,
  tests, examples and benches are linted and any warning fails the gate.
- Type checking: `cargo check` (inherent in the Rust compiler).
- Dependency audit and license compliance: cargo-deny (advisories, licenses,
  bans, and sources in a single tool), run against the fleet policy that
  vergil-tooling packages, so a repo's own `deny.toml` is not used. The
  gating run writes its JSON diagnostics to `cargo-deny.json`, the audit
  gate's evidence.
- Testing: cargo-nextest runs the unit and integration tests and writes a
  JUnit report to `junit.xml`. Rustdoc examples run separately with
  `cargo test --doc`, because nextest does not run doctests.
- Coverage: cargo-llvm-cov (LLVM instrumentation, cross-platform) measures
  that nextest run, held to 100% line coverage. Its report writes Cobertura
  XML to `coverage.xml`. `coverage.xml` and `junit.xml` are the test gate's
  evidence.
- If a repository uses different tools, document the reason and equivalents.

## Toolchain Selection

Vergil selects the Rust toolchain, never the repository. Each Vergil Rust
image pins its own installed release, so every CI leg and dev container runs
the `x.y` series that Vergil resolved from the version window (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)).

- A repository declares only an `x.y` floor, through `rust-version` in its
  root `Cargo.toml` (`[package]`, or `[workspace.package]` for a workspace).
  `vrg-validate` warns when the floor falls below the window's oldest
  series.
- A `rust-toolchain.toml` or legacy `rust-toolchain` file is an error in
  `vrg-validate`. Delete it.
- A component or target the image lacks is added to the image through a
  vergil-containers change. A repository never selects it through a toolchain
  file, and is never exempted from the check.

## CI Gates

Every CI check is a hard gate. See
[Source Control Guidelines](../../source-control-guidelines.md#ci-gates) for the
hard-gates-only standard and the
[CI Architecture](../../../guides/ci-architecture.md) guide for how required
status checks are configured.

Hard gate definition:

- Merge-blocking. A required status check must be configured on the target
  branch. Any failure blocks merge until a new commit passes.

There is no non-blocking category: every version a repo resolves from the
version window (for Rust, the newest stable release and the one before it) is
a hard CI leg. See the
[Runtime Version Support Policy](../runtime-version-support-policy.md).

Hard gates for a Rust repository (all are required status checks; the
[CI gates](../../../guides/ci-architecture.md#ci-gates) table gives the full
rules):

- `quality / evidence`: the common checks, the custom validator, rustfmt,
  clippy and `cargo check`
- `test / evidence`: the nextest run under cargo-llvm-cov at 100% line
  coverage, and the doctests
- `audit / evidence`: `cargo deny` against the fleet policy
- `security / trivy` and `security / semgrep`
- `security / codeql`, and GitHub's own `CodeQL`, `Trivy` and `Semgrep OSS`
  checks, when GitHub Advanced Security is available (CodeQL analyzes Rust)
- `version / version-bump`, `docs / docs` and
  `test / integration / <version>`, when `vergil.toml` turns them on

Branch applicability:

- develop: all hard gates required
- release: all hard gates required
- main: all hard gates required

## Document Map

- Naming conventions: [naming-conventions.md](naming-conventions.md)
