# Issue #363 Cross-Repo Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 3 and Phase 4 remnants of the standard-tooling.toml migration (#363): strip config sections from `docs/repository-standards.md` in 8 consuming repos, update stale co-author references in "Commit and PR scripts" sections, and delete the one remaining `st-config.toml`.

**Architecture:** Each repo gets a one-commit chore PR that removes the `## AI co-authors`, `## Repository profile`, and related config sections from `docs/repository-standards.md`, updates the Table of Contents, and fixes any "Commit and PR scripts" text that still references `[AI co-authors](#ai-co-authors)` to reference `standard-tooling.toml` instead. One additional repo (`mq-rest-admin-dev-environment`) also deletes its stale `st-config.toml`. All PRs are independent and can be submitted in parallel.

**Tech Stack:** git, `st-commit`, `st-submit-pr`, markdownlint

**Spec:** `docs/specs/standard-tooling-toml.md` (Phase 3 and Phase 4 remnants)

---

## Scope

### What's already done

- **Phase 2 (build):** TOML reader, consumer migrations, `repo_profile.py` deletion — all on `develop`, released from v1.4.5.
- **Phase 4 (retire `st-config.toml`):** `st_install_tag()` reads `standard-tooling.toml`, `docker_cache.py` updated, `st-config.toml` deleted from this repo and 16 of 17 consuming repos.
- **6 repos already clean:** standard-actions, mq-rest-admin-common, mq-rest-admin-dev-environment, mq-rest-admin-python, mq-rest-admin-template, standard-tooling itself.
- **3 repos with only a CI gate label:** standard-tooling-docker, standard-tooling-plugin, standards-and-conventions — these mention "Repository profile validation (`repo-profile`)" in CI gates, which is still accurate (the CLI now validates `standard-tooling.toml`). No changes needed.

### What's left

| # | Repo | Work |
|---|------|------|
| 1 | the-infrastructure-mindset | Strip `## AI co-authors`, `## Repository profile` sections; update ToC |
| 2 | ai-research-methodology | Strip `## AI co-authors`, `## Repository profile` sections; update co-author reference in Commit scripts; update ToC |
| 3 | mq-rest-admin-go | Strip `## AI co-authors`, `## Repository profile` sections; update co-author reference in Commit scripts; update ToC |
| 4 | mq-rest-admin-java | Strip `## AI co-authors`, `## Repository profile` sections; update co-author reference in Commit scripts; update ToC |
| 5 | mq-rest-admin-ruby | Strip `## AI co-authors`, `## Repository profile` sections; update co-author reference in Commit scripts; update ToC |
| 6 | mq-rest-admin-rust | Strip `## AI co-authors`, `## Repository profile` sections; update co-author reference in Commit scripts; update ToC |
| 7 | mnemosys-core | Strip `## Repository profile`, `## AI co-author identities` sections; update ToC |
| 8 | mnemosys-operations | Strip `## Repository profile`, `## AI co-authors` sections; update ToC |
| 9 | mq-rest-admin-dev-environment | Delete `st-config.toml` |

---

## Common pattern

Tasks 1–8 follow the same pattern. Each task shows the exact resulting file content for its repo. The agent must:

1. `cd` into `~/dev/github/<repo>`
2. Create a branch: `git checkout -b chore/363-strip-config-sections`
3. Edit `docs/repository-standards.md` to the content shown
4. (Task 9 only) Delete `st-config.toml`
5. Run `markdownlint docs/repository-standards.md` if the repo has markdownlint available
6. Commit with `st-commit`
7. Submit with `st-submit-pr`

---

### Task 1: the-infrastructure-mindset

**Files:**
- Modify: `~/dev/github/the-infrastructure-mindset/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/the-infrastructure-mindset
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Infrastructure Mindset Repository Standards

## Table of Contents

- [Docs-only definition](#docs-only-definition)
- [Validation](#validation)
- [Issue naming](#issue-naming)
- [Local deviations](#local-deviations)

## Docs-only definition

- Entire repository.

## Validation

- canonical_local_validation_command: markdownlint .
- validation_required: yes (markdownlint required)

## Issue naming

- Issue titles must be descriptive and avoid redundant prefixes (for example,
  "[Issue]" or "[Task]") unless the prefix adds non-obvious, essential context.

## Local deviations

- Sub-issues must use GitHub's Sub-issues relationship; task list references
  alone are not sufficient. See
  `docs/workflows/article-publication-workflow.md` for the exact API procedure.
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/the-infrastructure-mindset
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/the-infrastructure-mindset
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 2: ai-research-methodology

**Files:**
- Modify: `~/dev/github/ai-research-methodology/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/ai-research-methodology
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# AI Research Methodology Repository Standards

## Table of Contents

- [Pre-flight checklist](#pre-flight-checklist)
- [Local validation](#local-validation)
- [Linting policy](#linting-policy)
- [Python invocation](#python-invocation)
- [Tooling requirement](#tooling-requirement)
- [Merge strategy override](#merge-strategy-override)

## Pre-flight checklist

- Before modifying any files, check the current branch with `git status -sb`.
- If on `develop`, create a short-lived `feature/*` branch or ask for explicit approval to proceed on `develop`.
- If approval is granted to work on `develop`, call it out in the response and proceed only for that user-approved scope.
- Enable repository git hooks before committing: `git config core.hooksPath .githooks`.

## Local validation

- `st-validate-local`

## Linting policy

- Linter: `ruff`
- Rule set: `select = ["ALL"]` with scoped ignores per `pyproject.toml`
- Enforcement: CI and local validation
- Format: `ruff format` (double quotes, 120 char line length)

## Python invocation

- Always use `uv run` to invoke Python tools: `uv run pytest`, `uv run ruff`, `uv run mypy`.
- Never use `python3` directly outside of `uv run`.

## Tooling requirement

### Committing changes

```bash
st-commit \
  --type TYPE --message TEXT --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): one of
  `feat|fix|docs|style|refactor|test|chore|ci|build`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`standard-tooling.toml` and the git hooks validate the result.

### Submitting PRs

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--docs-only] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Fixes`):
  `Fixes|Closes|Resolves|Ref`
- `--title` (optional): PR title (default: most recent commit
  subject)
- `--notes` (optional): additional notes
- `--docs-only` (optional): applies docs-only testing exception
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy
automatically.

## Merge strategy override

| Source branch | Target | Strategy |
|---------------|--------|----------|
| `feature/*`, `bugfix/*`, `chore/*`, `docs/*` | `develop` | Squash (`--squash`) |
| `release/*` | `main` | Regular merge (`--merge`) |
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

Updated: "Commit and PR scripts" co-author reference changed from `[AI co-authors](#ai-co-authors) section` to `standard-tooling.toml`.

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/ai-research-methodology
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/ai-research-methodology
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 3: mq-rest-admin-go

**Files:**
- Modify: `~/dev/github/mq-rest-admin-go/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mq-rest-admin-go
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Repository Standards

## Table of Contents

- [Pre-flight checklist](#pre-flight-checklist)
- [Local validation](#local-validation)
- [Tooling requirement](#tooling-requirement)
- [Merge strategy override](#merge-strategy-override)
- [Approved domain abbreviations](#approved-domain-abbreviations)
- [Accepted naming deviations](#accepted-naming-deviations)

## Pre-flight checklist

- Before modifying any files, check the current branch with `git status -sb`.
- If on `develop`, create a short-lived `feature/*` branch or ask for explicit approval to proceed on `develop`.
- If approval is granted to work on `develop`, call it out in the response and proceed only for that user-approved scope.
- Enable repository git hooks before committing: `git config core.hooksPath scripts/git-hooks`.

## Local validation

Canonical local validation command:

```bash
scripts/dev/validate_local.sh
```

Individual checks (run by the validation script):

```bash
go vet ./...                    # Static analysis
golangci-lint run ./...         # Lint checks
gocyclo -over 15 ./mqrestadmin/ # Cyclomatic complexity gate
go test -race -count=1 ./...   # Unit tests with race detection
govulncheck ./...               # Vulnerability scanning
```

Integration tests (require MQ environment, not included in validation script):

```bash
go test -race -count=1 -tags=integration ./...
```

## Tooling requirement

Required for daily workflow:

- Go 1.22+ (`brew install go` or <https://go.dev/dl/>)
- `golangci-lint` (`brew install golangci-lint`)
- `gocyclo` (`go install github.com/fzipp/gocyclo/cmd/gocyclo@latest`)
- `govulncheck` (`go install golang.org/x/vuln/cmd/govulncheck@latest`)
- `markdownlint` (required for docs validation and PR pre-submission)

## Merge strategy override

- Feature, bugfix, and chore PRs targeting `develop` use squash merges (`--squash`).
- Release PRs targeting `main` use regular merges (`--merge`) to preserve shared
  ancestry between `main` and `develop`.
- Auto-merge commands:
  - Feature PRs: `gh pr merge --auto --squash --delete-branch`
  - Release PRs: `gh pr merge --auto --merge --delete-branch`

## Commit and PR scripts

AI agents **must** use the wrapper scripts for commits and PR
submission. Do not construct commit messages or PR bodies manually.

### Committing

```bash
st-commit \
  --type TYPE --message MESSAGE --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): one of
  `feat|fix|docs|style|refactor|test|chore|ci|build`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`standard-tooling.toml` and the git hooks validate the result.

### Submitting PRs

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--docs-only] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Fixes`):
  `Fixes|Closes|Resolves|Ref`
- `--title` (optional): PR title (default: most recent commit
  subject)
- `--notes` (optional): additional notes
- `--docs-only` (optional): applies docs-only testing exception
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy
automatically.

## Approved domain abbreviations

Domain-specific abbreviations that are well-established in the IBM MQ ecosystem
and may be used in identifiers without expansion:

- `qmgr` — queue manager (established MQSC domain term)

## Accepted naming deviations

None yet.
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

Updated: "Commit and PR scripts" co-author reference changed from `[AI co-authors](#ai-co-authors) section` to `standard-tooling.toml`.

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mq-rest-admin-go
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mq-rest-admin-go
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 4: mq-rest-admin-java

**Files:**
- Modify: `~/dev/github/mq-rest-admin-java/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mq-rest-admin-java
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Repository Standards

## Table of Contents

- [Pre-flight checklist](#pre-flight-checklist)
- [Local validation](#local-validation)
- [Tooling requirement](#tooling-requirement)
- [Merge strategy override](#merge-strategy-override)
- [Approved domain abbreviations](#approved-domain-abbreviations)
- [Accepted naming deviations](#accepted-naming-deviations)

## Pre-flight checklist

- Before modifying any files, check the current branch with `git status -sb`.
- If on `develop`, create a short-lived `feature/*` branch or ask for explicit approval to proceed on `develop`.
- If approval is granted to work on `develop`, call it out in the response and proceed only for that user-approved scope.
- Enable repository git hooks before committing: `git config core.hooksPath scripts/git-hooks`.

## Local validation

Canonical local validation command:

```bash
scripts/dev/validate_local.sh
```

Individual checks (run by the validation script):

```bash
./mvnw verify           # Full validation pipeline (formatting, style, compile,
                        # tests, coverage, SpotBugs, PMD)
```

Other useful commands:

```bash
./mvnw clean verify     # Clean full validation
./mvnw compile          # Compile only
./mvnw test             # Unit tests only
./mvnw spotless:apply   # Auto-format code
```

## Tooling requirement

Required for daily workflow:

- Java 17+ (`brew install openjdk@17` or SDKMAN)
- Maven Wrapper (`./mvnw`, checked into repo -- no separate Maven install needed)
- `markdownlint` (required for docs validation and PR pre-submission)

## Merge strategy override

- Feature, bugfix, and chore PRs targeting `develop` use squash merges (`--squash`).
- Release PRs targeting `main` use regular merges (`--merge`) to preserve shared
  ancestry between `main` and `develop`.
- Auto-merge commands:
  - Feature PRs: `gh pr merge --auto --squash --delete-branch`
  - Release PRs: `gh pr merge --auto --merge --delete-branch`

## Commit and PR scripts

AI agents **must** use the wrapper scripts for commits and PR
submission. Do not construct commit messages or PR bodies manually.

### Committing

```bash
st-commit \
  --type TYPE --message MESSAGE --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): one of
  `feat|fix|docs|style|refactor|test|chore|ci|build`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`standard-tooling.toml` and the git hooks validate the result.

### Submitting PRs

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--docs-only] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Fixes`):
  `Fixes|Closes|Resolves|Ref`
- `--title` (optional): PR title (default: most recent commit
  subject)
- `--notes` (optional): additional notes
- `--docs-only` (optional): applies docs-only testing exception
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy
automatically.

## Approved domain abbreviations

Domain-specific abbreviations that are well-established in the IBM MQ ecosystem
and may be used in identifiers without expansion:

- `qmgr` — queue manager (established MQSC domain term)

## Accepted naming deviations

Identifiers that intentionally diverge from general naming rules for readability
or domain alignment:

- `MqRestTransport transport` field in `MqRestSession` — the `MqRest` prefix on
  the type is redundant within the `MqRestSession` class context, so the field
  uses the shorter `transport` name rather than `mqRestTransport`.
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

Updated: "Commit and PR scripts" co-author reference changed from `[AI co-authors](#ai-co-authors) section` to `standard-tooling.toml`.

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mq-rest-admin-java
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mq-rest-admin-java
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 5: mq-rest-admin-ruby

**Files:**
- Modify: `~/dev/github/mq-rest-admin-ruby/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mq-rest-admin-ruby
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Repository Standards

## Table of Contents

- [Pre-flight checklist](#pre-flight-checklist)
- [Local validation](#local-validation)
- [Tooling requirement](#tooling-requirement)
- [Merge strategy override](#merge-strategy-override)
- [Approved domain abbreviations](#approved-domain-abbreviations)
- [Accepted naming deviations](#accepted-naming-deviations)

## Pre-flight checklist

- Before modifying any files, check the current branch with `git status -sb`.
- If on `develop`, create a short-lived `feature/*` branch or ask for explicit approval to proceed on `develop`.
- If approval is granted to work on `develop`, call it out in the response and proceed only for that user-approved scope.
- Enable repository git hooks before committing: `git config core.hooksPath ../standard-tooling/scripts/lib/git-hooks`.

## Local validation

Canonical local validation command:

```bash
bundle exec rake
```

## Tooling requirement

Required for daily workflow:

- Ruby 3.2+, Bundler
- `markdownlint` (required for docs validation and PR pre-submission)

Required for integration testing:

- Docker (for local MQ container environment)

## Merge strategy override

- Feature, bugfix, and chore PRs targeting `develop` use squash merges (`--squash`).
- Release PRs targeting `main` use regular merges (`--merge`) to preserve shared
  ancestry between `main` and `develop`.
- Auto-merge commands:
  - Feature PRs: `gh pr merge --auto --squash --delete-branch`
  - Release PRs: `gh pr merge --auto --merge --delete-branch`

## Commit and PR scripts

AI agents **must** use the `st-commit` and `st-submit-pr` scripts for commits
and PR submission. Do not construct commit messages or PR bodies manually.

### Committing

```bash
st-commit \
  --type TYPE --message MESSAGE --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): one of
  `feat|fix|docs|style|refactor|test|chore|ci|build`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`standard-tooling.toml` and the git hooks validate the result.

### Submitting PRs

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--docs-only] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Fixes`):
  `Fixes|Closes|Resolves|Ref`
- `--title` (optional): PR title (default: most recent commit
  subject)
- `--notes` (optional): additional notes
- `--docs-only` (optional): applies docs-only testing exception
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy
automatically.

## Approved domain abbreviations

Domain-specific abbreviations that are well-established in the IBM MQ ecosystem
and may be used in identifiers without expansion:

- `qmgr` — queue manager (established MQSC domain term)

## Accepted naming deviations

None yet.
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

Updated: "Commit and PR scripts" co-author reference changed from `[AI co-authors](#ai-co-authors) section` to `standard-tooling.toml`.

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mq-rest-admin-ruby
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mq-rest-admin-ruby
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 6: mq-rest-admin-rust

**Files:**
- Modify: `~/dev/github/mq-rest-admin-rust/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mq-rest-admin-rust
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Repository Standards

## Table of Contents

- [Pre-flight checklist](#pre-flight-checklist)
- [Local validation](#local-validation)
- [Tooling requirement](#tooling-requirement)
- [Merge strategy override](#merge-strategy-override)
- [Approved domain abbreviations](#approved-domain-abbreviations)
- [Accepted naming deviations](#accepted-naming-deviations)

## Pre-flight checklist

- Before modifying any files, check the current branch with `git status -sb`.
- If on `develop`, create a short-lived `feature/*` branch or ask for explicit approval to proceed on `develop`.
- If approval is granted to work on `develop`, call it out in the response and proceed only for that user-approved scope.
- Enable repository git hooks before committing: `git config core.hooksPath ../standard-tooling/scripts/lib/git-hooks`.

## Local validation

Canonical local validation command:

```bash
validate-local-rust
```

## Tooling requirement

Required for daily workflow:

- Rust 1.92+ (via rust-toolchain.toml), cargo-deny, cargo-llvm-cov
- `markdownlint` (required for docs validation and PR pre-submission)

Required for integration testing:

- Docker (for local MQ container environment)

## Merge strategy override

- Feature, bugfix, and chore PRs targeting `develop` use squash merges (`--squash`).
- Release PRs targeting `main` use regular merges (`--merge`) to preserve shared
  ancestry between `main` and `develop`.
- Auto-merge commands:
  - Feature PRs: `gh pr merge --auto --squash --delete-branch`
  - Release PRs: `gh pr merge --auto --merge --delete-branch`

## Commit and PR scripts

AI agents **must** use the `st-commit` and `st-submit-pr` scripts for commits
and PR submission. Do not construct commit messages or PR bodies manually.

### Committing

```bash
st-commit \
  --type TYPE --message MESSAGE --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): one of
  `feat|fix|docs|style|refactor|test|chore|ci|build`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`standard-tooling.toml` and the git hooks validate the result.

### Submitting PRs

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Fixes`):
  `Fixes|Closes|Resolves|Ref`
- `--title` (optional): PR title (default: most recent commit
  subject)
- `--notes` (optional): additional notes
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy
automatically.

## Approved domain abbreviations

Domain-specific abbreviations that are well-established in the IBM MQ ecosystem
and may be used in identifiers without expansion:

- `qmgr` — queue manager (established MQSC domain term)

## Accepted naming deviations

None yet.
```

Removed sections:
- `## AI co-authors` (identities now in `standard-tooling.toml`)
- `## Repository profile` (values now in `standard-tooling.toml`)

Updated: "Commit and PR scripts" co-author reference changed from `[AI co-authors](#ai-co-authors) section` to `standard-tooling.toml`.

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mq-rest-admin-rust
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "AI co-authors and repository profile values now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mq-rest-admin-rust
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 7: mnemosys-core

**Files:**
- Modify: `~/dev/github/mnemosys-core/docs/repository-standards.md`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mnemosys-core
git checkout develop && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# MNEMOSYS Repository Standards

This file records MNEMOSYS-specific terminology and any canonical standards
incubated here before extraction to the Standards and Conventions repository.
The Standards and Conventions repository remains the canonical source of truth,
but MNEMOSYS Core is used to develop new standards that are finalized and
merged upstream once stable. Differences between this repository and the
Standards and Conventions repository should be treated as in-progress standards
unless explicitly documented here as conventions.

## Table of Contents

- [Access requirements](#access-requirements)
- [Project terminology](#project-terminology)
  - [Official project name](#official-project-name)
  - [Deprecated names](#deprecated-names)
  - [Name rationale](#name-rationale)
  - [Usage in historical documents](#usage-in-historical-documents)
  - [CI gates](#ci-gates)
- [Local development preflight](#local-development-preflight)

## Access requirements

When reading canonical standards, use the raw GitHub content endpoint for
deterministic access:
`https://raw.githubusercontent.com/wphillipmoore/standards-and-conventions/develop/<path>`

If the canonical docs cannot be retrieved (network failure, access failure, or
missing file), treat it as a fatal exception: stop and notify the user. Do not
proceed with assumptions or alternate sources.

## Project terminology

### Official project name

**MNEMOSYS** (pronounced /ˈniːməˌsɪs/, or "NEE-muh-sis")

This is the canonical name for the project and must be used consistently across
all:

- Code (comments, docstrings, API descriptions)
- Documentation (design docs, user guides, README files)
- Repository metadata (package names, project descriptions)
- External communications

### Deprecated names

The following names are deprecated and should not be used in new code or
documentation:

- **FSIPS** (Functional Skill Integration & Practice System) - original working
  name, replaced by MNEMOSYS
- **RPM** (Recall, Practice, Maintenance) - originally conceived as a separate
  extension, now integrated as core MNEMOSYS functionality

### Name rationale

From the Greek root "mneme" meaning "memory, remembrance."

The name reflects the system's core thesis: skills are memory structures with
half-lives that require deliberate recall to survive. MNEMOSYS optimizes for
long-term retention and memory survival, not short-term acquisition.

See `docs/project/final/Philosophy.md` for the complete philosophical
foundation.

### Usage in historical documents

Early design documents (v0.1 snapshots) may reference deprecated names in their
original context. When updating these documents, add a nomenclature note
explaining the name evolution while preserving the historical snapshot.

### CI gates

CI gate definitions follow the canonical standards.

Hard gates (all are required status checks):

- `test-and-validate (3.14)`
- `integration-tests`
- `dependency-audit`

Soft gates:

- None.

Branch applicability:

- develop: all hard gates required
- release: all hard gates required
- main: all hard gates required

## Local development preflight

Before starting any new development effort, run the unit tests and confirm they
pass. This guards against inheriting broken local artifacts from prior work.

All Python commands must run inside the uv environment. Use
`uv run python3 ...` for every Python invocation and do not rely on a system
`python` binary.

Enable repository git hooks before committing:

- `git config core.hooksPath scripts/git-hooks`

Use one of:

- `uv run pytest tests/`
- `uv run python3 scripts/dev/validate_local.py`
- Docs-only changes: `uv run python3 scripts/dev/validate_docs.py`

Docs-only validation requires `markdownlint` `0.41.0` or newer on the PATH. If
it is not available, `npx` must be installed so the validation script can run
the minimum supported version.

Tooling requirement:

- `uv` `0.9.26` (install with `python3 -m pip install uv==0.9.26`).
```

Removed sections:
- `## Repository profile` (values now in `standard-tooling.toml`)
- `## AI co-author identities` (identities now in `standard-tooling.toml`)

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mnemosys-core
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "Repository profile and AI co-author identities now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mnemosys-core
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 8: mnemosys-operations

**Files:**
- Modify: `~/dev/github/mnemosys-operations/docs/repository-standards.md`

Note: This repo's default branch is `main`, not `develop`.

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mnemosys-operations
git checkout main && git pull
git checkout -b chore/363-strip-config-sections
```

- [ ] **Step 2: Edit `docs/repository-standards.md`**

Replace the entire file with:

```markdown
# Mnemosys Operations Repository Standards

## Table of Contents

- [Versioning alignment](#versioning-alignment)
- [Release environments](#release-environments)
- [Local validation](#local-validation)
- [Tooling requirement](#tooling-requirement)
- [Local deviations](#local-deviations)

## Versioning alignment

MAJOR.MINOR synced with mnemosys-core; PATCH.BUILD are repo-specific.

## Release environments

Develop, test, production.

## Local validation

- `python3 scripts/dev/validate_local.py`
- Docs-only changes: `python3 scripts/dev/validate_docs.py`

## Tooling requirement

- `uv` `0.9.26` (install with `python3 -m pip install uv==0.9.26`).

## Local deviations

None.
```

Removed sections:
- `## Repository profile` (values now in `standard-tooling.toml`)
- `## AI co-authors` (identities now in `standard-tooling.toml`)

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mnemosys-operations
st-commit --type chore --message "strip config sections from repository-standards.md" --agent claude --body "Repository profile and AI co-authors now live in standard-tooling.toml. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mnemosys-operations
st-submit-pr --issue 363 --summary "Strip config sections from repository-standards.md — values now in standard-tooling.toml" --linkage Ref --notes "Part of the standard-tooling.toml migration (standard-tooling#363 Phase 3)."
```

---

### Task 9: mq-rest-admin-dev-environment — delete stale st-config.toml

**Files:**
- Delete: `~/dev/github/mq-rest-admin-dev-environment/st-config.toml`

- [ ] **Step 1: Create branch**

```bash
cd ~/dev/github/mq-rest-admin-dev-environment
git checkout develop && git pull
git checkout -b chore/363-delete-st-config-toml
```

- [ ] **Step 2: Delete `st-config.toml`**

```bash
cd ~/dev/github/mq-rest-admin-dev-environment
git rm st-config.toml
```

- [ ] **Step 3: Commit**

```bash
cd ~/dev/github/mq-rest-admin-dev-environment
st-commit --type chore --message "delete stale st-config.toml" --agent claude --body "standard-tooling now reads from standard-tooling.toml. The old st-config.toml is unused. Ref wphillipmoore/standard-tooling#363"
```

- [ ] **Step 4: Submit PR**

```bash
cd ~/dev/github/mq-rest-admin-dev-environment
st-submit-pr --issue 363 --summary "Delete stale st-config.toml — standard-tooling now reads standard-tooling.toml" --linkage Ref --notes "Last remaining st-config.toml in the fleet. Part of the standard-tooling.toml migration (standard-tooling#363 Phase 4)."
```

---

## Post-Implementation

After all 9 PRs are submitted and merged:

1. Verify no `st-config.toml` remains in any repo.
2. Verify no `## AI co-authors` or `## Repository profile` config sections remain in any repo's `docs/repository-standards.md`.
3. Close issue #363 in standard-tooling.
