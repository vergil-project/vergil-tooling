# vrg-repo-profile

**Installed as:** `vrg-repo-profile` (Python console script)

Validates that `docs/repository-standards.md` contains all required
repository profile attributes with non-placeholder values.

## Usage

```bash
vrg-repo-profile
```

Run from the repository root. The script reads
`docs/repository-standards.md` relative to the current directory.

## Required Attributes

The following attributes must be present and non-empty:

| Attribute | Example Values |
| --------- | -------------- |
| `repository_type` | `application`, `library`, `tooling` |
| `versioning_scheme` | `semver`, `calver`, `none` |
| `branching_model` | `library-release`, `application-promotion` |
| `release_model` | `tagged-release`, `continuous-deploy` |
| `supported_release_lines` | `1`, `2` |
| `primary_language` | `python`, `go`, `java`, `shell` |

## Validation Rules

1. **Presence** -- each attribute must exist in the file.
2. **No placeholders** -- values containing `<`, `>`, or `|` are
   rejected as unfilled template placeholders.

## Format

Attributes are expected as YAML-like key-value pairs in markdown:

```markdown
- repository_type: library
- versioning_scheme: semver
```

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All required attributes present and valid |
| 1 | One or more attributes missing or placeholder |
| 2 | Profile file not found |
