# Replace Git Hook with Claude Code Hook

**Date:** 2026-05-25
**Status:** Draft
**Issue:** #1135, #724

## Motivation

The `.githooks/pre-commit` hook that blocks raw `git commit` is
ineffective against AI agents:

1. **Manual setup per clone.** Requires `git config core.hooksPath
   .githooks` after every clone. Forgetting this step leaves the hook
   inactive with no visible warning.

2. **Trivially bypassable gate.** The hook checks for
   `VRG_COMMIT_CONTEXT=1`, an env var that `vrg-commit` sets before
   invoking `git commit`. Agents read the hook source, discover the
   variable, and set it themselves — defeating the gate entirely.

3. **Deny rules are whack-a-mole.** The `.claude/settings.json` deny
   rules (`Bash(git *)`, `Bash(gh *)`) use prefix matching. Agents
   evade them with env-var prefixes (`VRG_COMMIT_CONTEXT=1 git
   commit`), command chaining (`cd /path && git commit`), subshells
   (`bash -c "git commit"`), and other shell constructs. Every
   evasion pattern requires a new rule, and the shell is too
   expressive to enumerate.

The git hook contributes no validation of its own. All real commit
validation lives in `vrg-commit`: protected branch checks, branch
naming, issue numbers, worktree convention, conventional commit
format, co-author trailers, and auto-close keyword rejection. The
hook is a routing mechanism that checks whether the caller set an env
var — nothing more.

For human developers (who can be trusted to use `vrg-commit`
voluntarily), the hook is a minor convenience reminder. For AI agents
(the primary enforcement concern), it is a bypassable obstacle that
creates a false sense of security.

## Decision

Replace the git hook, deny rules, and command-specific plugin hooks
with a Claude Code `PreToolUse` hook backed by a shared
`vrg-hook-guard` console script. The tool enforces the wrapper
routing policy: all `git` operations go through `vrg-git`, all `gh`
operations go through `vrg-gh`.

## Design

### Architecture: two layers, one tool

```text
                    vrg-hook-guard
                    (Python, vergil-tooling)
                    Installed once via uv tool install.
                    On PATH. Reads hook JSON from stdin,
                    blocks raw git/gh commands, directs
                    to vrg-git/vrg-gh.
                             |
                  exec vrg-hook-guard
                             |
                +------------+------------+
                |                         |
          Plugin layer              Per-repo layer
          hooks/hooks.json          .claude/settings.json
          -> wrapper shim           -> wrapper shim
          Per-developer             Per-clone
          (plugin install)          (checked into repo)
```

Both layers call the same tool through an identical thin shell shim.
If both are active, both fire in parallel; most-restrictive decision
wins (deny beats allow). No conflict.

### Component 1: `vrg-hook-guard`

A new console script entry point in vergil-tooling, registered in
`pyproject.toml` under `[project.scripts]`.

**Interface:**

- **Input:** Claude Code hook JSON on stdin. Required fields:
  `tool_input.command` and `cwd`.
- **Output:** JSON with `permissionDecision: "deny"` on stdout if the
  command is blocked; silent `exit 0` if allowed.
- **Install:** Ships with vergil-tooling, on PATH via `uv tool
  install`.

**Behavior:**

1. Read hook JSON from stdin.
2. Extract `cwd` and check for `vergil.toml` (managed-repo gating).
   Exit 0 silently if unmanaged.
3. Extract `tool_input.command`.
4. Check for raw `git` or `gh` invocations using a regex that
   covers known evasion patterns:

| Pattern              | Example                                      |
|----------------------|----------------------------------------------|
| Direct               | `git commit -m "..."`                        |
| Chained              | `cd /path && git push`                       |
| Piped / sequenced    | `git add . ; git commit`                     |
| Env var prefix       | `VRG_COMMIT_CONTEXT=1 git commit`            |
| Command wrapper      | `env git commit`, `command gh pr create`     |
| Subshell             | `bash -c "git commit"`, `sh -c "gh pr merge"`|
| Parenthesized        | `(git reset --hard)`                         |
| Backtick / `$()`     | `` `git commit` ``, `$(gh api ...)`          |

5. **Matching rule:** Match `git` and `gh` as standalone commands,
   not as substrings. `vrg-git` and `vrg-gh` must NOT match. The
   regex must use a negative lookbehind or equivalent to ensure
   that a preceding `vrg-` prefix excludes the match (e.g.,
   `(?<!vrg-)git\b` or a two-step check: match `git`, then verify
   the preceding context is not `vrg-`).
6. If matched: output deny JSON. For `git` commands, direct to
   `vrg-git`. For `gh` commands, direct to `vrg-gh`.
7. If not matched: exit 0.

The regex does not need to be perfect — it is a guardrail, not a
security boundary. The goal is to catch the patterns that agents
actually produce, not to formally parse shell syntax.

### Component 2: shell shim

A thin shell script, identical in both the plugin and each consuming
repo. Two responsibilities:

1. If `vrg-hook-guard` is on PATH: `exec` it (stdin passes through).
2. If `vrg-hook-guard` is not on PATH: parse the command from stdin
   using `jq`, and hard-deny only if it contains a raw `git` or `gh`
   invocation. Non-git/gh commands pass through — the agent is not
   paralyzed, but git/gh operations are blocked until the environment
   is fixed.

```bash
#!/usr/bin/env bash
set -euo pipefail
if command -v vrg-hook-guard &>/dev/null; then
  exec vrg-hook-guard
fi

input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // empty')

if echo "$command" | grep -qE '(^|[^-])\bgit\b'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "vergil-tooling is not available. This repository requires a correctly configured environment — all git/gh operations are blocked until resolved."
    }
  }'
  exit 0
fi

if echo "$command" | grep -qE '(^|[^-])\bgh\b'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "vergil-tooling is not available. This repository requires a correctly configured environment — all git/gh operations are blocked until resolved."
    }
  }'
  exit 0
fi

exit 0
```

The shim requires `jq` for the fallback path. `jq` is a required
tool in all Vergil VM images and Docker dev images.

**Plugin location:**
`${CLAUDE_PLUGIN_ROOT}/hooks/scripts/guard.sh`

**Per-repo location:** `.claude/hooks/guard.sh`

### Component 3: hook wiring

**Plugin `hooks/hooks.json`** — replace the existing
`block-raw-git-commit.sh` and `block-raw-gh-pr-create.sh` entries
with a single guard entry:

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/guard.sh",
      "statusMessage": "Checking for raw git/gh..."
    }
  ]
}
```

**Per-repo `.claude/settings.json`** — add a hooks section:

```json
{
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

### Removals

| Item | Location | Rationale |
|------|----------|-----------|
| `.githooks/pre-commit` | All Vergil-managed repos | Replaced by Claude Code hook |
| `VRG_COMMIT_CONTEXT=1` | `vrg-commit` source | No consumer remains |
| `git config core.hooksPath .githooks` | Setup docs, CLAUDE.md | No git hooks to configure |
| `Bash(git *)` deny rules | `.claude/settings.json` (all repos) | Replaced by hook |
| `Bash(gh *)` deny rules | `.claude/settings.json` (all repos) | Replaced by hook |
| `block-raw-git-commit.sh` | vergil-claude-plugin | Replaced by `guard.sh` shim |
| `block-raw-gh-pr-create.sh` | vergil-claude-plugin | Replaced by `guard.sh` shim |
| Hook references in docs | `docs/specs/host-level-tool.md`, CLAUDE.md, etc. | Stale after removal |

### Component 4: worktree-convention check in `vrg-git` (#724)

With `vrg-hook-guard` blocking raw `git`, agents are forced through
`vrg-git`. But `vrg-git` currently allows `checkout` and `switch`
without checking the worktree context. During the vergil-docker
v2.0.1 publish, an agent checked out a non-develop branch directly
in the main worktree — the worktree convention was violated before
any commit was attempted.

Add a check to `vrg-git` for `checkout` and `switch` subcommands:

- Detect whether the current working directory is the main worktree
  (not inside `.worktrees/`).
- If in the main worktree and the target branch is not the default
  branch (`develop` or `main`), reject the operation with a message
  directing the agent to use a worktree.
- Allow branch switches inside `.worktrees/` unconditionally — those
  are the agent's assigned workspaces.
- Allow `checkout` of files (e.g., `git checkout -- path`) — this is
  not a branch switch.

This closes the gap where `vrg-hook-guard` blocks the raw-git
evasion path and `vrg-git` blocks the wrapper path.

### What stays unchanged

- `vrg-git` continues to block dangerous subcommands at the wrapper
  level (and now also enforces the worktree convention for
  checkout/switch).
- `vrg-commit` continues to perform all validation checks (protected
  branches, branch naming, issue numbers, worktree convention,
  conventional commit format, co-author trailers, no auto-close
  keywords).
- `Bash(vrg-*)` allow rule stays in `.claude/settings.json`.
- All other plugin hooks (block-protected-branch-work,
  enforce-host-container-split, block-autoclose-linkage, etc.) are
  unaffected.

### Prerequisites

- **`jq`** must be available in all Vergil VM images and Docker dev
  images. It is used by the shim's fallback path when
  `vrg-hook-guard` is not installed. Add `jq` to the base image
  layer in vergil-docker if not already present.

## Scope

### In scope

- New `vrg-hook-guard` console script in vergil-tooling, with
  `pyproject.toml` entry point.
- Shell shim (`guard.sh`) in vergil-claude-plugin and all consuming
  repos.
- Hook wiring in plugin `hooks.json` and per-repo
  `.claude/settings.json`.
- Removal of `.githooks/pre-commit`, `VRG_COMMIT_CONTEXT`, deny
  rules, command-specific plugin hooks, and associated documentation
  from vergil-tooling.
- Worktree-convention check in `vrg-git` for `checkout`/`switch`
  subcommands (#724).

### Out of scope

- Consolidating other plugin hooks (block-protected-branch-work,
  etc.) into `vrg-hook-guard`. Each hook remains independent.
  Consolidation is a future option if the pattern proves out.
- Removing `.githooks/pre-commit` and deny rules from consuming
  repos. That is a separate rollout after vergil-tooling and the
  plugin ship the new mechanism.
- Enforcement outside Claude Code sessions. CI uses
  `vrg-container-run` and its own workflow; other AI tools are not
  in scope.
- Adding `jq` to VM/Docker images. That is a prerequisite tracked
  separately.

## Rollout

Changes span three repositories, deployed in order:

1. **vergil-tooling** — add `vrg-hook-guard` console script; add
   `.claude/hooks/guard.sh` shim and hook wiring; remove
   `.githooks/pre-commit`, `VRG_COMMIT_CONTEXT`, deny rules, and
   update documentation.
2. **vergil-claude-plugin** — replace `block-raw-git-commit.sh` and
   `block-raw-gh-pr-create.sh` with `guard.sh` shim; update
   `hooks.json`.
3. **Consuming repos** — add `.claude/hooks/guard.sh` shim and hook
   wiring in `.claude/settings.json`; remove `.githooks/pre-commit`
   and deny rules.

Step 1 must ship and be installable before steps 2 and 3, since both
depend on `vrg-hook-guard` being on PATH.
