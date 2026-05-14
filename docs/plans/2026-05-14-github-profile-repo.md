# `.github` Profile Repository — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the `vergil-project/.github` repository with the org
profile README, default community health files, and VERGIL tooling
harness. Then consolidate duplicated templates out of the four
existing repos.

**Architecture:** Three-phase approach — prerequisites (Discussions,
license gaps), `.github` repo creation with all content, and
consolidation (remove per-repo duplicates after verifying inheritance).

**Tech Stack:** GitHub API via `gh` CLI, markdown, YAML

**Spec:** `docs/specs/2026-05-14-github-profile-repo-design.md`

**Spec correction:** The design spec lists the LICENSE as "MIT". The
actual license across VERGIL repos is GPL-3. Additionally,
vergil-docker and vergil-claude-plugin are missing LICENSE files
entirely — this is addressed in Task 2.

---

## Phase 1: Prerequisites

These tasks address gaps discovered during design that must be resolved
before the `.github` repo is created.

### Task 0: Upstream Tooling Fix (vergil-tooling)

**Files:** None (separate vergil-tooling change)

The `vergil.toml` config parser currently requires
`[dependencies].vergil-tooling`. This is a migration artifact being
consolidated to just `[dependencies].vergil`. That change must land
before Task 4's `vergil.toml` (which omits `[dependencies]`) can
pass validation.

- [ ] Verify the config parser accepts `[dependencies].vergil`
      without requiring `vergil-tooling`

### Task 1: Enable GitHub Discussions on All Four Repos

**Files:** None (GitHub API / web UI)

SUPPORT.md references GitHub Discussions as the channel for questions
and general conversation. Discussions must be enabled before
SUPPORT.md goes live.

- [ ] Enable Discussions on `vergil-project/vergil-tooling` via
      `gh repo edit --enable-discussions`
- [ ] Enable Discussions on `vergil-project/vergil-actions`
- [ ] Enable Discussions on `vergil-project/vergil-docker`
- [ ] Enable Discussions on `vergil-project/vergil-claude-plugin`
- [ ] Verify: visit each repo's Discussions tab in browser to confirm
      it is active

### Task 2: Add Missing LICENSE Files

**Files:** `LICENSE` in vergil-docker, vergil-claude-plugin

vergil-tooling and vergil-actions have GPL-3 licenses.
vergil-docker and vergil-claude-plugin do not. GitHub does not
inherit LICENSE files — each repo must have its own. These must
exist before CONTRIBUTING.md references a unified license.

- [ ] Copy GPL-3 LICENSE to vergil-docker (PR)
- [ ] Copy GPL-3 LICENSE to vergil-claude-plugin (PR)
- [ ] Verify: GitHub shows "GPL-3.0" badge on both repos

---

## Phase 2: Create the `.github` Repository

All content is created in a single repo. Tasks are ordered by
dependency — the repo must exist before files can be pushed, and
some files reference others.

### Task 3: Create the Repository

**Files:** None (GitHub API)

- [ ] Create `vergil-project/.github` as a **public** repository
      via `gh repo create vergil-project/.github --public --description "Org-level configuration, community health files, and profile for the vergil-project organization"`
- [ ] Clone locally to the standard project directory
- [ ] Initialize with `develop` as the default branch (matching
      all other VERGIL repos)

### Task 4: VERGIL Tooling Harness

**Files:** `vergil.toml`, `.githooks/pre-commit`, `.claude/settings.json`, `CLAUDE.md`

Minimal tooling setup so the repo follows the same development
workflow as the other four repos.

- [ ] Create `vergil.toml` with `primary-language = "none"` and the
      five required `[project]` fields. No `[ci]`, `[publish]`, or
      `[dependencies]` sections initially — add them if needed when
      CI is configured in Task 10.

      ```toml
      [project]
      repository-type = "documentation"
      versioning-scheme = "semver"
      branching-model = "library-release"
      release-model = "tagged-release"
      primary-language = "none"

      [project.co-authors]
      wphillipmoore-agent = "Co-Authored-By: wphillipmoore-agent <284101533+wphillipmoore-agent@users.noreply.github.com>"
      ```

- [ ] Copy `.githooks/pre-commit` from vergil-tooling (identical
      across all repos)
- [ ] Create `.claude/settings.json` with vergil plugin marketplace
      config (identical to other repos)
- [ ] Create `CLAUDE.md` — minimal agent guidance: docs-only repo,
      no Python, use `vrg-commit`, validation via
      `vrg-docker-run -- uv run vrg-validate`
- [ ] Create `LICENSE` — GPL-3 (matching other VERGIL repos)
- [ ] Verify: `git config core.hooksPath .githooks` works, raw
      `git commit` is rejected, `vrg-commit` works

### Task 5: Issue Templates and PR Template

**Files:** `ISSUE_TEMPLATE/issue.yml`, `ISSUE_TEMPLATE/config.yml`, `pull_request_template.md`

Migrated from the per-repo copies. These are identical across all
four repos today.

- [ ] Copy `ISSUE_TEMPLATE/issue.yml` from vergil-tooling
- [ ] Copy `ISSUE_TEMPLATE/config.yml` from vergil-tooling
- [ ] Copy `pull_request_template.md` from vergil-tooling
- [ ] Verify: content is byte-identical to the per-repo versions

### Task 6: CODE_OF_CONDUCT.md

**Files:** `CODE_OF_CONDUCT.md`

- [ ] Adopt Contributor Covenant v2.1 — use the canonical text from
      https://www.contributor-covenant.org/version/2/1/code_of_conduct/
- [ ] Set enforcement contact to the project maintainer (Phillip
      Moore, email from the existing author section)
- [ ] Review: ensure the text is unmodified except for the
      enforcement contact section

### Task 7: SECURITY.md

**Files:** `SECURITY.md`

- [ ] Write vulnerability reporting instructions — preferred channel
      is GitHub's private vulnerability reporting feature (if enabled
      on the org) or private email
- [ ] Define scope: CLI tooling (vergil-tooling), container images
      (vergil-docker), CI workflows (vergil-actions),
      vergil-claude-plugin configuration and hook definitions
- [ ] Define out-of-scope: vulnerabilities in upstream dependencies
- [ ] Set response commitment: acknowledge within 7 days, target fix
      or mitigation plan within 30 days (adjust if these timelines
      don't feel realistic)
- [ ] Enable private vulnerability reporting on the org if not
      already enabled

### Task 8: CONTRIBUTING.md

**Files:** `CONTRIBUTING.md`

The largest file. Content draws from the org governance spec (#717,
Section 5) and the existing development workflow documentation.

- [ ] Write "How the project works" section — four-repo architecture
      overview, what each component does, how they relate
- [ ] Write "Development setup" section — prerequisites (Docker, uv),
      installing vergil-tooling via `uv tool install`, enabling git
      hooks via `git config core.hooksPath .githooks`
- [ ] Write "Workflow" section — branch from `develop`, use
      `vrg-commit`, use `vrg-submit-pr`, validate via
      `vrg-docker-run -- uv run vrg-validate`
- [ ] Write "For contributors using AI tools" section — identity
      model (`<username>-agent`), accountability principle, agent
      identity for commits, human identity for reviews
- [ ] Write "For contributors not using AI" section — fork/branch/PR,
      same quality bar
- [ ] Write "What to expect from review" section — human approval
      required, CI must pass, cross-human review at 2+ contributors
- [ ] Write "Template override convention" section — document the
      all-or-nothing inheritance rule for template directories
- [ ] Review: ensure all claims are consistent with the org
      governance spec and current tooling behavior

### Task 9: SUPPORT.md

**Files:** `SUPPORT.md`

- [ ] Write support channels: GitHub Issues for bugs and feature
      requests, GitHub Discussions for questions and conversation
- [ ] Link to docs site for reference material
- [ ] State clearly: community project, no SLA-backed support
- [ ] Verify: Discussions links point to working URLs (depends on
      Task 1 being complete)

### Task 10: Root README.md

**Files:** `README.md`

Short description of what the `.github` repo is, not the org profile.

- [ ] Write one-paragraph description: this repo holds org-level
      default configuration for the vergil-project organization
- [ ] Explain which files live here and how GitHub's default community
      health file inheritance works
- [ ] Document the template override convention (all-or-nothing per
      directory)
- [ ] Link to the profile README (`profile/README.md`) and
      CONTRIBUTING.md
- [ ] Note the `.github/.github/workflows/` nesting and why it exists

### Task 11: Org Profile README

**Files:** `profile/README.md`

Adapted from the existing draft at
`/Users/pmoore/dev/github/career-strategy/drafts/vergil-readme-draft.md`.

- [ ] Copy the draft as the starting point
- [ ] Update repo URLs: `wphillipmoore/` → `vergil-project/`
- [ ] Update docs site URL to post-rename location
- [ ] Update repo name: `vergil-plugin` → `vergil-claude-plugin`
- [ ] Update tool names: `st-commit` → `vrg-commit`,
      `st-validate` → `vrg-validate`,
      `st-prepare-release` → `vrg-prepare-release`
- [ ] Update "Current state" to reflect v2.x and completed rename
- [ ] Add link to `CONTRIBUTING.md` as a call to action
- [ ] Add link to docs site
- [ ] Review: verify all URLs resolve, all repo names are correct,
      numbers in "By the numbers" table are still accurate

### Task 12: CI Workflow

**Files:** `.github/workflows/ci.yml`

Minimal CI — this is a docs-only repo. The workflow must handle
`primary-language = "none"` in `vergil.toml`.

- [ ] Determine which vergil-actions reusable workflows are
      compatible with `language: none` (or whether a new value is
      needed). At minimum: `ci-quality.yml` for common checks
      (markdownlint, yamllint, shellcheck, actionlint).
- [ ] If vergil-actions workflows require a language value, open a
      follow-up issue to add `none` support — use a minimal inline
      workflow in the meantime
- [ ] Write `ci.yml` referencing the appropriate reusable workflows
- [ ] Verify: push a test branch and confirm CI runs the common
      checks without errors

---

## Phase 3: Consolidation

After the `.github` repo is live and inheritance is verified, remove
the duplicated files from the four existing repos.

### Task 13: Verify Inheritance

**Files:** None (manual testing)

Before removing per-repo files, confirm that org-level defaults
actually work.

- [ ] Navigate to vergil-tooling → Issues → New issue. Confirm the
      org-level issue template appears.
- [ ] Navigate to vergil-actions → Issues → New issue. Confirm the
      same.
- [ ] Open a draft PR on one repo. Confirm the PR template from the
      `.github` repo is populated.
- [ ] Navigate to vergil-tooling → Insights → Community Standards (or
      equivalent). Confirm CONTRIBUTING.md, CODE_OF_CONDUCT.md,
      SECURITY.md, and SUPPORT.md appear as inherited.

**Important:** At this point, per-repo copies still exist. GitHub
should prefer the per-repo copies (the all-or-nothing rule). To
truly test inheritance, temporarily rename one repo's
`ISSUE_TEMPLATE/` directory and confirm the org default takes over,
then rename it back. This verifies the fallback mechanism.

### Task 14: Remove Per-Repo Duplicates

**Files:** Issue templates and PR templates in all four repos

One PR per repo. These can run in parallel since they're independent.

- [ ] vergil-tooling: remove `.github/ISSUE_TEMPLATE/issue.yml`,
      `.github/ISSUE_TEMPLATE/config.yml`,
      `.github/pull_request_template.md`
- [ ] vergil-actions: remove the same three files
- [ ] vergil-docker: remove the same three files
- [ ] vergil-claude-plugin: remove the same three files
- [ ] After merging all four PRs, verify inheritance again: create a
      test issue on each repo and confirm the org template appears

### Task 15: Post-Consolidation Verification

**Files:** None (manual testing)

Final verification that everything works end-to-end.

- [ ] Create a real issue on one repo using the inherited template —
      confirm all fields work (type dropdown, problem/goal,
      acceptance criteria)
- [ ] Confirm blank issues are still disabled (config.yml inheritance)
- [ ] Open a PR on one repo — confirm the PR template appears
- [ ] Visit the org profile page — confirm `profile/README.md`
      renders correctly
- [ ] Spot-check community health files: visit a repo's
      "Community Standards" page and confirm all files show as present
