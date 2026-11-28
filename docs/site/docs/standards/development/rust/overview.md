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
- Workspaces: every workspace member is validated, not only the root
  package. Clippy, check, the coverage run (the tests and the coverage
  report), the doctests, the audit and the release build all run with
  `--workspace`, and
  `cargo fmt --all` formats every member. The doctests are skipped, with a
  notice, only when no workspace member has a library target. A
  single-crate repository is validated exactly as before.
- Formatting: rustfmt (canonical; any formatting configuration goes in
  `rustfmt.toml`).
- Linting: clippy (official Rust linter, 800+ lint rules), run on all targets
  (`cargo clippy --all-targets --locked --workspace -- -D warnings`), so the
  library, binaries, tests, examples and benches are linted and any warning
  fails the gate. The run writes cargo's JSON messages to
  `quality-clippy.json`, the lint gate's evidence, and prints the readable
  diagnostics to the log.
- Type checking: `cargo check --locked --workspace` (inherent in the Rust
  compiler). The run writes cargo's JSON messages to
  `quality-cargo-check.json`, the typecheck gate's evidence, and prints the
  readable diagnostics to the log.
- Dependency audit and license compliance: cargo-deny (advisories, licenses,
  bans, and sources in a single tool), run against the fleet policy that
  vergil-tooling packages, so a repo's own `deny.toml` is not used. The
  gating run writes its JSON diagnostics to `cargo-deny.json`, the audit
  gate's evidence.
- Testing: cargo-nextest runs the unit and integration tests and writes a
  JUnit report to `junit.xml`. Rustdoc examples run separately with
  `cargo test --doc`, because nextest does not run doctests.
- Coverage: cargo-llvm-cov (LLVM instrumentation, cross-platform) measures
  that nextest run, held to 100% line coverage and 100% region coverage. Its
  report writes Cobertura XML to `coverage.xml`. `coverage.xml` and
  `junit.xml` are the test gate's evidence.
  - A region is a span of code with its own execution count. Each arm of an
    `if` or a `match`, and each `?` early return, is its own region, so a
    line can run while one of its regions never does. Regions are the stable
    toolchain's stand-in for branch coverage. Branch coverage itself needs a
    nightly toolchain and is deferred to
    [vergil-project/.github#1171](https://github.com/vergil-project/.github/issues/1171).
  - The report names the lines that never ran. When the gate fails,
    `vrg-validate` also lists every uncovered region as
    `<file>:<line>:<col>: uncovered region`, from the same report's JSON
    export. The list counts regions as cargo-llvm-cov's region total does: for
    a generic function, only its best-covered instantiation counts. If that
    list disagrees with the report's own totals, the report read stale build
    output, and `vrg-validate` says so and names the fix.
  - `vrg-validate` records the toolchain that built the coverage objects
    (`rustc -vV`, `cargo -vV` and `cargo llvm-cov --version`) in
    `.vergil/rust-coverage-toolchain.txt`. When the toolchain changes, or
    there is no record, it removes `target/llvm-cov-target` whole with
    `cargo llvm-cov clean` before the tests run. The usual clean covers only
    the workspace's own artifacts as the current cargo lays them out, so an
    earlier toolchain's objects would otherwise reach the report and fail
    the gate on correct code.
  - Nothing can be excluded from coverage. An unreachable region is a design
    signal: remove it or make it testable.
- Release build: `vrg-validate` builds the release profile in its build
  stage, after the typecheck and before the tests, with
  `cargo build --release --locked --workspace`. The release job's build runs
  the same command, so validation builds what the release ships. The tests
  compile the debug profile with coverage instrumentation, so without this
  stage the `--release` profile (its optimisation, and any code under
  `cfg(debug_assertions)` or `cfg(not(debug_assertions))`) would be built for
  the first time after the release PR merges. A failing release build fails
  `vrg-validate`, as every other stage does. Run it alone with
  `vrg-validate --check build`.
- If a repository uses different tools, document the reason and equivalents.

## Tool Ownership

Where each tool lives follows the
[Tool Ownership Guideline](../tool-ownership.md). For Rust:

- The repository declares no gate tool.
- rustfmt and clippy ship with the toolchain, and cargo-deny, cargo-llvm-cov
  and cargo-nextest come from the image. Each image tool is pinned to a
  version, so an upstream release cannot change a gate's verdict unreviewed:
  cargo-deny and cargo-nextest are checked against a per-architecture SHA-256
  pinned beside the version, and cargo-llvm-cov is built from crates.io with
  `--locked`.

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
  clippy, `cargo check` and the release build (`quality / build / <version>`,
  once on the primary version)
- `test / evidence`: the nextest run under cargo-llvm-cov at 100% line and
  region coverage, and the doctests
- `audit / evidence`: `cargo deny` against the fleet policy
- `security / trivy` and `security / semgrep`
- `security / codeql`, and GitHub's own `CodeQL` and `Trivy` checks
  (CodeQL analyzes Rust)
- `version / version-bump`, `docs / docs` and
  `test / integration / <version>`, when `vergil.toml` turns them on

Branch applicability:

- develop: all hard gates required
- release: all hard gates required
- main: all hard gates required

## Document Map

- Naming conventions: [naming-conventions.md](naming-conventions.md)
