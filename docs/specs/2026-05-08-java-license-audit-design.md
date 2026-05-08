# Centralize Java License Audit

**Issue:** [#600](https://github.com/wphillipmoore/standard-tooling/issues/600)
**Date:** 2026-05-08
**Status:** Draft

## Problem

The Java audit entry in `validate_commands.py` runs the Maven license
plugin without policy enforcement flags. The bespoke audit job in
mq-rest-admin-java adds three critical `-D` properties that the
centralized registry omits:

- `-Dlicense.failIfWarning=true` — fail on violations (without this,
  the check is decorative)
- `-Dlicense.includedLicenses=...` — org-wide license allowlist
- `-Dlicense.excludedScopes=test` — skip test-only dependencies

Without these flags, `st-validate --check audit` for Java runs the
license plugin without blocking on policy violations.

## Decision

Add a `_MAVEN_LICENSES_ALLOWLIST` constant to `validate_commands.py`
and pass it along with the other `-D` flags in the Java audit command.
This mirrors the existing Go (`_GO_LICENSES_ALLOWLIST`) and Python
(`_PIP_LICENSES_ALLOWLIST`) patterns.

### Allowlist

The initial set comes from the bespoke audit job in
mq-rest-admin-java. The Maven license plugin matches against the
`<license><name>` value declared in each dependency's POM, so this
list includes both SPDX identifiers and human-readable variants
(notably three Apache variants). Pipe-delimited per Maven convention:

```
Apache 2.0|Apache-2.0|The Apache License, Version 2.0|BSD-2-Clause|BSD-3-Clause|GPL-3.0-or-later|ISC|MIT License|MPL-2.0
```

### Plugin version

No version pin in the registry. The repo's POM controls plugin
versions, consistent with how other Maven plugins (spotless,
checkstyle) are managed and how other languages handle tool versions.

### Scope exclusions

- **Config-driven allowlist (`standard-tooling.toml`):** Deferred.
  No demonstrated need with one Java repo. The Go design (#604)
  explicitly deferred the same thing.
- **Unified cross-language allowlist:** Deferred. Python's
  `pip-licenses` uses PyPI classifiers, Go's `go-licenses` uses
  SPDX identifiers, and Maven uses POM-declared license names — a
  shared constant would require a mapping layer with no payoff.
- **Plugin version pin:** Left to the repo's POM (see above).
- **Replacing the bespoke audit job in mq-rest-admin-java:** Follow-up
  work in the Java repo after this lands and is released.
- **Test gap (#617):** Factored into a separate issue.

## Change

**File:** `src/standard_tooling/lib/validate_commands.py`

Add a module-level constant:

```python
_MAVEN_LICENSES_ALLOWLIST = "|".join(
    [
        "Apache 2.0",
        "Apache-2.0",
        "The Apache License, Version 2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-or-later",
        "ISC",
        "MIT License",
        "MPL-2.0",
    ]
)
```

Update the Java audit registry entry from:

```python
CheckKind.AUDIT: [
    ["./mvnw", "dependency:tree", "-B", "-q"],
    ["./mvnw", "org.codehaus.mojo:license-maven-plugin:add-third-party", "-B"],
],
```

to:

```python
CheckKind.AUDIT: [
    ["./mvnw", "dependency:tree", "-B", "-q"],
    [
        "./mvnw",
        "org.codehaus.mojo:license-maven-plugin:add-third-party",
        "-Dlicense.excludedScopes=test",
        "-Dlicense.failIfWarning=true",
        f"-Dlicense.includedLicenses={_MAVEN_LICENSES_ALLOWLIST}",
        "-B",
    ],
],
```

**File:** `tests/standard_tooling/test_validate_commands.py`

Update `test_java_audit_commands` to verify the `-D` flags are present.

Add `test_java_audit_maven_licenses_allowlist_intact` to verify the
allowlist contains the expected number of licenses (9) and includes
key entries. Mirrors `test_go_audit_go_licenses_allowlist_intact`.

## Validation

`st-docker-run -- uv run st-validate`

## Related

- [#604](https://github.com/wphillipmoore/standard-tooling/issues/604) —
  Go license allowlist (merged, same pattern)
- [#603](https://github.com/wphillipmoore/standard-tooling/issues/603) —
  Ruby license_finder centralization
- [#617](https://github.com/wphillipmoore/standard-tooling/issues/617) —
  Java test gap (factored out of #600)
