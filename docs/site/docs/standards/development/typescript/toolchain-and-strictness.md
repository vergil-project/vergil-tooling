# TypeScript Toolchain and Strictness

## Purpose

Document the concrete strictness set enforced in the TYPECHECK stage and the
no-standing-suppression rule that governs it. This page **describes** the set; it
does not choose it. The set is decided and tested in the shareable base tsconfig
(`src/vergil_tooling/configs/typescript/tsconfig.base.json`) and the language
registry (`src/vergil_tooling/lib/languages.py`), and this page mirrors that
single source of truth.

## TYPECHECK Is "The Compiler"

For TypeScript, the TYPECHECK stage is `tsc --noEmit`: a full typecheck under the
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

The base pins these against a **stable TypeScript 5.x release** (pinned in the
dev images), so the set means the same thing on every run.

## Ownership Lives in the Base tsconfig

This page describes the set; it does not choose it. The set is authored and
tested in the packaged `tsconfig.base.json` — that is the single source of truth,
and this document mirrors it. If the set changes, it changes in the base
tsconfig first and this page is updated to match, never the other way around.

Consumers do not restate the set. A repository's `tsconfig.json` extends the base
and inherits the strict set through that inheritance; enforcing it is the base's
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

- **Prettier** runs `prettier --check` against the packaged Prettier config
  (`semi: true`, `singleQuote: false`, `trailingComma: "all"`,
  `printWidth: 100`) — formatting only, no logic judgments.
- **ESLint** runs its flat config with `js.configs.recommended` plus
  typescript-eslint's `recommendedTypeChecked` (type-aware rules resolved via
  `projectService: true`, which auto-discovers the nearest `tsconfig`), and
  carries the `ban-ts-comment` rule above.

TYPECHECK is `tsc`'s own soundness judgment; LINT is dedicated static analysis.
Both gate a change; neither substitutes for the other. Together they are the
TypeScript stand-in for C++'s two independent diagnostic engines.
