# Claude Code Hook Guard

**Path:** `.claude/hooks/guard.sh`

The hook guard is a Claude Code `PreToolUse` hook that blocks raw
`git` and `gh` commands in agent sessions. It delegates to
`vrg-hook-guard` (when vergil-tooling is installed) or falls back
to a `jq`-based hard deny. In an agent identity, `vrg-hook-guard` also
refuses raw reads of issue and PR content (see
[Raw content reads](#raw-content-reads)).

## Raw content reads

For an agent identity, `vrg-hook-guard` refuses the obvious raw
`vrg-gh` reads of issue and PR content, with a deny decision that names
[`vrg-issue-read`](../cli-tools-overview.md#vrg-issue-read), the trusted
read command that keeps only content the trust layer trusts:

- `--comments` on any `issue` or `pr` command;
- a `--json` that selects `body`, `comments`, `reviews`, `latestReviews`
  or `closingIssuesReferences` on an `issue`, `pr` or
  `search issues|prs` command;
- an `issue view` or `pr view` with no `--json`, whose default output
  prints the body;
- an `api` path with a `comments` or `reviews` segment;
- an `issue list`, `pr list`, `search issues` or `search prs` whose query
  matches text: a free-text term or an `in:` qualifier, in a list's
  `--search` or a search's query, or a search's `--match`.

A list or search made only of qualifiers (labels, states, dates,
authors, `reason:`, `is:`) that requests no content field is a metadata
read and stays allowed, as do reads such as
`vrg-gh issue view <N> --json labels,parent,state` and run and release
listings. Human mode is unchanged: the check applies only when an agent
identity signal is present.

The list is a guard against accidents, not a security boundary: what
keeps untrusted content from driving behaviour is the trust layer
itself. The shim's `jq` fallback, used only when vergil-tooling is not
installed, does not apply this check.

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
