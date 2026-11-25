# Identity Setup

This guide walks through creating and configuring the GitHub App
identities that VERGIL tooling uses for AI agents. Each contributor
registers **two** GitHub Apps — one per agent role — under their
personal account.

For the model these identities serve, see
[Identity Architecture](identity-architecture.md). For how
credentials are selected at runtime, see
[Credential Management](credential-management.md). For repo-level
onboarding (installing tools, hooks, CI), see
[Consuming Repo Setup](consuming-repo-setup.md).

## The two agent identities

Every AI agent is represented by a GitHub App whose name encodes the
human who owns it and the agent's role: `<username>-vergil-<role>`.
The App's installation is the agent's only credential, and the agent's
capability is bounded entirely by the App's declared permission shape.
There are no agent user accounts and no collaborator grants — the bot
identity (`<username>-vergil-<role>[bot]`) that appears on commits and
PRs comes from the App itself.

| App | Role | Shape |
|---|---|---|
| `<username>-vergil-user` | Daily development (Driver) | write code, write PRs |
| `<username>-vergil-audit` | PR review (Officials) | read code, write PRs (inverted) |

A third role — `<username>-vergil-admin` — is a reserved slot and is
**not** provisioned. Do not create it.

The two Apps have deliberately **inverted** permission shapes on code.
The user App writes code, workflow files included, and opens its own
PRs; the audit App can only read code but can write (review/comment
on) PRs. This split is the core of the permission model — set the
permissions exactly as documented.

## Why GitHub Apps

- **No shadow-ban risk** — Apps are first-class GitHub citizens
- **Short-lived tokens** — 1-hour installation tokens, minted on
  demand, instead of long-lived PATs
- **Multi-org from one identity** — install the same App on every org
  you manage
- **Server-side merge control** — branch protection recognizes the App
  identity
- **Dynamic token resolution** — the tooling acquires tokens per-org at
  runtime from the App's private key; no static token configuration

## Prerequisites

- A GitHub account that is an **owner** of the organizations where the
  agents will operate
- A `~/.config/vergil/keys/` directory on your development machine

---

## Part 1 — The user App (`<username>-vergil-user`)

### Step 1.1 — Register the App

1. Go to **github.com** → your profile icon → **Settings** →
   **Developer settings** (bottom of the left sidebar) →
   **GitHub Apps** → **New GitHub App**.

2. Fill in the registration form:

   | Field | Value |
   |---|---|
   | **GitHub App name** | `<username>-vergil-user` |
   | **Description** | AI agent identity (user / Driver role) owned by `<username>`. Daily development agent running in an isolated VM; authenticates via installation tokens for commits, pushes, and issues. |
   | **Homepage URL** | your GitHub profile URL |
   | **Webhook** | uncheck **"Active"** (no webhook needed) |

3. Under **Repository permissions**, set exactly these and leave
   everything else at **No access**:

   | Permission | Access |
   |---|---|
   | **Contents** | Read and write |
   | **Issues** | Read and write |
   | **Pull requests** | **Read and write** |
   | **Actions** | Read-only |
   | **Checks** | Read-only |
   | **Commit statuses** | Read-only |
   | **Repository security advisories** | Read and write |
   | **Metadata** | Read-only (mandatory; usually auto-selected) |
   | **Workflows** | **Read and write** |

   !!! note "Repository security advisories is for security records"
       `vrg-security-record` creates, reads and updates the private
       draft advisories in each org's `.github` that hold security
       findings, and `vrg-intake-route` and `vrg-queue planning` read
       and retarget them (see [Security Records](security-records.md)).
       The user App, or a human, also runs `vrg-security-record sync`,
       which closes and reopens advisories to follow their issues, and
       `vrg-security-record close`, which closes one with a reason.
       `vrg-finalize-pr` also syncs the advisories of the `security`
       issues a merged PR closes. Each
       org's automation App (`<org>-automation`, see
       [Identity Architecture](identity-architecture.md#the-org-automation-apps))
       does not need the grant: the scheduled sweep makes no
       security-advisory call. The audit App does **not**
       get it: it is not in use today, and the decision is deferred
       until the audit identity is revived. Adding this permission to an
       existing App changes its permission set, so **each installation
       must accept the change** (the org or account owner approves it
       from the installation's settings page) before the new grant takes
       effect.

   !!! note "Actions, Checks and Commit statuses are for reading CI"
       `vrg-pr-await` and pr-watch read a PR's check runs, commit
       statuses and workflow runs. On a **private** repo, an App
       without these grants gets HTTP 403 "Resource not accessible by
       integration". On a public repo the reads succeed anyway, which
       hides a missing grant. All three are read-only and grant no
       ability to trigger, re-run or change CI.

   !!! note "Pull requests and Workflows are Read and write"
       The user agent submits and finalizes its own PRs into `develop`
       and pushes workflow-file changes like any other change (epic
       [vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384)).
       It opens a PR and turns its auto-merge on only through
       `vrg-submit-pr`, after `vrg-pr-workflow report-ready`, and the
       submit's gate requires a merge-review record or a merge-review
       exemption the human approved. What keeps releases human is the
       "Main updates" ruleset, which lets only the human admin update
       `main`, and the secrets that can write or act live only in each
       repo's `automation` environment, limited to `develop` and
       `main`. Raising either permission on an existing App changes its
       permission set, so **each installation must accept the change**
       before the new grant takes effect.

4. Under **Where can this GitHub App be installed?**, select
   **Any account** — this lets you install it on your organizations,
   not just your personal account.

   !!! warning "Cannot change this easily later"
       If you select "Only on this account", the App can only be
       installed on your personal account. To change it afterward:
       App settings → **Advanced** → **Danger zone** → **Make
       public**. Once public and installed elsewhere, this cannot be
       reverted.

5. Click **Create GitHub App**. You land on the App's settings page,
   which shows the **App ID** and **Client ID** near the top.

### Step 1.2 — Record the App ID and Client ID

On the **General** settings page, record both identifiers:

- **App ID** — a short number (e.g., `3940487`).
- **Client ID** — a string beginning with `Iv` (e.g.,
  `Iv23li...`).

The tooling authenticates using the **App ID** today (it becomes the
JWT `iss` claim via `VRG_APP_ID`). GitHub is gradually migrating
GitHub App authentication from the numeric App ID toward the Client
ID; that migration is only partially rolled out, so the App ID is
still what the tooling uses. Record **both** now so the eventual
switch to Client ID needs no return trip to the GitHub UI.

You do not need installation IDs — the tooling resolves those
dynamically at runtime.

### Step 1.3 — Generate a private key

1. Ensure the keys directory exists:

   ```bash
   mkdir -p ~/.config/vergil/keys
   ```

2. On the App settings page, scroll to **Private keys** → click
   **Generate a private key**. Your browser downloads a `.pem` file.

3. Move it into the keys directory, keeping the original filename:

   ```bash
   mv ~/Downloads/<username>-vergil-user.<YYYY-MM-DD>.private-key.pem \
      ~/.config/vergil/keys/
   ```

   GitHub names the file `<app-name>.<YYYY-MM-DD>.private-key.pem`
   (for example, `wphillipmoore-vergil-user.2026-06-02.private-key.pem`).
   Keep the name — the date is useful metadata for key rotation.

!!! note "No install prompt is expected here"
    On a freshly created App you may see no "install this App" banner
    after generating the key. Installation is a separate, deliberate
    step (next) — do not wait for a prompt.

### Step 1.4 — Install the App

1. On the App's settings page, left sidebar → **Install App**.
2. GitHub lists every account you can install on — your personal
   account and each organization you own.
3. Click **Install** next to **each account that owns repositories
   this agent will operate on**. A single App installed on multiple
   accounts is normal and expected — it works across every org you
   manage. Without an installation on a given account, the tooling
   cannot mint a token for that account's repos.
4. Choose repository access:
   - **All repositories** — covers every repo in that account,
     including ones added later (low friction).
   - **Only select repositories** — tighter; you return here to add
     more.
5. Click **Install**. You land on the installation's configuration
   page (its URL contains an installation ID, which you do **not**
   need to record — the tooling resolves it at runtime).

The user App is now fully provisioned. Configuring
`identities.toml` happens in **Part 3**, after the audit App exists.

---

## Part 2 — The audit App (`<username>-vergil-audit`)

The audit App follows the **same flow** as the user App, with two
differences: the name, and an **inverted permission shape** (it reads
code and writes PRs, and holds no Workflows access).

### Step 2.1 — Register the App

Follow Step 1.1, changing:

- **GitHub App name**: `<username>-vergil-audit`
- **Description**: AI agent identity (audit / Officials role) owned by
  `<username>`. PR-review agent running in an isolated VM; reads code
  and writes PR reviews/comments via installation tokens.
- **Repository permissions** — note these differ from the user App
  (Contents and Issues read-only, no Workflows):

  | Permission | Access |
  |---|---|
  | **Contents** | **Read-only** |
  | **Issues** | **Read-only** |
  | **Pull requests** | **Read and write** |
  | **Actions** | Read-only |
  | **Checks** | Read-only |
  | **Commit statuses** | Read-only |
  | **Repository security advisories** | **No access** (deferred) |
  | **Metadata** | Read-only (mandatory) |
  | **Workflows** | **No access** — leave untouched |

  Actions, Checks and Commit statuses are the same read-only CI
  grants as the user App (see the note in Step 1.1); they are not
  inverted. Repository security advisories stays at No access: the
  audit identity is not in use today, and whether it needs the grant
  is decided when it is revived.

Everything else (Webhook unchecked, **Any account**, the "Make
public" warning) is identical to Step 1.1.

### Step 2.2 — Record the App ID and Client ID

Same as Step 1.2 — record both for the audit App.

### Step 2.3 — Generate a private key

Same as Step 1.3. The downloaded file is named
`<username>-vergil-audit.<YYYY-MM-DD>.private-key.pem`; move it into
`~/.config/vergil/keys/`.

### Step 2.4 — Install the App

Same as Step 1.4 — install the audit App on the same set of accounts
as the user App.

---

## Part 3 — Configure `identities.toml`

`~/.config/vergil/identities.toml` is the host-side configuration that
tells VM provisioning which App credentials to inject into each agent
VM. The dual-identity model uses one stanza per role.

```toml
default_identity = "vergil-user"
vergil = "v2.0"        # vergil-tooling version for the human host
vergil-vm = "v2.1"     # VM template tag (from the vergil-vm repo)

[identities.vergil-user]
vm_instance = "vergil-user"
auth_type = "app"
app_id = <user-app-id>
private_key_path = "~/.config/vergil/keys/<username>-vergil-user.<date>.private-key.pem"
vergil = "v2.1"        # pin this VM's vergil-tooling to 2.1 (set once 2.1 is released)
# client_id = "Iv..."  # recorded for the App ID → Client ID migration; not yet read by the tooling

[identities.vergil-audit]
vm_instance = "vergil-audit"
auth_type = "app"
app_id = <audit-app-id>
private_key_path = "~/.config/vergil/keys/<username>-vergil-audit.<date>.private-key.pem"
vergil = "v2.1"
# client_id = "Iv..."
```

Notes:

- **`app_id`** is the identifier the tooling uses today (it becomes
  `VRG_APP_ID` inside the VM). The Client ID is recorded only as a
  comment until the App ID → Client ID migration lands in the tooling.
- **`private_key_path`** is relative to your home directory when it
  starts with `~`, otherwise absolute.
- **Per-identity `vergil` pin.** Setting `vergil = "v2.1"` on the
  agent identities lets the agent VMs run new tooling while the human
  host stays on a stable `v2.0` (the top-level default). Only set the
  `v2.1` pin once 2.1 has been released.
- **`VRG_IDENTITY_MODE`** (`user` / `audit`) is derived from the
  identity's stanza name at provisioning time — a name containing
  `user` provisions as user mode, `audit` as audit mode; a name
  containing neither (or both) fails provisioning. The mode is
  written to `~/.config/vergil/identity-mode` in the VM and exported
  from the shell profile. It selects the identity-aware allowlists
  at runtime; it is a soft-gate ergonomic, not the security boundary
  (the App credential is).
- **Query identity with `vrg-whoami`, never a single environment
  variable.** `VRG_IDENTITY_MODE` is only the first of five fallback
  steps (env var → mode file → `app.pem` → `VRG_APP_ID` → human); an
  unset value means "fall through," not "default to human." (The
  `automation` mode is checked before all five, and only in a CI job
  that sets `VRG_AUTOMATION_APP`; see
  [Identity modes](identity-architecture.md#identity-modes).) Ask the
  authoritative resolver instead: `vrg-whoami` prints the resolved role,
  `vrg-whoami --mode` emits a single token for scripting
  (`export VRG_IDENTITY_MODE="$(vrg-whoami --mode)"`), and
  `vrg-whoami --explain` reports which signal resolved and warns when
  signals disagree.

### Trusted authors

An optional top-level `[trusted_authors]` stanza lists the GitHub logins
whose issues and comments automation may trust: your own login, and the
logins of your agent identities (the Apps' bot accounts). Beside it, one
`[orgs.<org>]` stanza per org Vergil manages names that org's automation
App, the App that runs its fully mechanized workflows.

```toml
[trusted_authors]
human = ["<your-login>"]
agents = ["<user-app-bot-login>", "<audit-app-bot-login>"]

[orgs.<org>]
automation_app = "<automation-app-slug>"
```

- **Three roles.** `human` (a user account) and `agents` (bot accounts) are
  trusted in every Vergil org; an org's `automation_app` (login
  `<slug>[bot]`) is trusted only in that org's repos.
- **The listed orgs are the Vergil orgs.** An org without a stanza is not
  managed by Vergil. Adding an org to Vergil adds its stanza, as a step of
  setting the org up. The slug is an opaque value: copy it from the App,
  never derive it from the org's name.
- **Both lists are required and non-empty**, every entry is a non-empty
  string, no login may appear in two roles, at least one `[orgs.<org>]`
  stanza is required, and each needs its `automation_app`.
- **Store the bare login.** GitHub reports an issue opened by an App as
  `app/<login>`, that App's comments as `<login>`, and some reads as
  `<login>[bot]`; an `app/`-prefixed or `[bot]`-suffixed entry is
  rejected, and consumers strip them before comparing.
- **Delivery into VMs.** `vrg-vm` writes the stanzas into every agent VM as
  `~/.config/vergil/trusted-authors.toml` (mode 600) on every path that
  writes vergil config: `create`, `start`, `restart`, `rebuild`, the cloud
  credentials stage, and `update`. When the host has no `[trusted_authors]`
  stanza, `vrg-vm` **removes** that file, so a VM never trusts a stale list.
- **Delivery into GitHub Actions jobs.** A job reads a derived copy, the
  compact JSON `{"human": [...], "agents": [...], "orgs": {"<org>":
  {"automation_app": "<slug>"}}}`, from `$VRG_TRUSTED_AUTHORS`. It is read
  only when `GITHUB_ACTIONS` is `true` and neither file exists; when it is
  missing or invalid there, the read fails.
- **Publishing the Actions copy.** It comes from two org Actions variables,
  `VRG_TRUSTED_AUTHORS` (that JSON) and `VRG_AUTOMATION_APP` (the org's own
  `automation_app`), which the human writes from the host file with
  `vrg-trusted-authors publish`, once per org at setup and again whenever
  the file changes. The daily config audit runs `vrg-trusted-authors check`
  as the org's automation App, which needs read-only Variables permission
  at organization and repository level: it flags a missing, invalid or
  not-`all`-visible variable and any repository-level variable of either
  name. `vrg-trusted-authors render` prints what the host file implies, to
  compare with the audit's values.
- **Read it through `vrg-trusted-authors`, never by hand.** It reads the VM
  file if present, else the host stanzas, and prints `human: <login>`,
  `agent: <login>` and `org: <org> automation: <slug>` lines (`--json`
  prints the same JSON as the Actions copy). With nothing configured it exits
  1 with an error naming both paths; it never prints a default. Skills and
  doctrine name only this command, never the logins themselves.
- **The automation App's slug** for an org comes from its stanza, except in a
  GitHub Actions job of that same org, which uses the job's
  `$VRG_AUTOMATION_APP`; it is never read from the API.
- An invalid stanza makes every `vrg-vm` command fail loudly when it loads
  `identities.toml`.

**Publish before the first release that reads it.** Every tool that acts
on GitHub content reads the configuration. Add the org's `[orgs.<org>]`
stanza before installing the first vergil-tooling release that reads it;
right after installing it, run `vrg-trusted-authors publish` for the org,
so its GitHub Actions jobs find `VRG_TRUSTED_AUTHORS` when they first run
that release, and `vrg-vm update --all` to deliver the file to the VMs.
A job that runs before the variable is published stops loudly rather than
guess, and is re-run once it is.

The configuration drives the system-wide trust rule (epic
[vergil-project/.github#1467](https://github.com/vergil-project/.github/issues/1467)):
every tool and skill acts only on GitHub content that a trusted identity
wrote, judged by the trust layer, and reports the rest as metadata only.
Skills read issue and PR content through
[`vrg-issue-read`](../reference/cli-tools-overview.md#vrg-issue-read), and
`vrg-epic-audit --untrusted` lists the untrusted activity in an org's
repos for the morning review. With no configuration, every reader fails
rather than guess.

## Verification checklist

After both Apps are registered, installed, and configured:

- [ ] Two private key files exist in `~/.config/vergil/keys/` (one per
      App), with their generation dates in the filenames
- [ ] `identities.toml` has a `vergil-user` and a `vergil-audit` stanza, each with
      the correct App ID and key path
- [ ] Each App shows the correct repository permissions
      (user: contents/issues/PRs/workflows write; audit: contents/issues
      read, PRs write, no workflows; both hold Actions, Checks and
      Commit statuses read-only)
- [ ] The user App holds **Repository security advisories: Read and
      write**, the audit App does not, and every installation has
      accepted the change
- [ ] Both Apps are installed on every account that owns repos the
      agents will operate on
- [ ] The reserved `<username>-vergil-admin` App was **not** created

Full end-to-end verification requires the agent VMs to be provisioned
(vergil-vm) and vergil-tooling 2.1 to be released and pinned.

## Related

- [Identity Architecture](identity-architecture.md) — the model these
  Apps implement
- [Credential Management](credential-management.md) — how installation
  tokens are minted and selected at runtime
- [Consuming Repo Setup](consuming-repo-setup.md) — repo-level
  onboarding
