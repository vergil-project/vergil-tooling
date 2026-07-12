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

**`bypassPermissions`** is the standard mode — but only because the
agent runs inside a sandboxed, ephemeral VM (see
[The VM Sandbox Boundary](#the-vm-sandbox-boundary)). Inside that
sandbox the agent reads, edits, and runs shell commands without
per-action prompts, which is what makes it usable for real work.

### Why not `acceptEdits`

`acceptEdits` was the original target: auto-approve file edits, prompt
on any shell command not in the allowlist. It proved impractical for
the work VERGIL actually does. An agent doing infrastructure work runs
a constant stream of shell commands — container builds, cloud CLIs,
system inspection, ad-hoc scripts — and under `acceptEdits` every
non-allowlisted one stops for human approval. The prompts never end,
and autonomous work becomes impossible. Chasing an ever-growing bash
allowlist was a losing race.

So VERGIL moved the boundary outward instead of inward: rather than
constrain *which commands* the agent may run, **sandbox the whole agent
in a disposable VM** and let it run in `bypassPermissions` there. The
blast radius is the VM, which is ephemeral and reproducible.

### Bypass is not "no enforcement"

`bypassPermissions` removes the *prompts*, not the *guardrails*. The
`vrg-*` wrappers, the deny rules, and the hook guard all still apply —
a hook hard-block (exit code 2) overrides even bypass mode — and the
server-side GitHub App permission shapes remain the ultimate boundary
(see [The Actual Security Boundary](#the-actual-security-boundary)).
Bypass changes the agent's *experience inside the sandbox*; it does not
widen what the agent can do to the outside world.

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
| `branch` | `--force`; `-D` conditional | `-d` allowed; `-D` allowed when upstream is `[gone]` |
| `ls-remote` | — | Read-only |
| `rev-parse` | — | Read-only |
| `annotate` | — | Read-only — line-level authorship |
| `blame` | — | Read-only — line-level authorship |
| `cat-file` | — | Read-only — object type/size/content |
| `cherry` | — | Read-only — commits not yet upstream |
| `count-objects` | — | Read-only — repo object stats |
| `describe` | — | Read-only — nearest reachable tag |
| `diff-files` | — | Read-only — plumbing diff |
| `diff-index` | — | Read-only — plumbing diff |
| `diff-tree` | — | Read-only — plumbing diff |
| `for-each-ref` | — | Read-only — enumerate refs |
| `grep` | — | Read-only — search tracked content |
| `ls-files` | — | Read-only — list tracked files |
| `ls-tree` | — | Read-only — list a tree's contents |
| `merge-base` | — | Read-only — common-ancestor lookup |
| `name-rev` | — | Read-only — symbolic names for commits |
| `reflog` | `expire`, `delete` | Read-only show/exists allowed; mutating sub-ops denied |
| `rev-list` | — | Read-only — list commits |
| `shortlog` | — | Read-only — summarized log |
| `show-branch` | — | Read-only — compare branches |
| `show-ref` | — | Read-only — list refs |
| `var` | — | Read-only — logical git variables |
| `verify-commit` | — | Read-only — signature verification |
| `verify-tag` | — | Read-only — signature verification |
| `whatchanged` | — | Read-only — log with file changes |
| `worktree add` | — | Parallel agent work |
| `worktree list` | — | Read-only |
| `worktree remove` | — | Cleanup after merge |
| `add` | — | Staging files |
| `push` | `--force`, `-f`; `--force-with-lease` conditional | `--force-with-lease` allowed on non-protected branches |
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
| `issue close` | Post-finalization closure (escalates to human credentials) |
| `issue edit` | Updating metadata |
| `issue list` | Read-only |
| `issue comment` | Adding comments |
| `issue transfer` | Intra-org issue move (finite-epic relocation) |
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
`vrg-ensure-label`, `vrg-github-repo-config`, `vrg-finalize-pr`)
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
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh"
          }
        ]
      }
    ]
  }
}
```

The project layer allowlists all VRG tools via a single wildcard
and wires the Claude Code hook guard (`guard.sh`) as a `PreToolUse`
hook on every `Bash` tool invocation. The hook guard delegates to
`vrg-hook-guard`, which uses regex matching to block raw `git` and
`gh` commands while allowing `vrg-git`/`vrg-gh` wrappers through.

This configuration applies to everyone who clones the repo — no
per-developer setup needed.

### Global User Settings (`~/.claude/settings.json`)

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  }
}
```

This is the operator's per-host setting. `vrg-vm` copies it into every
identity VM — Lima and off-platform alike — via `copy_claude_config`,
so the agent boots straight into bypass inside the sandbox. The mode is
independent of the deny rules: raw `git`/`gh` are blocked regardless,
because the hook guard hard-blocks them even under bypass.

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
| `git push` | Blocked (hook guard) |
| `curl https://...` | Runs (bypass; contained to the sandbox VM) |
| `rm -rf .` | Runs (bypass; the disposable VM is the containment) |
| `vrg-gh pr merge` | No Claude Code prompt, but wrapper rejects it |

## Defense-in-Depth

### The Vergil Assumption

Every client-side enforcement layer in this model assumes the agent
is cooperative — that it is operating as Vergil, not Mimir. An
agent with file write access (which `bypassPermissions` grants) can
dismantle the entire client-side stack:

1. Delete or edit `.claude/settings.json` — Layer 1 gone
2. Run raw `git` and `gh` — Layer 2 bypassed (the wrappers are
   never invoked)
3. Remove plugin hook configuration — Layer 3 weakened
4. Delete `.claude/hooks/guard.sh` or remove the hooks
   wiring from `.claude/settings.json` — Layer 4 gone

After those four steps, the agent has unrestricted local access.
This is not a theoretical attack — it is trivial for any model
that decides to do it.

**Client-side layers are behavioral constraints, not security
boundaries.** They keep a well-intentioned agent on the rails.
They prevent mistakes, enforce consistency, and provide an audit
trail. They do not stop an adversary.

The two boundaries an agent **cannot** edit its way out of are not
client-side: the **VM sandbox** that contains it locally, and the
**server-side** GitHub App permission shape that bounds what reaches
GitHub.

### The VM Sandbox Boundary

Agents run inside a per-identity Vergil VM — never on the host. This is
what makes `bypassPermissions` acceptable: the agent has full rein
*inside* the VM, but the VM is the wall.

- **Containment.** The agent reaches only what the VM can reach. The
  host filesystem, other identities' VMs, and the operator's wider
  environment are outside the sandbox.
- **Disposability.** The VM is ephemeral and reproducible from its
  declared profile, so a bad outcome inside it (an `rm -rf`, a wedged
  toolchain) is recovered by a rebuild, not a forensic cleanup. On the
  off-platform backend, irreplaceable state lives on a separate
  persistent volume; injected credentials stay on the ephemeral boot
  disk and die with the VM.
- **Credential bounding.** Even with full local control, the agent can
  only reach GitHub through its injected App token — which the
  server-side boundary below constrains regardless of what happens in
  the sandbox.

The sandbox is why VERGIL runs `bypassPermissions` rather than chasing
a per-command allowlist: move the boundary to the edge of a disposable
VM, and the prompts inside it stop being the thing that protects you.

### The Actual Security Boundary

At the GitHub layer, the enforcement an agent cannot edit its way out
of is **server-side**: the GitHub App's installation permission shape
and branch protection rulesets.

- Each agent is a GitHub App whose installation token is bounded by
  its declared permission shape. The user App holds
  `pull_requests: read`, so it physically cannot open, approve, or
  merge a PR; the audit App holds `contents: read`, so it cannot
  write code or merge (merging through the API requires
  `contents: write`). Neither App holds Workflows access, so neither
  can push under `.github/workflows/`. GitHub rejects the API call
  regardless of what happens locally.
- Branch protection rulesets require review approval from a
  different identity before merging. No client-side manipulation
  changes this.
- An App can only act on accounts it is installed on and repos that
  installation covers.

This and the VM sandbox are the real security model. Everything else
is convenience.

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

**Layer 4 — Claude Code Hook Guard (`.claude/hooks/guard.sh`).**
The innermost layer. A `PreToolUse` hook wired in
`.claude/settings.json` that delegates to `vrg-hook-guard`,
blocking all raw `git` and `gh` commands via regex matching.
With layers 1-3 in place, agents cannot reach this layer
through normal operation.

### Layer Interaction Matrix

| Operation | L1 Permissions | L2 Wrapper | L3 Plugin Hook | L4 Hook Guard | Server-Side |
|---|---|---|---|---|---|
| `vrg-commit` | allowed | n/a | n/a | allows | push accepted |
| `vrg-git push` | allowed | validates: no `--force` | n/a | n/a | push accepted |
| `git push` | denied | n/a | blocked | n/a | push accepted (if layers bypassed) |
| `vrg-gh pr merge` | allowed | rejected | blocked | n/a | **API rejected** (no merge permission) |
| `gh pr create` | denied | n/a | blocked | n/a | PR created (if layers bypassed) |
| `gh pr merge` (raw) | denied | n/a | blocked | n/a | **API rejected** (no merge permission) |
| `rm -rf .` | prompts human | n/a | n/a | n/a | n/a |
| `vrg-git reset --hard` | allowed | rejected | n/a | n/a | n/a |

The rightmost column is the only one that holds against Mimir.

## History: the `acceptEdits` direction, and why it was dropped

The original plan phased *toward* a tight `acceptEdits` + allowlist
model — `acceptEdits` as the default mode, then progressively shrinking
the bash allowlist until `Bash(vrg *)` was the only allowed pattern and
everything else required human approval.

**That direction was not adopted.** In practice, prompting on every
non-allowlisted shell command made the agent unusable for the
infrastructure work VERGIL actually does, where running many varied
commands *is* the job. The allowlist could never keep up, and the
operator spent sessions approving commands instead of getting work
done.

VERGIL resolved this by changing *where* the boundary sits rather than
*how tight* the allowlist is: the agent runs in `bypassPermissions`
inside a disposable, sandboxed VM (see
[Base Permission Mode](#base-permission-mode) and
[The VM Sandbox Boundary](#the-vm-sandbox-boundary)). The `vrg-*`
wrappers, deny rules, and hooks remain — as consistency and
mistake-prevention guardrails, and as hard blocks on raw `git`/`gh`
that hold even under bypass — but they are no longer what makes
autonomy *safe*. The VM sandbox and the server-side App permissions
are.

The earlier `acceptEdits` design spec
(`docs/specs/2026-05-14-permission-model-design.md`) is retained as
historical context; it does not describe the model in use.

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
