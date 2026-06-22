# vrg-vm: bound the Lima instance name under UNIX_PATH_MAX

- **Issue:** vergil-project/vergil-tooling#1750
- **Status:** Approved design
- **Date:** 2026-06-22

## Problem

`vrg-vm create` for a dedicated VM fails when the derived Lima instance name
is long enough that Lima's pre-validated worst-case SSH socket path exceeds
`UNIX_PATH_MAX` (104). Observed in the field:

```text
instance name "vergil-user.logical-minds-foundry.mq-resiliency-lab-for-linux" too long:
".../ssh.sock.1234567890123456" must be less than UNIX_PATH_MAX=104 characters, but is 107
```

Lima validates the longest socket path it might ever create for an instance:

```text
<home>/.lima/<instance>/ssh.sock.<16-char reservation>
```

The `1234567890123456` segment is a fixed worst-case reservation inside Lima,
not a live value — so the suffix cannot be shortened. The home prefix is fixed
by the user. The only lever we control is the **instance name**.

Renaming the repository is explicitly out of scope as a fix: the tooling must
accommodate legitimately long `org`/`repo` names.

## Root cause

`vm_spec.instance_name()` joins the three tiers `identity.org.repo` with a
single dot and applies no length bound:

```python
return _TIER_SEP.join((identity, org, repo))
```

The cloud backend already solved the analogous constraint
(`vm_cloud.cloud_resource_name` truncates and hashes to fit GCP's 63-char
limit; `vm_cloud.cloud_labels` provides name-independent recovery). The Lima
path never adopted the pattern.

### Why recovery is load-bearing

The instance name is not just an identifier — it is currently the only channel
for reversing a VM back into its `(identity, org, repo)` triple:

- `discover_dedicated` (`vrg_vm.py`) calls `parse_instance_name` and uses the
  recovered `org`/`repo` to map a VM to its local repo via `_classify_instance`.
- `_all_update_targets` (`vrg_vm.py`) calls `parse_instance_name` to match a
  VM's identity tier against configured identities.

A truncated/hashed name cannot be parsed back into tiers, so the triple must be
recoverable from a separate channel. Lima offers none that survives this:
`limactl list --json` exposes only `name` and `status`, and the persisted
`~/.lima/<instance>/lima.yaml` is the *expanded* template — our `param.VERGIL_*`
substitution variables are consumed during expansion, not retained as a
readable map.

## Design

### 1. Name generation — `vm_spec.instance_name`

The length handling lives in `instance_name` (the Lima analog of
`cloud_resource_name`), so every caller inherits it.

1. **Base box unchanged.** Bare identity (`org`/`repo` is `None`) returns the
   identity verbatim, as today.
2. **Dotted name when it fits.** Compute the full `identity.org.repo` (existing
   format, existing "identity/org must not contain a dot" rule preserved) and
   return it verbatim when `len(full) <= budget`. This keeps every existing
   short-named VM **byte-identical**.
3. **Truncate + hash when over budget**, mirroring the cloud backend:

   ```python
   digest = hashlib.sha256(f"{identity}/{org}/{repo}".encode()).hexdigest()[:6]
   keep = budget - 6 - 1  # 6 hash chars + 1 separator
   name = f"{full[:keep].rstrip('._-')}-{digest}"
   ```

   The cloud backend strips only `'-'` because its slug contains only hyphens.
   Here the truncated string is the dotted `full`, which can be cut mid-token or
   mid-separator. Lima's name regex `^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$`
   forbids adjacent separators (`.-`) and trailing separators, so we
   `rstrip('._-')` to guarantee the prefix ends in an alphanumeric before the
   `-<hash>` join.

### 2. Budget computation

Derived from the exact path Lima validates and verified against the observed
107-char failure:

```python
UNIX_PATH_MAX = 104
# path = <home> + "/.lima/" + <name> + "/ssh.sock." + <16-char reservation>
def lima_name_budget(home: str | None = None) -> int:
    home = home if home is not None else str(Path.home())
    overhead = len("/.lima/") + len("/ssh.sock.") + 16   # 7 + 10 + 16 = 33
    return (UNIX_PATH_MAX - 1) - len(home) - overhead     # == 70 - len(home)
```

For `/Users/pmoore` (13) the budget is 57, matching the observed boundary (the
61-char name overflowed by 4). `home` is injectable for tests.

The budget depends on `len(home)`, so the mangled name is deterministic
**per machine**. VMs are not shared across machines, so this is sufficient; see
Edge cases for the one consequence.

**Pathological-home guard.** If the budget cannot fit `identity` plus the
`-<hash>` suffix (`budget < len(identity) + 7`), `instance_name` raises a clear
`SpecError` rather than emitting a name Lima will reject. This trades a confusing
downstream `limactl` error for an explicit, early one.

### 3. Recovery — per-instance sidecar

- **Write.** Immediately after `limactl create` succeeds, write
  `~/.lima/<instance>/vergil-meta.json`:

  ```json
  {"schema": 1, "identity": "...", "org": "...", "repo": "..."}
  ```

  It lives in Lima's own per-instance directory, so it is created with the VM
  and removed by `limactl delete --force` (which deletes the whole dir). No
  drift, no orphan cleanup, scoped to exactly one VM.
- **Read.** A new `recover_triple(instance) -> (identity, org, repo)`:
  reads the sidecar when present; otherwise falls back to
  `parse_instance_name(instance)` for legacy short names and base boxes.
- **Swap both call sites** (`discover_dedicated`, `_all_update_targets`) from
  `parse_instance_name` to `recover_triple`.

This also fixes a latent bug: a mangled name raises in `parse_instance_name`,
which both sites catch and `continue` past — so a long-named VM would be
silently invisible to discovery and to `vrg-vm update --all`. The sidecar makes
mangled VMs first-class.

## Components touched

| File | Change |
|---|---|
| `lib/vm_spec.py` | `lima_name_budget()`; length handling in `instance_name`; pathological-home guard |
| `lib/vm_spec.py` *(or a small recovery module)* | `recover_triple()`; sidecar read |
| `bin/vrg_vm.py` | write sidecar after create; `discover_dedicated` and `_all_update_targets` use `recover_triple` |
| `tests/...` | unit + integration coverage (below) |

## Testing

- **Budget math** across representative home lengths (short macOS, long macOS,
  Linux `/home/...`), including the verified `/Users/pmoore` → 57 boundary.
- **Name unchanged when it fits** — existing short triples round-trip byte-identical.
- **Mangled output is valid** — assert the result matches Lima's name regex,
  including a case engineered to truncate on a separator (exercises `rstrip('._-')`).
- **Hash determinism** — same triple → same name on a fixed home.
- **Pathological-home guard** — raises `SpecError`, not an invalid name.
- **Sidecar round-trip** — write then `recover_triple` returns the triple.
- **Fallback** — `recover_triple` returns `parse_instance_name` output when no
  sidecar exists (legacy short name; base box).

## Backward compatibility & migration

- Existing short-named dedicated VMs keep identical names and resolve via the
  `parse_instance_name` fallback — **zero migration**.
- No over-budget VM can exist today: it would have failed at `create`, exactly
  like the reported case. So there is nothing to rename or migrate.
- Base boxes (bare identity) are untouched.

## Edge cases

- **Home-length change between create and lookup.** The resolve path recomputes
  `instance_name` to find an existing VM. If `len(home)` changes after a VM was
  created over-budget (rare — e.g. the user's home path is renamed), the
  recomputed name would not match the live instance. Accepted: home-path length
  changes are rare, and building reverse-lookup reconciliation is YAGNI. Short
  (unmangled) VMs are unaffected because their names don't depend on the budget.

## Out of scope

- Relocating `LIMA_HOME` (reclaims too few characters; breaks `~/.lima`
  assumptions elsewhere).
- Reverse-lookup reconciliation for home-length changes (see Edge cases).
- Any change to the cloud backend (already correct).
