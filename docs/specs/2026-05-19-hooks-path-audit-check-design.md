# Design: Audit check for `core.hooksPath` configuration

**Issue:** [#825](https://github.com/vergil-project/vergil-tooling/issues/825)
**Date:** 2026-05-19
**Scope:** Audit check only. The init-tool requirement (setting
`core.hooksPath` during repo bootstrap) is tracked under
[#807](https://github.com/vergil-project/vergil-tooling/issues/807).

## Problem

The repo audit tool checks whether `.githooks/pre-commit` exists on
disk but has no way to verify that `core.hooksPath` is configured in
local git config. Without that setting, git ignores the hook file
entirely — the pre-commit gate is present but inactive.

Discovered during the-infrastructure-mindset conversion (#823).

## Design

### Where the check lives

Extend `_check_githooks()` in `src/vergil_tooling/lib/repo_config.py`.
The check fires conditionally: only when `.githooks/pre-commit`
exists on disk. If the hook file is missing, that diagnostic is
sufficient — reporting "hooksPath not configured" on top of
"hook file missing" is redundant noise.

### How the check works

After confirming `.githooks/pre-commit` exists, run:

```python
result = subprocess.run(
    ["git", "config", "core.hooksPath"],
    capture_output=True,
    text=True,
    cwd=repo_root,
)
```

The `cwd=repo_root` ensures the query reads the correct repo's
local config, not wherever the calling process happens to be.

Compare `result.stdout.strip()` against `.githooks`. Emit a
`DiffItem` when the value doesn't match:

```python
DiffItem(
    field="local.git_config.hooks_path",
    expected=".githooks",
    actual=<stdout.strip() or "not configured">,
)
```

Failure modes:
- **Key unset** (rc=1, empty stdout): actual = `"not configured"`.
- **Key set to wrong value**: actual = the wrong value.
- **Not a git repo** (rc=128 or similar): treated the same as
  unset. The audit tool runs from CWD which is expected to be a
  git repo, but the check degrades gracefully.
- **`git` not installed** (`FileNotFoundError`): propagates. A
  genuine environment problem for any user of this tooling.

### Field name

`local.git_config.hooks_path` — establishes a `local.git_config.*`
namespace for potential future git config checks without building
any infrastructure for that now.

### What doesn't change

- **CLI layer** (`vrg_github_repo_config.py`): no changes.
  `audit_local_config()` is called the same way.
- **Data model** (`ConfigDiff`, `DiffItem`): unchanged. The
  existing structure handles this naturally.
- **`--repo` skip logic**: already prevents local checks from
  running when CWD doesn't match the target repo.

## Testing

### New tests in `TestGithooks`

1. **`test_hooks_path_not_configured`** — `.githooks/pre-commit`
   exists, no git repo initialized (or key unset). Expects
   `local.git_config.hooks_path` in the diff with
   actual=`"not configured"`.

2. **`test_hooks_path_wrong_value`** — `.githooks/pre-commit`
   exists inside a `git init`'d repo with
   `core.hooksPath` set to `wrong/path`. Expects
   `local.git_config.hooks_path` in the diff with the wrong value.

3. **`test_hooks_path_configured`** — `.githooks/pre-commit` exists
   inside a `git init`'d repo with `core.hooksPath` set to
   `.githooks`. Expects no `local.git_config.hooks_path` in the
   diff.

### Integration test update

`_write_compliant_repo()` must be updated to `git init` the
`tmp_path` and set `core.hooksPath` to `.githooks`. Without this,
the existing `test_compliant_repo` test would fail because the new
check fires on the scaffolded repo.

## Implementation notes

This is the first `subprocess` call in `repo_config.py` — the
module was previously pure filesystem. `subprocess` is stdlib and
the call is a simple read-only query, so this is a minimal change
in character.

Raw `git config` (not `vrg-git`) is correct here: this is a
host-side audit tool reading local config, not an agent-context
operation that needs wrapper enforcement.
