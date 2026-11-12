# Markdown Validation

Markdown validation is handled by `vrg-validate` (common checks) as
part of the standard validation pipeline. It runs `markdownlint`
against published documentation and epic documents using a canonical
config bundled in vergil-tooling.

## Scope

Validation targets these files:

| Source | Pattern |
| ------ | ------- |
| Documentation site | `docs/site/**/*.md` |
| Project README | `README.md` |
| Epic specs, plans and retrospectives | `epics/**/*.md` |

Other markdown files (changelogs, release notes, internal docs) are
not validated by this check. The `[markdownlint].ignore` paths in
`vergil.toml` drop files under `docs/site/` and `epics/`.

## Configuration

The canonical config is bundled at
`src/vergil_tooling/configs/markdownlint.yaml` and resolved at
runtime via `importlib.resources`. Consuming repos do not need a
local `.markdownlint.yaml` -- the bundled config applies everywhere.

```yaml
default: true
no-duplicate-heading:
  siblings_only: true
MD013: false
MD060: false
```

Key rules:

| Rule | Setting | Rationale |
| ---- | ------- | --------- |
| Line length (MD013) | Disabled | Files are read rendered, not raw; see [Line length](#line-length) |
| Duplicate headings | Siblings only | Same heading text is fine across sections |
| Table column style (MD060) | Disabled | No reader benefit for strict pipe alignment |

## Line length

MD013 is off. The rule exists to keep raw Markdown comfortable to read
in a terminal or a plain editor, but nobody reads these files raw: they
are read rendered, on GitHub and on the docs sites, and every editor
soft-wraps. Dropping it is a deliberate step in the fleet's move toward
machine-generated, machine-checked content. An audit of the 1,778
tracked Markdown files in the three active orgs found 10,326 prose
lines over 100 columns and a 99th percentile of 272 columns, so no
workable limit avoids a mass rewrap that no reader would see.

This reverses the earlier 100-column limit on `docs/site/` and
`README.md`. `epics/` was already exempt, through a separate relaxed
config that is now gone.

## How it runs

`vrg-validate` runs common checks, which:

1. Discovers markdown files matching the scope above.
2. Resolves the bundled `markdownlint.yaml` config.
3. Invokes `markdownlint --config <bundled-config> <files...>` once,
   over every discovered file.

This runs inside the dev container where `markdownlint` is on PATH.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All files pass |
| Non-zero | One or more validation failures |
