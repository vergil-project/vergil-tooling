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

Each compiler×version is one entry in `[ci].versions` (`clang-20`, `gcc-14`),
and C++ repos must pin that list. Running both families is therefore a
property of a **library**: a `library` may pin any subset of the window, such
as `["clang-20", "gcc-14"]`, and every pinned entry is a hard CI leg. Every
other repository type resolves as an app and builds exactly one version, so
an app that pins more than one entry fails resolution. An app gets one
compiler in CI; to gate on both, the repo must be a `library` (see
[Version resolution](../../../guides/ci-architecture.md#version-resolution)).

## Prebuilt Toolchains Only

Compiler toolchains come **exclusively from prebuilt stable binary packages —
never built from source**. Building a compiler from source in the validation
path is slow, non-reproducible, and a supply-chain liability; the dev images
pin prebuilt stable releases instead.

Which versions are available is set by the version window that vergil-containers
publishes, limited to what is cleanly available prebuilt for the image base
(Debian trixie). Today the window holds **Clang 20 and Clang 19** and **GCC 14**
only: the GCC policy covers the maintained series the image base packages, and
newer GCC majors are not installable there. The window bends to availability;
it never drops a compiler family.

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
- **Conan's generator output lands under `build/`, not the source root.** INSTALL
  passes `--output-folder=build`, so the toolchain file and CMakeDeps `Find*.cmake`
  modules are written under `build/` and the working tree stays clean after a
  build. Every CMake *configure* then points at the relocated toolchain with
  `-DCMAKE_TOOLCHAIN_FILE=build/conan_toolchain.cmake` so `find_package` resolves
  from `build/`. This retired the old source-root `CMAKE_PREFIX_PATH` hack — and
  the gitignore whack-a-mole that chased Conan's root-dropped
  `conan_toolchain.cmake` / CMakeDeps modules — because those artifacts no longer
  touch the source root. The one file Conan still writes to the source root
  regardless of `--output-folder` is `CMakeUserPresets.json`, which the managed
  cpp `.gitignore` fragment therefore keeps
  ([#2912](https://github.com/vergil-project/vergil-tooling/issues/2912)).
- The AUDIT stage runs **`conan audit scan . -s build_type=Debug
  --lockfile=conan.lock`**, which scans the resolved dependency graph against
  ConanCenter's advisory database for known CVEs, then
  `conan graph info . -s build_type=Debug --format=json --lockfile=conan.lock`
  to surface dependency licenses (best-effort in v1 — there is no turnkey Conan
  allowlist gate yet). The scan covers every package in the graph in both
  contexts, so `test_requires` and `tool_requires` are included. Both steps
  resolve in the lock's own `build_type=Debug` configuration against the
  committed `conan.lock`, so a Debug-only locked dependency is not skipped and a
  missing lock fails rather than re-resolving
  ([#3553](https://github.com/vergil-project/vergil-tooling/issues/3553)).
  `conan audit` reads a
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
variable **`CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`**, which it needs **inside
the dev container**.

For a cpp repo, `vrg-container-run` resolves the token itself and forwards it
into the container. It does not rely on the token being exported by the shell
that launched it, so it works the same under bash, zsh, and non-interactive
shells. A consuming repo needs **no** `[container].env-prefixes` entry for it.
The first source that has a token wins:

1. **`CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER` in the environment**, if set and
   not blank. This is how CI provides it, and how an operator overrides the
   files for one run.
2. **`~/.config/vergil/conan.env`**, the file `vrg-vm` writes into agent VMs.
3. **The `conan_audit_token_path` file named in
   `~/.config/vergil/identities.toml`**, the host-side bare-token file. If more
   than one identity sets it, they must all name the same file.

A source that exists but cannot be read, is malformed, or holds an empty token
stops `vrg-container-run` with an error naming the file. It never falls back to
a lower source. The token value is passed to the container runtime through its
environment, never on its command line, and is never printed.

To wire the token up once:

- **CI.** Store the value as the org/CI secret
  **`CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`**. The CI audit job injects it into
  the container environment directly.
- **Agent VMs and the host.** Point the identity at a host file holding the bare
  token with the `conan_audit_token_path` key in `identities.toml`. `vrg-vm`
  credential injection copies it into the guest's `~/.config/vergil/conan.env`,
  and host-side runs read the file directly. Injection also adds a `~/.bashrc`
  line that sources `conan.env`, so `conan` run by hand in an interactive bash
  sees the token. The tooling does not depend on that line.

Without any token, `vrg-validate` skips `conan audit scan .` with a notice and
runs the rest of the AUDIT stage.

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
for the hard-gates-only standard: every CI check is a hard gate, and every
version a repo resolves from the version window is a hard CI leg (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)).

C++ hard gates run per resolved compiler×version: every entry a `library`
pins, or the single entry an app pins:

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
