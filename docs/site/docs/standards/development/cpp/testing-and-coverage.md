# C++ Testing and Coverage

## Core Principle

Maintain 100% line coverage **per compiler**, where 100% means: you tested
everything you could, and positively acknowledged the things you could not.

## Test Runner and Framework

Tests run through **CTest**, the mandated runner:

```bash
ctest --test-dir build --output-on-failure
```

**GoogleTest** is the documented default framework; Catch2 and doctest are
permitted alternatives. Whatever the framework, tests are registered with CTest
so the runner contract holds regardless of the choice. See the
[overview](overview.md#testing-ctest-runner-googletest-default) for the runner
rationale.

## The 100%-Per-Compiler Gate

Coverage is measured with **gcovr** and enforced at **100% line coverage, bound
per compiler (per image) — never on a single merged report**:

```bash
gcovr --config <configs>/cpp/gcovr.cfg --root . --filter src/ --fail-under-line 100
```

`--root`/`--filter` are passed on the command line, not in the packaged config
file: gcovr resolves relative paths inside a config file against the config
file's own directory, so anchoring them there pointed gcovr at the packaged
config directory and filtered all coverage out. On the command line they resolve
against the repo root (the working directory) as intended.

The image supplies the correct `gcov` executable for the compiler under test —
plain `gcov` on GCC, `llvm-cov gcov` on Clang — so each family's coverage is
computed with its own instrumentation.

**Why per-compiler, not merged.** A merged report hides gaps. If a line is
exercised under Clang but not under GCC, a single combined report still shows it
as covered, and the GCC-only gap disappears. Binding the gate to each compiler's
own report forces every line to be reached under **both** toolchains, which is
the whole point of the dual-compiler model — the two engines only both protect a
line if the tests actually run that line under both.

The coverage build is instrumented separately from the sanitizer build. TEST
does a coverage-instrumented build and run, then a **separate** build and run
under AddressSanitizer plus UndefinedBehaviorSanitizer in a distinct build
directory, so the sanitizer instrumentation never shares object files with the
coverage build.

## Exclusion Discipline

A legitimate coverage gap is acknowledged narrowly and visibly with a `gcovr`
exclusion marker: `// GCOVR_EXCL_LINE` for a single line, or a matched
`// GCOVR_EXCL_START` / `// GCOVR_EXCL_STOP` pair for a small contiguous region.

An exclusion is a **positive acknowledgment of a specific untestable branch**,
not a way to make the number go up. What separates a legitimate exclusion from
abuse:

**Legitimate** — narrow, adjacent to the untestable code, and self-explaining:

```cpp
switch (state) {
  case State::Ready:   return handleReady();
  case State::Closed:  return handleClosed();
}
// Unreachable: the enum is exhaustively handled above, but the compiler
// still emits an implicit-default path.
GCOVR_EXCL_LINE
throw std::logic_error("unreachable state");
```

A legitimate exclusion covers the smallest possible region, sits right on the
branch it excuses, and reads clearly as "this specific path cannot be exercised,
and here is why."

**Abuse** — anything that exempts more than the untestable branch itself:

- **Whole-file exemptions.** Excluding an entire file (or wrapping a whole file
  in a `START`/`STOP` pair) removes real, testable code from the denominator.
  This is the primary abuse pattern to reject in review — it converts a coverage
  gate into a coverage suggestion.
- **Unexplained markers.** An exclusion with no comment, or a vague one
  ("hard to test"), is not an acknowledgment — it is a mute. State the concrete
  reason the branch cannot be reached.
- **Exclusions standing in for missing tests.** If a branch *can* be tested,
  test it. An exclusion is for the genuinely unreachable or the genuinely
  untestable, not for code the author did not get around to covering.

The discipline is the same shape as the no-standing-suppression rule for
[compiler warnings](toolchain-and-warnings.md#no-standing-suppression-list):
gaps are acknowledged one at a time, in place, with a reason — never blanketed.

## Compiler-Specific `#ifdef` Blocks Are a Smell

Compiler-specific `#ifdef` blocks are a **code smell to minimize**. They
fragment behavior across toolchains and, in a per-compiler coverage world, they
split the code each compiler actually sees.

The good news is that clean preprocessor splits **mostly self-handle** under
per-compiler coverage. A branch that is compiled out for a given compiler is not
instrumented on that compiler's build, so it simply does not appear as a gap in
that compiler's report — no exclusion marker is needed. This is a reason to keep
such splits clean and minimal rather than to reach for exclusions:

- Prefer a portable formulation that both compilers compile the same way.
- When a split is genuinely unavoidable, keep each arm small so the compiled-in
  arm is fully testable on the compiler that sees it.
- Reserve `GCOVR_EXCL_*` for the truly unreachable, not for papering over a
  conditional block you could have written portably.

Minimizing these blocks keeps the two diagnostic engines looking at as nearly
the same code as possible, which is exactly what makes the dual-compiler gate
worth running.
