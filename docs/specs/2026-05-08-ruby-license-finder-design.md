# Centralize Ruby license_finder audit

**Issue:** [#603](https://github.com/wphillipmoore/standard-tooling/issues/603)
**Date:** 2026-05-08
**Status:** Approved

## Problem

The Ruby AUDIT entry in the command registry only runs `bundle-audit`
(vulnerability scanning). The bespoke CI job in mq-rest-admin-ruby also
runs `license_finder` with a repo-specific decisions file for license
compliance enforcement. Without `license_finder` in the registry,
`st-validate --check audit` for Ruby checks for known vulnerabilities but
not license policy compliance.

## Design

### Decisions file

Ship a static `license_finder` decisions file at
`src/standard_tooling/configs/ruby/license_finder.yml`. This file
contains the five permitted licenses currently enforced by
mq-rest-admin-ruby (the only Ruby consumer to date):

- MIT
- Simplified BSD
- New BSD
- ruby
- GPL-3.0-or-later

Standard-tooling owns this file centrally. Consuming repos do not
maintain their own decisions files.

### Registry entry

Add `license_finder` as a second command in the Ruby AUDIT registry
entry, using a `{configs}` placeholder for the config path:

```python
CheckKind.AUDIT: [
    ["bundle", "exec", "bundle-audit", "check", "--update"],
    ["license_finder", "--decisions-file={configs}/ruby/license_finder.yml"],
],
```

### Placeholder expansion in language_commands()

`language_commands()` gains a post-processing step: after the dict
lookup, it replaces `{configs}` in any command argument with the
resolved `importlib.resources.files("standard_tooling.configs")` path.
Commands without placeholders pass through unchanged.

```python
def language_commands(language: str, kind: CheckKind) -> list[list[str]]:
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    cmds = list(lang_entry.get(kind, []))
    if not cmds:
        return cmds
    configs_dir = str(files("standard_tooling.configs"))
    return [
        [arg.replace("{configs}", configs_dir) for arg in cmd]
        for cmd in cmds
    ]
```

### Package data

- Add `"configs/ruby/*.yml"` to the `pyproject.toml` package-data
  globs (alongside existing `"configs/*.yaml"` and `"configs/*.toml"`).
- Create `src/standard_tooling/configs/ruby/__init__.py` (empty) to
  make the subdirectory a Python subpackage for `importlib.resources`
  traversal.

### Dev container prerequisite

`license_finder` must be pre-installed in the `dev-ruby` container
image in standard-tooling-docker. This is a separate change in that
repository. The standard-tooling registry change can land
independently; the command will fail at audit time if the image has not
been updated.

### Tests

- Assert the Ruby AUDIT entry returns two commands (bundle-audit and
  license_finder with a resolved path).
- Assert `{configs}` is replaced with a real filesystem path (not left
  as a literal).
- Existing tests for other languages remain unchanged.

## Scope boundary

### In scope

1. Static decisions file at `configs/ruby/license_finder.yml`
2. Ruby AUDIT registry entry with `license_finder` command
3. `{configs}` placeholder expansion in `language_commands()`
4. `pyproject.toml` package-data update
5. Tests for the above

### Out of scope (explicit deferrals)

- Rationalizing the allowlist format across all languages (deferred
  until Ruby, Rust, and Java are all centralized)
- Per-repo overrides via `standard-tooling.toml`
- Removing the bespoke audit job from mq-rest-admin-ruby (tracked by
  wphillipmoore/mq-rest-admin-ruby#125)
- Adding `license_finder` to the dev-ruby container image (separate
  change in standard-tooling-docker)
