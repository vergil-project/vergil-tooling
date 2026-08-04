# C++ Toolchain and Warnings

## Purpose

Document the concrete compiler warning set enforced in the TYPECHECK stage and
the no-standing-suppression rule that governs it. This page **describes** the
set; it does not choose it. The set is decided and tested in the language
registry (`src/vergil_tooling/lib/languages.py`), and this page mirrors that
single source of truth.

## TYPECHECK Is "The Compiler"

For C++, the TYPECHECK stage is the **warnings build**: a full compile under
each compiler and version with the curated warning set promoted to errors. There
is no separate type checker the way Python has mypy or Rust has `cargo check` —
the compiler itself, run strict, is the type-and-soundness gate.

The warning flags are threaded into the build as a single `CMAKE_CXX_FLAGS`
cache value, so the whole set lands on every translation unit's compile line.

## The Curated Warning Set

The floor is `-Wall -Wextra -Werror -Wpedantic`, tuned upward with a curated set
of extras. The complete set enforced today:

```text
# Floor
-Wall
-Wextra
-Werror
-Wpedantic

# Curated extras
-Wshadow
-Wconversion
-Wsign-conversion
-Wcast-qual
-Wold-style-cast
-Wnon-virtual-dtor
-Woverloaded-virtual
-Wdouble-promotion
-Wformat=2
-Wimplicit-fallthrough
-Wnull-dereference
```

What each extra buys:

| Flag | Catches |
| --- | --- |
| `-Wshadow` | a local name silently shadowing an outer one |
| `-Wconversion` | implicit conversions that may change a value |
| `-Wsign-conversion` | implicit signed/unsigned conversions |
| `-Wcast-qual` | a cast that drops a `const`/`volatile` qualifier |
| `-Wold-style-cast` | C-style casts in C++ code (use named casts) |
| `-Wnon-virtual-dtor` | a base class with virtuals but a non-virtual destructor |
| `-Woverloaded-virtual` | a derived member hiding, not overriding, a virtual |
| `-Wdouble-promotion` | an implicit `float`-to-`double` promotion |
| `-Wformat=2` | format-string and security issues (`printf`-family) |
| `-Wimplicit-fallthrough` | a `switch` case falling through without annotation |
| `-Wnull-dereference` | a path that dereferences a known-null pointer |

## One Portable Set, On Purpose

This is a **single portable set that both current GCC and Clang accept** — it is
not a per-compiler list. The reasoning is a direct consequence of how the
warnings build runs:

- TYPECHECK is a per-compiler×version stage, but the registry command is
  version-agnostic — the *same* argument list runs inside every `clang-*` and
  `gcc-*` image. The two-diagnostics win comes from the two **compilers**
  interpreting these flags, not from two different flag lists.
- Under `-Werror`, a flag that only one compiler recognizes is itself a hard
  error. GCC rejects an unknown `-W` outright, and Clang's `-Werror` promotes
  `-Wunknown-warning-option`. So every flag in the set must be one that **both**
  current GCC (>= 13) and Clang (>= 18) accept.

**Per-compiler warning sets are deferred.** Compiler-exclusive warnings — GCC's
`-Wlogical-op` and `-Wduplicated-cond`, for example — are deliberately out of
the current set. Adding them requires a compiler-branched TYPECHECK command,
which means teaching the registry a compiler dimension; that is a separate,
tracked change, not something smuggled into the portable set. When it lands,
this page is updated to describe it.

## No Standing Suppression List

There is **no project-wide standing suppression list** for compiler warnings. A
warning is fixed, not muted. The set is promoted to errors precisely so a
warning cannot accumulate as ignored noise — under `-Werror` it stops the build
until the code is corrected.

If a specific, unavoidable warning genuinely must be silenced, the suppression
is **local and narrow**: a targeted pragma around the smallest possible region,
carrying a comment that states the reason. A broad, file-level, or repository-
level suppression that hides a whole warning category is not permitted — it
defeats the reason the flag is in the set. The discipline mirrors the coverage
exclusion rule in [Testing and Coverage](testing-and-coverage.md): gaps are
acknowledged narrowly and visibly, never blanketed away.

## Relationship to the Static-Analysis Stage

The warning set is distinct from the LINT stage, which runs `clang-format`,
`clang-tidy` (via `run-clang-tidy` over the `compile_commands.json` database),
and `cppcheck` once on the primary Clang image. TYPECHECK is the compiler's own
judgment under both families; LINT is dedicated static analysis. Both gate a
change; neither substitutes for the other.
