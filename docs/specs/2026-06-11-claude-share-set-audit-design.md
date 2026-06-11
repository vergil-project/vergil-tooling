# Audit the `.claude` share set — cleanups + plugins experiment

- **Issue:** [#1603](https://github.com/vergil-project/vergil-tooling/issues/1603)
- **Status:** Design approved
- **Date:** 2026-06-11
- **Owner of the affected code:** `src/vergil_tooling/lib/lima.py`
  (`create_vm`, `link_claude_dirs`, `copy_claude_config`). The
  `vergil-vm/templates/agent.yaml` template only declares the static
  `projects` mount; the `.claude` sub-mounts and symlinks are injected
  dynamically by `lima.py`.

## Background

Agent VMs share a curated subset of the host's `~/.claude` directory. The
share set is defined entirely in `lima.py`:

- **Mounts** (`create_vm`): `mounts[0]` projects_dir · `mounts[1]`
  `.claude/projects` (rw) · `mounts[2]` `.claude/skills` (ro) ·
  `mounts[3]` `.claude/sessions` (rw).
- **Symlinks** (`link_claude_dirs`): `_CLAUDE_LINK_DIRS = ("projects",
  "skills")` are symlinked onto the path-preserved host mounts;
  `_CLAUDE_UNLINK_DIRS = ("sessions",)` are kept VM-local (any stale
  symlink from an older build is removed).
- **Copy** (`copy_claude_config`): only `CLAUDE.md` + `settings.json`.

An audit (#1603) surfaced three findings of differing certainty:

1. **Plugins version skew (open question).** `~/.claude/plugins` is never
   shared — not mounted, symlinked, or copied. It is VM-local, installed
   at build time, so it drifts from the host between rebuilds. History
   confirms this was never implemented (not a regression).
2. **Sessions persistence is correct (decided).** Resume-after-rebuild
   works because it reads the **shared `projects/` transcript**, not the
   VM-local `sessions/` roster. The #1301 "keep the roster VM-local"
   change did not break resume.
3. **Vestigial sessions mount (decided).** `mounts[3]` mounts the host
   `~/.claude/sessions`, but the VM keeps `sessions/` VM-local
   (`_CLAUDE_UNLINK_DIRS`) and never reads the mount. It is dead weight.

## Scope

This issue ships the two **decided** cleanups (findings 2 and 3) and a
**documented, reproducible** EXDEV experiment procedure for finding 1.

**Out of scope:** any permanent plugins mount/symlink wiring. That is a
follow-up issue, decided by the experiment's outcome (see Component 3).
Nothing irreversible happens to plugins in this issue.

## Component 1 — Remove the vestigial sessions mount

In `create_vm` (`lima.py`):

- Delete the three `--set=.mounts[3]...` (sessions) args. `mounts[3]` is
  the last slot, so the remaining mounts do not re-index.
- Remove the now-dead `claude_sessions_path` / `claude_sessions` locals
  and the host `claude_sessions_path.mkdir(...)`. Nothing mounts or reads
  that host directory anymore — VM session detection reads each VM's
  *local* roster over `limactl shell`, and the host's own Claude creates
  `~/.claude/sessions` for itself when it needs it. `create_vm` only ever
  created it to back the (now-removed) mount.

**Keep unchanged:** `_CLAUDE_UNLINK_DIRS = ("sessions",)` and the
symlink-removal loop in `link_claude_dirs`. An existing VM carrying a
stale `sessions` symlink (from an older build) must still be cleaned up
on re-link. The mount removal and the symlink guard are independent
concerns; only the mount is vestigial.

## Component 2 — Document the persistence model

The point of finding 2 is that the projects-vs-sessions persistence model
should be written down in one place so it is not re-litigated. Two
pieces:

### Code comment

Expand the comment block at `_CLAUDE_LINK_DIRS` (and a short note at the
mounts in `create_vm`) to state the full model:

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

- the `.claude` share set (projects rw / skills ro / sessions VM-local),
- the resume-after-rebuild mechanism (reads the shared `projects/`
  transcript, not the local roster), and
- why `sessions/` must stay VM-local (the #1301 `EXDEV` story).

This is the durable "so it isn't re-litigated" record. Cross-reference
adjacent guides (e.g. `identity-architecture.md`) and the worktree
convention where relevant.

## Component 3 — Plugins EXDEV experiment procedure

This issue **documents and hands off** the experiment; it does not wire
plugins and does not flip any code on.

**Ownership.** The human runs the experiment on a macOS host. It needs a
live Lima VM and the real virtiofs boundary, which an agent session
(itself inside a VM/container) cannot faithfully reproduce. The agent
produces the procedure; the human runs it; the result seeds the
follow-up.

**Why an experiment is required.** `sessions/` was made VM-local
precisely because Claude's atomic temp-file + `rename()` writes hit
`EXDEV` across the virtiofs boundary (#1301). Plugin installs and
marketplace updates likely use the same temp+rename pattern. Wiring a
plugins mount without testing risks trading a stale-version bug for a
silent-write-failure bug.

**Procedure:**

1. Scratch/uncommitted change: add a `.claude/plugins` mount in
   `create_vm` and `"plugins"` to `_CLAUDE_LINK_DIRS`, mirroring
   `projects`.
2. Rebuild/start an agent VM; confirm `~/.claude/plugins` is a symlink
   onto the virtiofs mount.
3. In the VM, run `claude plugin marketplace update` and
   `claude plugin update`.
4. Observe: success (writes land on the host mount) vs. `EXDEV` /
   silent-failure on the atomic temp+rename.

**Decision the result feeds (follow-up issue, not this one):**

- **Success** → wire the mount + symlink permanently, so plugins track
  the host live.
- **`EXDEV`** → fallback: host→VM copy at provision, or a VM-side
  `claude plugin update` on session start.

## Testing

- `tests/vergil_tooling/test_lima.py`:
  - Update `test_adds_claude_submounts`: assert there are **no**
    `mounts[3]` / sessions args, and `mounts[0..2]` are unchanged.
  - Update `test_creates_host_claude_dirs`: drop the `sessions` directory
    assertion (projects/ and skills/ still created).
- `link_claude_dirs` behavior is unchanged; existing coverage stands.
- markdownlint on the new guide.
- Full gate: `vrg-container-run -- vrg-validate`.

## Related

- #1301 — sessions roster kept VM-local (`EXDEV` rationale).
- vergil-vm #73 — same `EXDEV` thread.
- #1602 — `vrg-git blame` allowlist gap surfaced during the audit
  (separate).
