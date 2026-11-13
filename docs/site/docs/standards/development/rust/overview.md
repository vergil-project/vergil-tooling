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

- Formatting: rustfmt (canonical; any formatting configuration goes in
  `rustfmt.toml`).
- Linting: clippy (official Rust linter, 800+ lint rules).
- Type checking: `cargo check` (inherent in the Rust compiler).
- Dependency audit and license compliance: cargo-deny (advisories, licenses,
  bans, and sources in a single tool).
- Coverage: cargo-llvm-cov (LLVM instrumentation, cross-platform).
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

Hard gates (all are required status checks):

- `test: unit (current)`
- `test: integration`
- `ci: dependency-audit`

Branch applicability:

- develop: all hard gates required
- release: all hard gates required
- main: all hard gates required

## Document Map

- Naming conventions: [naming-conventions.md](naming-conventions.md)
