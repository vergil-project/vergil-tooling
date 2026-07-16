# Agent VM `.claude` Share Set

This guide explains which parts of the host's `~/.claude` directory are
shared into agent VMs, how conversation resume survives a VM rebuild, and
why plugins and the session roster are deliberately kept VM-local. The
wiring lives in `src/vergil_tooling/lib/lima.py` (`create_vm`,
`link_claude_dirs`, `copy_claude_config`); the `vergil-vm` template only
declares the static `projects` mount.

The share set below describes **local (Lima) VMs**. **Cloud VMs** cannot
share the host store live, so they receive canonical agent data as a
read-only cached projection instead — see [Cloud VMs: read-only memory
projection](#cloud-vms-read-only-memory-projection).

## The share set

| Subdir | How it is shared | Writable | Why |
|---|---|---|---|
| `projects/` | virtiofs mount + symlink | yes | Durable conversation transcripts; must survive rebuilds. |
| `skills/` | virtiofs mount + symlink | no | Read-only reference. |
| `sessions/` | **not shared** (VM-local) | n/a | Disposable per-VM roster; sharing breaks atomic writes (EXDEV). |
| `plugins/` | **not shared** (VM-local) | n/a | Installed/refreshed in-VM from GitHub marketplaces. |

`copy_claude_config` additionally copies `CLAUDE.md` and `settings.json`
into each VM on create and on every start.

## Why resume survives a rebuild

Conversation transcripts are written as append-only `*.jsonl` files under
`~/.claude/projects/<slug>/`. That directory is a symlink onto the
path-preserved host mount, so the transcripts live on the host and are
untouched when a VM is destroyed and recreated. Resuming a conversation
reads those transcripts — **not** the `sessions/` roster — so a rebuild
never loses history.

## Why `sessions/` is VM-local

`~/.claude/sessions/` holds a small roster of `pid -> session` files that
Claude writes atomically: it writes a temp file in the VM-local tmpdir and
then `rename()`s it onto the target. Renaming across filesystems (VM-local
ext4 to the virtiofs host mount) fails with `EXDEV`, so the write would
silently fail and no roster file would ever appear. The roster is also
per-machine (pids only mean anything on the owning host), so there is no
value in sharing it. Session detection reads each VM's local roster
in-guest over `limactl shell`. See vergil-tooling #1301 and vergil-vm #73.

> A host `~/.claude/sessions` mount used to exist (`mounts[3]`) but the VM
> never read it once the roster was made VM-local; it was removed as dead
> weight (#1603).

## Why plugins are VM-local, not shared

Plugins are declared in the host `settings.json` (`enabledPlugins` and
`extraKnownMarketplaces`), which is copied into each VM. The marketplaces
are **GitHub repositories**, so each VM installs the enabled plugins itself
on first launch and keeps them current with an in-VM refresh — the same
model used for vergil-tooling. The host's materialized `~/.claude/plugins`
checkout is never shared: doing so would cross the macOS/Linux boundary
(fragile if any plugin ships a binary) and hit the same `EXDEV` write
problem as the roster. Instead, `update_plugins` refreshes the marketplaces
(`claude plugin marketplace update`) and then updates each **enabled** plugin to
its latest version with the plugin's own scope (`claude plugin update <id>
--scope <user|project>` — there is no bulk-update form), all inside the VM.
It is driven by `vrg-vm update` and a warn-mode stage on VM start/rebuild.

## Cloud VMs: read-only memory projection

The share set above works because **local (Lima) VMs** path-preserve the
project mount and symlink `~/.claude/{projects,skills}` onto the host's
live directories, so a memory write in a local-VM session flows straight
through to the durable host disk. **Cloud VMs** — off-platform x86 boxes
backed by the ephemeral `/vergil` data disk — cannot do that. Their disk
is nuked and rebuilt freely, and the checkout lives at a different path,
so a memory write there would either be lost on the next rebuild or land
under a slug no other environment reads. The corrosive part is that the
loss is **silent**: the harness advertises the memory directory as
persistent, an agent writes memory believing it durable, and the write
quietly evaporates.

To keep the physical host the single source of truth, a cloud VM receives
canonical agent data — the per-repo `memory/` subtree and the global
`CLAUDE.md` — as a **read-only cached projection** that never flows back
automatically. See epic
[vergil-project/.github#156](https://github.com/vergil-project/.github/issues/156)
and its
[spec](https://github.com/vergil-project/.github/blob/develop/epics/156-cloud-memory-projection/spec.md)
for the full rationale, trust model, and failure modes.

### The three platforms

A single platform-awareness signal distinguishes three states along two
axes — *sandbox* (in a VM) vs. *host* (not a VM), and *cloud* vs.
*local*:

| Platform | Sandbox? | Credentials | Canonical store |
|---|---|---|---|
| `physical-host` | no | human | live and writable (source of truth) |
| `local-vm` | yes | agent (GitHub App) | shared live via path-preserved mounts (durable) |
| `cloud-vm` | yes | agent (GitHub App) | read-only copy on ephemeral disk |

`physical-host` names the property that matters — *physical, not a VM* —
not the operating system; it is macOS today only because that is the
hardware in use. The read-only memory control activates **only** on
`cloud-vm`: `local-vm` writes already reach the live host store, and
`physical-host` *is* the store.

### The platform resolver (`vrg-whoami --platform`)

`vrg-whoami --platform` reports the platform as a single token, parallel
to the existing `--mode`; `vrg-whoami --explain` reports the resolving
signal and warns on disagreement. The resolver is **empirical** — it
derives the platform from heuristics (a `/vergil` mount ⇒ in a VM; a
reachable cloud metadata endpoint ⇒ cloud; host OS and resolved identity
as corroboration), with **no written marker file**.

It **fails closed** for the memory control: if a VM signal is present but
the box cannot be positively confirmed as `local-vm`, it is treated as
`cloud-vm` (locked), never as `physical-host`. Fail-open — silently
deciding "host" and re-enabling futile writes — is the exact failure this
model exists to kill, and is prohibited.

### Read-only projection, refreshed at session start

Projection is coupled to `vrg-vm` **cloud-session start**, not build: a
cloud VM is not used until a session opens, so the first session on a
freshly built box performs the initial projection and every subsequent
session refreshes it. Before opening the session, `vrg-vm`:

1. aligns the guest working directory to the host project path (via a
   symlink onto the `/vergil` checkout) so Claude derives the **same
   memory slug** as the host and finds the projected memory;
2. copies that repo's `memory/` + `MEMORY.md` and the global `CLAUDE.md`
   from the host over the established transport idiom (the same
   `cat > <file>` mechanism `copy_claude_config` uses — there is no
   `rsync` over the transport);
3. re-applies **surgical** read-only permissions — locking `memory/`,
   `MEMORY.md`, and `CLAUDE.md` while leaving the session-transcript
   `.jsonl` files writable (a blanket `chmod` of `projects/<slug>/`
   would break session logging);
4. **verifies** that the host path resolves in the guest and the slug
   memory directory is non-empty, failing **loudly** otherwise — a
   broken indirection would otherwise degrade silently to empty memory.

The read-only permissions are the hard backstop: a write attempt in the
cloud fails with `EACCES` — loudly, so a futile write can no longer
succeed-then-vanish.

### The policy clause (soft control)

The hard read-only permissions are paired with a **soft control**: a
condition-gated clause in the canonical `~/.claude/CLAUDE.md` that stops
an agent from *attempting* a cloud memory write in the first place. Its
substance: when `vrg-whoami --platform` reports `cloud-vm`, memory and
copied config are a read-only cache; never write memory — the write
would fail (`EACCES`) and would be futile anyway — and when memory needs
to change, file it via `/vergil:triage-capture` for implementation on
the host. Because that file is the developer's personal, global config,
the clause is authored once on the host and rides the copied file into
every environment; its condition is simply almost never true outside
`cloud-vm`. Keeping the clause part of the canonical file also keeps
`CLAUDE.md` byte-identical across environments, so it stays lockable.

### Issue-driven writeback

Canonical data never flows back automatically. When a cloud session
determines that memory or config must change, it files an issue through
the existing `/vergil:triage-capture` flow, producing an intake issue in
the org `.github` repository. The change is later implemented on the
physical host against the live store and re-projected on the next cloud
session or rebuild. Given how infrequently this data changes, this
issue-driven, human-executed writeback is sufficient — no automated
two-way sync.

See also: [Identity Architecture](identity-architecture.md).
