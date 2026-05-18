# Standard Tooling - Agent Instructions

## User Overrides (Optional)

Always apply this repository's `AGENTS.md` as the baseline. If
`~/AGENTS.md` exists and is readable, load it after and apply it as an
additive, user-specific overlay. The user-specific overlay may add
constraints but must not replace or weaken the repository instructions.
If it cannot be read, say so briefly and continue.

## Start Every Work Session: Create a Feature Branch

**Critical first step**: before making any changes in a new session, you
MUST:

1. Check current branch: `git branch --show-current`
2. If on an eternal branch (`develop`, `release`, `main`), create a feature
   branch immediately:

   ```bash
   git checkout -b feature/<descriptive-name>
   ```

3. If already on a short-lived branch, continue work there.

## Working Rules

- This repository is a Python package providing shared tooling scripts.
  Consumers resolve host-side `vrg-*` tools via `uv tool install` and
  in-container tools via the dev container image's pre-bake (non-Python
  repos) or a `[tool.uv.sources]` dev dep (Python repos).
- Test scripts with shellcheck before committing.
- Keep scripts portable across macOS and Linux.
- Do not add repo-specific logic; scripts must work in any consuming repo.
- **Release before consuming**: consuming repos' CI checks out
  vergil-tooling at a pinned tag. You must merge to `main` and tag a
  new version **before** updating consuming repos.

## Shell command policy

**Do NOT use heredocs** (`<<EOF` / `<<'EOF'`) for multi-line arguments to CLI
tools such as `gh`, `git commit`, or `curl`. Heredocs routinely fail due to
shell escaping issues with apostrophes, backticks, and special characters.
Always write multi-line content to a temporary file and pass it via `--body-file`
or `--file` instead.
