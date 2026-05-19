# Tooling gap analysis and next-generation audit expansion

Date: 2026-05-19
Status: Draft
Milestone: v2.1
Related: #828 (OpenSSF Scorecard + Best Practices Badge), #852 (action items)

## Purpose

Comprehensive audit of development quality, security, and compliance tools
across all six supported languages (Python, Ruby, Rust, Java, Go, TypeScript)
and cross-language infrastructure. Identifies gaps in the current tooling
stack and recommends additions organized by priority and pipeline tier.

This document is the reference artifact for v2.1 tooling expansion planning.
Actionable work items derived from this analysis are tracked in #852.

## Current tooling baseline

### Per-language tools

| Category | Python | Ruby | Rust | Java | Go |
|---|---|---|---|---|---|
| Lint | ruff (E/F/W/I/N/UP/S/B/A/C4/SIM/TCH/ARG/PTH/ERA) | RuboCop | cargo fmt, clippy -D warnings | Spotless, Checkstyle | golangci-lint |
| Type check | mypy (strict), ty | Steep | cargo check | javac | go vet |
| Test | pytest (100% branch cov) | rake | cargo llvm-cov (100% line) | Maven verify | go test -race + go-test-coverage |
| Dep audit | pip-audit | bundle-audit | cargo deny | (none — gap) | govulncheck |
| License | pip-licenses (18 allowed) | license_finder | cargo deny | license-maven-plugin (9 allowed) | go-licenses (7 allowed) |

TypeScript is not yet supported. Full toolchain recommendation is in the
TypeScript section below.

### Cross-language tools

| Tool | Scope |
|---|---|
| markdownlint | Markdown linting (docs + README) |
| shellcheck | Shell script linting |
| yamllint | YAML validation |
| hadolint | Dockerfile linting |
| actionlint | GitHub Actions workflow validation |
| CodeQL | Code scanning (SAST) |
| Trivy | Container vulnerability scanning |
| Semgrep | Static analysis (SAST) |
| OpenSSF Scorecard | Repository security posture |
| vrg-repo-profile | Repository structure validation |

### Existing but not visible in command registry

These capabilities exist in the platform but were not part of the
per-commit validation pipeline:

- **GitHub Secret Scanning + Push Protection**: enabled for public repos
  via `vrg-github-repo-config` (secret_scanning and
  secret_scanning_push_protection fields in github_config.py).
- **Build Provenance Attestations**: Docker images attested via
  `actions/attest-build-provenance@v4` in vergil-docker CD workflow.
  Release workflow in vergil-actions has attestation and SBOM inputs
  ready but may not be activated by all consuming repos.

## Gap matrix

| Category | Status | Gap Description |
|---|---|---|
| Coverage enforcement | Partial | Java and Ruby have no coverage thresholds. Python (100%), Rust (100%), and Go (config-based) enforce coverage. |
| Secret detection (local) | Gap | GitHub secret scanning covers push protection, but no pre-commit or CI-level tools (Gitleaks, TruffleHog) for defense-in-depth. |
| Actions security analysis | Gap | actionlint checks syntax but not security patterns (unpinned actions, template injection, dangerous triggers). |
| Dependency update automation | Gap | Custom workflow exists but scorecard requires Dependabot or Renovate. |
| SBOM generation | Partial | Trivy can generate SBOMs; release workflow has SBOM input. Not standardized across all repos. |
| Artifact signing | Partial | Docker images are attested. Library artifacts may not be. |
| Fuzzing / property-based testing | Gap | No fuzzing or PBT in any language. |
| Mutation testing | Gap | No mutation testing in any language. |
| Dead code detection | Gap | No dead code analysis in any language. |
| API compatibility / semver checking | Gap | No automated breaking-change detection for any language. |
| Code smell / complexity (advanced) | Partial | Python (ruff C901) and Go (gocyclo) have basic complexity. Ruby, Java lack it. |
| Documentation / prose quality | Partial | markdownlint covers structure. No prose quality linting (style, clarity, inclusive language). |
| Java static analysis | Gap | No SpotBugs, PMD, or Error Prone. Checkstyle covers style only. |
| Java null safety | Gap | No NullAway or Checker Framework. |
| Ruby coverage | Gap | No SimpleCov or coverage threshold. |
| Ruby code smells | Gap | No reek, flay, or flog. |
| Rust unused deps | Gap | No cargo-machete or cargo-udeps. |
| Policy-as-code | Gap | vrg-validate is custom; no OPA/Conftest for declarative policies. |
| Architecture verification | Gap | No ArchUnit or equivalent. |

## Recommended tools by priority tier

### Tier 1: High-value gaps (v2.1)

These address the most critical gaps and directly improve OpenSSF
Scorecard scores.

#### JaCoCo (Java coverage enforcement)

- **What**: JaCoCo Maven plugin with `check` goal enforcing coverage
  thresholds (line, branch, instruction counters).
- **Pipeline**: Per-commit (vrg-validate, CheckKind.TEST for Java).
- **Why**: Coverage enforcement parity with Python/Rust/Go. Currently
  Java tests run without measuring or gating on coverage.
- **Effort**: Low. Add JaCoCo plugin to consuming repos' pom.xml and
  update the command registry to verify thresholds.

#### SimpleCov (Ruby coverage enforcement)

- **What**: SimpleCov gem with minimum_coverage and branch coverage
  configuration.
- **Pipeline**: Per-commit (vrg-validate, CheckKind.TEST for Ruby).
- **Why**: Same parity rationale as JaCoCo.
- **Effort**: Low. Add SimpleCov to Gemfile, configure in test_helper,
  update command registry.

#### Gitleaks (pre-commit secret detection)

- **What**: Regex-based secret scanner. Runs in milliseconds. Catches
  API keys, tokens, passwords before they enter git history.
- **Pipeline**: Pre-commit hook (alongside vrg-commit gate).
- **Why**: GitHub secret scanning only catches secrets at push time.
  Gitleaks prevents them from being committed at all. Defense-in-depth.
- **Effort**: Low. Single binary, runs on all file types.

#### TruffleHog (CI secret detection)

- **What**: Deep scanner that verifies whether 700+ secret types are
  still active via safe read-only API calls. Scans git history, not
  just the working tree.
- **Pipeline**: Per-PR CI.
- **Why**: Complements Gitleaks with deeper scanning and active
  credential verification. Catches secrets in git history that
  pre-commit hooks cannot.
- **Effort**: Low-medium. GitHub Action available.

#### zizmor (GitHub Actions security analysis)

- **What**: Purpose-built static analysis for GitHub Actions. Detects
  unpinned actions, template injection, dangerous triggers, excessive
  permissions. Outputs SARIF for GitHub Advanced Security integration.
- **Pipeline**: Per-PR CI.
- **Why**: The March 2026 tj-actions supply chain attack compromised
  23,000+ repos via unpinned action references. actionlint checks
  syntax but not security. zizmor fills this gap.
- **Effort**: Low. Rust binary, GitHub Action available.

#### Renovate or Dependabot (automated dependency updates)

- **What**: Automated PR creation for dependency updates.
- **Pipeline**: Automated (scheduled).
- **Why**: OpenSSF Scorecard requires recognition of an automated
  dependency update tool. Renovate supports 90+ package managers
  (better multi-language coverage); Dependabot is zero-setup and
  native to GitHub.
- **Recommendation**: Renovate for its breadth across six languages.
- **Effort**: Medium. Requires configuration per repo and merge
  workflow integration.

### Tier 2: Strong additions (v2.1-2.2)

#### Cross-language

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| Syft | SBOM generation | Per-release CD | Most versatile SBOM generator. CycloneDX + SPDX output. Pairs with Grype for vuln correlation. |
| Vale | Prose quality | Per-commit (vrg-validate) | Style, clarity, consistency linting for documentation. Replaces alex, textlint, write-good. |
| woke | Inclusive language | Per-commit (vrg-validate) | Scans all file types for non-inclusive terms. Customizable YAML rules. |

#### Python

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| hypothesis | Property-based testing | Per-commit (vrg-validate) | Generates random inputs, finds minimal failing cases. High value for parsing/serialization code. |
| vulture | Dead code | Periodic audit | Run with --min-confidence 80. Not a commit gate. |
| pipdeptree | Dependency visualization | Developer utility | Not a CI gate. Useful for auditing transitive dependency bloat. |

Investigated but not recommended for Python:
- **Bandit**: ruff S rules cover the high-value subset; Semgrep covers the rest.
- **safety / snyk**: pip-audit already fills this niche (OSV database).
- **isort**: ruff I rules are equivalent.
- **darglint**: Unmaintained.
- **radon**: ruff C901 sufficient for gating; radon adds Halstead metrics but rarely actionable.
- **atheris**: Overkill for a tooling library; hypothesis covers the same territory more ergonomically.
- **check-manifest / twine check**: Only relevant if publishing to PyPI.

#### Ruby

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| reek | Code smells | Per-commit (vrg-validate) | 20+ smell types. Catches problems RuboCop misses (Feature Envy, Data Clump). |
| flay | Structural duplication | Per-commit (vrg-validate) | Detects copy-paste across files. Unique capability. |
| ruby-audit | Interpreter CVEs | Per-commit (vrg-validate) | Complements bundle-audit (gem CVEs) with Ruby interpreter CVEs. |
| bundler-leak | Memory leak detection | Per-commit (vrg-validate) | Checks gems against ruby-mem-advisory-db. Trivial to add alongside bundle-audit. |
| debride | Dead code | Periodic audit | Similar to vulture for Python. Not a commit gate. |

Investigated but not recommended for Ruby:
- **Brakeman**: Rails-only; not applicable to non-Rails projects.
- **Sorbet**: Steep + RBS aligns with Ruby core direction; Sorbet's proprietary `sig {}` syntax is a dead end.
- **Ruzzy**: Linux-only; limited Ruby fuzzing value.
- **flog**: Useful but overlaps with RuboCop's complexity cops.

#### Rust

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| cargo-machete | Unused dependencies | Per-commit (vrg-validate) | Works on stable (unlike cargo-udeps which needs nightly). Fast, low friction. |
| cargo-semver-checks | API compatibility | Per-PR CI | 242 lints for semver violations. Table stakes for library crates. |
| RUSTDOCFLAGS="-D warnings" | Doc quality | Per-commit (vrg-validate) | Catches broken intra-doc links and missing docs at compile time. |

Investigated but not recommended for Rust:
- **cargo-audit**: Fully subsumed by cargo deny.
- **cargo-geiger**: Report only, not CI-gatable.
- **cargo-udeps**: Requires nightly; cargo-machete works on stable.
- **cargo-bloat**: Niche; only relevant for size-constrained binaries.
- **cargo-crev**: Requires community participation; cargo-vet is more practical.
- **cargo-modules / cargo-count**: Exploration tools, not CI gates; clippy complexity lints cover actionable cases.

#### Go

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| gorelease | API compatibility | Per-PR CI | Checks whether version bumps are consistent with actual API changes. |
| deadcode | Dead code | Periodic audit | Official Go team tool (golang.org/x/tools). Uses Rapid Type Analysis. |
| maintidx + gocritic | Complexity | Per-commit (via golangci-lint) | Zero cost to enable since golangci-lint already runs. |
| revive exported rule | Doc enforcement | Per-commit (via golangci-lint) | Requires doc comments on public symbols. |
| -trimpath | Build flag | Production builds | Strips build paths from binaries for reproducibility/security. |

Investigated but not recommended for Go:
- **nancy**: govulncheck's reachability analysis is superior.
- **gosec/staticcheck/errcheck/wrapcheck standalone**: Already in golangci-lint.
- **gomarkdoc**: Not a quality gate.

#### Java

| Tool | Category | Pipeline | Notes |
|---|---|---|---|
| SpotBugs + find-sec-bugs | Static analysis + security | Per-commit (vrg-validate) | Bytecode analysis. find-sec-bugs detects 144 vulnerability types across 826+ API signatures. |
| PMD | Static analysis + complexity | Per-commit (vrg-validate) | CPD (copy-paste detection) alone justifies it. Also adds complexity rules. |
| Error Prone | Compiler plugin | Per-commit (vrg-validate) | Highest bug detection rate in academic benchmarks. ~10-15% build overhead. |
| NullAway | Null safety | Per-commit (vrg-validate) | JSpecify + NullAway is the industry direction. Spring 7 endorses it. Only 1.15x overhead. |
| japicmp | API compatibility | Per-PR CI | Compares JAR versions for source/binary incompatible changes. |
| Maven Enforcer | Dependency convergence | Per-commit (vrg-validate) | Enforces convergence + banned-dependencies rules. |
| ArchUnit | Architecture rules | Per-commit (vrg-validate) | Architecture rules as JUnit tests. Unique capability for preventing architectural drift. |
| OWASP dependency-check | Vulnerability audit | Per-commit (vrg-validate) | NVD-based dependency scanning. Requires NVD API key since v9.0. |

Investigated but not recommended for Java:
- **ProGuard / UCDetector**: Awkward for server-side Java; PMD unused-code rules cover practical cases.
- **Infer (Meta)**: Steeper integration curve; SpotBugs/PMD/Error Prone trio covers the same ground.

### Tier 3: Advanced capabilities (v2.2+)

#### Fuzzing (new pipeline tier: scheduled deep audit)

| Tool | Language | Notes |
|---|---|---|
| cargo-fuzz | Rust | libFuzzer-based. Primary fuzzer for crates parsing untrusted input. |
| go test -fuzz | Go | Built-in since Go 1.18. No external tools needed. |
| Jazzer | Java | Google's Java fuzzer. Integrated with OSS-Fuzz. Development has stalled; monitor. |
| hypothesis (extended) | Python | Already recommended in Tier 2. Extend with fuzzing harnesses. |
| fast-check | TypeScript | Property-based testing. The closest TS equivalent to fuzzing. |

#### Mutation testing (new pipeline tier: scheduled deep audit)

| Tool | Language | Notes |
|---|---|---|
| mutmut | Python | AST-based. ~1200 mutants/min. Best for critical library code. |
| mutant | Ruby | Used by Trail of Bits for security validation. |
| cargo-mutants | Rust | Function-level mutations. Works on stable compiler. |
| PIT (pitest) | Java | Industry standard. scmMutationCoverage goal enables incremental CI runs. |
| gremlins | Go | Most actively maintained Go mutation tester. |
| Stryker | TypeScript | The standard for JS/TS mutation testing. |

#### Other advanced tools

| Tool | Category | Language | Notes |
|---|---|---|---|
| Miri | Undefined behavior | Rust | Interprets MIR. Nightly only, 10-100x slower. For unsafe crates. |
| cargo-vet | Supply chain trust | Rust | Audit attestations stored in-tree. Ongoing maintenance cost. |
| cargo-msrv | MSRV verification | Rust | For library crates that declare a minimum supported Rust version. |
| cargo-nextest | Test runner | Rust | Parallel execution, per-test timeouts. Drop-in replacement for cargo test. |
| Conftest/OPA | Policy-as-code | All | Declarative Rego policies for config files, workflows, manifests. |
| Allstar | Repo health | All | GitHub App. Continuous monitoring of org-wide security policies. |
| OpenRewrite | Automated migration | Java | 5000+ recipes for version upgrades, framework migrations. On-demand. |
| jqwik | Property-based testing | Java | JUnit 5 PBT framework. Lighter than Jazzer for property testing. |

### Tier 4: TypeScript complete toolchain

When TypeScript is added as the sixth supported language, the following
toolchain should be configured in the command registry:

| Category | Tool | Notes |
|---|---|---|
| Package manager | pnpm | 3.4x faster than npm, strict dependency isolation, monorepo support. |
| Lint | ESLint + typescript-eslint | @typescript-eslint/strict + type-checked rules. Revisit Biome in 12 months. |
| Format | Prettier | Pairs with ESLint. Replaced by Biome's formatter if linter migrates. |
| Type check | tsc --noEmit | strict: true + noUncheckedIndexedAccess + exactOptionalPropertyTypes. |
| Test + coverage | Vitest | Built-in V8 coverage with enforced thresholds. 5-10x faster than Jest. |
| Dep audit | pnpm audit + audit-ci | audit-ci wraps pnpm audit with severity thresholds and allowlists. |
| License | license-checker | Allowlist/denylist configuration. CI gate on disallowed licenses. |
| Security | eslint-plugin-security | Code-level security anti-patterns. |
| Dead code + unused deps | Knip | Single tool replaces ts-unused-exports, ts-prune, depcheck. |
| Property-based testing | fast-check | Random input generation with shrinking. |
| Mutation testing | Stryker | TypeScript-aware. Incremental mode for CI. Scheduled deep audit tier. |
| API compat | API Extractor + publint | Breaking-change detection + package.json validation. |
| Complexity | ESLint complexity rules | Built-in: complexity, max-depth, max-nested-callbacks, max-params. |
| Documentation | TypeDoc + eslint-plugin-tsdoc | TSDoc comment format enforcement + HTML generation. |

Investigated but not recommended for TypeScript (yet):
- **Biome**: ~85% parity with typescript-eslint type-checked rules. Revisit in 12 months.
- **Oxlint**: 520+ rules at 50-100x speed, but lint-only (no formatter). Type-aware in preview.
- **Bun**: 18x faster than npm but ~95% Node.js compatibility. The 5% gap is a risk.

## Pipeline architecture

### Current tiers

| Tier | Trigger | Target | Existing |
|---|---|---|---|
| Per-commit | vrg-validate (pre-commit hook) | Seconds | lint, typecheck, test, audit, common checks |
| Per-PR CI | pull_request event | ~5-8 min | CodeQL, Trivy, Semgrep, standards, version bump |
| Periodic ops | Scheduled (daily) | Minutes | github-config audit |

### Proposed additions

| Tier | Trigger | Target | New tools |
|---|---|---|---|
| Pre-commit hook | Before commit | Milliseconds | Gitleaks |
| Per-commit | vrg-validate | Seconds | All Tier 1-2 lint/test/audit tools |
| Per-PR CI | pull_request | ~8-12 min | TruffleHog, zizmor, semver checks |
| Per-release CD | Release workflow | Minutes | Syft (SBOM), attestation activation |
| **Scheduled deep audit** (new) | Weekly or on-demand | 30-60 min | Fuzzing, mutation testing, dead code, complexity trending |

## Scorecard impact projection

Current average score: ~4.9/10.

| Remediation | Scorecard Check | Projected Impact |
|---|---|---|
| Enable Renovate/Dependabot | Dependency-Update-Tool | 0 -> 10 |
| Add required PR approval | Code-Review | 0 -> 10 |
| Set workflow permissions to read-all | Token-Permissions | 0 -> 10 |
| Pin action refs to SHAs (zizmor + pin-github-action) | Pinned-Dependencies | 0 -> 10 |
| Register at bestpractices.dev | CII-Best-Practices | 0 -> 10 |
| Activate attestation inputs in consuming repos | Signed-Releases | 0 -> 10 |
| Integrate ClusterFuzzLite or per-language fuzzers | Fuzzing | 0 -> 10 |

Projected score after Tier 1 + scorecard remediations: ~8-9/10.

## Sources

Research conducted 2026-05-19 via parallel agent investigation covering
web search and ecosystem documentation review.

### Cross-language

- Sbomify: SBOM Generation Tools Comparison (2026-01-26)
- OpenSSF: Choosing an SBOM Generation Tool (2025-06-05)
- Sigstore documentation and cosign v3 release notes
- GitHub Artifact Attestations documentation
- GitHub: SLSA Level 3 with Artifact Attestations (github.blog)
- NomadX: Secret Scanners Comparison 2026
- Gitleaks vs TruffleHog 2026 Benchmarks (AppSecSanta)
- Checkov vs KICS 2026 (AppSecSanta)
- Grafana: Hardening GitHub Actions with zizmor (2026-03-28)
- StepSecurity: GitHub Actions Pinning Guide
- Vale CLI documentation (GitHub)
- woke documentation (getwoke.tech)
- OPA/Conftest documentation (openpolicyagent.org)
- OpenSSF Allstar documentation (GitHub)
- ClusterFuzzLite documentation (GitHub)
- Spectral documentation (Stoplight)
- Pact documentation (pact.io)
- Knip documentation (knip.dev)
- Lizard documentation (GitHub)

### Python

- Ruff vs Bandit Performance Comparison (McGinnis, 2026-02-10)
- Semgrep vs Bandit 2026 (dev.to/rahulxsingh)
- Mutation Testing with mutmut (Johal, 2026)
- Mutation Testing Tools Comparison (IEEE, 10.1109/ACCESS.2024.3519116)
- vulture documentation (GitHub, jendrikseipp)
- Python Dead Code Study (dev.to/duriantaco)
- Hypothesis documentation (hypothesis.readthedocs.io)
- ty beta announcement (Astral, pydevtools.com)
- pipdeptree documentation (PyPI)

### Ruby

- State of Static Typing in Ruby 2025 (dev.to/aeremin)
- Sorbet vs RBS (BetterStack)
- State of Ruby 2026 (devnewsletter.com)
- Brakeman 8.0.3 release notes (brakemanscanner.org)
- mutant documentation (GitHub, mbj)
- Reek documentation (GitHub, troessner)
- Debride v1.15.1 release notes (zenspider.com)
- Bundler-leak documentation (GitHub, rubymem)
- Ruzzy: Ruby Fuzzer (ACM, 10.1145/3675741.3675749)

### Rust

- LogRocket: Comparing Rust Supply Chain Safety Tools
- cargo-deny configuration documentation (Embark Studios)
- cargo-mutants documentation (mutants.rs)
- Rust Fuzz Book: cargo-fuzz (rust-fuzz.github.io)
- honggfuzz-rs documentation (GitHub)
- cargo-semver-checks documentation (GitHub, obi1kenobi)
- cargo-vet documentation (Mozilla)
- cargo-msrv documentation (GitHub, foresterre)
- Miri POPL 2026 paper (ETH Zurich)
- Miri in GitHub Actions CI (kflansburg.com)
- Cargo 1.93 development cycle blog (rust-lang.org)

### Go

- golangci-lint linters list and changelog (golangci-lint.run)
- Go deadcode blog post (go.dev/blog)
- gorelease documentation (pkg.go.dev)
- go-apidiff documentation (GitHub, joelanford)
- govulncheck tutorial (go.dev)
- go-critic documentation (go-critic.com)
- gremlins documentation (GitHub, go-gremlins)
- JetBrains: Go Ecosystem 2025

### Java

- Java Code Geeks: Static Analysis and Code Generation (2025-10)
- SpotBugs vs Error Prone discussion (GitHub, spotbugs)
- SonarQube vs PMD 2026 (dev.to/rahulxsingh)
- PIT Mutation Testing in Java (JavaPro, 2026-01-21)
- PIT documentation (pitest.org)
- ArchUnit documentation (archunit.org)
- Spring Null Safety with JSpecify and NullAway (spring.io, 2025-03-10)
- NullAway documentation (GitHub, Uber)
- find-sec-bugs documentation (find-sec-bugs.github.io)
- OWASP Dependency-Check documentation
- JaCoCo coverage counters (eclemma.org)
- japicmp documentation (siom79.github.io)
- Revapi documentation (revapi.org)
- OpenRewrite documentation (Moderne)
- Maven Enforcer Plugin documentation (maven.apache.org)

### TypeScript

- Package Manager Showdown 2026 (dev.to/pockit_tools)
- Biome vs ESLint vs Oxlint 2026 (PkgPulse)
- Biome: The ESLint and Prettier Killer? (dev.to/pockit_tools, 2026)
- Oxlint v1.0 Stable Released (InfoQ, 2025-08)
- Vitest vs Jest 2026 (Tech Insider)
- Vitest 3 vs Jest 30 2026 (PkgPulse)
- Stryker Mutator documentation (oneuptime.com, 2026-01-25)
- Knip documentation (knip.dev)
- fast-check documentation (fast-check.dev)
- API Extractor documentation (api-extractor.com)
- publint documentation (publint.dev)
- TypeDoc documentation (typedoc.org)
- TypeDoc vs JSDoc vs API Extractor 2026 (PkgPulse)
- Rust-Based Tooling Dominating JavaScript 2026 (dev.to/dataformathub)
- Jazzer: Java Fuzzing Complete Guide (code-intelligence.com)
