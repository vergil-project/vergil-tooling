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
| Security records, such as the Scorecard snapshots | `docs/security/**/*.md` |
| Project README | `README.md` |
| Epic specs, plans and retrospectives | `epics/**/*.md` |
| Extra paths the repo lists in `[markdownlint].paths` | a listed directory's `**/*.md`, or a listed `.md` file |

Other markdown files (changelogs, release notes, internal docs, and the
rest of `docs/`, such as plans and specs) are not validated by this
check unless the repo lists them in `[markdownlint].paths`. The
`[markdownlint].ignore` paths in `vergil.toml` drop files under
`docs/site/`, `docs/security/`, `epics/` and the extra paths.

### Extra paths

A repo that keeps markdown outside the fixed scope, such as a plugin's
`skills/` tree, names it in `vergil.toml`:

```toml
[markdownlint]
paths = ["skills", "docs/guides/extra.md"]
```

Each entry is a repo-relative path. A directory is searched recursively
for `*.md`; a file is linted as it is. The listed files join the fixed
scope in the same single markdownlint pass, against the same bundled
config, so `vrg-validate` and `vrg-validate --markdown FILE` agree on
them. An entry that does not exist, or a file that is not `.md`, fails
the check; it is never skipped silently. An absolute path, a path with a
`..` component, an empty entry and a `.` entry (the repo root) are
rejected when `vergil.toml` is read. `[markdownlint].ignore` applies to
the listed paths too. A markdown file outside the fixed scope and not
listed is not linted.

The `paths` key takes effect once the release that carries it is
published. An older release warns on stderr that the key is
unrecognized and lints the fixed scope only, so a repo should not list
paths until its pinned vergil-tooling carries the key.

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

To lint a few files while working, run
`vrg-container-run -- vrg-validate --markdown [--fix] <file>...`. It
lints each given file the gate lints against the same bundled config,
and names and skips any file the gate does not lint, so its result
matches the gate's. A bare `markdownlint <file>` does not: it falls back
to markdownlint's defaults, MD013 included.

## Links to other Vergil docs sites

Right after markdownlint, the common checks run docs-links over the same
markdown, less `epics/archive/`. The `[markdownlint].ignore` paths do
not apply to it: a file left out of the lint still publishes its links.
Its scope is the fixed default scope only: the `[markdownlint].paths`
extra paths are not checked by it.

The Vergil docs sites that mike versions serve every page under a
version segment, so a deep link written as
`https://vergil-project.github.io/<site>/<page>/` returns 404. Write it
as `https://vergil-project.github.io/<site>/latest/<page>/` instead.
The check accepts `latest`, `dev` or a version number (such as `2.1`)
as the first path segment, and a bare site root
(`https://vergil-project.github.io/<site>/`), which mike redirects to
the default version. Each hit is named by path and line, with its
`/latest/` form.

The versioned sites are a fixed list in vergil-tooling
(`vergil_tooling.lib.doc_links.VERSIONED_SITES`), so the check needs
no network: vergil-actions, vergil-claude-plugin, vergil-containers,
vergil-tooling, vergil-vm and the six vergil-fixture-* sites. The
org's `docs` and `packages` sites are published but not versioned, so
any path on them passes.

A link to any other site name on `https://vergil-project.github.io/`,
such as a retired site, fails too: the org does not publish it, so the
link returns 404 whatever its path. Each such hit is named by path and
line with the site name. The site name is compared exactly, and a
trailing dot that ends a sentence right after the site name is not part
of it. A repository that starts publishing versioned docs is added to
the list; until then the check flags links to its site from other
repositories. The repository being checked is the exception: when its
`vergil.toml` sets `[publish].docs = true` (the default) and its
`origin` is a `vergil-project` repository on GitHub, its own site is
treated as a published, versioned site, so a new repository's README
link to its own docs passes from its first pull request.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | All files pass |
| Non-zero | One or more validation failures |
