# Borrowing Another Repo's VM via `[vm] shared-from`

**Issue:** #1668
**Date:** 2026-06-16
**Status:** Draft
**Related:** #892 (identity VM isolation), stateless VM lifecycle
(`2026-05-24-stateless-vm-lifecycle-design.md`), dedicated per-repo
VMs (`vm_spec.py` footprint cascade)

## Problem

Some repos develop and publish libraries or tooling independently of a
sibling lab repo, yet their sessions need to run inside that lab's
heavyweight identity-VM. The motivating case: a standalone tooling repo
`logical-minds-foundry/mq-resiliency-tooling` whose work targets the MQ
cluster that lives in `logical-minds-foundry/mq-resiliency-lab`'s
dedicated VM (large footprint, nested virtualization, vagrant plugins).

Today `vrg-vm session <org>/<repo>` always derives a *dedicated* VM from
the requested repo's own `[vm]` stanza, naming the instance
`identity.org.repo`. There is no way to say "this repo has no box of its
own — run my sessions in that other repo's box."

We want:

```bash
cd ~/dev/projects
vrg-vm session logical-minds-foundry/mq-resiliency-tooling
```

to shell into the VM for `logical-minds-foundry/mq-resiliency-lab` while
landing in the tooling repo's checkout.

## Mechanism

`_cmd_session` (`bin/vrg_vm.py`) resolves two things independently:

- **`target.instance`** — *which VM to shell into* — from `_resolve_target`,
  which reads the requested repo's `[vm]` stanza and names the instance
  `instance_name(identity, org, repo)`.
- **`workspace_abs`** (`--workdir`) — *which directory to `cd` into inside
  the VM* — straight from `args.workspace`, never from `target.org/repo`.

Because the whole `projects_dir` is mounted into every box, both the
borrower's checkout and the lender's checkout are siblings inside any VM.

So the feature reduces to a single idea:

> When the requested repo declares `shared-from`, redirect **instance + spec**
> resolution to the lender, and leave the **working directory** on the
> borrower's checkout.

Shell into the lender's box; `cd` into the borrower's repo. No new mount,
no new box, no second copy of the workspace.

## Config Schema

A borrower declares the redirect in its own `vergil.toml`:

```toml
[vm]
shared-from = "logical-minds-foundry/mq-resiliency-lab"
```

Parsing changes in `lib/config.py`:

- New field `VmStanza.shared_from: tuple[str, str] | None` — the parsed
  `(org, repo)` of the lender, or `None` when absent.
- **Value format.** Fully-qualified `org/repo` is required: exactly one
  `/`, both sides non-empty, no surrounding or interior whitespace. A bare
  repo name (`"mq-resiliency-lab"`) is a `ConfigError` — the redirect is
  always explicit about the org so it reads the same regardless of the
  borrower's own org and supports cross-org borrowing without a special
  case.
- **Mutual exclusivity.** When `shared-from` is present it must be the
  *only* key under `[vm]`. Any footprint or package key (`cpus`, `memory`,
  `disk`, `stale_days`, `packages`, `apt_repos`, `vagrant_plugins`,
  `port_forwards`, `nested`) or any `[vm.<role>]` overlay present alongside
  it is a `ConfigError`. A repo either *describes* a box or *borrows* one;
  describing both is contradictory and would silently diverge the
  borrower's box from the lender's. The error names the offending keys.
- **Top-level only.** `shared-from` is rejected inside a `[vm.<role>]`
  overlay — a role cannot redirect.

`shared_from` is **not** added to `_VM_KEYS` (the footprint key set used by
the overlay cascade); it is handled explicitly in `parse_vm_stanza` so the
mutual-exclusivity and top-level-only rules can be enforced at parse time.

## Resolution

A single helper resolves the redirect once, reused by both resolver paths:

```text
resolve_borrow(identity, req_org, req_repo) -> Borrow | None
```

It reads the requested repo's `vergil.toml`. If `[vm].shared-from` is set it
validates and returns `Borrow(target_org, target_repo, target_stanza)`;
otherwise it returns `None`. Validation (all hard errors, never a silent
fallback):

1. **Self-reference.** `(target_org, target_repo) == (req_org, req_repo)` →
   error. A repo cannot borrow from itself.
2. **Missing lender.** The lender's directory, its `vergil.toml`, or its
   `[vm]` stanza is absent → error ("borrowed repo `<org>/<repo>` declares
   no VM").
3. **Chains.** The lender's stanza itself declares `shared-from` → error.
   Borrowing is one hop only; a borrower must point at a repo that owns a
   real box, not at another borrower. This keeps resolution finite and the
   ownership model legible.

When a `Borrow` is returned, the effective target is composed from the
**lender's** stanza:

- `instance = instance_name(identity, target_org, target_repo)` — the
  lender's instance, byte-for-byte.
- `spec = compose_vm_spec(identity, base, stanza=target_stanza,
  override=identity.overrides.get((target_org, target_repo)))` — host
  overrides are keyed by the **lender** `(org, repo)`, so the borrower sees
  exactly the box the lender's owner sees.
- `fingerprint = spec_fingerprint(spec)` — identical to the lender's,
  because both inputs are identical.

The `Target` returned for a borrowed session therefore carries the lender's
`org`/`repo`/`instance`/`spec`/`fingerprint`. The session's working
directory is unaffected: `_cmd_session` still derives `workspace_abs` from
`args.workspace` (the borrower's path). This automatically makes the
spec-check and staleness logic line up — the borrower computes the same
fingerprint and targets the same instance, so there is no rebuild churn and
no spurious `NEEDS-REBUILD`.

## Command Policy

Each subcommand is classified **USE** (follow the redirect) or **MANAGE**
(block on a borrowing repo). The borrower may *use* the shared box but not
*manage* its lifecycle — managing the shared box is done through the lender
repo, which owns it.

| Policy | Commands | Resolver | Behavior on a `shared-from` repo |
|---|---|---|---|
| **USE** | `session`, `start` | `_resolve_target` | Redirect instance + spec to the lender; workspace stays the borrower's checkout. |
| **MANAGE** | `create`, `rebuild` | `_resolve_target` | Block: error + exit 1. |
| **MANAGE** | `stop`, `restart`, `update`, `destroy` | `_resolve_instance` | Block: error + exit 1. |

The `start`/`stop` asymmetry is intentional: `start` is what a borrower
needs to *use* the box (bring it up before shelling in), whereas `stop` and
`restart` interrupt whatever the lender's owner is running. Powering the
shared box on is benign; powering it off on the owner's behalf is a
management action and belongs to the lender.

The block message names the lender and the equivalent command, e.g.:

```text
ERROR: logical-minds-foundry/mq-resiliency-tooling borrows the VM of
logical-minds-foundry/mq-resiliency-lab. Manage that box via the lender:
  vrg-vm destroy logical-minds-foundry/mq-resiliency-lab
```

### Wiring

- `_resolve_target` gains a `borrow_allowed: bool` parameter. `session` and
  `start` pass `True` (redirect); `create` and `rebuild` pass `False`
  (block). On a detected borrow with `borrow_allowed=False`, it raises
  `BorrowBlocked`.
- `_resolve_instance` is used only by MANAGE commands (`stop`, `restart`,
  `update`, `destroy`). It currently reads no `vergil.toml` (so orphaned VMs
  stay reachable). It gains a lightweight `shared-from` probe: if the
  requested repo's config is readable and declares `shared-from`, raise
  `BorrowBlocked`; if the config is missing (a true orphan), fall through to
  the existing instance-name behavior unchanged.
- `BorrowBlocked` carries the preformatted message and is caught at the
  command-dispatch layer in `main`, which prints it to stderr and returns 1.
  This keeps each `_cmd_*` handler free of redirect bookkeeping.

## Edge Cases

- **Borrower not yet created.** When a USE command targets a lender box that
  does not exist, the existing `_preflight_target` / `start` "VM does not
  exist" errors fire against the lender instance and point the user at the
  lender ref (the box's owner) to create it. Creating via the borrower alias
  is itself a MANAGE op and is blocked, so the guidance is unambiguous.
- **`list`.** A borrowing repo owns no instance, so it never appears as its
  own box. Its sessions run in the lender instance and therefore show under
  the lender in `vrg-vm list --sessions`. No change required.
- **Orphaned borrower.** If the borrower's `vergil.toml` is deleted, MANAGE
  commands fall through to plain instance-name resolution (which yields the
  borrower's own never-created instance name) — harmless, and consistent
  with how orphaned dedicated repos already behave.

## Testing

- **`lib/config.py` parsing** (extend the config test suite):
  - valid `shared-from` parses to `(org, repo)`;
  - bare-repo value rejected;
  - `shared-from` + any footprint/package key rejected (mutual exclusivity);
  - `shared-from` inside `[vm.<role>]` rejected;
  - malformed value (empty side, extra slash, whitespace) rejected.
- **Resolution** (extend `test_vrg_vm_resolve.py` / `test_vrg_vm.py`):
  - USE redirect — `_resolve_target` for a borrower yields the lender's
    instance, spec, and fingerprint while the session workspace stays the
    borrower's path;
  - host override keyed by the lender applies;
  - MANAGE block — `create`/`rebuild`/`stop`/`restart`/`update`/`destroy`
    on a borrower raise `BorrowBlocked` / exit 1 with a lender-pointing
    message;
  - self-reference, chain, missing-lender, and missing-`[vm]` errors.

## Out of Scope

- Multi-hop borrow chains (rejected by design).
- Overlaying borrower-specific packages on top of the lender's box (would
  diverge the fingerprint and defeat sharing; a repo that needs its own
  footprint should declare its own `[vm]`, not borrow).
- Borrowing the base (non-dedicated) box — `shared-from` always names a
  repo that owns a dedicated box.
