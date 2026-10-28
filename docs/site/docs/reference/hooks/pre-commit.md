# Claude Code Hook Guard

**Path:** `.claude/hooks/guard.sh`

The hook guard is a Claude Code `PreToolUse` hook that blocks raw
`git` and `gh` commands in agent sessions. It delegates to
`vrg-hook-guard` (when vergil-tooling is installed) or falls back
to a `jq`-based hard deny.

## Malformed input fails closed

Inside a repository with a `vergil.toml`, the guard refuses any tool
call it cannot check instead of letting it through. That covers
unreadable or empty stdin, input that is not valid JSON or is not a
JSON object, and a `Bash` payload with no `tool_input.command`
string. The guard exits with code 2, the `PreToolUse` blocking exit
code, so Claude Code refuses the call and shows the agent a stderr
message that names the problem:

```text
vrg-hook-guard: blocked: the hook input is not valid JSON (...). The guard cannot check this call for raw git/gh, so it fails closed and refuses it. ...
```

A raw `git` or `gh` command in well-formed input is denied as before,
with a JSON `permissionDecision: "deny"` on stdout and exit 0.
Behaviour on well-formed input is unchanged. That includes an empty
command string, which is allowed, and a payload for a tool other than
`Bash`, which passes through. Outside a managed repository the guard
still allows everything, malformed input included. When the input
names no usable `cwd`, the managed-repo check uses the hook process's
own working directory.

The shim's `jq` fallback behaves the same way. It also refuses every
call, with exit code 2, when neither `vrg-hook-guard` nor `jq` is
installed.

The five branch/context checks below live in `vrg-commit` itself and
run before `git commit` is invoked:

## Checks

`vrg-commit` runs five checks in order:

### 1. Detached HEAD

Commits on a detached HEAD are blocked unconditionally.

### 2. Protected Branch

Direct commits to `develop`, `release`, and `main` are forbidden.

### 3. Branch Prefix Validation

Allowed prefixes depend on `[project].branching-model` in
`vergil.toml`:

| Branching Model | Allowed Prefixes |
| --------------- | ---------------- |
| `docs-single-branch` | `feature/*`, `bugfix/*`, `chore/*` |
| `application-promotion` | `feature`, `bugfix`, `hotfix`, `chore`, `promotion` |
| `library-release` | `feature`, `bugfix`, `hotfix`, `chore`, `release` |

An unrecognized branching model is an error. Without a `vergil.toml`,
`vrg-commit` warns and falls back to `feature/*`, `bugfix/*` and
`chore/*`.

### 4. Issue Number Naming

Work branches (`feature/*`, `bugfix/*`, `hotfix/*`, `chore/*`) must
include a repository issue number:

```text
{type}/{issue}-{description}
```

**Example:** `feature/42-add-caching`

`release/*` and `promotion/*` branches are exempt from this check
because they are created by automated workflows.

### 5. Main-worktree commits

When the repository has a `.worktrees/` directory (the worktree
convention), commits on a work branch from the main worktree are
forbidden. Make them from a worktree under `.worktrees/<name>/`.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All checks passed |
| 1 | Validation failure |

## Configuration

The hook guard is wired via `.claude/settings.json` and requires
no per-clone setup. `vrg-commit` reads the branching model from
`[project].branching-model` in `vergil.toml`. No other configuration
is required.
