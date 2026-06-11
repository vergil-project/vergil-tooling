# Agent VM `.claude` Share Set

This guide explains which parts of the host's `~/.claude` directory are
shared into agent VMs, how conversation resume survives a VM rebuild, and
why plugins and the session roster are deliberately kept VM-local. The
wiring lives in `src/vergil_tooling/lib/lima.py` (`create_vm`,
`link_claude_dirs`, `copy_claude_config`); the `vergil-vm` template only
declares the static `projects` mount.

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
problem as the roster. Instead, `update_plugins` runs `claude plugin
marketplace update` + `claude plugin update` inside the VM, driven by
`vrg-vm update` and a warn-mode stage on VM start/rebuild.

See also: [Identity Architecture](identity-architecture.md).
