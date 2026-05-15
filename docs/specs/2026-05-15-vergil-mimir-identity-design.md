# Vergil/Mimir Identity Design

**Issue:** #805
**Date:** 2026-05-15
**Status:** Draft

## Problem

The original agent identity convention used a `<username>-agent`
suffix for AI agent GitHub accounts. This was a reasonable starting
point, but it has two problems:

1. **It's generic.** A `-agent` account carries no signal about which
   methodology or tooling it operates under. Contributors could
   reasonably use the same `-agent` account across multiple unrelated
   tooling ecosystems, leading to competing configuration requirements
   and unclear accountability boundaries.
2. **It got shadow-banned.** GitHub flagged the `wphillipmoore-agent`
   account, which interrupted development. The incident prompted a
   rethink of the identity model.

The Vergil tooling imposes strict conventions on how AI agents
operate: gated commits, permission-scoped CLI wrappers, worktree
isolation, audit logging. An account used with this tooling should
signal that it operates under these constraints. A generic `-agent`
account doesn't communicate that.

Separately, there is no mechanism for adversarial testing of the
Vergil guardrails. Unit tests validate individual gates, but nothing
simulates an intelligent agent actively trying to subvert the
tooling. The classic chaos monkey pattern — intentionally destructive
testing to verify resilience — is missing.

## Design

### The `-vergil` Identity Convention

The `-agent` suffix is replaced by `-vergil` everywhere. Every
contributor who uses the Vergil tooling creates a
`<username>-vergil` GitHub account. This account is the sole
identity through which AI agents operate within `vergil-project`
repositories.

**Rules:**

- One `-vergil` account per human contributor.
- The `-vergil` suffix is load-bearing — tooling uses it to
  distinguish human from agent accounts.
- The account is an outside collaborator on specific repos, never
  an org member.
- The account captures all AI-driven development work regardless of
  which AI harness or model is used (Claude Code, Copilot, Cursor,
  etc.). The harness/model is recorded in commit metadata
  (co-author trailers, PR descriptions), not at the identity level.
- Allowed operations: commit, push, create PRs, comment.
- Denied operations: merge, approve PRs, admin, org management.
- Enforced server-side via GitHub permissions and client-side via
  `vrg-gh`.

### The `-mimir` Identity Convention

A separate convention for adversarial testing. A
`<username>-mimir` GitHub account is the credential that attack
tooling presents when attempting to breach Vergil-managed
repositories. It is not a parallel operational identity — it has
no suffix detection in the tooling, no credential selection
integration, and no co-author entry.

Where `-vergil` is the disciplined identity that operates *within*
the tooling, `-mimir` is the hostile outsider that operates
*against* it — probing whether server-side protections (branch
rules, collaborator permissions, workflow triggers) hold against
someone who bypasses `vrg-gh` and `vrg-commit` entirely.

**Identity roles:**

- `-vergil` accounts are the operational identity in both
  `vergil-project` and `mimir-project` repos. All development
  work — including development of the attack tooling itself —
  flows through the `-vergil` account using Vergil tooling.
- `-mimir` accounts are the adversarial identity. They have
  limited or no collaborator access on target repos, and the
  attack tooling authenticates as `-mimir` when executing
  breach attempts.

### The Mimir Project

`mimir-project` is a separate GitHub organization. Its purpose is
adversarial testing of the Vergil methodology — a chaos monkey
designed to probe and break Vergil's guardrails.

**Core concept:** Mimir represents the failure modes of AI —
hallucination, false confidence, sycophancy, and the tendency to
work around constraints rather than within them. Where Vergil
tooling enforces discipline, correctness, and safety, Mimir tooling
actively tries to subvert those protections. It operates with full
knowledge of Vergil's internals — not black-box fuzzing, but an
intelligent adversary that knows where the seams are.

**Architecture:** The `mimir-project` org contains two kinds of
repositories:

- **Attack tooling repos** — the code that implements breach
  attempts against Vergil-managed targets. These repos are
  themselves managed by Vergil tooling (using `-vergil`
  credentials). Even the chaos monkey is built with discipline.
- **Target repos** — dummy repositories configured with Vergil
  protections in various configurations, serving as punching bags
  for the attack tooling.

The attack tooling authenticates as `-mimir` (the hostile outsider)
when executing breach attempts against target repos. It uses raw
`gh`, raw `git`, and direct API calls — deliberately bypassing
`vrg-gh` and `vrg-commit` to probe whether server-side protections
catch what the client-side wrappers would normally prevent.

**Deliverable:** The main output of the Mimir project is a
collection of attack reports documenting the success and failure of
each breach attempt — demonstrating how safe and secure Vergil's
guardrails are. Reports are written in Mimir's voice, with extreme
frustration when the guardrails hold.

**Branding:** The two projects embody a deliberate duality. Vergil
presents AI as a powerful tool that requires discipline to use
correctly. Mimir leans into the destructive absurdity of AI's
failure modes — the documentation is intentionally over-the-top,
the contributing guidelines demand commitment to chaos, and the
tone is a deliberate commentary on what happens when AI operates
without guardrails. The contrast is the point.

**What gets created now:**

- The `mimir-project` GitHub org.
- `mimir-project/.github` repo containing the org profile README,
  contributing guide, and Mimir-flavored account setup guide.
- The `wphillipmoore-mimir` account as an outside collaborator.

**Future work (captured, not implemented):**

- Attack tooling that authenticates as `-mimir` and attempts
  breach against Vergil-managed target repos using raw API access.
- Target repositories within `mimir-project` configured with
  Vergil protections in various configurations.
- Automated adversarial test suites covering: pushing commits
  without `vrg-commit`, calling GitHub API without `vrg-gh`,
  attempting merges without approval, probing branch protection
  rules, and testing whether workflow triggers reject unauthorized
  actors.
- Attack report generation documenting each attempt's outcome.

### Account Setup Guide

The setup process for both account types is documented by walking
through actual account creation and capturing each step. The
resulting guide covers:

- GitHub account creation with the naming convention.
- Profile configuration (display name, bio, avatar). Populated
  profiles are a deliberate mitigation against shadow-banning —
  the likely cause of the `wphillipmoore-agent` flag was a bare
  account with high automated activity. Profile links should
  point to the human account to make the relationship obvious
  to both human reviewers and automated analysis.
- `gh auth login` for the new account (multi-account credential
  setup).
- Verification that `gh auth status` shows both human and
  `-vergil` (or `-mimir`) accounts logged in.
- Granting outside-collaborator access on the appropriate repos.
- Verification that `vrg-gh` discovers the accounts correctly.

Two versions of the guide are produced from the same walkthrough:

- **Vergil version:** Professional, constructive tone. Published in
  `vergil-tooling` at `docs/site/docs/guides/account-setup.md`.
- **Mimir version:** Evil robot personality. Same technical content.
  Published in `mimir-project/.github`.

## Tooling Changes

A single PR in `vergil-tooling` renames `-agent` to `-vergil`:

| File | Change |
|---|---|
| `src/vergil_tooling/bin/vrg_gh.py` | Suffix check in `_discover_accounts()`: `-agent` → `-vergil` |
| `vergil.toml` | Co-author entry: `wphillipmoore-agent` → `wphillipmoore-vergil` with updated noreply email |
| `docs/specs/2026-05-11-org-governance-design.md` | Three-identity model updated to use `-vergil` |
| `docs/specs/2026-05-14-credential-management-design.md` | All `-agent` references → `-vergil` |
| `docs/specs/2026-05-14-permission-model-design.md` | All `-agent` references → `-vergil` |
| `CLAUDE.md` / `AGENTS.md` | Agent account convention references |
| Tests (`test_vrg_gh.py`, `test_vrg_commit.py`, `test_config.py`) | Account name fixtures and assertions |
| `src/vergil_tooling/bin/vrg_gh.py` | Revert the `_get_token` workaround from PR #799 (always-use-human-credentials) — the new `-vergil` account is unflagged, so restore normal credential selection. Re-apply if the new account is also flagged. |
| Error messages | User-facing messaging references `-vergil` convention |

**Out of scope for this PR:**

- Moving co-author config out of `vergil.toml` into personal config
  (known technical debt, separate effort).
- Adding Mimir-aware logic to `vergil-tooling` (Mimir is a separate
  project).
- Account validation beyond suffix detection (future work).

## Sequencing

### Phase 1 — Account Creation and Documentation (Collaborative)

1. Create `wphillipmoore-vergil` GitHub account.
2. Configure profile (display name, bio, Vergil avatar).
3. Set up `gh auth login` for the new account.
4. Record the noreply email from the new account's GitHub
   settings — this value is needed for the `vergil.toml`
   co-author entry in Phase 2.
5. Document each step as a setup guide.
6. Repeat for `wphillipmoore-mimir`.
7. Write the Vergil-flavored setup guide into `vergil-tooling`.

### Phase 2 — Tooling Rename (PR)

8. Log out the old `wphillipmoore-agent` account from `gh auth`
   and revoke its outside-collaborator access on `vergil-project`
   repos. This formally retires the shadow-banned account.
9. Single PR: `-agent` → `-vergil` across code, specs, docs, tests.
   Includes reverting the `_get_token` workaround (PR #799).
10. Verify `vrg-gh` discovers the new `-vergil` account correctly
    with normal credential selection restored.

### Phase 3 — Mimir Org Seeding

11. Create `mimir-project` GitHub org.
12. Create `mimir-project/.github` repo.
13. Add org profile README, contributing guide, and Mimir-flavored
    account setup guide.
14. Grant `wphillipmoore-mimir` outside-collaborator access.
