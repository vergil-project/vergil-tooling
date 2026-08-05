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
available prebuilt. The shipped v1 matrix is **Clang 20 (primary) and Clang 19**
plus **GCC 14 (primary) and GCC 13**. GCC illustrates the availability rule: the
spec's target pair was gcc-15/14, but gcc-15 is not cleanly prebuilt for the
image base (Debian trixie), so the matrix steps back one release to gcc-13/14
rather than dropping a compiler. The matrix bends to availability; it never
drops to one compiler.

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
  writes a `conan.lock` (`conan lock create`) that pins the graph for the Debug
  builds.
- The AUDIT stage runs **`conan audit scan .`**, which scans the resolved
  dependency graph against ConanCenter's advisory database for known CVEs, then
  `conan graph info . --format=json` to surface dependency licenses (best-effort
  in v1 — there is no turnkey Conan allowlist gate yet). `conan audit` reads a
  ConanCenter provider token from the environment; a consuming repo must supply
  it once (see [ConanCenter Audit Token](#conancenter-audit-token) below).

An earlier iteration briefly switched AUDIT to **OSV-Scanner** over the
`conan.lock`, but that was **reverted**: OSV.dev carries no ConanCenter package
data, so the scan reported a false all-clear — it never had the CVE data to find
anything. `conan audit scan .` is the shipped v1 auditor (decision reversal:
vergil-project/.github#209).

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

## ConanCenter Audit Token

`conan audit scan .` queries ConanCenter's hosted advisory provider, which
requires an authentication token. The command reads it from the environment
variable **`CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`**. Because that variable is
consumed **inside the dev container**, a consuming C++ repository must arrange
for it to reach both local runs and CI. Three pieces wire it end to end:

1. **Pass the token into the container.** Add the `CONAN_AUDIT_PROVIDER_TOKEN`
   prefix to `[container].env-prefixes` in the repo's `vergil.toml`, so
   `vrg-container-run` forwards the token into the container environment:

   ```toml
   [container]
   env-prefixes = ["CONAN_AUDIT_PROVIDER_TOKEN"]
   ```

2. **Provide the token in CI.** Store the value as the org/CI secret
   **`CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`** so it is present in the
   environment on PR CI runs.

3. **Provide the token on VMs / local agents.** Point the agent identity at a
   host file holding the bare token via the `conan_audit_token_path` key in
   `identities.toml`. Credential injection reads that file and exports
   `CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER` into the guest environment (the same
   mechanism used for the Anthropic token), so agent sessions run AUDIT with the
   token available.

Without the token, `conan audit scan .` cannot reach the advisory provider and
the AUDIT stage fails; the three pieces above are the one-time setup that makes
a consuming C++ repo audit-ready.

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
