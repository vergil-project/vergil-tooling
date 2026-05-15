# Identity Architecture

VERGIL enforces a hard separation between human and AI agent
identity at the GitHub account level. This page describes the
identity model, the naming conventions that make it work, and how
it extends to adversarial testing.

For the step-by-step account creation process, see
[Account Setup](account-setup.md). For how credentials are managed
at runtime, see [Credential Management](credential-management.md).

## The Three Identities

Every VERGIL-managed org operates with three distinct identities:

| Identity | Role | Scope |
|---|---|---|
| `<username>` | Human — reviews, approvals, merges, admin | Org owner or member across all orgs |
| `<username>-vergil` | AI agents — all development work | Outside collaborator on specific repos |
| `vergil-release[bot]` | GitHub App — mechanized release automation | Org-level installation per org |

The human account owns the decision-making authority: code review,
PR approval, merge, and administrative operations. The agent
account does the development work: commits, pushes, PR creation,
issue tracking. The GitHub App handles release automation that
requires neither human judgment nor agent identity.

## The `-vergil` Convention

The `-vergil` suffix is load-bearing — the tooling depends on it.

### How Discovery Works

`vrg-gh` and `github.py` discover accounts by parsing
`gh auth status` and finding the one account that ends in
`-vergil`. The human account name is derived by stripping the
suffix. No configuration file maps usernames — the convention
itself is the configuration.

```text
gh auth status
  ✓ Logged in to github.com account jdoe (keyring)
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  ✓ Logged in to github.com account jdoe-mimir (keyring)
```

The tooling sees `jdoe-vergil`, derives `jdoe` as the human
account, and ignores `jdoe-mimir` entirely. Any number of other
accounts can be present — only the `-vergil` suffix matters.

### What the Agent Account Can Do

- Commit and push to feature branches
- Create pull requests (via `vrg-submit-pr`)
- Create and comment on issues
- Read repository and CI status

### What the Agent Account Cannot Do

- Merge pull requests
- Approve pull requests
- Access admin settings
- Manage org membership
- Create or delete repositories

These restrictions are enforced at multiple layers: GitHub's
own permissions (outside collaborator with Write access), the
`vrg-gh` wrapper's subcommand validation, and Claude Code's
permission deny rules. See
[Permission Model](permission-model.md) for the full
defense-in-depth architecture.

### Outside Collaborator by Design

The agent account is an outside collaborator on each repo, never
an org member. This is intentional:

- Outside collaborators cannot access org-level settings
- Access is granted per-repo, not per-org
- Removing access is a single operation per repo
- The account cannot see private repos it hasn't been invited to

### One Account, All Orgs

A single `-vergil` account works across every org the contributor
participates in. Classic PATs are not scoped to a single org, so
one token covers `vergil-project`, `vergils-nemesis`, and any
future orgs. Adding a new org requires only an outside collaborator
invitation — no new accounts, no new tokens.

### Harness Independence

The `-vergil` account captures all AI-driven development work
regardless of which AI tool is being used — Claude Code, Copilot,
Cursor, or any future harness. The specific tool is recorded in
commit metadata (co-author trailers, PR descriptions), not at the
identity level. The account represents "AI operating under Vergil
discipline," not "Claude Code specifically."

## The `-mimir` Convention

Mimir is the adversarial testing counterpart to Vergil. Where
Vergil represents AI operating with discipline, Mimir represents
AI's failure modes — hallucination, false confidence, sycophancy,
and the tendency to work around constraints rather than within
them.

A `<username>-mimir` GitHub account is the credential that attack
tooling presents when attempting to breach Vergil-managed repos.
It has no integration with the Vergil tooling — no suffix
detection, no credential selection, no co-author entry. It
operates *against* the tooling, not within it.

### Identity Roles

- `-vergil` accounts are the operational identity in both
  `vergil-project` and `vergils-nemesis` repos. All development
  work — including development of the attack tooling itself —
  flows through the `-vergil` account using Vergil tooling.
- `-mimir` accounts are the adversarial identity. They
  authenticate when executing breach attempts, using raw `gh`,
  raw `git`, and direct API calls — deliberately bypassing
  `vrg-gh` and `vrg-commit`.

### The Vergils-Nemesis Org

[vergils-nemesis](https://github.com/vergils-nemesis) is the
GitHub org for adversarial testing. It contains two kinds of
repositories:

- **Attack tooling repos** — code that implements breach attempts
  against Vergil-managed targets. These repos are themselves
  managed by Vergil tooling (built with discipline, used for
  destruction).
- **Target repos** — dummy repositories configured with Vergil
  protections, serving as test beds for the attack tooling.

The main output is attack reports documenting the success and
failure of each breach attempt — demonstrating whether Vergil's
guardrails hold under adversarial pressure.

## The GitHub App

`vergil-release[bot]` is an org-level GitHub App that handles
mechanized release automation. It is not a contributor identity —
it is an automation identity with its own auth flow (JWT exchange
for short-lived installation tokens).

The App creates release PRs, and the human account approves and
merges them. This separation ensures that no single identity can
both create and approve a release.

## Related

- [Account Setup](account-setup.md) — creating and configuring
  both accounts
- [Credential Management](credential-management.md) — how tokens
  are stored and selected at runtime
- [Permission Model](permission-model.md) — enforcement layers
  that constrain agent operations
- [Git Workflow](git-workflow.md) — the per-change development
  cycle
