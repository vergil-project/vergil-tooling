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
| `<org>-automation` | CI automation: nightly ops, epic roll-up, package-index dispatch | GitHub App owned by the org, one per org |

The human account owns the decision-making authority: code review,
PR approval, merge, and administrative operations. The two agent
Apps do the bounded work — the user App writes code, the audit App
reviews PRs. Each org's automation App runs scheduled and
event-driven CI work that needs neither human judgment nor a
per-contributor identity (see
[The org automation Apps](#the-org-automation-apps)).

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
- Read PR and CI status (`pull_requests: read`, plus `checks: read`,
  `statuses: read` and `actions: read`)
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
- Read CI status (`checks: read`, `statuses: read`, `actions: read`)

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
defense-in-depth architecture. Where GitHub's permission shapes are
coarser than the role model wants, the gap is recorded in
[Security Boundaries](security-boundaries.md).

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
Cursor, or any future harness. The specific tool is not recorded
at the identity level, and commits carry no AI co-author trailer
(#3200). The App represents "AI operating under VERGIL
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
selection. It authenticates with raw `gh`, raw
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

## The org automation Apps

Each org has its own automation GitHub App, named
`<org>-automation`. The App is **owned by the org** (registered under
the org's Developer settings), and its installation covers **all
repositories** in that org:

| Org | App |
|---|---|
| `vergil-project` | `vergil-project-automation` |
| `logical-minds-foundry` | `logical-minds-foundry-automation` |
| `mnemosys-project` | `mnemosys-project-automation` |

These Apps are an automation identity, not a per-contributor one, and
they are separate from the agent Apps above. Workflows reach an org's
App through the `APP_CLIENT_ID` and `APP_PRIVATE_KEY` Actions secrets
and mint a short-lived installation token with
`actions/create-github-app-token`. The `vergil-actions` reusable
workflows use it for:

- **Nightly ops** (`ops-github-config.yml`): the
  `vrg-github-repo-config audit`, the branch sweep, and the
  `vrg-dependabot-audit` open-alert gate. Each of these jobs mints a
  token scoped to the calling repo.
- **Epic roll-up and sweep** (`ops-epic-rollup.yml`,
  `ops-epic-sweep.yml`): closing epics and tasks across the org.
- **Package-index dispatch** (`cd-release.yml`): sending
  `package-released` to the org's `packages` repo after a packaged
  release.

Three of the App's permissions matter to the tooling in this repo:

- **Dependabot alerts: Read.** `vrg-dependabot-audit` needs it to list
  a repo's open alerts (epic
  [vergil-project/.github#420](https://github.com/vergil-project/.github/issues/420)).
  Without it, or until an installation accepts it, the nightly
  `dependabot-audit` job exits 2 ("could not check").
- **Administration: Read-only.** The Apps run only the read-only
  `vrg-github-repo-config audit`. `vrg-github-repo-config apply` is a
  human action that runs as the human's own `gh` login, because the
  tooling uses App credentials only when it finds an App key
  (`VRG_APP_ID` or `~/.config/vergil/app.pem`). Read-only is the
  correct least privilege.
- **Repository security advisories: Read and write.** The scheduled
  sweep (`vrg-epic-audit --close` in `ops-epic-sweep.yml`) lists the
  draft advisories in the org's `.github` and closes or reopens each
  one to follow its issues (see [Security Records](security-records.md),
  epic
  [vergil-project/.github#517](https://github.com/vergil-project/.github/issues/517)).
  The user App holds the same grant (see
  [Account Setup](account-setup.md#step-11-register-the-app)). Each
  installation must accept the change before the grant takes effect.

Release PRs are not opened by an App. `vrg-release` runs on the
human's host under the human's own `gh` login.

## Related

- [Account Setup](account-setup.md) — registering and configuring
  the user and audit Apps
- [Credential Management](credential-management.md) — how
  installation tokens are minted and selected at runtime
- [Permission Model](permission-model.md) — enforcement layers
  that constrain agent operations
- [Security Boundaries](security-boundaries.md) — register of
  known cross-system seams and their mitigations
- [Git Workflow](git-workflow.md) — the per-change development
  cycle
