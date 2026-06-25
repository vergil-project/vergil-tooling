# Identity Architecture

VERGIL enforces a hard separation between human and AI agent
identity at the GitHub App level. This page describes the identity
model, the naming conventions that make it work, and how it extends
to adversarial testing.

For the step-by-step App creation process, see
[Account Setup](account-setup.md). For how credentials are minted
and selected at runtime, see
[Credential Management](credential-management.md).

## The identities

Every contributor operates with a human account and a set of AI
agent identities. Each agent identity is a **GitHub App**, not a
user account:

| Identity | Role | Backed by |
|---|---|---|
| `<username>` | Human — review, approval, merge, admin (Chief Steward) | The human's own GitHub account |
| `<username>-vergil-user` | Daily development (Driver) | GitHub App, write-shaped permissions |
| `<username>-vergil-audit` | PR review (Officials) | GitHub App, read-shaped permissions (inverted) |
| `<username>-vergil-admin` | Reserved — **not provisioned** | — |
| `vergil-release[bot]` | Mechanized release automation | Org-level GitHub App |

The human account owns the decision-making authority: code review,
PR approval, merge, and administrative operations. The two agent
Apps do the bounded work — the user App writes code, the audit App
reviews PRs. The release App handles automation that requires
neither human judgment nor a per-contributor identity.

A third agent role — `<username>-vergil-admin` — is a reserved slot
and is **not** created. Do not provision it.

## Every agent is a GitHub App

There are no agent user accounts. There are no classic PATs. There
are no collaborator grants. An AI agent's entire identity and
capability is its GitHub App:

- **The App installation is the only credential.** The tooling
  authenticates as the App (App ID as the JWT `iss` claim), finds
  the App's installation on the target org, and mints a short-lived
  installation token on demand. There is no static token to leak or
  rotate.
- **Capability is bounded by the App's permission shape.** What an
  agent can do is exactly what its App declares — nothing more. The
  permission shape *is* the security boundary.
- **Attribution is clean and automatic.** Commits and PRs made
  through the App carry the bot identity `<username>-vergil-<role>[bot]`.
  Anyone reading the history can see at a glance what the human did
  versus what that human's agents did — and which role did it.
- **It scales.** The model is `<username>-vergil-<role>`, so any
  number of engineers can each own their own user and audit Apps
  without naming collisions or shared credentials.

### The `<username>-vergil-<role>` convention

Every agent App's name encodes two things: the human who owns it
and the role it plays. `jdoe-vergil-user` is jdoe's development
agent; `jdoe-vergil-audit` is jdoe's PR-review agent. The bot
identity that appears on commits (`jdoe-vergil-user[bot]`) comes
directly from the App — there is nothing else to configure.

## Inverted permission shapes

The user and audit Apps have deliberately **inverted** repository
permission shapes. This split is the core of the model:

| Permission | `<username>-vergil-user` | `<username>-vergil-audit` |
|---|---|---|
| Contents | Read and write | **Read-only** |
| Issues | Read and write | Read-only |
| Pull requests | **Read-only** | Read and write |
| Metadata | Read-only | Read-only |
| Workflows | No access | No access |

The user App can write code but only read PRs. The audit App can
only read code but can write (review/comment on) PRs. Neither holds
Workflows access, so neither can push changes under
`.github/workflows/`.

### What the user agent can do

- Commit and push to feature branches (`contents: write`)
- Create and comment on issues (`issues: write`)
- Read PR and CI status (`pull_requests: read`)
- Record PR metadata in `.vergil/pr-workflow.json` (via `vrg-pr-workflow`)
  to stage a PR for the human

### What the user agent cannot do

- **Open, edit, comment on, approve, or merge PRs** — its App holds
  `pull_requests: read`. Its workflow ends at "ready for PR"; the
  human submits the PR from the host.
- **Push changes under `.github/workflows/`** — no Workflows access,
  so GitHub rejects such a push server-side.
- Access admin settings or manage org membership.

### What the audit agent can do

- Read code and issues (`contents: read`, `issues: read`)
- Write PR reviews and comments (`pull_requests: write`)

### What the audit agent cannot do

- **Write code** — `contents: read`.
- **Merge a PR** — merging through the API requires `contents: write`,
  which the audit App does not hold. This is a server-side hard gate,
  not a tooling convention.
- Push changes under `.github/workflows/`.

These restrictions are enforced primarily by the App permission
shapes (a server-side hard gate) and branch protection, with the
`vrg-gh`/`vrg-git` wrappers as a soft ergonomic layer on top. See
[Permission Model](permission-model.md) for the full
defense-in-depth architecture.

> **Note (2026-06-25, #1872):** The local interactive USER/AUDIT loop
> (where a running AUDIT agent reviewed code in a shared worktree before
> PR submission) was removed. The audit *identity* — the `vergil-audit`
> GitHub App, its inverted permission shape, and the `audit` mode in
> `VRG_IDENTITY_MODE` — is retained as dormant infrastructure for a
> future API-driven review phase. The identity model described above
> remains current and accurate.

## One App, all orgs

A single App is installed on every account the contributor operates
in — their personal account and each org they own. Installation
tokens are minted per-org at runtime from the App's private key, so
one App covers `vergil-project`, `vergils-nemesis`, and any future
orgs. Adding a new org requires only installing the existing App on
it — no new Apps, no new keys, no credential reconfiguration.

## Harness independence

The agent App identity captures AI-driven development work
regardless of which AI tool produced it — Claude Code, Copilot,
Cursor, or any future harness. The specific tool is recorded in
commit metadata (co-author trailers, PR descriptions), not at the
identity level. The App represents "AI operating under VERGIL
discipline in a given role," not "Claude Code specifically."

## The `-mimir` convention

Mimir is the adversarial testing counterpart to Vergil. Where
Vergil represents AI operating with discipline, Mimir represents
AI's failure modes — hallucination, false confidence, sycophancy,
and the tendency to work around constraints rather than within
them.

A `<username>-mimir` identity is the credential that attack tooling
presents when attempting to breach Vergil-managed repos. It has no
integration with the Vergil tooling — no App, no credential
selection, no co-author entry. It authenticates with raw `gh`, raw
`git`, and direct API calls, deliberately bypassing `vrg-gh` and
`vrg-commit`. It operates *against* the tooling, not within it.

### The Vergils-Nemesis org

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

## The release App

`vergil-release[bot]` is an org-level GitHub App that handles
mechanized release automation. It is not a per-contributor identity —
it is an automation identity with its own auth flow (JWT exchange
for short-lived installation tokens).

The App creates release PRs, and the human account approves and
merges them. This separation ensures that no single identity can
both create and approve a release.

## Related

- [Account Setup](account-setup.md) — registering and configuring
  the user and audit Apps
- [Credential Management](credential-management.md) — how
  installation tokens are minted and selected at runtime
- [Permission Model](permission-model.md) — enforcement layers
  that constrain agent operations
- [Git Workflow](git-workflow.md) — the per-change development
  cycle
