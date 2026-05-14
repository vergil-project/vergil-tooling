# Claude Code Permission Model Design

**Issue:** #754
**Date:** 2026-05-14
**Status:** Draft

## Problem

The VERGIL project currently runs Claude Code in `bypassPermissions`
mode globally. All operations are auto-approved with no prompts. The
only enforcement layer is the vergil plugin's hook system, which
blocks specific known-dangerous patterns (raw `git commit`, raw
`gh pr create`, agent merges, etc.) but operates as a blocklist —
chasing individual dangerous commands rather than constraining the
agent to a known-safe set of operations.

This is insufficient for an org that aims to accept external
contributors using AI agents. The permission model must:

- Constrain agents to a validated set of operations by default
- Channel all system interaction through VRG wrapper tools that
  validate, constrain, and log what happens
- Prevent agents from using raw shell primitives to work around
  restrictions (pipes, redirection, subshells, command chaining)
- Provide defense-in-depth through multiple independent enforcement
  layers
- Maintain development velocity — the constraints must not make
  agents unusable for real work
- Support a phased rollout that tightens over time as more
  operations are mechanized

## Strategic Goal

**The agent never interacts with the system through raw shell.**
Every operation goes through a VRG wrapper that validates,
constrains, and logs what happens. The permission allowlist consists
exclusively of `vrg-*` commands. If an agent needs something
outside the allowlist, it requires human approval.

This is a forcing function for tooling development: if agents keep
getting prompted for the same operation, that is a signal to build
a VRG tool for it. The allowlist shrinks the surface area of what
agents can do, and every exception is a candidate for
mechanization.

## Phasing

### Phase 1 (this spec)

- Switch from `bypassPermissions` to `acceptEdits` as the default
  mode
- Build `vrg-git` and `vrg-gh` wrappers with subcommand allowlists
  and flag validation
- Allowlist all `vrg-*` commands in Claude Code permissions
- Temporarily allowlist a small set of read-only bash commands with
  documented intent to revisit
- Deny list for raw `git` and `gh` at the global level
- Hooks remain as the innermost enforcement layer
- Everything not allowlisted prompts the human

### Phase 2 (future)

- Evaluate whether read-only bash exceptions can be replaced by
  `vrg-search`, Explore subagent patterns, or Claude Code's native
  `Read` tool
- Build the unified `vrg` CLI with subcommands (replacing the
  current `vrg-*` prefix pattern)
- Tighten the allowlist as mechanization covers more operations
- Each prompt-on-unknown that occurs frequently in practice is a
  signal to build a new VRG tool

### Phase 3 (future)

- The ideal end state: `Bash(vrg *)` is the only allowlisted
  pattern. All operations flow through one command with validated
  subcommands. No raw shell access without human approval.

## Section 1: Base Permission Mode

**`acceptEdits`** is the default mode. The agent freely reads and
modifies code files — that is its primary job. The vergil plugin
hooks already enforce worktree write restrictions and block writes
to the main worktree. Gating every file edit with a prompt would
make development unusable.

The real risk surface is shell commands, not file edits.
`acceptEdits` auto-approves:

- File reads (Read tool)
- File edits (Edit tool)
- File writes (Write tool)
- Common filesystem commands (`mkdir`, `touch`, `mv`, `cp`)

Shell commands not in the allowlist still prompt the human. This
is the escalation mechanism: the agent encounters something it
cannot do, explains the situation, and the human approves or
denies in the moment. When the human is actively debugging with
the agent, they approve exploratory commands as they come. When
the agent is working autonomously, it blocks and waits.

## Section 2: `vrg-git` Wrapper

A Python CLI tool (consistent with the rest of vergil-tooling)
that validates subcommands and flags before executing `git`. The
agent never calls `git` directly — the permission model denies raw
`git` and the hooks block it as a backstop.

### Architecture

The wrapper receives arguments as a Python argv list, not a shell
string. No shell expansion, no pipes, no redirection. It validates
against the allowlist, then execs `git` with the validated
arguments via `subprocess.run` with `shell=False`.

### Subcommand Allowlist

| Subcommand | Allowed | Denied flags | Notes |
|---|---|---|---|
| `status` | yes | — | Read-only |
| `log` | yes | — | Read-only |
| `diff` | yes | — | Read-only |
| `show` | yes | — | Read-only |
| `branch` | yes | `-D`, `--force` | `-d` (safe delete) allowed |
| `ls-remote` | yes | — | Read-only |
| `rev-parse` | yes | — | Read-only |
| `worktree add` | yes | — | Parallel agent work |
| `worktree list` | yes | — | Read-only |
| `worktree remove` | yes | — | Cleanup after merge |
| `add` | yes | — | Staging files |
| `push` | yes | `--force`, `--force-with-lease`, `-f` | Normal push allowed; force push denied |
| `fetch` | yes | — | Read-only |
| `pull` | yes | — | Fast-forward branch updates |
| `checkout` | yes | `-- .`, `-- *` (restore patterns) | Branch switching allowed; broad file restoration denied. Specific-file restore (`-- path/to/file`) is allowed — the agent already has equivalent capability through the Write tool, and worktree isolation is the real safety net. |
| `switch` | yes | — | Branch switching |
| `stash` | yes | — | Temporary state management. All subcommands (`drop`, `clear` included) are allowed — stash is an out-of-band recovery mechanism, not a normal workflow operation. The real fix is the tooling that prevents the mistakes that lead to stash usage; in the recovery context, discarding stashed state is legitimate. |
| `merge` | yes | — | Branch updates |
| `cherry-pick` | yes | — | Selective commit application |
| `rebase` | yes | `-i`, `--interactive` | Non-interactive rebase allowed; interactive denied |

### Denied Subcommands

Anything not on the allowlist is rejected by default. Notable
denials:

- `commit` — all commits flow through `vrg-commit`, which sets
  `VRG_COMMIT_CONTEXT=1` and enforces branch prefix, issue number,
  and worktree convention checks
- `reset` — too dangerous in all forms
- `clean` — file deletion
- `config` — modifying git configuration
- `remote` — modifying remote configuration
- `reflog` — not needed for normal development
- `gc`, `prune` — maintenance operations
- `filter-branch`, `replace` — history rewriting

### Flag Validation

For subcommands with dangerous flag variants, the wrapper scans
the remaining arguments against a deny list of flags. This is
pattern matching on known-dangerous flags, not a complete git
argument parser.

### Error Behavior

When a subcommand or flag is denied, the wrapper exits with a
clear error message explaining what was blocked and why.

### Escape Hatch

None in the wrapper itself. If the agent genuinely needs a denied
operation, it explains the situation to the human, who can run the
raw `git` command themselves via `! git <command>` in the Claude
Code prompt.

## Section 3: `vrg-gh` Wrapper

Same architecture as `vrg-git` — Python CLI, argv list,
`subprocess.run` with `shell=False`, no shell expansion.

The `gh` CLI has a deep subcommand tree. The wrapper validates the
top-level subcommand and the second-level subcommand as a pair.

### Subcommand Allowlist

| Subcommand | Allowed | Denied flags | Notes |
|---|---|---|---|
| `issue view` | yes | — | Read-only |
| `issue create` | yes | — | Issue tracking |
| `issue close` | yes | — | Post-finalization closure |
| `issue edit` | yes | — | Updating metadata |
| `issue list` | yes | — | Read-only |
| `issue comment` | yes | — | Adding comments |
| `pr view` | yes | — | Read-only |
| `pr checks` | yes | — | Read-only, CI status |
| `pr list` | yes | — | Read-only |
| `pr diff` | yes | — | Read-only |
| `pr comment` | yes | — | Review context |
| `pr edit` | yes | — | Updating metadata |
| `pr merge` | denied | — | Agents do not merge |
| `pr review --approve` | denied | — | Agents do not approve |
| `pr close` | denied | — | Agents do not close PRs |
| `pr create` | denied | — | Use `vrg-submit-pr` |
| `run list` | yes | — | Read-only, CI status |
| `run view` | yes | — | Read-only, CI status |
| `run watch` | yes | — | Blocking wait for CI |
| `repo view` | yes | — | Read-only |
| `repo edit` | denied | — | Admin operation |
| `repo create` | denied | — | Admin operation |
| `repo delete` | denied | — | Destructive |
| `api` | denied | — | Raw API escape hatch |
| `auth` | denied | — | Credential management |
| `label list` | yes | — | Read-only |
| `label create` | yes | — | Used by `vrg-ensure-label` |

### The `gh api` Denial

The `gh api` subcommand can perform any GitHub API operation —
create repos, delete branches, modify settings, push code via the
Contents API. The existing `block-github-contents-api` hook only
catches writes to the Contents API specifically. Denying `gh api`
entirely in the wrapper closes the whole escape hatch.

### VRG Tools That Bypass the Wrapper

Existing VRG tools that call `gh` directly in their Python code
(`vrg-submit-pr`, `vrg-merge-when-green`, `vrg-wait-until-green`,
`vrg-ensure-label`, `vrg-github-config`, `vrg-finalize-repo`)
bypass the wrapper because they are already mechanized. The
wrapper only constrains agent-initiated `gh` calls via the
`Bash` tool.

## Section 4: Permission Configuration

Three layers of settings that work together.

### Global User Settings (`~/.claude/settings.json`)

```json
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "deny": [
      "Bash(git *)",
      "Bash(gh *)"
    ]
  }
}
```

The global layer sets the default mode and hard-denies raw `git`
and `gh` across all projects. Deny rules at any scope cannot be
overridden by allow rules at a lower scope — this is Claude Code's
precedence model.

**Implementation risk:** The `Bash(git *)` deny pattern must be
verified to not match `vrg-git *` via substring matching. Claude
Code's documentation states that bash rules match on the command
name (the first word of each command segment), so `git` and
`vrg-git` should be distinct. This must be verified during
implementation before the deny rules go live. If the matching is
substring-based, the deny rules move to the hook layer instead.

### Project Settings (`.claude/settings.json`)

```json
{
  "permissions": {
    "allow": [
      "Bash(vrg-*)"
    ]
  }
}
```

The project layer allowlists all VRG tools via a single wildcard.
`vrg-commit`, `vrg-submit-pr`, `vrg-validate`, `vrg-docker-run`,
`vrg-git`, `vrg-gh`, and any future `vrg-*` tools run without
prompting. No per-tool entries are needed.

### Project Local Settings (`.claude/settings.local.json`)

Gitignored. Per-developer. This is where the temporary read-only
bash exceptions live during phase 1:

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

These are documented as phase 2 candidates for mechanization or
removal. Each developer can adjust based on their comfort level.
When alternatives are found (Explore subagent, `vrg-search`,
native `Read` tool), this list shrinks without changing committed
project settings.

### What This Means in Practice

- `vrg-commit` → no prompt (project allow)
- `vrg-git status` → no prompt (project allow)
- `grep -r "foo" src/` → no prompt (local allow, phase 1)
- `git push` → denied (global deny)
- `curl https://...` → prompts the human
- `rm -rf .` → prompts the human
- `vrg-gh pr merge` → no Claude Code prompt, but the wrapper
  rejects it

## Section 5: Defense-in-Depth Model

Four enforcement layers, each catching what the layer above misses.

### Layer 1 — Claude Code Permissions (outermost)

The allowlist/deny configuration from Section 4. If a command is
not allowlisted and not denied, it prompts the human. Denied
commands are blocked without prompt.

### Layer 2 — VRG Wrappers (`vrg-git`, `vrg-gh`)

Validate subcommands and flags before executing the underlying
tool. Even if an agent gets `vrg-git reset --hard` through the
permission layer (it is allowlisted as `vrg-git *`), the wrapper
rejects it.

### Layer 3 — Vergil Plugin Hooks

The existing 12 hooks remain as a backstop. They catch patterns
that slip through layers 1 and 2. Hooks exit code 2 (hard block)
which overrides even bypass mode.

### Layer 4 — Git-Level Hooks (`.githooks/pre-commit`)

The innermost layer. The pre-commit hook rejects raw `git commit`
without `VRG_COMMIT_CONTEXT=1`. With layers 1-3 in place, agents
cannot reach this layer through normal operation — it exists as a
final safety net.

**Known weakness:** The `VRG_COMMIT_CONTEXT=1` environment
variable is a shared secret between `vrg-commit` and the
pre-commit hook. An agent that knows the variable can set it to
bypass the hook. With layers 1-3 enforcing the `git` deny and
the `vrg-git` wrapper rejecting `commit` as a subcommand, this
weakness is mitigated — the agent cannot reach `git commit`
regardless of what environment variables it sets. This is
documented as a known weakness in layer 4 but not addressed
because layers 1-3 close the hole.

### Layer Interaction Matrix

| Operation | L1 Permissions | L2 Wrapper | L3 Plugin Hook | L4 Git Hook |
|---|---|---|---|---|
| `vrg-commit` | allowed | n/a | n/a | admits |
| `vrg-git push` | allowed | validates: no --force | n/a | n/a |
| `git push` | denied | n/a | blocked | n/a |
| `vrg-gh pr merge` | allowed | rejected | blocked | n/a |
| `gh pr create` | denied | n/a | blocked | n/a |
| `rm -rf .` | prompts human | n/a | n/a | n/a |
| `vrg-git reset --hard` | allowed | rejected | n/a | n/a |

Redundancy is intentional. No single layer is trusted.

### Logging and Auditing

The `vrg-git` and `vrg-gh` wrappers log every invocation (command,
arguments, allowed/denied, timestamp) to a local log file. This
provides an audit trail and feeds back into phase 2 planning:

- Commands that agents are prompted for frequently are candidates
  for new VRG tools
- Commands that humans routinely approve are candidates for the
  allowlist
- Commands that humans routinely deny validate the deny list

## Section 6: Migration Path

### Rollout Order

1. Build and ship `vrg-git` and `vrg-gh` wrappers in
   vergil-tooling
2. Update vergil plugin hooks to redirect agents to the wrappers
   (existing `block-raw-git-commit` and `block-raw-gh-pr-create`
   hooks evolve their error messages)
3. Update CLAUDE.md across all repos to instruct agents to use
   `vrg-git` and `vrg-gh` instead of raw commands
4. Deploy `.claude/settings.json` to each consuming repo with the
   project-level `Bash(vrg-*)` allowlist. Provide a template
   `settings.local.json` in documentation for the phase 1
   read-only exceptions.
5. Deploy the permission configuration (global deny, project allow,
   local exceptions) for vergil-tooling itself
6. Switch `defaultMode` from `bypassPermissions` to `acceptEdits`
7. Run for a week, collect data on what prompts appear, and refine

### Hook Evolution

Existing hooks after migration:

| Hook | Status |
|---|---|
| `block-raw-git-commit` | Backstop — L1 is primary gate |
| `block-raw-gh-pr-create` | Backstop — L1 is primary gate |
| `block-agent-merge` | Backstop — L2 wrapper is primary gate |
| `block-github-contents-api` | Backstop — `gh api` denied by L2 |
| `block-autoclose-linkage` | Still primary — validates `vrg-submit-pr` args |
| `block-heredoc` | Still primary — bash portability |
| `block-associative-arrays` | Still primary — bash portability |
| `enforce-host-container-split` | Still primary — routing concern |
| `block-protected-branch-work` | Still primary — worktree convention |
| `block-worktree-bypass-write` | Still primary — Write/Edit gating |
| `detect-deprecation-warnings` | Still primary — observability |
| `remind-finalize` | Still primary — operational reminder |

No hooks are removed. Hooks that become backstops get a comment
noting that the permission layer is now the primary enforcement.

### CLAUDE.md Updates

Every repo's CLAUDE.md is updated to instruct agents:

- Use `vrg-git` instead of `git` for all git operations
- Use `vrg-gh` instead of `gh` for all GitHub CLI operations
- These are not optional preferences — raw `git` and `gh` are
  denied by the permission model

### Feedback Loop

Wrapper logging plus prompt data from sessions feeds into phase 2
planning. After a month of operation:

- Which read-only bash commands are agents using most? Candidates
  for `vrg-search` or similar tools.
- Which wrapper subcommands are agents hitting most? Validates the
  allowlist design.
- Which prompts are humans approving routinely? Candidates for the
  allowlist.
- Which prompts are humans denying? Validates the deny list.

## Dependencies

- Org governance spec (#717) — the credential isolation model
  (agent PAT vs human PAT) is the GitHub-level complement to this
  harness-level permission model
- `.github` profile repo (#753) — CONTRIBUTING.md will reference
  the permission model as part of the contributor experience
- vergil-tooling — the wrappers are new CLI tools in this package

## Risks

| Risk | Mitigation |
|---|---|
| `Bash(git *)` deny pattern matches `vrg-git *` via substring | Verify Claude Code's matching behavior before deploying deny rules; fall back to hook-only enforcement if matching is substring-based |
| Switching from bypass mode surfaces unexpected prompt volume | Run for a week after migration, refine allowlist based on data |
| Wrapper allowlists are too restrictive for some workflows | The human can run raw commands via `! <command>` in the prompt; each case is data for allowlist refinement |
| Agents ignore CLAUDE.md instructions to use `vrg-git`/`vrg-gh` | Permission deny rules enforce compliance mechanically — instructions are guidance, the deny is the gate |
| Read-only bash exceptions (phase 1) used for command chaining attacks (`grep foo; dangerous-cmd`) | Claude Code's documented behavior splits compound commands on shell operators and evaluates each subcommand independently — deny rules on `git`/`gh` should fire against embedded dangerous commands. However, there is a known implementation gap (anthropics/claude-code#28784) where prefix matching on the full command string can produce unexpected approvals. Primary mitigation: the `Bash(git *)` and `Bash(gh *)` deny rules cover the highest-risk commands regardless. Fallback: a PreToolUse hook that rejects shell operators in read-only-allowlisted commands can be added if the implementation gap is not resolved before phase 1 deploys. Phase 2 evaluates replacing read-only exceptions entirely. |
| `VRG_COMMIT_CONTEXT` env var used to bypass pre-commit hook | Layers 1-3 prevent the agent from reaching `git commit`; layer 4 weakness is documented but mitigated |
