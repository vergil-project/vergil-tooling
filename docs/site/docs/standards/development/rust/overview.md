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

- Formatting: rustfmt (canonical, zero configuration via `rust-toolchain.toml`).
- Linting: clippy (official Rust linter, 800+ lint rules).
- Type checking: `cargo check` (inherent in the Rust compiler).
- Dependency audit and license compliance: cargo-deny (advisories, licenses,
  bans, and sources in a single tool).
- Coverage: cargo-llvm-cov (LLVM instrumentation, cross-platform).
- Toolchain pinning: `rust-toolchain.toml` (auto-installs the correct toolchain
  via rustup).
- If a repository uses different tools, document the reason and equivalents.

## CI Gates

Every CI check is a hard gate. See
[Source Control Guidelines](../../source-control-guidelines.md#ci-gates) for the
hard-gates-only standard and the
[CI Architecture](../../../guides/ci-architecture.md) guide for how required
status checks are configured.

Hard gate definition:

- Merge-blocking. A required status check must be configured on the target
  branch. Any failure blocks merge until a new commit passes.

The only sanctioned non-blocking category is the
**advisory-on-unsupported-versions carve-out**: a check is advisory *because*
the runtime version it runs against is outside the supported set (a preview
version not yet promoted, or an EOL version past upstream support), not because
the check itself is a generic soft gate. See the
[Runtime Version Support Policy](../runtime-version-support-policy.md) for the
carve-out. Rust runs no version-matrix advisory jobs today, so all of its gates
are hard gates.

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
