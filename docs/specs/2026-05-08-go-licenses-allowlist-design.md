# Centralize Go License Allowlist

**Issue:** [#604](https://github.com/wphillipmoore/standard-tooling/issues/604)
**Date:** 2026-05-08
**Status:** Draft

## Problem

The Go audit entry in `validate_commands.py` runs `go-licenses check ./...`
without the `--allowed_licenses` flag. Without this flag, the check passes
regardless of license type — it verifies that licenses exist but not that
they conform to org policy.

The bespoke CI job in mq-rest-admin-go fills this gap with an inline
`--allowed_licenses` flag, but that approach scatters policy across
individual repo workflows.

## Decision

Add a `_GO_LICENSES_ALLOWLIST` constant to `validate_commands.py` and
pass it via `--allowed_licenses=` in the Go audit command. This mirrors
the existing Python pattern (`_PIP_LICENSES_ALLOWLIST` passed to
`pip-licenses --allow-only=`).

### Allowlist

The initial set comes from the accumulated bespoke job in
mq-rest-admin-go:

```
Apache-2.0, BSD-2-Clause, BSD-3-Clause, MIT, ISC, MPL-2.0, GPL-3.0
```

These are SPDX identifiers accepted by `go-licenses --allowed_licenses`.

### Scope exclusions

- **`GOTOOLCHAIN` override:** Not needed. The Go dev container packages
  all required tools; if a tool is missing, validation fails hard.
  Container freshness is handled by rebuilding the dev-go image, not by
  env-var workarounds.
- **Config-driven allowlist (`standard-tooling.toml`):** Deferred.
  Extracting allowlists into config files and supporting per-repo
  overrides is future work once all languages are centralized.
- **Unified cross-language allowlist:** Deferred. Python's `pip-licenses`
  and Go's `go-licenses` use different identifier namespaces (PyPI
  classifiers vs. SPDX), so a shared constant would require a mapping
  layer with no immediate payoff.

## Change

**File:** `src/standard_tooling/lib/validate_commands.py`

Add a module-level constant:

```python
_GO_LICENSES_ALLOWLIST = ",".join(
    [
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0",
        "ISC",
        "MIT",
        "MPL-2.0",
    ]
)
```

Update the Go audit registry entry from:

```python
CheckKind.AUDIT: [["govulncheck", "./..."], ["go-licenses", "check", "./..."]],
```

to:

```python
CheckKind.AUDIT: [
    ["govulncheck", "./..."],
    ["go-licenses", "check", "./...", f"--allowed_licenses={_GO_LICENSES_ALLOWLIST}"],
],
```

No other files change.

## Validation

Validated through the existing pipeline: `st-docker-run -- uv run st-validate`.
No new tests required — the command registry is exercised by running
validation against a Go repo.

## Related

- [#600](https://github.com/wphillipmoore/standard-tooling/issues/600) —
  centralize bespoke Java audit and test CI jobs
- [#603](https://github.com/wphillipmoore/standard-tooling/issues/603) —
  centralize bespoke Ruby audit CI job (license_finder)
