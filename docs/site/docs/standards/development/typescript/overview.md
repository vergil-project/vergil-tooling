# TypeScript Development Standards Overview

## Purpose

Define consistent TypeScript standards that emphasize type safety, portability,
maintainability, and long-term survivability across repositories.

## Core Principles

- Strict typing is the baseline, not a luxury. Code must typecheck clean under
  `tsc` with the curated strict set promoted to errors.
- Portability and correctness override cleverness or brevity.
- Readability overrides micro-optimization.
- Suppressions must be explicit, documented, and justified.

## The Single-`tsc`, Single-Runtime Model

TypeScript has **no Clang-vs-GCC analog** — there is exactly one canonical
typechecker, `tsc`. The C++ "two independent diagnostic engines" become
**`tsc` (structural typing) plus typescript-eslint (type-aware lint)**, and both
run **once**, not per runtime.

Because there is one typechecker, the expensive matrix axis is the **Node major
version**, not the compiler. This makes TypeScript *lighter* than C++ on the
matrix:

- **TYPECHECK, LINT, and AUDIT run once.** There is nothing runtime-specific to
  gain from typechecking, formatting, or scanning dependencies more than once —
  `tsc --noEmit`, Prettier + ESLint, and `npm audit` each run a single time.
- **Only TEST fans out per Node version.** Runtime behavior differs across Node
  majors, so the test-and-coverage stage runs once per resolved Node major. See
  [Testing and Coverage](testing-and-coverage.md).

Node majors are `[ci].versions` entries with a family prefix (`node-24`). The
window has one family, so a TypeScript repo needs no pin and follows the window
as Python and Rust do: an unpinned **library** builds every window major (today
`node-24` and `node-22`), each a hard CI leg, and an unpinned app builds the
newest. A `library` may pin any subset of the window. Every other repository
type resolves as an app and builds exactly one version, so an app that pins
more than one major fails resolution (see
[Version resolution](../../../guides/ci-architecture.md#version-resolution)).

## npm + ESM Layout

Package management is **npm**, chosen for universality at zero adoption barrier:

- **INSTALL is `npm ci --ignore-scripts`** — a clean install from
  `package-lock.json`. The lockfile records the dependency graph; `npm ci`
  installs exactly what it records and fails if `package.json` and the lockfile
  disagree.
- **Install scripts never run.** Every `npm ci` Vergil runs for TypeScript
  passes `--ignore-scripts`. Repositories Vergil manages do not rely on install
  scripts; one that genuinely needs one declares it as an exception, under
  exception management
  ([vergil-project/.github#375](https://github.com/vergil-project/.github/issues/375)).
- **Modules are ESM.** The shipped base tsconfig sets `module` and
  `moduleResolution` to `nodenext` and targets `es2022`, so repositories are
  native ES modules resolved the way modern Node resolves them. A repository's
  `package.json` declares `"type": "module"` to match.

### Baking a non-npm dependency into the dev image

A repo occasionally needs a JS library present in the dev image itself — for
example a package a test driver loads at runtime that is not part of the repo's
own `package-lock.json`. That is a `[container].build-command` in `vergil.toml`,
not an apt package:

```toml
[container]
build-command = "npm install -g @coderline/alphatab"
build-cache-files = ["package-lock.json"]
```

One TypeScript-specific caveat is load-bearing. A global `npm install -g` puts a
library's **executables** on `PATH`, but does **not** add the npm global root
(`/usr/lib/node_modules`) to Node's module-resolution path. So `vrg-container-run`
sets `NODE_PATH` to that root whenever the repo declares a `build-command`, which
makes a baked library resolvable **via CommonJS `require`** — but `NODE_PATH` is
honoured only by `require`. **ESM `import` ignores `NODE_PATH`.** Since Vergil
repositories are native ESM (per the npm + ESM Layout section above), a baked
library consumed through `import` needs a different mechanism (a repo-local
`node_modules`, or staging the module where ESM resolution walks up to it). The
full contract and the runtime details are in
[Container Config Reference → `build-command`](../../../reference/container-config.md#build-command).

## Tool Ownership

Where each tool lives follows the
[Tool Ownership Guideline](../tool-ownership.md). For TypeScript:

- The repository declares typescript, eslint, typescript-eslint, `@eslint/js`,
  vitest and `@vitest/coverage-v8` as devDependencies. Its tests import
  vitest, and Vitest loads `@vitest/coverage-v8` from the repository for the
  coverage run. Its ESLint config imports typescript-eslint and `@eslint/js`,
  and typescript-eslint declares typescript and eslint as peer dependencies.
- The image owns prettier and license-checker.

### Required devDependencies and their accepted majors

The gates run the repository's own tools by path, from `node_modules/.bin`:
LINT's ESLint step, TYPECHECK (`node_modules/.bin/tsc`) and TEST
(`node_modules/.bin/vitest`). A tool missing from the repository fails the
gate; nothing falls back to a global binary or downloads one.

Before the gates run, `vrg-validate`'s `typescript-tools` stage (right after
INSTALL, in the full run and in every single `--check` of a language gate)
checks each required devDependency against the major Vergil accepts for it:

| devDependency | Accepted major |
|---|---|
| `typescript` | 6 |
| `typescript-eslint` | 8 |
| `eslint` | 10 |
| `@eslint/js` | 10 |
| `vitest` | 5 |
| `@vitest/coverage-v8` | 5 |

A package fails when `devDependencies` does not declare it, when its declared
range is not `^M…`, `~M…` or `M…` with `M` the accepted major (so
`latest`, `*` and `>=5` all fail), when `npm ci` did not install it, or
when the installed version has another major. The failure names every failing
package and the one command that fixes them all, for example
`npm install --save-dev typescript@^6 eslint@^10`.

The majors live in one place,
`src/vergil_tooling/configs/typescript/dev-dependencies.json`, which this
table mirrors. Within its major, each tool's version floats through the
repository's lockfile.

## Prebuilt Toolchains Only

Node toolchains come **exclusively from prebuilt stable binary packages — never
built from source**. Building a runtime from source in the validation path is
slow, non-reproducible, and a supply-chain liability; the dev images pin
prebuilt official stable Node binaries for each supported major instead
(`prod-ts-node:<major>`).

The image does not pin the repository's TypeScript tools. TypeScript, ESLint,
typescript-eslint and Vitest come from the repository's own devDependencies,
held to their accepted majors (see
[Required devDependencies](#required-devdependencies-and-their-accepted-majors)),
and their exact versions float through the lockfile. Only the image's own
tools, Prettier and license-checker, come with the image.

## `tsc --noEmit`: Compile-Correctness Without a Bundler

v1 mandates **no bundler and no emit step**. `tsc --noEmit` covers
compile-correctness — it reads the consumer's `tsconfig.json` and reports type
errors without writing any output — and there is no publish target. Bundling,
emit, and published-artifact validation (tsup / esbuild / rollup) are
deliberately deferred (spec §8, deferral ledger #6).

## Extending the Shareable Base tsconfig

The curated strict set is delivered as a **shareable strict base tsconfig
shipped by Vergil** that consumers `extends` — the direct analog to C++
enforcing a concrete `-Wall -Wextra -Werror -Wpedantic`-plus-extras warning set
through a shared flag list.

A consuming repository authors a minimal `tsconfig.json` that extends the
Vergil-shipped base and adds only repo-local paths:

```json
{
  "extends": "<vergil>/typescript/tsconfig.base.json",
  "compilerOptions": {
    "rootDir": "src",
    "outDir": "dist"
  },
  "include": ["src"]
}
```

The strict set lives in the base and is inherited through `extends`, so
TYPECHECK is a bare `tsc --noEmit` that reads the consumer's own
`tsconfig.json`. A repository never re-declares the strict flags — it inherits
them, exactly as a C++ repository inherits the warning set threaded onto every
compile line. The concrete set the base carries is documented in
[Toolchain and Strictness](toolchain-and-strictness.md).

## Testing: Vitest Runner, V8 Coverage Default

**Vitest is the mandated test runner**, run with the V8 coverage provider and
held to a **100% line threshold per Node version**:

```bash
vitest run --coverage --coverage.provider=v8 --coverage.thresholds.lines=100
```

The gate is a forcing function for explicitness, not a claim that every line was
exercised — see [Testing and Coverage](testing-and-coverage.md) for the 100%
philosophy and the `/* v8 ignore */` exclusion discipline.

## CI Gates

See [Source Control Guidelines](../../source-control-guidelines.md#ci-gates)
for the hard-gates-only standard: every CI check is a hard gate, and every
version a repo resolves from the version window is a hard CI leg (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)).

TypeScript hard gates:

- **TYPECHECK** (once) — `tsc --noEmit` with the curated strict set inherited
  from the base tsconfig (see
  [Toolchain and Strictness](toolchain-and-strictness.md)).
- **LINT** (once) — Prettier `--check` for format, then ESLint's flat config
  with type-aware typescript-eslint rules, carrying the no-standing-suppression
  rule (`@typescript-eslint/ban-ts-comment`).
- **TEST** (per Node version) — Vitest with the V8 coverage provider, held to
  100% line coverage per Node major (see
  [Testing and Coverage](testing-and-coverage.md)).
- **AUDIT** (once) — `npm audit --audit-level=high`, plus a best-effort
  license summary. The vulnerability audit covers every package in
  `package-lock.json`, devDependencies included, so a vulnerable docs or test
  tool fails the audit just as a runtime dependency does. The license summary
  still covers production dependencies only.

## Document Map

- Toolchain and strictness:
  [toolchain-and-strictness.md](toolchain-and-strictness.md)
- Testing and coverage: [testing-and-coverage.md](testing-and-coverage.md)
- Naming conventions: [naming-conventions.md](naming-conventions.md)
