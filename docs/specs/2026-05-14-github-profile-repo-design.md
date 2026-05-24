# `.github` Profile Repository Design

**Issue:** #753
**Date:** 2026-05-14
**Status:** Draft

## Problem

The `vergil-project` GitHub org has four repositories with identical
community health files (issue templates, PR templates) duplicated in
each. There is no org-level README, no CONTRIBUTING.md, no SECURITY.md,
no CODE_OF_CONDUCT.md, and no SUPPORT.md. The org profile page is
blank.

GitHub's `.github` profile repository provides org-level defaults that
are inherited by any repo that does not have its own copies. This design
consolidates duplicated files into the org-level repo, adds the missing
community health files, and establishes the org's public-facing profile.

## Approach

**Full consolidation with documented override convention (Approach C).**
All default community health files live in the `.github` repo. Per-repo
copies are removed. If a repo ever needs custom templates, it adds its
own full directory — GitHub's inheritance is all-or-nothing per
directory, not per file. This override mechanism is documented in
CONTRIBUTING.md so future contributors understand the model.

## Section 1: Repository Structure

```
vergil-project/.github/
├── profile/
│   └── README.md                  # Org profile page (public-facing)
├── ISSUE_TEMPLATE/
│   ├── issue.yml                  # Default issue template (migrated)
│   └── config.yml                 # Disables blank issues
├── pull_request_template.md       # Default PR template (migrated)
├── CONTRIBUTING.md                # Contributor guidelines
├── SECURITY.md                    # Vulnerability reporting policy
├── CODE_OF_CONDUCT.md             # Contributor Covenant v2.1
├── SUPPORT.md                     # Where to get help
├── .github/
│   └── workflows/
│       └── ci.yml                 # CI for this repo (common checks only)
├── .githooks/
│   └── pre-commit                 # Standard vrg-commit gate
├── .claude/
│   └── settings.json              # Vergil plugin marketplace config
├── CLAUDE.md                      # Agent guidance for this repo
├── vergil.toml                    # Minimal config (no primary_language)
├── README.md                      # Repo-level: what this repo is
└── LICENSE                        # GPL-3
```

### Key decisions

- The `.github` repo **must be public** for default community health
  files to be inherited by other repos.
- `profile/README.md` is the polished marketing-facing README (adapted
  from the existing draft). Root `README.md` is a short description of
  what the `.github` repo is and how org-level defaults work.
- The `.github/.github/workflows/` nesting is correct — the repo's own
  CI config lives in its own `.github/` directory like any other repo.
- `vergil.toml` uses `primary-language = "none"` so `vrg-validate`
  runs only common checks (markdownlint, yamllint, shellcheck,
  actionlint). All five `[project]` fields are required by the
  config parser; a value of `"none"` is the mechanism for skipping
  language-specific checks.
- No `FUNDING.yml` — GitHub Sponsors is a future concern.
- No `workflow-templates/` — the four VERGIL repos are infrastructure,
  not a pattern that gets replicated. Workflow templates add value in
  orgs where new repos are stamped out frequently.

## Section 2: Community Health Files

### CONTRIBUTING.md

Content outline:

- **How the project works** — brief orientation to the four-repo
  architecture and how they relate.
- **Development setup** — prerequisites (Docker, `uv`), installing
  vergil-tooling via `uv tool install`, enabling git hooks.
- **Workflow** — branch from `develop`, use `vrg-commit` for commits,
  use `vrg-submit-pr` for PRs, all validation runs via
  `vrg-container-run -- uv run vrg-validate`.
- **For contributors using AI tools** — the identity model
  (`<username>-agent` account), accountability principle, all AI work
  committed under the agent identity, all reviews under the human
  identity. Drawn from the org governance spec (#717, Section 5).
- **For contributors not using AI** — standard fork/branch/PR model,
  same quality bar.
- **What to expect from review** — PRs require human approval, CI must
  pass, cross-human review at 2+ contributors.
- **Template override convention** — if a repo needs custom issue or PR
  templates, it must provide the full set in its own directory.
  GitHub's org-level defaults are all-or-nothing per directory — a
  repo with any file in `.github/ISSUE_TEMPLATE/` loses all org
  defaults for that directory.

### SECURITY.md

- **Reporting** — private email or GitHub's private vulnerability
  reporting feature (if enabled on the org).
- **Scope** — what counts as a security issue: the CLI tooling, the
  container images, the CI workflows, and the vergil-claude-plugin
  configuration and hook definitions. Exact scope to be refined
  during implementation.
- **Response commitment** — acknowledgment and fix timelines, realistic
  for a small project that intends to grow.
- **Out of scope** — vulnerabilities in upstream dependencies that
  should be reported to the upstream maintainer.

### CODE_OF_CONDUCT.md

- Adopt the **Contributor Covenant v2.1** — the industry standard,
  widely recognized. Adopting a recognized standard is stronger than
  writing a custom one.
- Enforcement section names the project maintainer as the contact.

### SUPPORT.md

- **GitHub Issues** for bugs and feature requests.
- **GitHub Discussions** for questions and general conversation.
  Discussions must be enabled on all four repos as a prerequisite.
- **Docs site** for reference material.
- Clear statement that this is a community project, not a commercial
  product with SLA-backed support.

## Section 3: Org Profile README

The `profile/README.md` is adapted from the existing draft at
`/Users/pmoore/dev/github/career-strategy/drafts/vergil-readme-draft.md`.

### Updates needed

- Repo URLs: `wphillipmoore/` to `vergil-project/`.
- Docs site URL: updated to post-rename location.
- Repo name: `vergil-plugin` to `vergil-claude-plugin`.
- Tool names: `st-commit`, `st-validate`, `st-prepare-release` to
  `vrg-commit`, `vrg-validate`, `vrg-prepare-release`.
- "Current state" section: updated to reflect v2.x status and the
  completed rename.

### Structural decisions

- The "By the numbers" table and architecture diagram stay — they
  demonstrate scope concisely.
- The "Author" section stays — personal credibility is part of the
  story.
- The "Components" table becomes the primary navigation to the four
  repos.
- Link to `CONTRIBUTING.md` at the bottom as a call to action.
- Link to the docs site for detailed reference material.

### Root README.md (separate from the profile)

- One paragraph: "This repository holds org-level default configuration
  for the vergil-project GitHub organization."
- Explains what files live here and how GitHub's default community
  health file inheritance works.
- Documents the template override convention.
- Links to the profile README and CONTRIBUTING.md.

## Section 4: Consolidation

This section covers follow-up work in the four consuming repos,
after the `.github` repo is created and inheritance is verified.

### Removed from all four repos

Once the org-level defaults are in place, the identical per-repo copies
are removed from vergil-tooling, vergil-actions, vergil-docker, and
vergil-claude-plugin:

- `.github/ISSUE_TEMPLATE/issue.yml`
- `.github/ISSUE_TEMPLATE/config.yml`
- `.github/pull_request_template.md`

### What stays per-repo

- `.github/workflows/` — CI workflows are per-repo by nature.
- `.githooks/` — local to each clone, not inheritable via GitHub.
- `.claude/settings.json` — Claude Code config, not a GitHub feature.
- `CLAUDE.md` — agent guidance specific to each codebase.
- `vergil.toml` — repo-specific project config.
- `LICENSE` — must exist per-repo (GitHub does not inherit licenses).
- `README.md` — each repo has its own description.

### Rollout order

1. Enable GitHub Discussions on all four repos.
2. Create the `.github` repo with all org-level files.
3. Verify inheritance — create a test issue in one repo, confirm the
   org template appears.
4. Remove per-repo copies in coordinated PRs (one per repo, can be
   parallel).
5. Verify again post-removal — confirm templates still work via
   inheritance.

### Risk mitigation

- Restoring per-repo files is a one-commit fix per repo if inheritance
  breaks unexpectedly.
- Empty `.github/ISSUE_TEMPLATE/` directories are removed entirely
  after migration (Git does not track empty directories).

### Template override edge case

Documented in CONTRIBUTING.md: if a repo needs a custom issue template,
it must recreate the full `ISSUE_TEMPLATE/` directory with all
templates. GitHub does not merge per-repo templates with org defaults —
the presence of any file in the per-repo directory replaces the entire
org default for that directory. Standalone files (CONTRIBUTING.md,
SECURITY.md, etc.) inherit independently on a per-file basis.

## GitHub Inheritance Reference

Summary of GitHub's `.github` repo inheritance behavior, documented here
for clarity:

| File type | Inheritance model | Override behavior |
|---|---|---|
| Standalone files (CONTRIBUTING, SECURITY, etc.) | Per-file fallback | Repo's own file hides org default for that file only |
| Template directories (ISSUE_TEMPLATE/) | Per-directory fallback | Any file in repo's directory replaces entire org directory |
| PR template | Per-file fallback | Repo's own template hides org default |
| Profile README | Not inherited | Only displays on org profile page |
| Workflow templates | Copied, not inherited | Templates are starting points, not live defaults |
| LICENSE | Not inherited | Must exist per-repo |
| .githooks, CLAUDE.md, vergil.toml | Not GitHub features | Per-repo only, never inherited |

## Dependencies

- Org governance spec (#717) — contributor guidelines in
  CONTRIBUTING.md draw from Section 5.
- VERGIL rename — complete. URLs in the profile README reflect the
  current `vergil-project/` org.
- Missing LICENSE files — vergil-docker and vergil-claude-plugin do
  not have LICENSE files. These must be added (GPL-3, matching
  vergil-tooling and vergil-actions) before CONTRIBUTING.md can
  reference a unified license across the org.

## Risks

| Risk | Mitigation |
|---|---|
| Inheritance not working as expected | Verify with a test issue before removing per-repo copies |
| Future repo needs custom templates but developer doesn't know about the all-or-nothing rule | Documented in CONTRIBUTING.md and in the `.github` repo's root README |
| CI workflow untested with `primary-language = "none"` | Verify during implementation that vergil-actions CI steps handle this config correctly |
| `.github/.github/workflows/` nesting confuses contributors | Documented in root README; inherent to how GitHub handles the `.github` repo |
