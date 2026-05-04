# st-prepare-release

**Installed as:** `st-prepare-release` (Python console script)

**Source:** `src/standard_tooling/prepare_release.py`

Automates release preparation for library repositories: creates a
release branch, generates the changelog, creates a PR to main, and
enables auto-merge.

## Usage

```bash
st-prepare-release --issue 42
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--issue` | Yes | GitHub issue number for release tracking |

## Prerequisites

- Must be on the `develop` branch
- Working tree must be clean
- Local `develop` must match `origin/develop`
- Required tools: `gh`, `git-cliff`

## Ecosystem Detection

The tool auto-detects the project ecosystem to find the version:

| Ecosystem | Version Source |
| --------- | -------------- |
| Python | `pyproject.toml` |
| Maven | `pom.xml` |
| Go | `**/version.go` |
| VERSION file | `VERSION` (fallback) |

## Release Steps

1. **Precondition checks** -- verifies branch, clean tree, and
   tool availability.
2. **Create release branch** -- `release/{version}` from current
   develop HEAD.
3. **Merge main** -- incorporates prior release history to prevent
   changelog conflicts. Uses `-X ours` for auto-resolution.
4. **Generate changelog** -- runs `git-cliff` with a boundary tag,
   validates the generated output.
5. **Push branch** -- pushes `release/{version}` to origin.
6. **Create PR** -- creates a PR targeting `main` with
   `Ref #{issue}` linkage.
7. **Enable auto-merge** -- configures merge (not squash) strategy.
8. **Return to develop** -- checks out develop after completion.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Release preparation complete |
| Non-zero | Precondition failure or tool error |
