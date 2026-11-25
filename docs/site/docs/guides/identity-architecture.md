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
| `<username>` | Human — rulings, releases and promotion, admin (Chief Steward) | The human's own GitHub account |
| `<username>-vergil-user` | Daily development (Driver) | GitHub App, write-shaped permissions |
| `<username>-vergil-audit` | PR review (Officials) | GitHub App, read-shaped permissions (inverted) |
| `<username>-vergil-admin` | Reserved — **not provisioned** | — |
| `<org>-automation` | CI automation: nightly ops (including the automatic fleet sync), the scheduled upstream refresh, epic roll-up, package-index dispatch | GitHub App owned by the org, one per org |

The human account owns the decision-making authority: rulings,
merge-review exemptions, releases and promotion to `main`, and
administrative operations. The two agent Apps do the bounded work —
the user App writes code and submits its own PRs into `develop`, the
audit App reviews PRs. Each org's automation App runs scheduled and
event-driven CI work that needs neither human judgment nor a
per-contributor identity (see
[The org automation Apps](#the-org-automation-apps)).

A third agent role — `<username>-vergil-admin` — is a reserved slot
and is **not** created. Do not provision it.

### Identity modes

The tooling resolves each process to one **identity mode**
(`lib/identity_mode.py`; ask [`vrg-whoami`](../reference/cli-tools-overview.md#vrg-whoami)).
The mode tells identity-aware tools how to behave. It is a convenience,
not a control: the credential the process holds is the boundary.

| Mode | Identity | How it resolves | Rights in the tooling |
|---|---|---|---|
| `human` | The human's own `gh` login | The fall-through when no other signal is present | Everything, including submitting and merging PRs |
| `user` | `<username>-vergil-user` | `VRG_IDENTITY_MODE`, the mode file, or the provisioned App key or ID in an agent VM | The narrowest: stops at `vrg-pr-workflow report-ready`, never submits or merges |
| `audit` | `<username>-vergil-audit` | As for `user` | Dormant (see the note below) |
| `automation` | The org automation App, `<org>-automation` | Only when `VRG_AUTOMATION_APP` is set and the token verifies as that App's installation token; no mode signal selects it | A subset of the human's: `vrg-github-repo-config update`, which converges a repo and merges its own sync PR, and `vrg-upstream-refresh publish`, which opens a refresh PR on `automation/upstream-refresh/<run-id>` and merges it once CI passes |

**The automation invariant.** The `automation` mode is used only for
mechanized work, with no AI agent involved. Any work an AI agent does,
automated or not, runs under the agent identities. In the fleet today
two jobs set `VRG_AUTOMATION_APP`: the nightly `github-config` ops job
(epic vergil-project/.github#1064) and the `publish` job of the
scheduled upstream refresh (epic vergil-project/.github#1168). Every
other job that mints an
automation App token still resolves to `human`. A
`VRG_AUTOMATION_APP` whose token does not verify, or an agent signal
alongside a verified automation token, is an error, never a guess.

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
permission shapes on code: the user App writes it, the audit App only
reads it. This split is the core of the model:

| Permission | `<username>-vergil-user` | `<username>-vergil-audit` |
|---|---|---|
| Contents | Read and write | **Read-only** |
| Issues | Read and write | Read-only |
| Pull requests | Read and write | Read and write |
| Metadata | Read-only | Read-only |
| Workflows | Read and write | No access |

The user App writes code, workflow files included, and opens and
merges its own PRs into `develop`; the audit App can only read code
but can write (review/comment on) PRs. Epic
[vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384)
raised the user App's Pull requests from read and its Workflows from
none. Every secret that can write or act lives in each repo's
`automation` environment, limited to `develop` and `main`, so a
workflow the user agent pushes on a feature branch runs without them.

### What the user agent can do

- Commit and push to feature branches, workflow files included
  (`contents: write`, `workflows: write`)
- Create and comment on issues (`issues: write`)
- Read PR and CI status (`checks: read`, `statuses: read` and
  `actions: read`)
- Submit and finalize its own PRs into `develop` (`pull_requests:
  write`): it records the PR metadata with `vrg-pr-workflow
  report-ready`, then runs `vrg-submit-pr`, which opens the PR and
  turns its auto-merge on once the merge-review gate passes

### What the user agent cannot do

- **Open or merge a PR outside `vrg-submit-pr`** — `vrg-gh` denies
  `pr create`, `pr edit` and `pr merge` to it, and `vrg-submit-pr`
  refuses its CLI mode, its relay branch mode and `--release`.
- **Update `main`** — the "Main updates" ruleset lets only the human
  admin update it, so releases and promotion stay with the human. This
  is a server-side hard gate.
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
| `mq-rest-admin-project` | `mq-rest-admin-project-automation` |

These Apps are an automation identity, not a per-contributor one, and
they are separate from the agent Apps above. Workflows reach an org's
App through the `APP_CLIENT_ID` and `APP_PRIVATE_KEY` secrets, which
live only in each repo's `automation` environment, limited to `develop`
and `main` (epic
[vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384)).
A caller in `vergil-project` passes them to the vergil-actions
workflows with `secrets: inherit`; a caller in any other organization
passes them by name, as does a call job to its repo's own local
reusable workflow (see
[CI Architecture](ci-architecture.md#cd-and-ops-reusable-workflow-secrets)).
The workflows mint a short-lived installation token with
`actions/create-github-app-token`. The `vergil-actions` reusable
workflows use it for:

- **Nightly ops** (`ops-github-config.yml`): the `github-config` job's
  `vrg-github-repo-config update` (the automatic fleet sync, which
  runs in the `automation` mode), the branch sweep, and the
  `vrg-dependabot-audit` open-alert gate. Each of these jobs mints a
  token scoped to the calling repo and to the permissions it needs.
- **Upstream refresh** (`ops-upstream-refresh.yml`): the `publish`
  job's `vrg-upstream-refresh publish`, which runs in the `automation`
  mode. It does not use `actions/create-github-app-token`: the
  workflow passes the two secrets to the tool, which mints its own
  tokens in-process, limited to the one repo, renewed and revoked by
  the tool (see
  [Upstream Refresh](../reference/upstream-refresh-config.md#identity-and-credentials)).
  The `prepare` job holds no secrets.
- **Epic roll-up and sweep** (`ops-epic-rollup.yml`,
  `ops-epic-sweep.yml`): closing epics and tasks across the org.
- **Release tagging** (`cd-release.yml`): pushing the release tags
  with a token scoped to the releasing repo, Contents write only.
- **Package-index dispatch** (`cd-release.yml`): sending
  `package-released` to the org's `packages` repo after a packaged
  release.

The App's full permission set, and the scoped permissions each job's
token requests, are in the vergil-actions
[configuration reference](https://vergil-project.github.io/vergil-actions/latest/configuration/).
Five of the App's permissions matter to the tooling in this repo:

- **Actions: Read.** `vrg-github-repo-config` needs it to read the
  repo's `automation` environment and its deployment branch policies
  (vergil-project/vergil-tooling#4773). Without it, or until an
  installation accepts it, `update` reports the environment as not
  checked.
- **Dependabot alerts: Read.** `vrg-dependabot-audit` needs it to list
  a repo's open alerts (epic
  [vergil-project/.github#420](https://github.com/vergil-project/.github/issues/420)).
  Without it, or until an installation accepts it, the nightly
  `dependabot-audit` job exits 2 ("could not check").
- **Administration: Read and write.** The nightly `github-config` job
  runs `vrg-github-repo-config update`, which converges the repo's
  settings and rulesets, and GitHub returns a ruleset's bypass actors
  only to a caller that can edit it. Only that job's token requests
  Administration write, and the job runs only from `develop`.
- **Workflows: Read and write.** A sync PR from `update` can change
  files under `.github/workflows/` (the `vergil-*` workflow pins), and
  GitHub refuses such a push without it. Upstream refresh's `publish`
  token also requests it, for the wait's branch update when `develop`
  has changed a workflow file; its patch never touches `.github/**`.
- **Repository security advisories: not needed.** The scheduled
  sweep (`vrg-epic-audit --close` in `ops-epic-sweep.yml`) makes no
  security-advisory call. Syncing advisory states to their issues is
  `vrg-security-record sync`, which the human or the user agent runs; the
  user App holds Read and write for it (see
  [Security Records](security-records.md#permissions) and
  [Account Setup](account-setup.md#step-11-register-the-app)).

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
