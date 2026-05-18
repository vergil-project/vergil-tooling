# Co-Author Auto-Discovery Design

**Issue:** #725
**Date:** 2026-05-18
**Status:** Draft

## Problem

`[project.co-authors]` in `vergil.toml` hardcodes a single agent
identity (GitHub user ID + noreply email) at the repository level.
This blocks multi-contributor workflows — each developer has their
own AI agent account, and a repo-level config can only represent one.
Every consuming repo must carry this mapping, creating per-repo
maintenance for something that belongs to the developer, not the
repository.

## Solution

Replace the static `[project.co-authors]` config with dynamic
resolution at commit time. The agent account is discovered from
`gh auth status` (existing convention), and the GitHub API provides
the numeric user ID needed to construct the noreply email for the
`Co-Authored-By` trailer.

## Design

### Identity Resolution

New function in `src/vergil_tooling/lib/github.py`:

```
resolve_co_author_trailer() -> str
```

Steps:

1. Call `_discover_accounts()` to get the agent account name
   (e.g., `wphillipmoore-vergil`). This function already exists in
   `lib/github.py` and parses `gh auth status` for the single
   `-vergil` suffixed account.
2. Call `gh api /users/<agent>` via the existing `read_json()` helper
   to fetch the agent's GitHub profile.
3. Extract the `id` field (numeric GitHub user ID).
4. Construct and return:
   `Co-Authored-By: <agent> <<id>+<agent>@users.noreply.github.com>`

Error handling: `_discover_accounts()` already hard-errors if zero or
multiple `-vergil` accounts are found. `read_json()` already retries
transient GitHub API errors (502/503/504/429) with exponential
backoff. Persistent API failures surface as `GitHubAPIError`.

### Changes to `vrg-commit`

**Remove config-based co-author lookup.** The block that reads
`args.agent` from `st_config.project.co_authors` (lines 192-198 of
`vrg_commit.py`) is replaced with a single call to
`resolve_co_author_trailer()`.

**Deprecate `--agent` flag.** The flag remains in the argparse
definition but becomes optional with no default. When passed,
`vrg-commit` prints a deprecation warning to stderr and ignores the
value:

```
WARNING: --agent is deprecated and will be removed in a future
release. Co-author identity is now auto-discovered from
gh auth status.
```

The flag will be removed in a subsequent release once no consuming
scripts or agent sessions pass it.

**Commit flow after the change:**

1. Parse args (`--agent` accepted but ignored with warning).
2. Read `vergil.toml` for branching model and other config.
3. Validate commit context (branch checks, detached HEAD, etc.).
4. Call `resolve_co_author_trailer()` — discovers agent, queries API,
   returns trailer string.
5. Build commit message with trailer appended.
6. Execute `git commit --file`.

### Changes to `vergil.toml` and `config.py`

**`vergil.toml`:** Delete the `[project.co-authors]` section.

**`src/vergil_tooling/lib/config.py`:**

- Delete the `_COAUTHOR_RE` regex.
- Remove the `co_authors` field from `ProjectConfig`.
- Delete the co-author validation block in `_parse_raw_config()`.
- Remove the `co_authors` kwarg from the `ProjectConfig` constructor
  call.

Leftover `[project.co-authors]` sections in consuming repos are
harmless — the parser will ignore unknown keys under `[project]`.
This avoids coordination pressure during rollout.

### Deduplicate `_discover_accounts()`

`vrg_gh.py` has a duplicate of `_discover_accounts()` that
predates the `lib/github.py` version. The duplicate is removed and
`vrg_gh.py` imports from the lib.

### Test Changes

**`test_config.py`:**

- Remove co-author entries from TOML test fixtures.
- Remove `test_read_config_malformed_co_author`.
- Update `test_read_config_valid` to not assert on `co_authors`.
- Add a test confirming that a `[project.co-authors]` section in
  TOML is silently ignored (backward compatibility).

**`test_vrg_commit.py`:**

- Remove co-author entries from TOML test fixtures.
- Update tests that assert on agent resolution behavior to mock
  `resolve_co_author_trailer()` instead.
- Add a test: passing `--agent` prints deprecation warning and
  succeeds.
- Add a test: omitting `--agent` succeeds (auto-discovery path).

**`test_github.py` (new tests):**

- Test `resolve_co_author_trailer()` with a mocked `gh api` response
  returning a known numeric ID.
- Test that `_discover_accounts()` failure propagates cleanly.
- Test that a `gh api` failure (non-transient) raises
  `GitHubAPIError`.

### Cross-Repo Rollout

1. Land the vergil-tooling PR.
2. Publish a new vergil-tooling release.
3. Clean up consuming repos' `vergil.toml` files at their own pace —
   leftover `[project.co-authors]` sections are inert.

## Rejected Alternatives

### Convention-Based Derivation (No API Call)

Construct the noreply email without the numeric ID prefix. GitHub
requires the numeric ID for reliable commit attribution — without it,
commits may not link to the agent account. Attribution is the purpose
of the trailer, so this defeats the goal.

### API Call + Local Cache

Cache the resolved trailer in `~/.config/vergil/co-authors.toml`
after the first API call. Adds cache file management, cache
invalidation logic, and extra code paths. The network dependency
already exists (`gh auth` is required for commits), so offline
capability provides no practical benefit. The complexity is not
justified.

### Per-User Config File

Store agent identity in `~/.config/vergil/agents.toml` or similar.
Requires manual setup per developer, config file maintenance, and
diverges from the convention-based approach the tooling already uses
for account discovery. Adds configuration surface area that
auto-discovery eliminates entirely.
