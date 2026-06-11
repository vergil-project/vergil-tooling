# Audit the `.claude` share set — cleanups + VM-local plugin lifecycle

- **Issue:** [#1603](https://github.com/vergil-project/vergil-tooling/issues/1603)
- **Status:** Design approved
- **Date:** 2026-06-11
- **Owner of the affected code:** `src/vergil_tooling/lib/lima.py`
  (`create_vm`, `link_claude_dirs`, `copy_claude_config`, `update_tooling`)
  and `src/vergil_tooling/bin/vrg_vm.py` (lifecycle pipeline + `update`
  command). The `vergil-vm/templates/agent.yaml` template only declares
  the static `projects` mount; the `.claude` sub-mounts, symlinks, and
  config copy are injected dynamically by `lima.py`.

## Background

Agent VMs share a curated subset of the host's `~/.claude` directory.
The share set is defined entirely in `lima.py`:

- **Mounts** (`create_vm`): `mounts[0]` projects_dir · `mounts[1]`
  `.claude/projects` (rw) · `mounts[2]` `.claude/skills` (ro) ·
  `mounts[3]` `.claude/sessions` (rw).
- **Symlinks** (`link_claude_dirs`): `_CLAUDE_LINK_DIRS = ("projects",
  "skills")` are symlinked onto the path-preserved host mounts;
  `_CLAUDE_UNLINK_DIRS = ("sessions",)` are kept VM-local (any stale
  symlink from an older build is removed).
- **Copy** (`copy_claude_config`): `_CLAUDE_CONFIG_FILES = ("CLAUDE.md",
  "settings.json")`.

An audit (#1603) surfaced three findings of differing certainty:

1. **Plugins version skew.** `~/.claude/plugins` is never shared — not
   mounted, symlinked, or copied. It is installed VM-locally on first
   launch and then drifts from the host between rebuilds (observed: a VM
   pinned to vergil **2.1.4** while the host had **2.1.8**). History
   confirms this was never a regression; plugins were never in the share
   set.
2. **Sessions persistence is correct (decided).** Resume-after-rebuild
   works because it reads the **shared `projects/` transcript**, not the
   VM-local `sessions/` roster. The #1301 "keep the roster VM-local"
   change did not break resume.
3. **Vestigial sessions mount (decided).** `mounts[3]` mounts the host
   `~/.claude/sessions`, but the VM keeps `sessions/` VM-local
   (`_CLAUDE_UNLINK_DIRS`) and never reads the mount. It is dead weight.
   (History: `32bc0484d` added the mount "for session persistence";
   #1301 later made the directory VM-local without removing the mount,
   orphaning it.)

## How the VM gets its plugins today

There is no `claude plugin` or marketplace command anywhere in the
agent-VM template, `lima.py`, or `vrg_vm.py`. The plugins arrive
indirectly:

- The host `settings.json` declares `enabledPlugins` (the on/off set —
  currently `paad`, `vergil`, `superpowers`, `diogenes` enabled) and
  `extraKnownMarketplaces` (their **GitHub source repos**).
- `copy_claude_config` copies `settings.json` into the VM on create and
  on every start (the `copy-config` lifecycle stage).
- On first launch in the VM, Claude reads `settings.json` and installs
  the enabled plugins **VM-locally** from their GitHub marketplaces into
  `~/.claude/plugins`.

Two consequences follow, and they decide the design:

1. The plugins are sourced from **GitHub repos**, not from the host's
   materialized `~/.claude/plugins` checkout. The "what to install" is
   already declared (`settings.json`) and already seeded into the VM.
2. Once installed at first launch, the VM never refreshes; the host
   advances and the VM stays pinned. That is the skew.

## Scope

This issue delivers all three findings:

- **Findings 2 and 3** — the decided cleanups (remove the vestigial
  mount; document the persistence model).
- **Finding 1** — a VM-local plugin lifecycle that mirrors how
  vergil-tooling is kept current in VMs. We **do not** share plugins
  across the host/VM boundary.

### Rejected approach: sharing plugins via a mount

An earlier direction was to mount + symlink `~/.claude/plugins` like
`projects`/`skills`, gated on an EXDEV experiment (atomic `rename()`
across the virtiofs boundary fails with `EXDEV`, the #1301 failure mode).
That approach is rejected:

- **It fights two problems, not one.** Beyond EXDEV, the host's
  `~/.claude/plugins` is a materialized checkout on macOS; symlinking it
  into a Linux VM is fragile and would break outright if any plugin ever
  shipped a compiled/binary component. Sharing the materialized directory
  is the wrong boundary to cross.
- **It is unnecessary.** The marketplaces are GitHub repos, so each VM
  can install and refresh plugins itself, platform-independently — the
  same model already used for vergil-tooling. No shared mount, no
  virtiofs writes, no EXDEV.

This is a deliberate retreat from making host/VM plugin state transparent.
Transparent cross-boundary sharing is an unusual code path; the
tooling-style "push the update into each VM" model is simpler and
robust.

## Component 1 — Remove the vestigial sessions mount

In `create_vm` (`lima.py`):

- Delete the three `--set=.mounts[3]...` (sessions) args. `mounts[3]` is
  the last slot, so the remaining mounts do not re-index.
- Remove the now-dead `claude_sessions_path` / `claude_sessions` locals
  and the host `claude_sessions_path.mkdir(...)`. Nothing mounts or reads
  that host directory: VM session detection reads each VM's *local*
  roster in-guest (`vrg_vm_resolve.read_roster` over `Path.home()/
  ".claude"/"sessions"`, invoked via `limactl shell`), and the host's own
  Claude creates `~/.claude/sessions` for itself when it needs it.
  `create_vm` only ever created it to back the now-removed mount.

**Keep unchanged:** `_CLAUDE_UNLINK_DIRS = ("sessions",)` and the
symlink-removal loop in `link_claude_dirs`. An existing VM carrying a
stale `sessions` symlink (from an older build) must still be cleaned up
on re-link. The mount removal and the symlink guard are independent
concerns; only the mount is vestigial.

Existing VMs created with the old `mounts[3]` keep the (unused) mount
until their next rebuild — harmless, since nothing reads it.

## Component 2 — Document the persistence model

Two pieces:

### Code comment

Expand the comment block at `_CLAUDE_LINK_DIRS` (and a short note at the
mounts in `create_vm`) to state the full model in one place:

- `projects/` → durable, **host-shared** transcript store. Resume reads
  these; survives VM rebuilds because the data lives on the host.
  Append-only writes, so virtiofs is fine.
- `skills/` → read-only reference mount.
- `sessions/` → disposable **VM-local** roster (pid→session); regenerated
  each run; never shared (atomic `rename()` across the virtiofs boundary
  fails with `EXDEV`, and pids are meaningless cross-machine). **Resume
  does not depend on it.**

The comment links to the guide below.

### Docs guide

New `docs/site/docs/guides/agent-vm-claude-share-set.md` narrating:

- the `.claude` share set (projects rw / skills ro / sessions VM-local);
- the resume-after-rebuild mechanism (reads the shared `projects/`
  transcript, not the local roster);
- why `sessions/` must stay VM-local (the #1301 `EXDEV` story); and
- how plugins are kept current (VM-local install + refresh, Component 3),
  and why they are deliberately *not* shared.

Cross-reference adjacent guides (e.g. `identity-architecture.md`) where
relevant.

## Component 3 — VM-local plugin lifecycle (mirrors vergil-tooling)

Plugins stay unmounted and unshared. Each VM owns its own plugin
directory and is refreshed from the GitHub marketplaces — exactly the
model already used for vergil-tooling, where `update_tooling` runs both
on demand (`vrg-vm update`) and as a warn-mode start stage.

### Refresh primitive

Add `update_plugins(instance: str)` to `lima.py`, mirroring
`update_tooling`. It runs, inside the VM:

- `claude plugin marketplace update` — refresh all marketplace metadata; then
- for each **enabled** plugin from `claude plugin list --json`,
  `claude plugin update <id> --scope <user|project>`.

`claude plugin update` has no bulk form and honours each plugin's scope
(the `vergil` plugin is `project`-scoped, others `user`), so the refresh
enumerates the installed list and updates each enabled plugin with its own
scope. It is best-effort across the set — one plugin failing does not block
the rest, and failures are surfaced by raising afterwards.

The "what" needs no new configuration: `settings.json` (already copied by
the `copy-config` stage on every start) is the source of truth for
enabled plugins and marketplaces, so newly enabled/disabled host plugins
propagate on the next start, and `update_plugins` only advances versions.

### Drive it two ways (mirroring tooling)

1. **Fold into `vrg-vm update`.** `vrg-vm update` and `vrg-vm update
   --all` refresh **both** tooling and plugins in one pass. Concretely,
   `_update_instance` calls `update_plugins` alongside `update_tooling`.
   "Update my VM(s)" brings everything current — the same gesture used
   after re-releasing tooling now also picks up re-released plugins.
2. **Warn-mode start/rebuild stage.** Add `Stage("update-plugins",
   _st_update_plugins, mode="warn")` beside the existing `update-tooling`
   stage in the start and rebuild pipelines, so a started or rebuilt VM
   converges to latest plugins. Warn mode: a failed refresh surfaces as
   ⚠ in the lifecycle summary, never aborts the session.

### First-launch install on create

The create pipeline relies on Claude's first-launch auto-install from
`settings.json`. **Implementation must verify this happens reliably in
the headless/agent launch path.** If first-launch auto-install is not
dependable without an interactive TUI, add an explicit plugin-install
step to the create pipeline (install the enabled plugins from
`settings.json`) so a freshly created VM is never plugin-less. Resolve
this during implementation; the refresh primitive above is install-safe
to re-run regardless.

## Testing

- `tests/vergil_tooling/test_lima.py`:
  - Update `test_adds_claude_submounts`: assert there are **no**
    `mounts[3]` / sessions args, and `mounts[0..2]` are unchanged.
  - Update `test_creates_host_claude_dirs`: drop the `sessions` directory
    assertion (projects/ and skills/ still created).
  - Add coverage for `update_plugins`: asserts the in-VM command is
    `claude plugin marketplace update` followed by `claude plugin
    update`, dispatched through the shell wrapper.
- `tests/vergil_tooling/test_vrg_vm.py`:
  - `vrg-vm update` / `--all` invokes the plugin refresh as well as the
    tooling update.
  - The start (and rebuild) pipeline includes the `update-plugins`
    warn-mode stage.
- `link_claude_dirs` behavior is unchanged; existing coverage stands.
- markdownlint on the new guide.
- Full gate: `vrg-container-run -- vrg-validate`.

## Related

- #1301 — sessions roster kept VM-local (`EXDEV` rationale).
- vergil-vm #73 — same `EXDEV` thread.
- `32bc0484d` — added the sessions mount "for persistence" (now
  vestigial).
- #1602 — `vrg-git blame` allowlist gap surfaced during the audit
  (separate).
- Prior art: `update_tooling` (`lima.py`) + `vrg-vm update` — the pattern
  Component 3 mirrors.
