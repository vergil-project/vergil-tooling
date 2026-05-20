# Agent Identity Design: GitHub Apps + VM Isolation

**Issue:** #933
**Date:** 2026-05-20
**Status:** Draft
**Supersedes:** Vergil/Mimir identity design (#805), credential
management design (#775) Section 4 (credential selection)
**Related:** #892 (identity VM isolation), #799 (human-credential
workaround)

## Problem

The original identity model required each contributor to create
two additional GitHub accounts (`<username>-vergil` for operational
AI work, `<username>-mimir` for adversarial testing) alongside
their personal account. This three-identity model provided visible
separation between human and agent work in GitHub's UI, server-side
access restriction via account permissions, and enabled branch
protection rules requiring PR reviews (different identities for
author and reviewer).

In practice, the user-account approach has proven unviable:

1. **GitHub shadow-bans agent accounts.** The `wphillipmoore-agent`
   account was flagged. Its replacements (`wphillipmoore-vergil`,
   `wphillipmoore-mimir`) are also flagged — visible to the owner
   when logged in, but returning 404 to all other users and
   unauthenticated requests.

2. **Adoption friction.** Requiring every contributor to create
   and maintain 2-3 GitHub accounts is a significant barrier.
   GitHub's terms of service discourage multiple accounts, and
   the shadow-ban experience suggests automated enforcement
   against this pattern.

3. **The tooling complexity is not justified.** Account discovery
   (`_discover_accounts()`), credential selection (`_get_token()`),
   escalation logic, suffix conventions — all of this code exists
   to manage two credentials on the same filesystem.

However, a simple single-account model (one user account +
fine-grained PAT in the VM) has a fundamental gap: **the agent
cannot be prevented from merging PRs server-side.** GitHub's
`Contents: Read/Write` permission — which the agent needs to push
commits — also grants the ability to merge. The only way to
enforce "the agent cannot merge" server-side is branch protection
rules requiring PR reviews, which in turn require the PR author
and reviewer to be different identities. With a single account,
the human cannot approve their own agent's PRs.

This constraint holds regardless of team size during the solo
developer phase, and remains relevant in early-stage open source
projects where the primary maintainer cannot bottleneck on other
contributors' availability for approvals.

## Design Principle

**The system must never require contributors to create additional
GitHub user accounts. Agent identity separation is achieved
through per-contributor GitHub Apps, which are first-class GitHub
citizens not subject to shadow-banning.**

## Identity Model

### Two Tiers of Agent Credential

The system supports two credential modes for the agent VM. The
GitHub App is the recommended path; fine-grained PATs are a
simpler fallback for contributors who accept the merge-control
tradeoff.

**Tier 1 — Per-Contributor GitHub App (Recommended)**

Each contributor registers a GitHub App for their agent identity.
The App is installed on the orgs they contribute to. Work created
by the agent shows up as `<app-name>[bot]` in GitHub's UI —
visually distinct and clearly automated.

This provides:

- **Server-side merge control.** Branch protection "require
  reviews" works: the App authors the PR, the human approves
  and merges. Different identities, valid approval.
- **Clean audit trail.** PRs are visibly authored by a bot
  identity tied to a specific contributor.
- **No shadow-ban risk.** GitHub Apps are a supported, first-class
  integration mechanism.
- **Short-lived credentials.** Installation tokens expire after
  1 hour. The VM holds the App's private key and refreshes tokens
  transparently. A leaked token is dead in an hour.

**Tier 2 — Fine-Grained PAT (Fallback)**

A contributor who does not want to set up a GitHub App can
provision a fine-grained PAT from their own account into the VM.
The agent operates as the contributor's own identity. This works
but has a known limitation: the agent cannot be prevented from
merging server-side (only `vrg-gh` wrapper enforcement, not
branch protection). Contributors who use this mode accept that
merge control is a software gate, not a hard boundary.

### Per-Contributor GitHub App

Each contributor registers a GitHub App. Naming convention is
TBD (e.g., `<username>-vergil`, or a project-standard pattern).
The App is installed on each org the contributor works in.

**App permissions:**

| Permission | Access | Rationale |
|---|---|---|
| Contents | Read/Write | Push commits, read files |
| Pull requests | Read/Write | Create and update PRs |
| Issues | Read/Write | Create, comment, close |
| Metadata | Read | Required by GitHub |
| Administration | None | No repo settings, no branch rule changes |
| Actions | None | Cannot trigger or modify workflows |
| Organization | None | No org management |

These are the same permissions the fine-grained PAT would have.
The difference is enforcement: branch protection rules can
distinguish the App identity from the human identity, enabling
"require reviews" to function correctly.

**Credentials provisioned into the VM:**

| Credential | Purpose | Lifetime |
|---|---|---|
| App ID | Identifies the App for JWT generation | Permanent (set at App creation) |
| Private key (.pem) | Signs JWTs for installation token exchange | Permanent (rotatable) |
| Installation ID | Identifies the App's installation on a specific org | Permanent per org |

The VM does not store long-lived API tokens. On each GitHub
operation, the tooling generates a JWT from the private key,
exchanges it for a 1-hour installation token, and uses that token
for the API call. Token refresh is transparent — the pattern
already exists in the `vergil-release[bot]` implementation
(~20 lines of Python using PyJWT).

**How commits and PRs work:**

- **Git commits** are authored by the human. `git config
  user.name` and `user.email` inside the VM are set to the
  contributor's identity. The `Co-Authored-By` trailer identifies
  the AI harness and model. The human is accountable for the
  work at the commit level.
- **PR creation** uses the App's installation token. GitHub shows
  the PR as authored by `<app-name>[bot]`. The human can approve
  and merge this PR because it was created by a different identity.
- **Branch protection** works correctly: PR author (bot) is not
  the same identity as the reviewer (human). "Require reviews"
  is enforceable.

This split — human as commit author, App as PR creator — gives
both layers of attribution: the commit log traces responsibility
to the human, and the PR history traces automation to the bot.

### Relationship to vergil-release[bot]

The existing `vergil-release[bot]` GitHub App handles mechanized
release automation (branch creation, changelog, release PRs). It
is an org-level shared App — any contributor with access to its
private key can trigger releases.

Per-contributor agent Apps are a different concept: they represent
an individual contributor's AI agent, not a shared automation
process. The two coexist:

- `vergil-release[bot]` — shared, org-level, release automation
- `<username>-vergil[bot]` — per-contributor, agent development
  work

Whether `vergil-release[bot]` should also become per-contributor
is a namespace question deferred to implementation.

### VM Isolation (Unchanged)

The VM architecture from the identity VM isolation design (#892)
is unchanged. The VM provides:

- **Filesystem isolation.** Host secrets are invisible to the
  agent. The human's credentials never enter the VM.
- **Network control.** Egress filtering via HAProxy + pf prevents
  data exfiltration.
- **Credential containment.** The App's private key and any
  derived tokens exist only inside the VM.

The VM boundary is the primary access restriction. The GitHub
App's identity separation is the mechanism for server-side merge
control and audit trail. These are complementary, not redundant.

### Configuration

```toml
# ~/.config/vergil/identities.toml

# Recommended: GitHub App credentials
[identities.vergil]
vm_instance = "vergil-agent"
auth_type = "app"
app_id = 12345
installation_id = 67890
private_key_path = "~/.config/vergil/keys/vergil-agent.pem"

# Fallback: fine-grained PAT (no merge control)
# [identities.vergil]
# vm_instance = "vergil-agent"
# auth_type = "pat"
# github_user = "wphillipmoore"
```

The `auth_type` field determines how `vrg-vm-init` provisions
credentials:

- `app` — injects App ID, installation ID, and private key into
  the VM. The tooling inside the VM handles JWT → installation
  token exchange.
- `pat` — injects a fine-grained PAT (or classic PAT) via
  `gh auth login --with-token`. Simpler setup, weaker merge
  control.

### Audit Trail

Agent work is attributed at two levels:

- **Commit level:** Author is the human contributor.
  `Co-Authored-By` trailer identifies the AI harness and model.
  The human is accountable.
- **PR level:** Author is the contributor's App
  (`<app-name>[bot]`). Visually distinct from human-authored PRs.
  Clearly automated.

In teams, this is immediately useful: "Alice's bot created this
PR, Bob reviewed and approved it." In solo mode: "My bot created
this PR, I reviewed and approved it." Both are clean audit trails.

## Credential Strategy

### GitHub App Authentication (Primary)

The agent VM holds the App's private key. Authentication flow:

1. Tooling reads App ID and private key from VM filesystem
2. Generates a JWT (RS256, 10-minute expiry)
3. Exchanges JWT for an installation token via GitHub API
   (`POST /app/installations/{id}/access_tokens`)
4. Uses the installation token for GitHub operations
5. Token expires after 1 hour; refresh is transparent

This is the same flow used by `vergil-release[bot]`. The
implementation is ~20 lines of Python using PyJWT.

**Where token exchange happens:** `vrg-gh` handles it inline.
Before each `gh` call, `vrg-gh` generates a fresh installation
token via JWT exchange and injects it as `GH_TOKEN` into the
subprocess environment. No background daemon, no separate
helper script, no stale env vars. The JWT exchange adds ~200ms
(one API roundtrip), which is negligible for GitHub operations.
The token exchange function is shared with the
`vergil-release[bot]` implementation in `github.py`.

`vrg-git` uses the same approach for operations that contact
GitHub (`push`, `pull`, `fetch`). It generates a fresh
installation token and configures git to use it via the
`GIT_ASKPASS` or credential helper mechanism. Operations that
are purely local (`add`, `status`, `log`, `diff`) skip token
exchange.

**Multi-org:** A GitHub App can be installed on multiple orgs.
Each installation has its own installation ID. The tooling
selects the correct installation based on which org the current
repository belongs to. One App covers all orgs — no per-org
token management.

### Fine-Grained PAT (Fallback)

Contributors who prefer simplicity over merge control can
provision a fine-grained PAT instead. Scoped to specific
repositories and permissions (Contents, Pull requests, Issues —
Read/Write; no Administration, Actions, or Organization).

**Limitation:** Fine-grained PATs authenticate as the
contributor's own account. Branch protection "require reviews"
cannot distinguish the agent from the human. Merge control is
enforced only by `vrg-gh` wrapper logic, not server-side.

**Multi-org:** Fine-grained PATs are scoped to a single resource
owner. Multi-org requires one PAT per org.

### Classic PAT (Legacy Fallback)

A classic PAT with `repo` + `read:org` scope works inside the VM
as a lowest-common-denominator option. Same merge-control
limitation as fine-grained PATs, plus coarser permission
granularity.

### Host-Side Credentials (Unchanged)

The human's full-access credentials remain on the host. `gh auth`
on the host has the human's token with broad scopes. Raw `gh` and
the GitHub UI are the human's tools for operations the agent
cannot perform.

## Tooling Changes

### Deleted

| Component | What's removed |
|---|---|
| `vrg-gh` (`vrg_gh.py`) | `_get_token()`, credential selection, escalation logic for `pr merge` / `pr review --approve` / `issue close`, `GH_TOKEN` injection per subprocess |
| `github.py` | `_discover_accounts()`, `-vergil` suffix detection, hardcoded noreply email mapping |
| Plugin hooks | Credential-related enforcement (workflow enforcement may remain) |

### New

| Component | What's added |
|---|---|
| `github.py` or new module | GitHub App JWT → installation token exchange (shared with `vergil-release[bot]` implementation) |
| `vrg-vm-init` | App credential provisioning path (inject App ID, installation ID, private key) |
| `identities.toml` | `auth_type` field, App credential fields |

### Simplified

| Component | Change |
|---|---|
| `vrg-gh` | Becomes a pure workflow enforcement wrapper: subcommand allowlist + flag denylist. Handles App token exchange inline before each `gh` call. |
| `vrg-commit` | Co-author resolution no longer depends on account discovery. Co-author identity comes from configuration. |
| `vrg-submit-pr` | PR creation uses the App installation token (via `github.py`'s shared token exchange), so PRs are authored by `<app-name>[bot]`. No code change needed if `github.py` handles token exchange transparently. |

### Unchanged

| Component | Why |
|---|---|
| `vrg-commit` workflow | Conventional commits, branch naming, issue linking — none depends on credential type |
| `vrg-validate` | Full validation pipeline via `vrg-docker-run` |
| Git hooks | Pre-commit gate requiring `vrg-commit` |
| `vrg-git` | Subcommand allowlist and audit logging |
| VM architecture | Lima, mounts, egress filtering — all unchanged |

## Branch Protection

With the GitHub App model, branch protection rules can be
configured for full enforcement:

| Rule | Effect |
|---|---|
| Require pull request reviews (1 approval) | Agent (App) creates PR; human approves. Different identities. |
| Require status checks to pass | CI must pass before merge. |
| Require branches to be up to date | No stale merges. |
| Restrict who can push to matching branches | Only the App and the human account. |

**Solo developer mode:** The contributor is both the agent
operator and the reviewer. This works because the PR author
(App) and the reviewer (human) are different GitHub identities.
No need to wait for another human.

**Multi-contributor mode:** Additional contributors' Apps create
PRs; any authorized human can review and merge. The contributor
whose App created the PR can also review if needed (though
cross-review is preferred practice).

**Fallback (PAT mode):** Contributors using fine-grained PATs
cannot enable "require reviews" for their own PRs. They rely on
`vrg-gh` wrapper enforcement for merge control and manual review
discipline.

## Specs Superseded or Updated

| Document | Action |
|---|---|
| Vergil/Mimir identity design (#805) | Superseded. Mimir deferred indefinitely. |
| Credential management design (#775), Section 4 | Superseded. Credential selection logic replaced by VM-based single-credential model. |
| Identity VM isolation design (#892) | Updated: GitHub App as primary credential, single-account references revised. |
| Identity architecture guide | Rewritten: two-tier model (App primary, PAT fallback). |
| Account setup guide | Retired. Replaced by App registration guide + VM provisioning guide. |
| `CLAUDE.md` / `AGENTS.md` | Remove `-vergil` account convention references. |
| `vergil.toml` | Co-author entry updated to not depend on account discovery. |

## Open Questions

1. **App-based push + PR creation.** The agent pushes commits and
   creates PRs using the App's installation token. Verify during
   Phase 1 that `gh pr create` correctly attributes the PR to the
   App when the head branch was also pushed via the same
   installation token. If GitHub requires consistent identity
   between push and PR creation, this works naturally; if not,
   document the workaround.

2. **Git transport.** With App authentication, git operations use
   HTTPS with the installation token, not SSH. SSH key
   provisioning is dropped from `vrg-vm-init` for App mode
   (no `~/.ssh` setup, no key injection, no known_hosts). This
   simplifies VM provisioning. PAT fallback mode also uses HTTPS.

3. **App naming convention.** Per-contributor App names need a
   convention (e.g., `<username>-vergil`, `vergil-<username>`,
   or something else). Decide during Phase 1 App registration.

3. **Relationship between per-contributor agent Apps and
   `vergil-release[bot]`.** Whether the release App should also
   become per-contributor or remain shared is deferred to
   implementation.

## Mimir

Deferred indefinitely. The adversarial testing concept remains
valid and becomes more interesting with the GitHub App model — a
per-contributor Mimir App could authenticate as a distinct bot
identity for adversarial testing. Design deferred until the
primary agent App model is operational.

## Migration

No immediate code changes are required. The current state — the
human-credential workaround from PR #799 — continues to work.

**Phase 1 — GitHub App setup (manual, per-contributor):**

1. Register a GitHub App (naming convention TBD).
2. Configure permissions (Contents, PRs, Issues — Read/Write).
3. Generate private key.
4. Install on target orgs.
5. Store App ID, installation ID, and private key in local config.

**Phase 2 — VM credential provisioning (Plan 3 update):**

1. Update `vrg-vm-init` to support `auth_type = "app"`.
2. Inject App credentials into the VM.
3. Implement JWT → installation token exchange inside the VM.
4. Configure `gh` inside the VM to use the installation token.

**Phase 3 — Wrapper simplification (Plan 5):**

1. Delete credential selection code from `vrg-gh` and `github.py`.
2. Update `vrg-commit` co-author resolution.
3. Mark superseded specs.
4. Update onboarding documentation.
5. Enable branch protection "require reviews" on repositories.

## Impact Assessment

| Capability | Before (user accounts) | After (GitHub App + VM) | Net change |
|---|---|---|---|
| Merge control | Server-side (account permissions) | Server-side (branch protection + App identity) | Equivalent |
| Access restriction | Account permissions + wrapper logic | VM boundary + App permissions | Stronger |
| Credential isolation | Two tokens on same filesystem | App key inside VM, human creds on host | Stronger |
| Audit trail | Commit author = agent account | PR author = App bot, commit author = human | Equivalent (different split) |
| Shadow-ban risk | High (user accounts flagged) | None (Apps are first-class) | Much better |
| Adoption friction | Create 2-3 GitHub accounts | Register 1 GitHub App | Better |
| Maintenance burden | Account discovery, credential selection, escalation | Token refresh (~20 lines) | Much better |
| Token security | Long-lived PATs | 1-hour installation tokens + rotatable private key | Better |
