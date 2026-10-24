# Java Coding Standards Overview

## Purpose

Define consistent Java standards that emphasize readability, maintainability,
and long-term survivability across repositories.

## Core Principles

- Google Java Style Guide compliance is the default and highest priority.
- Readability overrides cleverness or brevity.
- Exceptions must be explicit, documented, and justified.

## Tooling Expectations

- Formatting: google-java-format (opinionated, zero configuration).
- Style enforcement: Checkstyle with the Google checks configuration.
- Static analysis: SpotBugs (bytecode-level bug detection) and PMD
  (source-level bug detection).
- Compiler plugin: Error Prone (catches common mistakes at compile time).
- Null safety: NullAway (Error Prone plugin, low-overhead null checks).
- Build system: project-dependent (Maven or Gradle). Document the choice and
  equivalent commands in the repository README.
- If a repository uses different tools, document the reason and equivalents.

## CI Gates

See [Source Control Guidelines](../../source-control-guidelines.md#ci-gates)
for the hard-gates-only standard: every CI check is a hard gate, and every
version a repo resolves from the version window is a hard CI leg (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)).

Required checks for Java repositories are maintained in the
[standard-actions CI gates documentation](https://vergil-project.github.io/standard-actions/ci-gates/required-checks/).

## Vulnerability audit

The `vrg-validate` audit stage, which is also the CI `audit` gate, fails on
any dependency with a known vulnerability. It checks the full resolved
graph: every module in the reactor and every scope, test scope included.

The stage runs `./mvnw org.cyclonedx:cyclonedx-maven-plugin:makeAggregateBom`
to write a CycloneDX SBOM of the reactor to `target/bom.json`. Maven
resolves that graph the same way it resolves the build. The stage then runs
`osv-scanner scan source --lockfile=target/bom.json`, which checks every
package against the [OSV](https://osv.dev) advisory database. The scan needs
network access to `api.osv.dev` but no API key. A project with no
dependencies passes. A missing SBOM fails the audit.

The `osv-scanner` binary comes from the Java dev image. If the image does
not provide it, the audit fails with a message that names the missing tool.
It never skips the scan.

To suppress a reviewed false positive, commit an `osv-scanner.toml` at the
repository root. The audit passes it to the scanner whenever it exists. Give
every entry a reason and an expiry date:

```toml
[[IgnoredVulns]]
id = "GHSA-xxxx-xxxx-xxxx"
ignoreUntil = 2027-01-01
reason = "Vulnerable code path is unreachable: <evidence>"
```

If the scan fails, upgrade the dependency, or pin a fixed transitive
version in `<dependencyManagement>`, before committing.

## Document Map

- Naming conventions: [naming-conventions.md](naming-conventions.md)
