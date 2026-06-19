# Credential-less VM Identity (`auth_type = "none"`)

**Issue:** [vergil-tooling #1705](https://github.com/vergil-project/vergil-tooling/issues/1705)

**Date:** 2026-06-19

**Status:** Design — pending review

**Related:** Per-repo VM profiles design (vergil-vm #99), which established
`(identity, org/repo)` VM keying and the `<identity>--<org>--<repo>` naming
this builds on.

## Problem

Every Vergil VM identity is assumed to carry GitHub App credentials. The
credential injection step — `inject_credentials()` in `lib/lima.py`, wired as
a `fail_fast` `credentials` stage in the `create`, `start`, `restart`, and
`rebuild` lifecycles — hard-requires two things an unauthenticated identity
cannot supply:

1. A readable private key file. An empty/missing `private_key_path` aborts
   with `SystemExit(1)` at the top of the function.
2. A derivable identity *mode*. `derive_identity_mode(name)` returns `""`
   unless the name contains exactly one of `"user"` / `"audit"`, and
   `inject_credentials()` then aborts with a message instructing the operator
   to *rename the identity so the name contains 'user' or 'audit'*.

So there is no way to declare an identity that intentionally has **no**
credentials, and the natural name for one (`anonymous`) is actively rejected
by the mode check.

### Why we want one

A second, isolated VM is needed to test the
`logical-minds-foundry/mq-cluster-tooling` MQ-lab bootstrap from scratch,
**in parallel** with ongoing credentialed agent work that touches shared
infrastructure (observability clusters, DTCC sim, and so on). A from-scratch
bootstrap cannot safely run against those shared stacks, so it needs its own
box. Two properties are wanted of that box:

- **No credentials of any kind.** The box is for running the lab build
  (`zsh` + `mqlab`), not for code or git work. All git work for the repo
  stays in the primary credentialed checkout. If a harness is ever run in the
  box, the *absence* of credentials is the guarantee that it can only touch
  local files — it cannot act on GitHub. This is defense-in-depth, not a
  workflow nicety.
- **Reusable.** `anonymous` is intended as a general-purpose throwaway
  identity for this class of isolated, no-git testing — not a one-off.

The per-repo VM profiles work already made multiple identities first-class
and keys VMs on `(identity, org/repo)`. The only thing missing is an identity
that opts out of credentials.

## Goals

- Allow an identity to declare `auth_type = "none"` and build/start/rebuild a
  usable VM with **no** credential injection of any kind (no GitHub App key,
  no `app.env`, no identity-mode file, no git identity/HTTPS rewrite, no
  Claude OAuth token).
- Keep the change to a single seam, so all four lifecycles are covered at
  once.
- Fail loudly on a mistyped `auth_type` rather than silently injecting or
  silently skipping.
- Preserve every existing invariant for credentialed (`auth_type = "app"`)
  identities — byte-for-byte unchanged behaviour.

## Non-goals

- **First-class `auth_type = none` treatment.** No new `vrg-vm list` column,
  no change to the credential-verification step, no documentation-site
  overhaul in this pass. This is the deliberate quick-and-dirty scope; the
  capability can be promoted later if it earns its keep.
- **Changing the credential model for existing identities.** The `"app"` path
  is untouched.
- **Authoring the consuming-repo overlay or user config.** Those edits
  (`[vm.anonymous]` in `mq-cluster-tooling`'s `vergil.toml`; the
  `[identities.anonymous]` stanza in `identities.toml`) are operator actions,
  documented here for completeness but not code in this repo.

## Design

### 1. The seam: guard `inject_credentials()`

A single early-return at the top of `inject_credentials()`
(`lib/lima.py`), before any current check:

```python
def inject_credentials(instance: str, identity: Identity) -> None:
    """Inject GitHub App and Claude Code credentials into a running VM."""
    if identity.auth_type == "none":
        print("  Skipping credential injection (credential-less identity)")
        return
    ...
```

Because the `credentials` stage in `create`/`start`/`restart`/`rebuild` all
route through this one function, the early return makes the whole stage a
clean no-op for a credential-less identity. It short-circuits **before** both
present blockers (the missing-key check and the mode-derivation abort), so a
name like `anonymous` — which has no derivable mode — is no longer rejected.

Everything the credentials stage would otherwise do is skipped together: the
App private key (`app.pem`), the `app.env` (`APP_ID`), the identity-mode file
(`~/.config/vergil/identity-mode`), the host git identity, the
`url.https://github.com/.insteadOf` HTTPS rewrite, and the Claude OAuth token
(`claude.env` / `.claude/.credentials.json`).

**Consequence inside the box (intended).** With no identity-mode file and no
credentials, the in-VM `vrg-git` / `vrg-gh` wrappers have neither a role nor a
token; any GitHub operation fails for lack of credentials. That is exactly the
intended posture: the box runs `zsh` + `mqlab` and can only scribble on local
files.

### 2. Validate `auth_type` at config load

In `lib/identity.py`, where the identity is parsed (`load_config`), validate
that `auth_type ∈ {"app", "none"}`. An unrecognized value (e.g. a typo
`"non"`) raises `SystemExit(1)` with a clear message naming the identity and
the offending value. This upholds no-silent-failures: a mistyped `auth_type`
must never silently skip injection (a credentialed identity coming up with no
creds) nor silently inject.

`auth_type` already defaults to `"app"` and `app_id` / `private_key_path`
already default to empty strings, so no other parsing change is required; the
validation is the only addition.

### 3. No other code changes

`_resolve_target`, `compose_vm_spec`, instance naming, the `shared_from`
borrowing path, and the lifecycle stage lists are all unchanged. The
`anonymous` identity flows through them exactly as any other identity:
`compose_vm_spec(identity="anonymous", ...)` composes the repo's `[vm]` ⊕
`[vm.anonymous]` stanza, and a non-empty composed spec yields the dedicated
instance `anonymous--<org>--<repo>` with no collision against
`vergil-user--<org>--<repo>`.

## Operator-side configuration (documented, not code here)

### User config — `~/.config/vergil/identities.toml`

```toml
[identities.anonymous]
vm_instance  = "anonymous"      # required key; base-box name for this identity
auth_type    = "none"           # the sentinel that skips the credentials stage
projects_dir = "~/dev/projects" # so resolution can find the repo's vergil.toml
# no app_id, no private_key_path, no claude_token_path
```

### Consuming repo — `mq-cluster-tooling`'s `vergil.toml`

The heavy footprint and `nested` currently live under `[vm.vergil-user]`, so
without an overlay `anonymous` would compose `[vm]` packages + the base
footprint (4 CPU / 4 GiB / 50 GiB) with nested virtualization **off** — which
cannot boot the lab's nodes. Add a trimmed-but-nested role overlay:

```toml
[vm.anonymous]
cpus   = 6          # starting estimate — NOT derived from the lab's real needs
memory = "24GiB"    # starting estimate
disk   = "150GiB"   # starting estimate
nested = true       # REQUIRED — /dev/kvm to boot the nodes
# stale_days optional
```

The shared `[vm]` packages already apply to every identity, so `anonymous`
inherits the full toolchain; only the footprint and `nested` overlay are new.

**Footprint caveat (numbers are estimates, not measurements).** `6 CPU /
24 GiB / 150 GiB` is an untested starting point, not derived from the lab's
per-node requirements. `nested = true` is the one non-negotiable value. The
first bootstrap run is the validation: if nodes OOM or fail to boot, raise the
footprint. These figures should be replaced with grounded values once the
lab's node count and sizes are known.

## Rollout

This is a `vergil-tooling` change, so it is not usable on the host until it is
merged and the tool is reinstalled in the host environment
(`uv tool install …`). It is therefore not an instant unblock. If an immediate
second box is needed before this lands, the zero-code bridge is to point the
existing `vergil-audit` identity at the repo (plus a `[vm.vergil-audit]`
footprint overlay) — at the cost of carrying audit's real credentials in the
box. The `anonymous` approach is the durable answer and was chosen
deliberately over that bridge.

## Security boundary

The credential-less box is a tightening, not a loosening, of the existing
trust model: it holds strictly fewer secrets than any credentialed VM. The
one boundary to state plainly is the operator's responsibility for the
`auth_type` value — which the load-time validation (§2) backstops by refusing
to proceed on an unrecognized value, so a typo cannot silently produce a
credentialed identity that comes up with no credentials (or vice versa). No
new attack surface is introduced inside the box; if anything, a harness run
there is strictly less capable than in a credentialed box.

## Testing

- A credential-less identity (`auth_type = "none"`) runs `create` to a
  usable VM: the lifecycle completes, and the box has **no** `app.pem`, **no**
  `app.env`, **no** `~/.config/vergil/identity-mode`, and **no** Claude
  credentials.
- The same identity with a name that carries no role token (`anonymous`)
  builds without hitting the mode-derivation abort.
- A credentialed identity (`auth_type = "app"`) is **unchanged** — credential
  injection still runs and still aborts as before on a missing key.
- `auth_type` validation: an unrecognized value fails loudly at config load,
  naming the identity and the bad value.

## Acceptance criteria

- `vrg-vm create logical-minds-foundry/mq-cluster-tooling --identity anonymous`
  builds `anonymous--logical-minds-foundry--mq-cluster-tooling`, sized by the
  composed `[vm] ⊕ [vm.anonymous]` spec with `nested` honoured, and injects no
  credentials of any kind.
- The box coexists with `vergil-user--logical-minds-foundry--mq-cluster-tooling`
  with no name collision.
- Inside the box, an interactive `zsh` login can run `mqlab` through the
  bootstrap; in-VM `vrg-git`/`vrg-gh` have no credentials and cannot perform
  GitHub operations.
- Existing credentialed identities build/start/rebuild exactly as before.
- A mistyped `auth_type` aborts `create`/`start` at config load with a clear
  message.

## Out of scope / deferred

- Promotion to a first-class capability: a `vrg-vm list` indication that an
  identity is credential-less, an adjusted credential-verify step, and
  documentation-site coverage.
- A grounded footprint for `[vm.anonymous]` derived from the lab's real node
  requirements (replaces the estimates above after the first bootstrap run).
