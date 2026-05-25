# Claude Code Hook Guard

**Path:** `.claude/hooks/guard.sh`

The hook guard is a Claude Code `PreToolUse` hook that blocks raw
`git` and `gh` commands in agent sessions. It delegates to
`vrg-hook-guard` (when vergil-tooling is installed) or falls back
to a `jq`-based hard deny.

The five branch/context checks below live in `vrg-commit` itself and
run before `git commit` is invoked:

## Checks

`vrg-commit` runs five checks in order:

### 1. Detached HEAD

Commits on a detached HEAD are blocked unconditionally.

### 2. Protected Branch

Direct commits to `develop`, `release`, and `main` are forbidden.

### 3. Branching Model Detection

Reads `branching_model` from `docs/repository-standards.md`. If not
found, falls back to `feature/*` and `bugfix/*` with a warning.

### 4. Branch Prefix Validation

Allowed prefixes depend on the branching model:

| Branching Model | Allowed Prefixes |
| --------------- | ---------------- |
| `docs-single-branch` | `feature/*`, `bugfix/*`, `chore/*` |
| `application-promotion` | `feature`, `bugfix`, `hotfix`, `chore`, `promo` |
| `library-release` | `feature`, `bugfix`, `hotfix`, `chore`, `release` |

### 5. Issue Number Naming

Work branches (`feature/*`, `bugfix/*`, `hotfix/*`, `chore/*`) must
include a repository issue number:

```text
{type}/{issue}-{description}
```

**Example:** `feature/42-add-caching`

`release/*` and `promotion/*` branches are exempt from this check
because they are created by automated workflows.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All checks passed |
| 1 | Validation failure |

## Configuration

The hook guard is wired via `.claude/settings.json` and requires
no per-clone setup. `vrg-commit` reads `branching_model` from
`docs/repository-standards.md`. No other configuration is required.
