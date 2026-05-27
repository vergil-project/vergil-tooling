# AI contribution compliance review

**Issue:** [#1223](https://github.com/vergil-project/vergil-tooling/issues/1223)
**Date:** 2026-05-27

## Context

The CPython project published updated guidelines for using AI tools
when contributing to CPython. The guidelines codify best practices
that apply broadly to any AI-assisted contribution workflow:
accountability, review discipline, explainability, disclosure, scope
control, test integrity, and style consistency.

This spec reviews Vergil's compliance posture against those
principles. It applies generally to all Vergil-managed repositories,
not only to CPython upstream contributions. CPython's guidelines are
the catalyst; the compliance bar is universal.

**Reference:** [CPython AI contribution guidelines][cpython-discuss]

[cpython-discuss]: https://discuss.python.org/t/updated-guidelines-for-using-ai-tools-when-contributing-to-cpython/107462

## Guideline principles

Seven principles normalized from CPython's guidelines:

| # | Principle | Source |
|---|-----------|--------|
| P1 | **Human accountability** — the human contributor bears full responsibility for all submissions | "Contributors bear full responsibility" |
| P2 | **Review before submission** — AI output must be carefully reviewed before opening a PR or issue | "AI output must be reviewed carefully" |
| P3 | **Explainability** — contributors must understand changes well enough to explain them independently | "Must understand their changes" |
| P4 | **Disclosure readiness** — contributors should be prepared to describe how AI tools were used | "Describe how AI tools were used if asked" |
| P5 | **Minimal, focused changes** — modifications should be minimal and focused | "Modifications should be minimal and focused" |
| P6 | **Test integrity** — tests must genuinely validate changes; weakening or removing tests to force passing is prohibited | "Altering, bypassing, or removing tests is prohibited" |
| P7 | **Style consistency** — existing code style must be maintained | "Existing code style must be maintained" |

CPython's backward-compatibility requirement is folded into P5
(minimal changes inherently reduce compatibility risk). Their
repeat-offender blocking policy is an upstream enforcement mechanism
outside Vergil's scope.

## Compliance status levels

| Level | Meaning |
|-------|---------|
| **Enforced** | Tooling prevents violation (hook guard, CI gate, validation pipeline) |
| **Structured** | Workflow strongly encourages compliance but does not mechanically prevent violation |
| **Advisory** | Documented guidance exists but no enforcement mechanism |
| **Gap** | No current mechanism addresses this principle |

## Compliance matrix

| Principle | Status | Vergil enforcement | Evidence |
|-----------|--------|--------------------|----------|
| P1: Human accountability | **Structured** | All PRs require manual merge — no auto-merge. Human must explicitly act to land code. Protected branch rules prevent direct commits to `develop`/`main`. | `vrg-submit-pr`, `vrg-commit` branch protection checks, org-wide auto-merge disabled |
| P2: Review before submission | **Structured** | PR workflow is the only path to protected branches. Human reviews diff before merging. No mechanism forces the human to actually read the diff. | `vrg-submit-pr`, branch protection rules, worktree convention (main worktree is read-only) |
| P3: Explainability | **Advisory** | No tooling verifies that the human understands the changes they are merging. Conventional commit messages and PR descriptions provide context but do not verify comprehension. | `vrg-commit` (conventional format), PR body template via `vrg-submit-pr` |
| P4: Disclosure readiness | **Enforced** | `vrg-commit` reads `VRG_CO_AUTHOR` from the environment and appends a `Co-Authored-By` trailer to every commit made in an AI agent session. Every commit in git history is attributable to its AI tool. | `vrg-commit` (`VRG_CO_AUTHOR` env var, co-author trailer), Claude Code harness sets the env var |
| P5: Minimal, focused changes | **Structured** | Worktree convention enforces one issue per worktree. Branch naming encodes issue number (`feature/<N>-<slug>`). Agent instructions in CLAUDE.md discourage scope creep. No mechanical enforcement that a diff is minimal. | Worktree convention, CLAUDE.md agent instructions, branch naming |
| P6: Test integrity | **Enforced** | `vrg-validate` runs the full test suite as mandatory pre-commit validation. CI re-runs tests independently (Tier 2). Tests cannot be skipped without failing the pipeline. **Caveat:** nothing prevents an agent from weakening test assertions to match wrong behavior. | `vrg-validate`, `.github/workflows/ci.yml` |
| P7: Style consistency | **Enforced** | Linting (ruff), type checking (mypy), markdownlint, shellcheck, yamllint, hadolint, actionlint — all enforced via `vrg-validate`. Conventional commit format enforced by `vrg-commit`. | `vrg-validate` command registry, `pyproject.toml` (ruff/mypy config) |

### Summary

- **Enforced (3):** P4 (disclosure), P6 (test integrity — with caveat), P7 (style)
- **Structured (3):** P1 (accountability), P2 (review), P5 (minimal changes)
- **Advisory (1):** P3 (explainability)
- **Gap (0):** no principle is completely unaddressed

## Gap analysis

### P3: Explainability — advisory only

This is the weakest area. Nothing in Vergil's workflow verifies
that the human contributor understands the changes they are
merging. Conventional commit format and PR descriptions provide
context, but a human could merge AI-generated code they do not
fully comprehend.

This is inherently difficult to enforce mechanically — it is a
human discipline problem. Possible mitigations worth exploring:

- A structured self-attestation step in `vrg-submit-pr` (e.g.,
  a checklist the human must acknowledge)
- Documentation guidance that explicitly sets the expectation
- PR template additions that prompt the human to summarize the
  change in their own words

**Follow-up:** to be filed as a separate issue.

### P6: Test integrity — enforced with a caveat

Tests must pass, but nothing prevents an agent from weakening
test assertions to make a broken implementation pass. For example,
an agent could change `assert result == 42` to
`assert result is not None` and the pipeline would still succeed.

This is a known hard problem in AI-assisted development. Possible
mitigations worth exploring:

- A test-diff review prompt in the PR workflow that highlights
  assertion changes
- A heuristic check for assertion weakening (detecting loosened
  comparisons in test files)
- Explicit prohibition in agent instructions (CLAUDE.md) with
  guidance on what constitutes test weakening

**Follow-up:** to be filed as a separate issue.

### Structured principles (P1, P2, P5)

Human accountability, review-before-submission, and minimal
changes are all enforced by workflow structure (mandatory PRs,
one-issue-per-worktree, manual merge) but ultimately depend on
the human acting responsibly. This is appropriate —
over-mechanizing these would create friction without proportional
benefit. Classified as acceptable as-is rather than gaps needing
remediation.

## Scope boundary

Out of scope for this review:

- **Project-specific style guides.** Vergil enforces its own
  linting and style rules. When contributing to an upstream
  project, the contributor is responsible for following that
  project's specific style guide. Vergil does not attempt
  per-upstream linting configuration.

- **Upstream enforcement mechanisms.** CPython's policy on
  blocking repeat offenders is their enforcement, not ours.
  Vergil's job is to prevent low-quality submissions from being
  made in the first place.

- **Backward compatibility analysis.** Folded into P5 (minimal
  changes). Semantic backward-compatibility checking is not a
  realistic tooling target for a general-purpose workflow.

- **Non-code contributions.** Issue-filing workflows via `vrg-gh`
  do not currently have AI-quality guardrails for issue text.
  This is low risk and low priority compared to code
  contributions.

## Follow-up issues

| # | Title | Principle | Purpose |
|---|-------|-----------|---------|
| TBD | Explore explainability attestation in `vrg-submit-pr` | P3 | Evaluate options for a human comprehension gate in the PR submission workflow |
| TBD | Explore test integrity guardrails against assertion weakening | P6 | Evaluate options for detecting or preventing test assertion weakening by AI agents |
