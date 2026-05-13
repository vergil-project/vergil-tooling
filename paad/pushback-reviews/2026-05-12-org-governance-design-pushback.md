# Pushback Review: VERGIL Org Governance Design

**Date:** 2026-05-12
**Spec:** docs/specs/2026-05-11-org-governance-design.md
**Commit:** 2eff7ed

## Source Control Conflicts

None — no conflicts with recent changes. The spec's assumptions about
the current state of `st-merge-when-green`, `st-prepare-release`,
`st-commit`, and the VERGIL rename spec were all verified against the
codebase and found accurate.

## Issues Reviewed

### [1] Release workflow auto-approval undermines Principle 3
- **Category:** Contradiction
- **Severity:** Serious
- **Issue:** The spec had `vrg-release` using the agent PAT to create
  release PRs and the human PAT to auto-approve them in the same script.
  This technically satisfied branch protection but violated Principle 3
  ("nothing ships until a human understands what it does and why") and
  misattributed mechanized automation as AI-authored work in the audit
  trail.
- **Resolution:** Introduced a GitHub App (`vergil-release`) as a third
  identity category for mechanized automation. The App creates release
  PRs (visually distinct as `vergil-release[bot]`), and the human PAT
  approves and merges. This gives a clean three-way audit trail: human /
  AI / automation. The existing GitHub App (used in CI for version bump
  PRs) is migrated to the org rather than creating a new one. Updated
  Sections 1, 2, 3, 4, and 6.

### [2] "No bypass" policy is practically bypass-with-extra-steps
- **Category:** Ambiguity
- **Severity:** Serious
- **Issue:** The spec said "there are no bypass permissions" but an org
  owner can temporarily modify the ruleset, merge, and restore it — which
  is a bypass with extra steps. The framing created false assurance, and
  the audit trail for ruleset modification is harder to correlate than a
  single bypass event.
- **Resolution:** Reframed as "No Standing Bypass Policy." The policy is
  honest about what it is: no standing bypass permission, but exceptions
  require deliberate ruleset modification. Any such modification is
  treated as an incident to be retrospected.

### [3] No credential compromise or rotation plan
- **Category:** Omission
- **Severity:** Moderate
- **Issue:** Section 3 defined credential storage and selection but said
  nothing about rotation schedules, compromise response, PAT expiration,
  or ongoing observability of the credential namespace.
- **Resolution:** Added a comprehensive Credential Lifecycle subsection
  covering: 1-year PAT expiration with proactive rotation, a credential
  audit tool (`vrg-credential-audit`) reporting on expiration, staleness,
  and usage patterns, a five-step compromise response procedure with a
  blast-radius table, and ongoing observability of the credential
  namespace as an operational concern.

### [4] "Require branches to be up to date" creates merge serialization
- **Category:** Feasibility
- **Severity:** Moderate
- **Issue:** The "require up to date" rule serializes all merges to
  `develop` — at scale of multiple contributors, only one PR can merge
  at a time, and the rest must rebase and re-run CI. GitHub merge queue
  solves this but requires a paid plan.
- **Resolution:** Retained the rule with an explicit rationale (dropping
  it opens a correctness hole that contradicts the project's commitment
  to extreme hygiene). Documented GitHub merge queue as the correct
  long-term solution when the org moves to a paid plan. Added a
  corresponding entry in the Risks table.

### [5] Credential selection mechanism is underspecified
- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** Section 3 said credentials were selected "via session launch
  mechanism — shell wrapper, Claude Code hook, or equivalent" without
  specifying invariants, platform support, credential naming, or the
  boundary between what tooling can enforce and what falls to guidelines.
- **Resolution:** Replaced the Credential Selection and `gh` CLI
  subsections with a comprehensive Credential Tooling section
  establishing: the core principle ("the tool selects the credential,
  not the developer"), standard credential names across all platforms,
  platform-specific secure storage backends (macOS Keychain, Linux
  Secret Service, Windows Credential Manager) using the `keyring` Python
  library as abstraction, credential isolation by tool context, per-
  subprocess `GH_TOKEN` scoping, and an honest accounting of what the
  tooling cannot enforce on developer machines.

### [6] Co-author metadata migration not coordinated with rename
- **Category:** Omission
- **Severity:** Minor
- **Issue:** The governance spec retired `wphillipmoore-claude` and
  `wphillipmoore-codex` in favor of `wphillipmoore-agent` but did not
  mention updating the co-author config entries in `standard-tooling.toml`.
- **Resolution:** Added a note in the Migration subsection that the
  per-harness co-author entries (`claude`, `codex`) are replaced with a
  single `agent` entry, coordinated with the VERGIL rename.

### [7] External contributor governance deferral affects #719
- **Category:** Omission
- **Severity:** Minor
- **Issue:** The cross-human review check (#719) parses the
  `<username>-agent` naming convention. External contributors who don't
  follow the convention would break the check.
- **Resolution:** Added a note in the deferral section flagging the #719
  dependency. Also added a "What Is Explicitly Out of Scope" subsection
  rejecting autonomous AI contributions as a foundational design decision.
  Added an exemption for App-authored PRs in the cross-human review
  section.

## Unresolved Issues

None — all issues were addressed.

## Consistency Pass

A final read-through after all edits caught and fixed three additional
issues:

1. Section 4 intro still said "entirely under human credentials" after
   the App identity was introduced — updated to reflect the App + human
   credential split
2. Cross-human review section did not account for App-authored PRs
   (`vergil-release[bot]` doesn't match the `-agent` pattern) — added
   explicit exemption
3. Section 6 Org-Level Rulesets still said "no bypass" instead of "no
   standing bypass" — aligned to the renamed policy terminology

## Summary

- **Issues found:** 7
- **Issues resolved:** 7
- **Unresolved:** 0
- **Spec status:** Ready for implementation planning
