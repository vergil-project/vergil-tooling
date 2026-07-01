# CLI `--help` Convention

## Purpose

Running a tool with `--help` is the canonical way for both humans and agents to
learn what a tool does and how to invoke it. Every `vrg-*` console script must
answer `--help` with a useful description so that no one has to read source to
discover a tool's behavior or scope.

## Scope

Applies to every human-facing `vrg-*` console script declared in
`[project.scripts]`. Non-human entry points — hooks invoked by another program
rather than a person at a prompt — are exempt (see below).

## The rule

A covered tool must respond to `-h` / `--help` by:

1. Exiting `0`.
2. Printing a non-empty description to stdout that covers:
   - a one-line statement of what the tool does;
   - its scope and inputs (what it reads, and — where relevant — whether it
     operates on the current repo, its org, or something else);
   - whether it is **read-only** or **makes changes**. A tool that makes
     changes should document its `--dry-run`.

The standard mechanism is `argparse`: give the tool an `ArgumentParser` with a
`description` (and `epilog` where useful), and `-h/--help` comes for free.
Tools that take no flags still get a parser so their `--help` describes them.
Wrappers that forward to another CLI intercept `--help` themselves to explain
their wrapper-specific behavior before forwarding.

## Enforcement

`tests/vergil_tooling/test_help_coverage.py` is the gate. It enumerates every
console script from `[project.scripts]`, runs each covered tool with `--help`
in a subprocess, and asserts exit `0` with non-empty output. `--help`
short-circuits before a tool's real work, so the check is hermetic.

Two lists steer the gate:

- **`EXEMPT`** — non-human entry points with no `--help` contract (currently
  `vrg-hook-guard`, a Claude Code hook invoked with a JSON event on stdin).
- **`KNOWN_GAPS`** — tools that do not yet answer `--help`. This is a temporary
  ratchet: `test_gap_set_matches_reality` fails if the documented gaps drift
  from the source, so fixing a tool forces its removal from the list, and a new
  tool shipped without help fails until it gains a `--help` (or is added to
  `EXEMPT`). Do **not** add a tool to `KNOWN_GAPS` to silence the gate — give
  the tool a `--help` instead.
