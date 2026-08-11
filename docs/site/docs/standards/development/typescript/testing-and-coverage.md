# TypeScript Testing and Coverage

## Core Principle

Maintain 100% line coverage **per Node version**, where 100% means: you tested
everything you could, and positively acknowledged the things you could not.

> **100% means "you tested everything you could, and positively acknowledged the
> things you couldn't."** Setting the bar at 90% moves the ambiguity into the
> untracked 10% — nobody can tell whether that gap is legitimately untestable or
> merely skipped. Holding 100% + explicit markers forces every gap to be a
> deliberate, reviewable decision instead of silent forgotten code.

## Test Runner and Coverage Provider

Tests run through **Vitest**, the mandated runner, with the **V8 coverage
provider**:

```bash
vitest run --coverage --coverage.provider=v8 --coverage.thresholds.lines=100
```

The `--coverage.thresholds.lines=100` flag is the gate: Vitest exits non-zero
when line coverage drops below 100%, so anything under the bar fails the stage.

## The 100%-Per-Node-Version Gate

Coverage is enforced at **100% line coverage, bound per Node version (per image)
— never on a single merged report**. TEST is the **only** stage that fans out
across the Node matrix (v1: `node-22` and `node-24`), precisely because runtime
behavior differs across Node majors; TYPECHECK, LINT, and AUDIT run once.

**Why per-Node-version, not merged.** A merged report hides gaps. If a line is
exercised under `node-24` but not under `node-22`, a single combined report
still shows it as covered, and the `node-22`-only gap disappears. Binding the
gate to each Node version's own report forces every line to be reached under
**every** supported Node major — the whole point of testing across the matrix is
that a line is only protected on a runtime if the tests actually run it there.

The 100%-per-version gate is affordable because TypeScript support targets
**greenfield Vergil components with a limited set of use cases** — not a
grandfathered legacy import where a 100% bar would be pure friction.

### Coverage-provider contingency

The V8 provider maps native byte-range coverage back to TypeScript source
through source maps. If that mapping proves imprecise at the 100% bar (spec §4
caveat 2), the provider switches to `@vitest/coverage-istanbul`, which
instruments the source directly. That switch is proven or rejected against the
real gate in T10 — it is a known contingency, not a default.

## Exclusion Discipline

A legitimate coverage gap is acknowledged narrowly and visibly with a V8 ignore
marker: `/* v8 ignore next */` for a single line, or a matched
`/* v8 ignore start */` / `/* v8 ignore stop */` pair for a small contiguous
region.

An exclusion is a **positive acknowledgment of a specific untestable branch**,
not a way to make the number go up. Markers are reserved for
**genuinely-unreachable runtime branches** — a defensive `default:` on an
exhaustive `switch`, code after an `assertNever`, an impossible-condition guard.

**Legitimate** — narrow, adjacent to the untestable code, and self-explaining:

```ts
switch (state) {
  case State.Ready:
    return handleReady();
  case State.Closed:
    return handleClosed();
}
// Unreachable: the union is exhaustively handled above; this guard exists only
// to catch a future variant added without a matching case.
/* v8 ignore next */
throw new Error(`unreachable state: ${state as string}`);
```

A legitimate exclusion covers the smallest possible region, sits right on the
branch it excuses, and reads clearly as "this specific path cannot be exercised,
and here is why."

**Abuse** — anything that exempts more than the untestable branch itself:

- **Whole-file exemptions.** Excluding an entire file (or wrapping a whole file
  in a `start`/`stop` pair, or reaching for a coverage `exclude` glob to drop a
  source file from the denominator) removes real, testable code from the count.
  This is the primary abuse pattern to reject in review — it converts a coverage
  gate into a coverage suggestion.
- **Unexplained markers.** An `/* v8 ignore */` with no comment, or a vague one
  ("hard to test"), is not an acknowledgment — it is a mute. State the concrete
  reason the branch cannot be reached.
- **Exclusions standing in for missing tests.** If a branch *can* be tested,
  test it. An exclusion is for the genuinely unreachable or the genuinely
  untestable, not for code the author did not get around to covering.

The discipline is the same shape as the no-standing-suppression rule for
[type errors](toolchain-and-strictness.md#no-standing-suppression-list): gaps
are acknowledged one at a time, in place, with a reason — never blanketed. It is
the written norm for both human and agent authors.

## Tests Must Assert Behavior

100% coverage is necessary but not sufficient. A line counted as covered by a
test that asserts nothing is a false signal — the gate proves a line *ran*, not
that it did the right thing. Tests must assert correct behavior, not merely
execute code to satisfy the threshold.
