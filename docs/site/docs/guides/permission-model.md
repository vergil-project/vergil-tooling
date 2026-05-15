# Permission Model

VERGIL constrains AI agents to a validated set of operations
through layered enforcement. Every system interaction flows through
wrapper tools that validate, constrain, and log what happens. Raw
shell access to `git` and `gh` is denied mechanically — not by
convention, not by instruction, but by the permission system
itself.

For the identity model that permissions protect, see
[Identity Architecture](identity-architecture.md). For how
credentials are selected per-operation, see
[Credential Management](credential-management.md).

## Base Permission Mode

**`acceptEdits`** is the target mode. The agent freely reads and
modifies code files — that is its primary job. The real risk
surface is shell commands, not file edits.

`acceptEdits` auto-approves:

- File reads (Read tool)
- File edits (Edit tool)
- File writes (Write tool)

Shell commands not in the allowlist prompt the human. This is the
escalation mechanism: the agent encounters something it cannot do,
explains the situation, and the human approves or denies.

## `vrg-git` Wrapper

A Python CLI tool that validates subcommands and flags before
executing `git`. Arguments arrive as a Python argv list — no
shell expansion, no pipes, no redirection.

### Subcommand Allowlist

| Subcommand | Denied Flags | Notes |
|---|---|---|
| `status` | — | Read-only |
| `log` | — | Read-only |
| `diff` | — | Read-only |
| `show` | — | Read-only |
| `branch` | `-D`, `--force` | Safe delete (`-d`) allowed |
| `ls-remote` | — | Read-only |
| `rev-parse` | — | Read-only |
| `worktree add` | — | Parallel agent work |
| `worktree list` | — | Read-only |
| `worktree remove` | — | Cleanup after merge |
| `add` | — | Staging files |
| `push` | `--force`, `-f`, `--force-with-lease` | Normal push only |
| `fetch` | — | Read-only |
| `pull` | — | Fast-forward updates |
| `checkout` | `-- .`, `-- *` | Specific-file restore allowed |
| `switch` | — | Branch switching |
| `stash` | — | All stash subcommands allowed |
| `merge` | — | Branch updates |
| `cherry-pick` | — | Selective commit application |
| `rebase` | `-i`, `--interactive` | Non-interactive only |

### Denied Subcommands

Anything not on the allowlist is rejected. Notable denials:

- **`commit`** — all commits flow through `vrg-commit`
- **`reset`** — too dangerous in all forms
- **`clean`** — file deletion
- **`config`** — modifying git configuration
- **`remote`** — modifying remote configuration
- **`filter-branch`**, **`replace`** — history rewriting

### Escape Hatch

None in the wrapper. If the agent genuinely needs a denied
operation, it explains the situation to the human, who runs the
raw command via `! git <command>` in the Claude Code prompt.

## `vrg-gh` Wrapper

Same architecture as `vrg-git`. Validates the top-level and
second-level subcommands as a pair.

### Subcommand Allowlist

| Subcommand | Notes |
|---|---|
| `issue view` | Read-only |
| `issue create` | Issue tracking |
| `issue close` | Post-finalization closure |
| `issue edit` | Updating metadata |
| `issue list` | Read-only |
| `issue comment` | Adding comments |
| `pr view` | Read-only |
| `pr checks` | CI status |
| `pr list` | Read-only |
| `pr diff` | Read-only |
| `pr comment` | Review context |
| `pr edit` | Updating metadata |
| `run list` | CI status |
| `run view` | CI status |
| `run watch` | Blocking wait for CI |
| `repo view` | Read-only |
| `label list` | Read-only |
| `label create` | Used by `vrg-ensure-label` |

### Denied Subcommands

| Subcommand | Reason |
|---|---|
| `pr merge` | Agents do not merge (conditionally allowed for release workflows via credential escalation) |
| `pr review --approve` | Agents do not approve (conditionally allowed for release workflows) |
| `pr close` | Agents do not close PRs |
| `pr create` | Use `vrg-submit-pr` instead |
| `repo edit` | Admin operation |
| `repo create` | Admin operation |
| `repo delete` | Destructive |
| `api` | Raw API access — can perform any operation |
| `auth` | Credential management |

### The `gh api` Denial

`gh api` can perform any GitHub API operation — create repos,
delete branches, modify settings, push code via the Contents API.
Denying it entirely in the wrapper closes the escape hatch that
individual hook-based blocks cannot fully cover.

### Credential Selection

`vrg-gh` is responsible for choosing which account's token to use
per-command. See [Credential Management](credential-management.md)
for the selection logic. The `pr merge` and `pr review --approve`
entries above are conditionally allowed for release workflow
operations under the human account.

### VRG Tools That Bypass the Wrapper

Mechanized VRG tools (`vrg-submit-pr`, `vrg-merge-when-green`,
`vrg-ensure-label`, `vrg-github-config`, `vrg-finalize-repo`)
call `gh` directly in their Python code. They bypass the wrapper
because they are already validated — the wrapper only constrains
agent-initiated `gh` calls via the Bash tool.

## Permission Configuration

### Project Settings (`.claude/settings.json`)

```json
{
  "permissions": {
    "allow": [
      "Bash(vrg-*)"
    ],
    "deny": [
      "Bash(git *)",
      "Bash(*/git *)",
      "Bash(gh *)",
      "Bash(*/gh *)"
    ]
  }
}
```

The project layer is the primary enforcement point. It allowlists
all VRG tools via a single wildcard and hard-denies raw `git` and
`gh`. The `*/git *` patterns cover fully qualified path
invocations (e.g., `/usr/bin/git status`).

Deny rules at any scope cannot be overridden by allow rules at any
other scope. A deny in `.claude/settings.json` applies to everyone
who clones the repo — no per-developer setup needed.

### Global User Settings (`~/.claude/settings.json`)

```json
{
  "permissions": {
    "defaultMode": "acceptEdits"
  }
}
```

Switching `defaultMode` to `acceptEdits` makes unknown commands
prompt the human instead of auto-approving. This is independent
of the deny rules — raw `git`/`gh` are blocked regardless of mode.

### Local Settings (`.claude/settings.local.json`)

Gitignored, per-developer. Houses temporary read-only bash
exceptions:

```json
{
  "permissions": {
    "allow": [
      "Bash(grep *)",
      "Bash(find *)",
      "Bash(ls *)",
      "Bash(diff *)",
      "Bash(cat *)",
      "Bash(head *)",
      "Bash(tail *)",
      "Bash(wc *)",
      "Bash(which *)",
      "Bash(sort *)",
      "Bash(uniq *)",
      "Bash(stat *)",
      "Bash(du *)",
      "Bash(file *)"
    ]
  }
}
```

These are documented as candidates for removal as alternatives
are found (Explore subagent, native Read tool, future
`vrg-search`).

### What This Means in Practice

| Command | Result |
|---|---|
| `vrg-commit` | No prompt (project allow) |
| `vrg-git status` | No prompt (project allow) |
| `grep -r "foo" src/` | No prompt (local allow) |
| `git push` | Denied (project deny) |
| `curl https://...` | Prompts the human |
| `rm -rf .` | Prompts the human |
| `vrg-gh pr merge` | No Claude Code prompt, but wrapper rejects it |

## Defense-in-Depth

### The Vergil Assumption

Every client-side enforcement layer in this model assumes the agent
is cooperative — that it is operating as Vergil, not Mimir. An
agent with file write access (which `acceptEdits` grants) can
dismantle the entire client-side stack:

1. Delete or edit `.claude/settings.json` — Layer 1 gone
2. Run raw `git` and `gh` — Layer 2 bypassed (the wrappers are
   never invoked)
3. Remove plugin hook configuration — Layer 3 weakened
4. Delete `.githooks/pre-commit` or run
   `git config --unset core.hooksPath` — Layer 4 gone

After those four steps, the agent has unrestricted local access.
This is not a theoretical attack — it is trivial for any model
that decides to do it.

**Client-side layers are behavioral constraints, not security
boundaries.** They keep a well-intentioned agent on the rails.
They prevent mistakes, enforce consistency, and provide an audit
trail. They do not stop an adversary.

### The Actual Security Boundary

The only enforcement an agent cannot edit its way out of is
**server-side**: GitHub's own permissions, branch protection
rulesets, and collaborator access controls.

- The agent account is an outside collaborator with Write access.
  It physically cannot merge, approve, or access admin settings —
  GitHub rejects the API call regardless of what happens locally.
- Branch protection rulesets require review approval from a
  different account before merging. No client-side manipulation
  changes this.
- The agent account cannot see or access repos it has not been
  invited to.

This is the real security model. Everything else is convenience.

### Why Client-Side Layers Still Matter

If server-side enforcement is the only real boundary, why bother
with four client-side layers?

- **Mistake prevention.** Most failures are not adversarial. An
  agent that accidentally runs `git reset --hard` is not trying
  to subvert the tooling — it made a mistake. The wrappers catch
  this.
- **Consistency.** Routing all operations through `vrg-*` tools
  ensures commits have the right format, PRs have the right
  metadata, and branches follow the naming convention.
- **Audit trail.** Wrapper logging records what the agent did
  and when. Even if a layer is bypassed, the gap in the log is
  itself a signal.
- **Friction.** An agent that has to actively dismantle four
  layers is doing something visibly wrong. That visibility is
  the value — not the strength of any individual gate.

The [Mimir project](identity-architecture.md#the-mimir-convention)
exists specifically to test this boundary — to verify that
server-side controls hold when every client-side layer is
deliberately bypassed.

### Client-Side Layers

Four layers, each catching mistakes the layer above misses.
Redundancy is intentional — no single layer is trusted, and none
are sufficient against adversarial behavior.

**Layer 1 — Claude Code Permissions (outermost).** The
allowlist/deny configuration. Denied commands are blocked without
prompt. Unknown commands prompt the human.

**Layer 2 — VRG Wrappers.** `vrg-git` and `vrg-gh` validate
subcommands and flags before executing the underlying tool. Even
if `vrg-git reset --hard` passes the permission layer (it is
allowlisted as `vrg-*`), the wrapper rejects it.

**Layer 3 — Vergil Plugin Hooks.** The existing hook system
remains as a backstop. Hooks exit code 2 (hard block) overrides
even bypass mode. Hooks that become redundant with layers 1-2 are
retained — they catch regressions if a higher layer is
misconfigured.

**Layer 4 — Git-Level Hooks (`.githooks/pre-commit`).** The
innermost layer. Rejects raw `git commit` without the
`VRG_COMMIT_CONTEXT=1` environment variable that `vrg-commit`
sets. With layers 1-3 in place, agents cannot reach this layer
through normal operation.

### Layer Interaction Matrix

| Operation | L1 Permissions | L2 Wrapper | L3 Plugin Hook | L4 Git Hook | Server-Side |
|---|---|---|---|---|---|
| `vrg-commit` | allowed | n/a | n/a | admits | push accepted |
| `vrg-git push` | allowed | validates: no `--force` | n/a | n/a | push accepted |
| `git push` | denied | n/a | blocked | n/a | push accepted (if layers bypassed) |
| `vrg-gh pr merge` | allowed | rejected | blocked | n/a | **API rejected** (no merge permission) |
| `gh pr create` | denied | n/a | blocked | n/a | PR created (if layers bypassed) |
| `gh pr merge` (raw) | denied | n/a | blocked | n/a | **API rejected** (no merge permission) |
| `rm -rf .` | prompts human | n/a | n/a | n/a | n/a |
| `vrg-git reset --hard` | allowed | rejected | n/a | n/a | n/a |

The rightmost column is the only one that holds against Mimir.

## Phasing

### Phase 1 (current target)

- `acceptEdits` as default mode
- `vrg-git` and `vrg-gh` wrappers with subcommand validation
- Project-level deny rules for raw `git` and `gh`
- Temporary read-only bash exceptions in local settings
- Hooks retained as backstop

### Phase 2 (future)

- Evaluate replacing read-only bash exceptions with dedicated
  tools or subagent patterns
- Tighten the allowlist as more operations are mechanized
- Each frequent prompt is a signal to build a new VRG tool

### Phase 3 (future)

- Ideal end state: `Bash(vrg *)` is the only allowlisted pattern
- All operations flow through one command with validated
  subcommands
- No raw shell access without human approval

## Related

- [Identity Architecture](identity-architecture.md) — the
  accounts that permissions protect
- [Credential Management](credential-management.md) — how
  `vrg-gh` selects credentials per-operation
- [Account Setup](account-setup.md) — creating and configuring
  accounts
- [Permission model design spec][perm-spec] — full decision
  rationale and alternatives considered

[perm-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-14-permission-model-design.md
