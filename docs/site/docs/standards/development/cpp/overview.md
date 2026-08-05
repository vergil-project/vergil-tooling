# C++ Development Standards Overview

## Purpose

Define consistent C++ standards that emphasize safety, portability,
maintainability, and long-term survivability across repositories.

## Core Principles

- Two independent compilers are the baseline, not a luxury. Code must build
  and pass warnings clean under both Clang and GCC.
- Portability and correctness override cleverness or brevity.
- Readability overrides micro-optimization.
- Exceptions must be explicit, documented, and justified.

## The Dual-Compiler Model

C++ repositories are validated under **two co-equal, first-class open-source
compilers**: **Clang/LLVM (primary)** and **GCC (secondary)**. Both are
merge-blocking diagnostic engines, not a primary gate with a nightly
afterthought.

The payoff is two independent diagnostic engines. Clang and GCC disagree about
which constructs are undefined, non-portable, or suspect; a warning one raises
the other often misses. Holding code clean under both catches a class of
portability and undefined-behavior defects that either compiler alone would let
through.

"Primary" and "secondary" describe defaults and ordering, not authority. LINT
and AUDIT run **once** on the primary Clang image (there is nothing
compiler-specific to gain from running a formatter or a dependency scan twice).
The compiler-sensitive stages — TYPECHECK (the warnings build) and TEST
(coverage and sanitizers) — run **per compiler and per version**, so both
families gate every change equally.

## Prebuilt Toolchains Only

Compiler toolchains come **exclusively from prebuilt stable binary packages —
never built from source**. Building a compiler from source in the validation
path is slow, non-reproducible, and a supply-chain liability; the dev images
pin prebuilt stable releases instead.

The target is **two recent majors per family**, taken from whatever is cleanly
available prebuilt. When the newest majors lack prebuilt binaries, the fallback
steps back a release while keeping both families present (for example,
gcc-13/14 and clang-18/19 rather than dropping a compiler). The matrix bends to
availability; it never drops to one compiler.

## Build System and Dependency Management

The mandated build system is **CMake**, with **Conan 2** as the dependency
manager.

- **CMake** configures out-of-tree build directories and exports
  `compile_commands.json` (via `CMAKE_EXPORT_COMPILE_COMMANDS=ON`). That
  compilation database is what `clang-tidy` and other static-analysis tooling
  read to see each translation unit's exact flags.
- **Conan 2** resolves and builds dependencies
  (`conan install . -s build_type=Debug --build=missing`, matching the CMake
  Debug build so dependency binaries share the same configuration). INSTALL also
  writes a `conan.lock` (`conan lock create`), which the AUDIT stage scans.
- The AUDIT stage runs **OSV-Scanner** over that `conan.lock`
  (`osv-scanner scan source --lockfile=conan.lock`) for dependency CVEs.
  OSV-Scanner is tokenless, offline-capable, and scales with per-PR volume;
  `conan audit` was reconsidered because its hosted provider needs a token and
  is rate-limited (decision: vergil-project/.github#209).

vergil-tooling passes **intent** to the project's `CMakeLists.txt` as CMake
cache variables rather than raw compiler flags, so every validation command
stays portable across both compiler families. The project's `CMakeLists.txt`
translates that intent per compiler (for example, libc++'s `-stdlib` is
Clang-only and must never appear as a raw flag in a shared command):

| Cache variable | Meaning |
| --- | --- |
| `VERGIL_CPP_STD` | the `[cpp].std` value (for example, `c++20`) |
| `VERGIL_CPP_STDLIB` | the `[cpp].stdlib` value (for example, `libstdc++`) |
| `VERGIL_CPP_COVERAGE` | `ON` selects coverage instrumentation |
| `VERGIL_CPP_SANITIZE` | the sanitizer list (for example, `address,undefined`) |

## Testing: CTest Runner, GoogleTest Default

**CTest is the mandated test runner.** Every C++ repository exposes its tests
through CTest, and the validation pipeline invokes them with
`ctest --test-dir <build-dir> --output-on-failure`. CTest is the stable
integration surface; the underlying test framework is a repository choice.

**GoogleTest is the documented default framework.** A repository may instead use
Catch2 or doctest, but GoogleTest is the recommended starting point and the one
new repositories should reach for absent a specific reason. Whatever the
framework, tests are registered with CTest so the runner contract holds.

## CI Gates

See [Source Control Guidelines](../../source-control-guidelines.md#ci-gates)
for hard gate and soft gate definitions.

C++ hard gates run per compiler and per version:

- **TYPECHECK** — the warnings build under each compiler×version (see
  [Toolchain and Warnings](toolchain-and-warnings.md)).
- **TEST** — the coverage-instrumented build and run, held to 100% line
  coverage per compiler, plus a separate AddressSanitizer + UndefinedBehavior
  Sanitizer build and run (see [Testing and Coverage](testing-and-coverage.md)).

LINT and AUDIT run once on the primary Clang image.

## Document Map

- Toolchain and warnings: [toolchain-and-warnings.md](toolchain-and-warnings.md)
- Testing and coverage: [testing-and-coverage.md](testing-and-coverage.md)
- Naming conventions: [naming-conventions.md](naming-conventions.md)
