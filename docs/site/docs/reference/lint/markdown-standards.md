# Markdown Validation

Markdown validation is handled by `st-validate-local-common` as part
of the standard validation pipeline. It runs `markdownlint` against
published documentation using a canonical config bundled in
standard-tooling.

## Scope

Validation targets published markdown only:

| Source | Pattern |
| ------ | ------- |
| Documentation site | `docs/site/**/*.md` |
| Project README | `README.md` |

Other markdown files (changelogs, release notes, internal docs) are
not validated by this check.

## Configuration

The canonical config is bundled at
`src/standard_tooling/configs/markdownlint.yaml` and resolved at
runtime via `importlib.resources`. Consuming repos do not need a
local `.markdownlint.yaml` -- the bundled config applies everywhere.

```yaml
default: true
no-duplicate-heading:
  siblings_only: true
MD013:
  line_length: 100
  tables: false
  code_blocks: false
MD060: false
```

Key rules:

| Rule | Setting | Rationale |
| ---- | ------- | --------- |
| Line length (MD013) | 100 chars | Matches ruff `line-length` setting |
| Tables | Exempt from line length | Wide reference tables are legitimate |
| Code blocks | Exempt from line length | Pasted commands and snippets exceed it |
| Duplicate headings | Siblings only | Same heading text is fine across sections |
| Table column style (MD060) | Disabled | No reader benefit for strict pipe alignment |

## How it runs

`st-validate-local` dispatches to `st-validate-local-common`, which:

1. Discovers markdown files matching the scope above.
2. Resolves the bundled `markdownlint.yaml` config.
3. Invokes `markdownlint --config <bundled-config> <files...>`.

This runs inside the dev container where `markdownlint` is on PATH.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All files pass |
| Non-zero | One or more validation failures |
