# Tool Ownership Guideline

## Purpose

Define one cross-language rule for where a development tool lives: declared
in the repository, or owned by Vergil's shared container image. A new language
applies the same test, and its outcome differs from another language's only
where the language's own design forces it.

## Scope

Applies to every tool a Vergil gate runs (formatters, linters, typecheckers,
test runners, coverage, audit and license tools) for every language in
`[project].primary-language`. The language runtime and its toolchain selection
are covered by the
[Runtime Version Support Policy](runtime-version-support-policy.md), not here.

## The rule

- **Default: follow the ecosystem.** A tool lives wherever the language
  community recommends.
- **Exception: the image may own a tool** when the *reason* behind the
  ecosystem's advice is met as well by Vergil's shared image. "Everyone on the
  team runs the same version" is such a reason: everyone runs the same image.
  A technical constraint is not such a reason, and a tool held in the repo by
  one stays in the repo.

## The test

A repository declares a tool when any of these hold:

- its code or tests import the tool;
- the tool builds the artifact the repository ships;
- a tool the repository declares requires it (for example, as a peer
  dependency).

Every other tool may move to the image.

## What the image costs, and why it is accepted

| | Tool declared in the repo | Tool owned by the image |
|---|---|---|
| Version across the fleet | Each repo's own; repos drift apart | One fleet-wide version |
| Update churn | Every repo carries the tool list and its updates | None in the repo; the nightly image rebuild moves it |
| When a new tool version breaks a gate | The bump's own commit turns red | A repo can turn red with no commit of its own |
| Rebuilding an old release | Uses the tool versions it was tested with | Uses whatever the image carries now |

Vergil floats on purpose. A repo turning red because the image moved is how
the leading edge is discovered, and it is fixed forward rather than avoided by
holding tools back.

What the evidence records today: the versions of the repo-declared tools,
through the repository's lockfile. The evidence does not yet record the
versions of image-owned tools; that is follow-on work on
[vergil-tooling#4416](https://github.com/vergil-project/vergil-tooling/issues/4416).

## The sources

Each entry gives what the source says, then the reading this guideline takes
from it.

- [Prettier: Install](https://prettier.io/docs/install). Says to install an
  exact version locally, "so everyone in the project gets the exact same
  version", because "even a patch release of Prettier can result in slightly
  different formatting". *Reading:* the reason is team consistency, which the
  shared image meets.
- [Ruff: Versioning](https://docs.astral.sh/ruff/versioning/). Says a minor
  release may change the behavior of a stable rule, add or remove stable rules
  from the default set, and change the formatter's stable style. It gives no
  advice on where to install Ruff. *Reading:* the risk is different output on
  different versions, a consistency reason the shared image meets.
- [ESLint: Getting Started](https://eslint.org/docs/latest/use/getting-started).
  Says ESLint can be installed globally, but "any plugins or shareable configs
  that you use must still be installed locally". *Reading:* a technical
  constraint, so a repo whose ESLint config imports plugins declares them.
- [mypy: Using installed packages](https://mypy.readthedocs.io/en/stable/installed_packages.html).
  Says mypy finds installed packages only in the environment it runs in, and
  that `--python-executable` points it at another environment's packages.
  *Reading:* mypy needs the project's environment, not a declaration in the
  project, so the image may own it.
- [Vitest: globals](https://vitest.dev/config/globals). Says the default is
  `false`: "By default, `vitest` does not provide global APIs for
  explicitness." *Reading:* test files import Vitest, so the repo declares it.
- [pytest: Good Integration Practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
  Says to install the application, its dependencies "and the `pytest` package
  itself into a virtual environment", with pytest in the `dev` dependency
  group. *Reading:* tests import pytest and its fixtures, so the repo declares
  it.

## Applied, per language

| Language | The repository declares | The image owns |
|---|---|---|
| TypeScript | typescript, eslint, typescript-eslint, `@eslint/js`, vitest, `@vitest/coverage-v8` | prettier, license-checker |
| Python | pytest, pytest-cov, any pytest plugins | ruff, mypy, pip-audit, pip-licenses (migration pending) |
| Rust | nothing | cargo-deny, cargo-llvm-cov, cargo-nextest; rustfmt and clippy ship with the toolchain |

- **TypeScript.** eslint is on the repository side because typescript-eslint
  declares it as a peer dependency. `npm ci` installs a matching eslint in the
  repository anyway, and a global eslint beside it could be a major version
  typescript-eslint does not support.
- **Python.** Python repositories still declare ruff, mypy, pip-audit and
  pip-licenses today. Moving them to the image under this guideline is
  pending, tracked by
  [vergil-project/.github#1210](https://github.com/vergil-project/.github/issues/1210).
- **Rust.** Rust already follows the guideline: nothing a Rust repository's
  code or tests import is a gate tool.

## Adding a language

1. List every tool the language's gates run.
2. For each, find the ecosystem's advice on where it is installed and the
   reason it gives, from the tool's own documentation.
3. Apply [the test](#the-test). A tool that passes none of its conditions,
   and whose ecosystem advice rests on consistency, goes to the image.
4. Add the language's row to [Applied, per language](#applied-per-language),
   with its sources, and list the split on the language's standards overview.
