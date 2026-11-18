# TypeScript Toolchain and Strictness

## Purpose

Document the concrete strictness set enforced in the TYPECHECK stage and the
no-standing-suppression rule that governs it. This page **describes** the set; it
does not choose it. The set is decided and tested in the shareable base tsconfig
(`src/vergil_tooling/configs/typescript/tsconfig.base.json`) and the language
registry (`src/vergil_tooling/lib/languages.py`), and this page mirrors that
single source of truth. It also states the rule that Vergil, not the
repository, selects the Node runtime and the package manager.

## Toolchain Selection

Vergil selects the Node runtime, never the repository, as it selects Python's
interpreter. Each Vergil TypeScript image carries one Node major, so every CI
leg and dev container runs the major that Vergil resolved from the version
window (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)), with
the npm bundled with that major.

- A repository declares at most an `engines.node` range in its root
  `package.json`. The range should admit every major in the window.
- Every setting that names a Node or package-manager version is an error in
  `vrg-validate`, with its fix:

| Setting | Fix |
| --- | --- |
| a `.nvmrc` file at the repository root | delete it |
| a `.node-version` file at the repository root | delete it |
| a `packageManager` field in the root `package.json`, whatever it names, `npm` included | remove the field |
| a `volta` field in the root `package.json` (Volta pins Node and package managers) | remove the field |
| an `npm`, `pnpm` or `yarn` key under `engines` in the root `package.json` | remove the key |
| a `runtime` or `packageManager` entry under `devEngines` in the root `package.json`, as an object or an array of objects | remove the entry |

`devEngines` is reported because npm acts on it: npm checks it before
`install`, `ci` and `run`, and by default fails when the running Node or npm
does not match (see npm's
[`devEngines` documentation](https://docs.npmjs.com/cli/v11/configuring-npm/package-json#devengines)).
`devEngines`' `os`, `cpu` and `libc` entries are allowed. An `engines` or
`devEngines` that is not an object, a `devEngines` entry that is not an object
or an array of objects, or a root `package.json` that cannot be parsed, is an
error naming the field or the file.

### The `engines.node` Range

`vrg-validate` checks `engines.node` on two sides against the window, as it
checks Python's `requires-python`, and prints a warning, with its fix, for
each side the range misses. A warning does not fail validation.

| The range | Warning | Fix |
| --- | --- | --- |
| admits a Node major below the window's oldest (`>=20` against 26/24/22) | `engines.node >=20 admits Node 20, below the oldest supported major 22` | raise it to `>=22` (the oldest major) |
| admits no release of the window's newest major (`^24` excludes 26) | `engines.node ^24 excludes Node 26` | drop the upper bound, or widen it to admit 26 |

A floor above the window's oldest major is a deliberate restriction and is
allowed: `>=24` against 26/24/22 gives no warning. A range with `||` is read
as a whole, so `^22 || ^24` has a floor of 22 and excludes 26. Prerelease
versions play no part in either test. Only the root `package.json` is read;
workspaces are not checked, and a `package.json` with no `engines.node` gives
no warning.

The range is read with npm's grammar (node-semver's
[range syntax](https://github.com/npm/node-semver#ranges): comparators,
`x`-ranges, `~`, `^`, hyphen ranges and `||`). Two kinds of value are errors, which
fail validation:

- an `engines.node` that is not a string, or a string that is not an npm
  semver range (`latest`, for example);
- a valid range that admits no Node release (`>=26 <24`, for example).

## TYPECHECK Is "The Compiler"

For TypeScript, the TYPECHECK stage is `tsc --noEmit`, run as the
repository's own `node_modules/.bin/tsc`: a full typecheck under the
one canonical type engine with the curated strict set turned on. There is no
Clang-vs-GCC analog — there is exactly one `tsc` — so the two-diagnostics win
comes instead from pairing `tsc` (structural typing) with typescript-eslint
(type-aware lint), each run once.

The strict flags are not passed on the `tsc` command line. They live in the
**shareable base tsconfig** that every consumer `extends`, so the whole set lands
on every repository through inheritance — the direct analog of C++ threading its
warning set onto every translation unit's compile line. TYPECHECK is therefore a
bare `tsc --noEmit` that reads the consumer's own `tsconfig.json`, which extends
the base.

## The Curated Strict Set

The floor is `strict: true`, tuned upward with a curated set of extras. `strict`
already implies a family of checks:

```text
# strict: true implies
noImplicitAny
strictNullChecks
strictFunctionTypes
strictBindCallApply
strictPropertyInitialization
noImplicitThis
useUnknownInCatchVariables
alwaysStrict
```

On top of the `strict` floor, the base tsconfig enables a curated set of extras.
The complete set enforced today:

```text
# Floor
strict

# Curated extras
noUncheckedIndexedAccess
exactOptionalPropertyTypes
noImplicitOverride
noImplicitReturns
noFallthroughCasesInSwitch
noPropertyAccessFromIndexSignature
noUnusedLocals
noUnusedParameters
```

What each extra buys:

| Option | Catches |
| --- | --- |
| `noUncheckedIndexedAccess` | an index access (`arr[i]`, `map[key]`) treated as always-present when it may be `undefined` |
| `exactOptionalPropertyTypes` | assigning explicit `undefined` to an optional property declared without `\| undefined` |
| `noImplicitOverride` | a method that overrides a base member without the `override` keyword |
| `noImplicitReturns` | a code path through a function that falls off the end without returning a value |
| `noFallthroughCasesInSwitch` | a non-empty `switch` case falling through without `break`/`return` |
| `noPropertyAccessFromIndexSignature` | dotted access to an index-signature member, which hides that the key may not exist |
| `noUnusedLocals` | a declared local that is never read |
| `noUnusedParameters` | a function parameter that is never read (prefix with `_` to intend it) |

The set targets **TypeScript 6.x**: the repository declares `typescript` as a
devDependency, and `vrg-validate` holds both its declared range and its
installed version to major 6 (see
[Required devDependencies](overview.md#required-devdependencies-and-their-accepted-majors)),
so the set means the same thing on every run.

## Ownership Lives in the Base tsconfig

This page describes the set; it does not choose it. The set is authored and
tested in the packaged `tsconfig.base.json` — that is the single source of truth,
and this document mirrors it. If the set changes, it changes in the base
tsconfig first and this page is updated to match, never the other way around.

Consumers do not restate the set. A repository's `tsconfig.json` extends the
base, written into the repository as the managed `tsconfig.vergil.json`, and
inherits the strict set through that inheritance; enforcing it is the base's
job, not each repo's. This is the direct analog of a C++ repository inheriting
the shared warning flags rather than re-declaring `-Wall -Wextra` on its own
compile lines.

## No Standing Suppression List

There is **no project-wide standing suppression list** for type errors. A type
error is fixed, not muted. The strict set is turned on precisely so an error
cannot accumulate as ignored noise — under `tsc --noEmit` it fails the gate until
the code is corrected.

The no-standing-suppression rule is enforced two ways — as the written norm here,
and mechanically by the ESLint flat config through
`@typescript-eslint/ban-ts-comment`:

- **`// @ts-ignore` is banned outright.** It mutes an error with no record of
  what it was.
- **`// @ts-nocheck` is banned outright.** It silences an entire file — the
  whole-file abuse pattern, exactly the one the coverage rule also rejects.
- **`// @ts-expect-error` is allowed only with a description.** The rule is
  configured `allow-with-description`, so a bare `@ts-expect-error` fails lint. A
  legitimate suppression states the concrete reason inline and is narrow — it
  sits on the single line it excuses and fails lint again the moment the error it
  documents goes away (because the expected error no longer occurs).

`eslint-disable` follows the same discipline: local, narrow, and reasoned — never
a file-level or repo-level blanket that hides a whole rule.

The discipline mirrors the coverage exclusion rule in
[Testing and Coverage](testing-and-coverage.md#exclusion-discipline): gaps are
acknowledged narrowly and visibly, one at a time, with a reason — never
blanketed away.

## Relationship to the LINT Stage

The strict set is distinct from the LINT stage, which runs **Prettier** (format)
and **ESLint** (type-aware typescript-eslint rules) once:

- **Prettier** runs `prettier --check` against the managed `.prettierrc.json`
  (`semi: true`, `singleQuote: false`, `trailingComma: "all"`,
  `printWidth: 100`) — formatting only, no logic judgments. It checks every
  file it can parse but the Vergil-managed ones and the files a release
  writes, `CHANGELOG.md` and `releases/*.md` (see
  [One Prettier config](overview.md#one-prettier-config)).
- **ESLint** runs the repository's `eslint.config.mjs`, which spreads Vergil's
  managed `eslint.vergil.config.mjs`: its flat config with
  `js.configs.recommended` plus typescript-eslint's `recommendedTypeChecked` (type-aware rules resolved via
  `projectService: true`, which auto-discovers the nearest `tsconfig`), and
  carries the `ban-ts-comment` rule above.

TYPECHECK is `tsc`'s own soundness judgment; LINT is dedicated static analysis.
Both gate a change; neither substitutes for the other. Together they are the
TypeScript stand-in for C++'s two independent diagnostic engines.
