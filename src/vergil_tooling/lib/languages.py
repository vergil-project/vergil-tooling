"""Unified per-language metadata registry.

Defines validation commands, build/publish commands, and credential
metadata for each supported language. This is the single source of
truth for all per-language data — update one place when adding a
language.

Replaces the former ``validate_commands`` module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import files


class CheckKind(Enum):
    INSTALL = "install"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"
    # The language's release build, run before release (epic
    # vergil-project/.github#1174). It has no entry in ``Language.checks``: its
    # command is ``EcosystemInfo.build_cmd``, the one the release job runs, and
    # it runs only for a language whose ``validate_build`` is set.
    BUILD = "build"


class Cardinality(Enum):
    """How many CI gates a check kind yields across the version matrix.

    ``PER_VERSION`` — one required check per configured version (the
    default, matching every single-image language's historical behavior).
    ``ONCE`` — a single required check keyed to the primary version, for
    stages that run once per language rather than once per compiler×version
    (e.g. a matrix language's lint/audit). This keeps run-once stages from
    accumulating N redundant, merge-blocking checks in branch protection.
    """

    PER_VERSION = "per-version"
    ONCE = "once"


@dataclass(frozen=True)
class EcosystemInfo:
    build_cmd: list[str] | None
    publish_cmd: list[str] | None
    publish_env_var: str | None
    # Run ``build_cmd`` as the BUILD check in ``vrg-validate`` and in CI, so the
    # release build is proven on the PR rather than first in the release job.
    # Off by default: each language turns it on with a fixture run behind it.
    validate_build: bool = False


@dataclass(frozen=True)
class Language:
    name: str
    checks: dict[CheckKind, list[list[str]]]
    ecosystem: EcosystemInfo
    # The per-language *lock* command — the one a scaffold (or a deliberate
    # re-lock) runs to resolve and write the committed lockfile the pipeline
    # then consumes. ``None`` for a language that commits no resolved lock.
    # Kept on the registry entry (the single source of truth that already owns
    # INSTALL/LINT/TEST) rather than a new hardcoded spot; #3021 removed
    # ``conan lock create`` from INSTALL and this gives it a proper home (epic
    # vergil-project/.github#342).
    lock: list[str] | None = None
    # Per-kind gate cardinality. A kind absent from this mapping defaults to
    # ``PER_VERSION`` — so the existing single-image languages, which declare
    # nothing here, keep emitting one gate per version exactly as before. A
    # matrix language (e.g. C++) declares ``ONCE`` for the kinds it runs a
    # single time across the compiler×version matrix.
    cardinality: dict[CheckKind, Cardinality] = field(default_factory=dict)


# -- Machine-readable report paths --------------------------------------------
#
# Evidence-producing gate commands write these machine-readable reports at the
# workspace root. The filenames form the path contract shared with the T8
# evidence-producer composite (epic vergil-project/.github#140): the composite
# globs these exact names to bundle real report data instead of empty
# envelopes. Keep this list in sync with the T8 composite glob.
#
# RUFF_REPORT and MYPY_REPORT close the ``quality`` gate's evidence gap: lint
# and typecheck previously emitted no report files, so the producer bundled an
# empty payload for that gate. These two filenames are the shared glob contract
# with the T8 evidence-producer composite (vergil-actions#836) — a locked
# contract with the producer bridge PR, so keep the names exact.
RUFF_REPORT = "quality-ruff.json"
MYPY_REPORT = "quality-mypy.xml"
COVERAGE_REPORT = "coverage.xml"
JUNIT_REPORT = "junit.xml"
PIP_AUDIT_REPORT = "pip-audit.json"
LICENSES_REPORT = "licenses.json"

# The workspace-root report files the Python gate commands emit. Exposed as a
# single source of truth so consumers (and tests) can assert the contract
# without re-deriving the individual filenames.
PYTHON_REPORT_FILES = (
    RUFF_REPORT,
    MYPY_REPORT,
    COVERAGE_REPORT,
    JUNIT_REPORT,
    PIP_AUDIT_REPORT,
    LICENSES_REPORT,
)

# cargo-deny's JSON diagnostics from the rust AUDIT gate's own run, the audit
# gate's evidence (vergil-project/vergil-tooling#4394). The name is the
# contract with vergil-actions' ``ci-audit.yml``, which stages it for rust.
# cargo-deny writes JSON Lines (one object per line) to stderr and has no
# output-file flag, so the AUDIT command redirects its stderr here.
CARGO_DENY_REPORT = "cargo-deny.json"

# The rust ``quality`` gate's evidence (epic vergil-project/.github#1165, spec
# §6.5): clippy's and ``cargo check``'s ``--message-format=json`` output, from
# the LINT and TYPECHECK gates' own runs. The names are the contract with
# vergil-actions' ``reports.sh`` (its ``rust/lint`` and ``rust/typecheck``
# entries), beside RUFF_REPORT and MYPY_REPORT, so keep them exact.
CLIPPY_REPORT = "quality-clippy.json"
CARGO_CHECK_REPORT = "quality-cargo-check.json"

# The workspace-root report files the Rust gate commands emit: the LINT and
# TYPECHECK gates' cargo JSON messages, the TEST gate's Cobertura coverage and
# cargo-nextest JUnit report (vergil-project/vergil-tooling#4394, #4395) and the
# AUDIT gate's cargo-deny diagnostics.
RUST_REPORT_FILES = (
    CLIPPY_REPORT,
    CARGO_CHECK_REPORT,
    COVERAGE_REPORT,
    JUNIT_REPORT,
    CARGO_DENY_REPORT,
)

# The TypeScript gates' evidence (epic vergil-project/.github#1174, spec §6.6),
# each from the gate's own run: ESLint's JSON formatter output from LINT, the
# diagnostics of ``tsc`` from TYPECHECK and ``npm audit --json`` from AUDIT.
# TEST writes COVERAGE_REPORT and JUNIT_REPORT, as for Python and Rust. The
# names are the contract with vergil-actions' ``reports.sh`` (its
# ``typescript/*`` entries), so keep them exact.
ESLINT_REPORT = "quality-eslint.json"
TSC_REPORT = "quality-tsc.txt"
NPM_AUDIT_REPORT = "npm-audit.json"

# The workspace-root report files the TypeScript gate commands emit.
TYPESCRIPT_REPORT_FILES = (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    NPM_AUDIT_REPORT,
    ESLINT_REPORT,
    TSC_REPORT,
)

# The pip-audit input: every package pinned in ``uv.lock`` — all dependency
# groups and extras, not just the synced ``dev`` environment (issue #3498). An
# installed-environment audit never saw a ``docs``-only pin, so a vulnerable
# mkdocs-material/pymdown-extensions pin stayed green. It is an intermediate
# file, not evidence, so it lives under the already-ignored ``.vergil/`` (``uv
# export`` creates the directory) rather than widening the managed gitignore.
PIP_AUDIT_REQUIREMENTS = ".vergil/pip-audit-requirements.txt"


# -- License allowlists (unchanged from validate_commands) --------------------

_PIP_LICENSES_ALLOWLIST = ";".join(
    [
        "Apache-2.0",
        "Apache-2.0 AND CNRI-Python",
        "Apache-2.0 OR BSD-2-Clause",
        "Apache-2.0 OR BSD-3-Clause",
        "Apache Software License",
        "BSD License",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC License (ISCL)",
        "MIT",
        # MIT without its attribution clause, more permissive than MIT
        # (cffi 2.x, which cryptography needs on Python 3.14+; #4494).
        "MIT-0",
        "MIT License",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later",
        "PSF-2.0",
        "Public Domain",
        "Python Software Foundation License",
    ]
)

_GO_LICENSES_ALLOWLIST = ",".join(
    [
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0",
        "ISC",
        "MIT",
        "MPL-2.0",
    ]
)

# Rust / cargo-deny license allowlist (epic vergil-project/.github#1091). Unlike
# the joined strings above, this stays a ``list[str]``: it is the ``[licenses]``
# ``allow`` array of the packaged ``configs/rust/deny.toml``, which the rust
# AUDIT command passes with ``--config``. A test pins the two equal. cargo-deny
# evaluates each crate's SPDX expression against it, so ``MIT OR Apache-2.0``
# passes when either term is allowed. An exception is a distinct license, so
# ``Apache-2.0 WITH LLVM-exception`` is listed in its own right.
_RUST_LICENSES_ALLOWLIST = [
    "Apache-2.0",
    "Apache-2.0 WITH LLVM-exception",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSL-1.0",
    "ISC",
    "MIT",
    "MPL-2.0",
    "Unicode-3.0",
    "Zlib",
]

# The rust AUDIT command (issue #4394). One ``cargo deny check`` run both gates
# and writes the evidence: ``--format json`` (a root option, like ``--config``)
# makes its diagnostics JSON Lines, and ``check --show-stats`` adds a closing
# ``summary`` record even on a clean run, so the report is never empty. Neither
# flag changes what is checked. cargo-deny writes diagnostics to stderr and has
# no output-file flag, so the shell sends stderr to the report, prints the
# report back to stderr for the log, and exits with cargo-deny's own status.
# ``2>`` is plain POSIX ``sh``: ``tee`` would need ``pipefail`` to keep that
# status, which the image's ``sh`` (dash) lacks. ``cat`` failing fails the gate.
# ``--locked`` is a root option too: the audit reads the committed ``Cargo.lock``
# as it is (epic vergil-project/.github#1165, spec §6.1). So is ``--workspace``,
# which makes every workspace member a root of the crate graph, so the audit
# covers each member's dependencies, not only the root package's (spec §6.4).
_RUST_AUDIT_SCRIPT = (
    'cargo deny --format json --config "{configs}/rust/deny.toml" --locked --workspace '
    "check --show-stats "
    f"2>{CARGO_DENY_REPORT}; "
    "rc=$?; "
    f"cat {CARGO_DENY_REPORT} >&2 || exit 1; "
    'exit "$rc"'
)

# The rust LINT clippy and TYPECHECK commands (epic vergil-project/.github#1165,
# spec §6.5). One cargo run both gates and writes the report:
# ``--message-format=json`` sends cargo's JSON messages (one object per line) to
# stdout, which the shell writes to the report. The compiler's diagnostics are
# then only in that JSON, so once cargo exits ``jq`` prints each
# ``compiler-message``'s ``message.rendered`` text, the diagnostic as cargo
# would have shown it, to stderr for the log; cargo's own status lines stay on
# its stderr, so they come first. The JSON holds a diagnostic once per target
# that hit it (``--all-targets`` builds the lib and the lib's tests from the
# same source), where cargo's human output prints identical diagnostics once,
# so the filter prints each distinct rendered text once, in first-seen order.
# The command exits with cargo's status, so a failing lint or check still
# writes its report. ``jq`` failing (missing, or the report unreadable) fails
# the gate; jq is in every vergil-containers image (its ``base`` layer). The
# ``>`` redirect replaces any stale report before cargo runs. Plain POSIX
# ``sh`` (dash).
_CARGO_RENDER_FILTER = (
    '[inputs | select(.reason == "compiler-message") | .message.rendered // empty] '
    "| reduce .[] as $r ([]; if any(.[]; . == $r) then . else . + [$r] end) "
    "| .[]"
)


def _rust_json_report_script(cargo_args: str, report: str) -> str:
    """The ``sh -c`` script that runs *cargo_args* and writes its JSON to *report*."""
    return (
        f"{cargo_args} >{report}; "
        "rc=$?; "
        f"jq -j -n '{_CARGO_RENDER_FILTER}' {report} >&2 || exit 1; "
        'exit "$rc"'
    )


# ``--all-targets`` is ``--lib --bins --tests --benches --examples``; with no
# target flag clippy checks only the lib and bins, so ``tests/`` and
# ``#[cfg(test)]`` code went unlinted (vergil-project/vergil-tooling#4379).
# ``--message-format`` is cargo's option, so it goes before the ``--`` that
# starts clippy's own.
_RUST_CLIPPY_SCRIPT = _rust_json_report_script(
    "cargo clippy --all-targets --locked --workspace --message-format=json -- -D warnings",
    CLIPPY_REPORT,
)
_RUST_CHECK_SCRIPT = _rust_json_report_script(
    "cargo check --locked --workspace --message-format=json",
    CARGO_CHECK_REPORT,
)

# The rust doctest command, over every workspace member (spec §6.4).
# vrg-validate drops exactly this command from the TEST stage when no workspace
# member has a library target (``_skip_rust_doctests_without_lib``). Both sides
# use this one list, so a flag added here can never make the skip stop matching.
RUST_DOCTEST_CMD = ["cargo", "test", "--doc", "--locked", "--workspace"]

# The rust TEST coverage command (issue #4395). One cargo-nextest run under
# cargo-llvm-cov, then one report from that run's profiles; it gates and writes
# both reports:
#
# * ``cargo llvm-cov clean --workspace`` first, then ``nextest --no-report``
#   and ``report``: cargo-llvm-cov's documented split. ``--no-report`` turns off
#   the clean a combined run does, so the explicit clean replaces it
#   (https://github.com/taiki-e/cargo-llvm-cov#basic-usage and
#   #merge-coverages-generated-under-different-test-conditions). The split writes
#   ``coverage.xml`` even when a test fails, as pytest-cov does; a combined
#   run writes no report then.
# * ``NEXTEST_PROFILE=vergil`` and ``--tool-config-file vergil:<file>`` select
#   the packaged ``configs/rust/nextest.toml`` profile, which turns on JUnit.
#   A tool config ranks below the repo's own ``.config/nextest.toml``
#   (https://nexte.st/docs/configuration/#tool-specific-configuration).
#   cargo-llvm-cov passes nextest's options through (``cargo llvm-cov nextest
#   [OPTIONS] [NEXTEST_OPTIONS]``). The profile is named by nextest's own
#   ``NEXTEST_PROFILE`` (https://nexte.st/docs/running/), not ``--profile``,
#   which cargo-llvm-cov documents as cargo's build profile.
# * ``--locked`` on the test run and on the report, which both read
#   ``Cargo.lock``, so a stale lockfile fails the gate instead of being
#   re-resolved.
# * ``--workspace`` on the test run and on the report, so every workspace
#   member's tests run and every member's sources are gated (spec §6.4). A
#   report without it covers only the root package's sources, even when the
#   test run had ``--workspace``: a live run of cargo-llvm-cov 0.9.1 on a
#   workspace with a binary root and a library member reported only
#   ``src/main.rs``. ``report --help`` does not list the flag, but the report
#   accepts it.
# * ``report --fail-under-lines 100 --fail-under-regions 100`` is the gate on
#   llvm-cov's own line and region summary (epic vergil-project/.github#1165,
#   spec §6.2); ``--cobertura --output-path`` writes ``coverage.xml`` and
#   ``--show-missing-lines`` names the uncovered lines. A missed region on a
#   line that ran is not among them, so when this command fails vrg-validate
#   runs :func:`rust_coverage_export_cmd` and lists every uncovered region
#   (:mod:`vergil_tooling.lib.rust_coverage`).
# * Nextest writes the JUnit report under its store directory,
#   ``target/nextest/<profile>/`` (https://nexte.st/docs/machine-readable/junit/,
#   ``store.dir`` in https://nexte.st/docs/configuration/reference/); the
#   command copies it to ``junit.xml`` at the root. All three report paths are
#   removed first, so a stale report from an earlier run is never staged. A
#   run whose tests passed but left no JUnit report (a repo that moved
#   ``store.dir``) fails the gate.
#
# The command exits with the test run's status if it failed, else the
# report's. Plain POSIX ``sh`` (the image's ``sh`` is dash).
_NEXTEST_PROFILE = "vergil"
_NEXTEST_JUNIT = f"target/nextest/{_NEXTEST_PROFILE}/{JUNIT_REPORT}"
_RUST_TEST_SCRIPT = (
    f"rm -f {COVERAGE_REPORT} {JUNIT_REPORT} {_NEXTEST_JUNIT}; "
    "cargo llvm-cov clean --workspace || exit 1; "
    f"NEXTEST_PROFILE={_NEXTEST_PROFILE} cargo llvm-cov nextest --locked --workspace "
    '--no-report --tool-config-file "vergil:{configs}/rust/nextest.toml"; '
    "test_rc=$?; "
    "cargo llvm-cov report --locked --workspace --fail-under-lines 100 --fail-under-regions 100 "
    "--show-missing-lines "
    f"--cobertura --output-path {COVERAGE_REPORT}; "
    "cov_rc=$?; "
    f"if [ -f {_NEXTEST_JUNIT} ]; then cp {_NEXTEST_JUNIT} {JUNIT_REPORT} || exit 1; "
    'elif [ "$test_rc" -eq 0 ]; then '
    f'echo "error: cargo-nextest wrote no JUnit report at {_NEXTEST_JUNIT}" >&2; exit 1; '
    "fi; "
    '[ "$test_rc" -ne 0 ] && exit "$test_rc"; '
    'exit "$cov_rc"'
)


def rust_coverage_export_cmd(output_path: str) -> list[str]:
    """The JSON export of the Rust TEST gate's coverage report, to *output_path*.

    It reads the profiles the gate's nextest run left, with the gate's own
    ``--locked --workspace``, so it covers the sources the gate judged.
    """
    return [
        "cargo",
        "llvm-cov",
        "report",
        "--locked",
        "--workspace",
        "--json",
        "--output-path",
        output_path,
    ]


# C++ license allowlist (epic vergil-project/.github#207 §4). C++/Conan has no
# turnkey allowlist-enforcing gate the way pip-licenses / go-licenses do, so v1
# license checking is *best-effort surfacing* (see the AUDIT commands below) and
# hardened gating is deferred (spec §9 ledger #7). This constant is the reviewed
# allowlist that the C++ standards docs (T9) reference and that a future hardened
# gate will enforce; keeping it here alongside the other language allowlists
# keeps the single-source-of-truth contract intact.
_CPP_LICENSES_ALLOWLIST = ";".join(
    [
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSL-1.0",
        "ISC",
        "MIT",
        "MPL-2.0",
        "Unlicense",
        "Zlib",
    ]
)

# TypeScript / npm license allowlist (epic vergil-project/.github#284 §4).
# Mirrors the C++ pattern exactly: npm has no turnkey allowlist-enforcing gate
# with reliable metadata (npm package license fields are inconsistent), so v1
# license checking is *best-effort surfacing* (see the AUDIT commands below) and
# hardened gating is deferred (spec §9 ledger #7). This constant is the reviewed
# allowlist the TypeScript standards docs (T8) reference and that a future
# hardened gate (e.g. ``license-checker --onlyAllow``, which consumes this exact
# ``;``-separated form) will enforce; keeping it here alongside the other
# language allowlists preserves the single-source-of-truth contract.
_TYPESCRIPT_LICENSES_ALLOWLIST = ";".join(
    [
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MPL-2.0",
        "Python-2.0",
        "Unlicense",
    ]
)

# The TypeScript LINT ESLint, TYPECHECK, TEST and AUDIT commands (epic
# vergil-project/.github#1174, spec §6.6). Each runs its tool once, and that
# one run both gates and writes the report, so the evidence is the gate's own
# result. Each exits with the tool's status, so a failing gate still writes its
# report, and each prints the tool's findings to the log. Plain POSIX ``sh``
# (the image's ``sh`` is dash, with no ``pipefail``, so no ``tee``).
#
# ESLint takes one formatter per run, so ``--format json --output-file`` writes
# the report and prints nothing; once ESLint exits, ``jq`` prints each message
# from the report as ``path:line:column: severity message [rule]``, the path
# relative to the repo root. A stale report is removed first. An ESLint run that
# fails before linting (a broken config) writes no report and has printed its
# own error, so the gate exits with ESLint's status; one that passes with no
# report fails the gate. ``jq`` failing fails the gate; jq is in every
# vergil-containers image (its ``base`` layer).
_ESLINT_RENDER_FILTER = (
    ".[] | (.filePath | ltrimstr($root)) as $f | .messages[] "
    '| "\\($f):\\(.line // 0):\\(.column // 0): '
    '\\(if .severity == 2 then "error" else "warning" end) '
    '\\(.message) [\\(.ruleId // "fatal")]"'
)
_TYPESCRIPT_ESLINT_SCRIPT = (
    f"rm -f {ESLINT_REPORT}; "
    f"node_modules/.bin/eslint . --format json --output-file {ESLINT_REPORT}; "
    "rc=$?; "
    f"if [ -f {ESLINT_REPORT} ]; then "
    f"jq -r --arg root \"$PWD/\" '{_ESLINT_RENDER_FILTER}' {ESLINT_REPORT} || exit 1; "
    'elif [ "$rc" -eq 0 ]; then '
    f'echo "error: ESLint wrote no report at {ESLINT_REPORT}" >&2; exit 1; '
    "fi; "
    'exit "$rc"'
)

# ``tsc`` writes its diagnostics to stdout, one per line with
# ``--pretty false`` (``file(line,col): error TSnnnn: message``), so the shell
# writes stdout to the report and prints the report back for the log.
# ``--diagnostics`` adds the compiler's closing statistics (files and lines
# checked, timings) after the diagnostics, so a clean typecheck still writes a
# report with real data, as cargo-deny's ``--show-stats`` does for rust AUDIT.
# Neither flag changes what is checked. ``cat`` failing fails the gate.
_TYPESCRIPT_TSC_SCRIPT = (
    f"node_modules/.bin/tsc --noEmit --pretty false --diagnostics >{TSC_REPORT}; "
    "rc=$?; "
    f"cat {TSC_REPORT} || exit 1; "
    'exit "$rc"'
)

# Vitest writes ``junit.xml`` through its JUnit reporter, beside the default
# console reporter, and the Cobertura coverage report beside the ``text``
# table. Istanbul's Cobertura reporter names its file
# ``cobertura-coverage.xml`` under the reports directory, which the command
# fixes as ``coverage/`` (Vitest empties the reports directory before each
# run, so it must never be the repo root), then copies it to ``coverage.xml``. Vitest writes no coverage
# when a test fails unless ``--coverage.reportOnFailure`` is set. The two root
# reports and the Cobertura file are removed first, so a stale one is never
# staged; a run that passes but leaves either report missing fails the gate.
_VITEST_COBERTURA = "coverage/cobertura-coverage.xml"
_TYPESCRIPT_TEST_SCRIPT = (
    f"rm -f {COVERAGE_REPORT} {JUNIT_REPORT} {_VITEST_COBERTURA}; "
    "node_modules/.bin/vitest run --coverage --coverage.provider=v8 "
    "--coverage.thresholds.lines=100 --coverage.reportOnFailure "
    "--coverage.reportsDirectory=coverage "
    "--coverage.reporter=text --coverage.reporter=cobertura "
    f"--reporter=default --reporter=junit --outputFile.junit={JUNIT_REPORT}; "
    "rc=$?; "
    f"if [ -f {_VITEST_COBERTURA} ]; then cp {_VITEST_COBERTURA} {COVERAGE_REPORT} || exit 1; "
    'elif [ "$rc" -eq 0 ]; then '
    f'echo "error: Vitest wrote no Cobertura report at {_VITEST_COBERTURA}" >&2; exit 1; '
    "fi; "
    f'if [ "$rc" -eq 0 ] && [ ! -f {JUNIT_REPORT} ]; then '
    f'echo "error: Vitest wrote no JUnit report at {JUNIT_REPORT}" >&2; exit 1; '
    "fi; "
    'exit "$rc"'
)

# ``npm audit --json`` writes the report to stdout and keeps the
# ``--audit-level`` exit status, so the shell writes it to the report, prints
# it back for the log, and exits with npm's status. ``cat`` failing fails the
# gate.
_TYPESCRIPT_AUDIT_SCRIPT = (
    f"npm audit --audit-level=high --json >{NPM_AUDIT_REPORT}; "
    "rc=$?; "
    f"cat {NPM_AUDIT_REPORT} || exit 1; "
    'exit "$rc"'
)

# -- C++ warning set (epic vergil-project/.github#207 §3.2, owned by T5) -------
#
# "Warnings to 11" resolved to a concrete, reviewable list. The floor is
# ``-Wall -Wextra -Werror -Wpedantic``; the curated extras below extend it.
#
# **One portable set, on purpose.** TYPECHECK is a per-compiler×version stage,
# but ``language_commands`` is version-agnostic — the *same* argv runs inside
# every ``clang-*`` and ``gcc-*`` image, and the two-diagnostics win comes from
# the two *compilers* interpreting these flags, not from two flag lists. Under
# ``-Werror`` an option only one compiler recognizes is itself a hard error
# (GCC rejects an unknown ``-W`` outright; Clang's ``-Werror`` promotes
# ``-Wunknown-warning-option``), so every flag here is one **both** current
# GCC (>=13) and Clang (>=18) accept. Compiler-exclusive gems (GCC's
# ``-Wlogical-op`` / ``-Wduplicated-cond``, etc.) are therefore deliberately
# out of the v1 set; adding a compiler-branched TYPECHECK command is a separate
# change (would need the registry to gain a compiler dimension) and is noted for
# the deferral ledger, not smuggled in here. T9 *documents* this set; it does
# not re-choose it.
_CPP_WARNING_FLAGS = [
    # floor (spec §3.2)
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wpedantic",
    # curated extras (spec §3.2 candidate baseline + closely-related companions)
    "-Wshadow",
    "-Wconversion",
    "-Wsign-conversion",
    "-Wcast-qual",
    "-Wold-style-cast",
    "-Wnon-virtual-dtor",
    "-Woverloaded-virtual",
    "-Wdouble-promotion",
    "-Wformat=2",
    "-Wimplicit-fallthrough",
    "-Wnull-dereference",
]
# The warning flags are threaded into the build as one ``CMAKE_CXX_FLAGS`` cache
# value (a single space-joined string), so the whole set lands on every
# translation unit's compile line.
_CPP_WARNINGS_CXX_FLAGS = " ".join(_CPP_WARNING_FLAGS)

# CMake build directories. INSTALL configures ``build`` (its
# ``compile_commands.json`` feeds the LINT clang-tidy pass); TYPECHECK reuses it
# for the warnings build; TEST does its ASan/UBSan build out-of-tree in
# ``build-sanitize`` so the sanitizer instrumentation never shares object files
# with the coverage build (spec §4: "the instrumentations do not share a build").
_CPP_BUILD_DIR = "build"
_CPP_SANITIZE_BUILD_DIR = "build-sanitize"

# Conan's generator output is written under ``build/`` (via ``conan install
# --output-folder=build``), not the source root, so the working tree stays clean
# after a build — retiring the gitignore whack-a-mole that chased Conan's
# root-dropped ``conan_toolchain.cmake`` / CMakeDeps ``Find*.cmake`` /
# ``*Config.cmake`` files (#2878, #2908). With the output relocated, every cmake
# *configure* must point at the relocated toolchain file so ``find_package``
# resolves from ``build/`` instead of the old source-root ``CMAKE_PREFIX_PATH``
# hack. A plain ``--output-folder=build`` writes ``conan_toolchain.cmake``
# directly under ``build/``, so the path is ``build/conan_toolchain.cmake``. The
# sanitizer configure (``-B build-sanitize``) intentionally references this same
# shared file — INSTALL runs ``conan install`` once and creates it first. (#2912)
_CPP_TOOLCHAIN_FILE = "build/conan_toolchain.cmake"

# The canonical parallel-worktree container (``<root>/.worktrees/``). cpp LINT
# runs from the repo root, so both file-walking linters (clang-format's find
# driver, cppcheck) must exclude it — a validation of one worktree must never
# read a sibling worktree's sources or build tree (#2906).
_WORKTREES_DIR = ".worktrees"

# -- Python TEST command (epic vergil-project/.github#333, Task 6) -------------
#
# The Python TEST command is *computed* by ``build_python_test_argv`` rather than
# stored as a fixed list: parallelism (pytest-xdist's work-stealing scheduler) is
# appended at call time from (xdist availability, ``[test].parallel`` intent).
# This constant is the single source of truth for the base gate argv — the flags
# present in **every** invocation, serial or parallel. The coverage gate
# (``--cov-branch --cov-fail-under=100``) is inviolate: it is here, so it is
# present in every computed command.
#
# Two levers the epic originally planned for this command were dropped after the
# Phase-0 evidence and must never appear here:
#   * ``COVERAGE_CORE=sysmon`` (Task 4) — inert under ``--cov-branch`` (coverage.py
#     silently falls back to the C tracer), so it was misleading, not a speedup.
#   * ``--import-mode=importlib`` (Task 10) — no measured speedup and the suite is
#     not importlib-safe.
_PYTHON_TEST_BASE_ARGV: tuple[str, ...] = (
    "pytest",
    "--cov=src",
    "--cov-branch",
    "--cov-fail-under=100",
    "--cov-report=term-missing",
    f"--cov-report=xml:{COVERAGE_REPORT}",
    f"--junitxml={JUNIT_REPORT}",
)


def build_python_test_argv(
    *, xdist_available: bool, parallel: bool, workers: int | str = "auto"
) -> list[str]:
    """Compute the Python ``pytest`` TEST argv.

    Starts from :data:`_PYTHON_TEST_BASE_ARGV` (which always carries the
    ``--cov-branch --cov-fail-under=100`` coverage gate) and appends
    ``-n <workers> --dist worksteal`` — pytest-xdist's work-stealing
    distribution — **iff** ``xdist_available and parallel``. ``workers`` is the
    resolved ``[test]`` worker count (``"auto"`` or a positive int; issue
    #3322). When xdist is not installed, or the repo opts out via
    ``[test].parallel = false``, the command runs serially with no error.

    Returns a plain ``list[str]`` — just the argv. No environment overlay is
    returned: the ``COVERAGE_CORE=sysmon`` lever (Task 4) and the
    ``--import-mode=importlib`` lever (Task 10) were both dropped, so neither a
    sysmon env var nor an import-mode flag is ever emitted.
    """
    argv = list(_PYTHON_TEST_BASE_ARGV)
    if xdist_available and parallel:
        argv += ["-n", str(workers), "--dist", "worksteal"]
    return argv


# -- Registry -----------------------------------------------------------------

_REGISTRY: dict[str, Language] = {
    "python": Language(
        name="python",
        checks={
            CheckKind.INSTALL: [["uv", "sync", "--frozen", "--group", "dev"]],
            CheckKind.LINT: [
                ["ruff", "check", "src/", "tests/"],
                ["ruff", "format", "--check", "src/", "tests/"],
                [
                    "ruff",
                    "check",
                    "--output-format=json",
                    f"--output-file={RUFF_REPORT}",
                    "src/",
                    "tests/",
                ],
            ],
            CheckKind.TYPECHECK: [
                ["mypy", "src/", "--junit-xml", MYPY_REPORT],
                ["ty", "check", "src", "tests"],
            ],
            # The declarative base gate argv. The command actually run is
            # *computed* by ``build_python_test_argv`` (invoked from
            # ``language_commands``), which appends xdist work-stealing when
            # available and not opted out. Keeping the base here preserves the
            # registry as the single source of the gate flags.
            CheckKind.TEST: [list(_PYTHON_TEST_BASE_ARGV)],
            CheckKind.AUDIT: [
                ["uv", "sync", "--check", "--frozen", "--group", "dev"],
                ["uv", "lock", "--check"],
                # Audit the whole lock, not the installed env (issue #3498).
                # ``--no-emit-project`` / ``--no-emit-local`` drop the project
                # itself and local path packages (nothing on an index to audit).
                # Hashes stay in the export so pip-audit can run
                # ``--disable-pip`` under ``--require-hashes``: any other
                # unhashed requirement (a VCS or URL dependency) fails the audit
                # loudly instead of being skipped. ``--quiet`` stops uv echoing
                # the export to stdout. Install and license check stay on ``dev``.
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--all-groups",
                    "--all-extras",
                    "--no-emit-project",
                    "--no-emit-local",
                    "--format=requirements.txt",
                    "--quiet",
                    f"--output-file={PIP_AUDIT_REQUIREMENTS}",
                ],
                [
                    "pip-audit",
                    f"--requirement={PIP_AUDIT_REQUIREMENTS}",
                    "--disable-pip",
                    "--require-hashes",
                    "--format=json",
                    f"--output={PIP_AUDIT_REPORT}",
                ],
                [
                    "pip-licenses",
                    f"--allow-only={_PIP_LICENSES_ALLOWLIST}",
                    "--format=json",
                    f"--output-file={LICENSES_REPORT}",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["uv", "build"],
            publish_cmd=["uv", "publish"],
            publish_env_var=None,
        ),
    ),
    "go": Language(
        name="go",
        checks={
            CheckKind.INSTALL: [["go", "mod", "download"]],
            CheckKind.LINT: [
                ["golangci-lint", "run", "./..."],
                ["gocyclo", "-over", "15", "."],
            ],
            CheckKind.TYPECHECK: [["go", "vet", "./..."]],
            CheckKind.TEST: [
                ["go", "test", "-race", "-count=1", "-coverprofile=coverage.out", "./..."],
                ["go-test-coverage", "--config", ".testcoverage.yml"],
            ],
            CheckKind.AUDIT: [
                ["govulncheck", "./..."],
                [
                    "go-licenses",
                    "check",
                    "./...",
                    f"--allowed_licenses={_GO_LICENSES_ALLOWLIST}",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["go", "build", "./..."],
            publish_cmd=None,
            publish_env_var=None,
        ),
    ),
    "rust": Language(
        name="rust",
        # ``--locked`` on every command that reads ``Cargo.lock``, so the
        # committed lockfile is used as it is and a stale one fails rather
        # than being re-resolved (epic vergil-project/.github#1165, spec §6.1).
        # ``cargo fmt`` reads no lockfile and takes none. vrg-validate fails a
        # missing ``Cargo.lock`` before the install stage runs. ``--workspace``
        # on clippy, check, the coverage run, the doctests and the audit, so a
        # workspace whose root is a package validates its member crates too,
        # not only the root (spec §6.4). ``cargo fmt --all`` already formats
        # every member, and ``cargo fetch`` resolves the whole lockfile.
        checks={
            CheckKind.INSTALL: [["cargo", "fetch", "--locked"]],
            CheckKind.LINT: [
                ["cargo", "fmt", "--all", "--", "--check"],
                # clippy writes ``quality-clippy.json``; see
                # ``_RUST_CLIPPY_SCRIPT``.
                ["sh", "-c", _RUST_CLIPPY_SCRIPT],
            ],
            # ``cargo check`` writes ``quality-cargo-check.json``; see
            # ``_RUST_CHECK_SCRIPT``.
            CheckKind.TYPECHECK: [["sh", "-c", _RUST_CHECK_SCRIPT]],
            # The coverage run is cargo-nextest under cargo-llvm-cov, gating
            # on 100% lines and regions and writing ``coverage.xml`` and ``junit.xml``
            # (issues #4394, #4395); see ``_RUST_TEST_SCRIPT``. Nextest runs
            # no doctests (https://nexte.st/docs/running/), so ``cargo test
            # --doc`` runs them as a second command. ``cargo llvm-cov
            # --doctests`` is not used: it passes rustdoc ``-Z
            # unstable-options --persist-doctests``, which stable rustc
            # refuses, and the dev image ships stable only. Doctests
            # therefore count toward no coverage; the 100% gate is met
            # by the unit and integration tests alone (issue #4378). ``cargo
            # test --doc`` refuses a crate with no library target, so
            # vrg-validate drops it when no workspace member has one
            # (``_skip_rust_doctests_without_lib``).
            CheckKind.TEST: [
                ["sh", "-c", _RUST_TEST_SCRIPT],
                list(RUST_DOCTEST_CMD),
            ],
            # The packaged fleet policy (epic vergil-project/.github#1091). An
            # explicit ``--config`` replaces cargo-deny's search for a repo's
            # own ``deny.toml``, so every Rust repo audits against the fleet
            # policy. cargo-deny 0.20 takes ``--config`` on its root command,
            # before ``check``. See ``_RUST_AUDIT_SCRIPT`` for the report.
            CheckKind.AUDIT: [["sh", "-c", _RUST_AUDIT_SCRIPT]],
        },
        ecosystem=EcosystemInfo(
            # The release build, run by the release job and, as the BUILD
            # check, by vrg-validate, so the --release profile (its
            # optimisation and its cfg(debug_assertions) code) is built
            # before the release PR merges (spec §6.9). --workspace builds
            # every member, as every other Rust gate does (spec §6.4).
            build_cmd=["cargo", "build", "--release", "--locked", "--workspace"],
            publish_cmd=["cargo", "publish", "--locked"],
            publish_env_var="CARGO_REGISTRY_TOKEN",
            validate_build=True,
        ),
    ),
    # C++ (epic vergil-project/.github#207, T5). CMake + Conan 2; two co-equal
    # open-source compilers (Clang primary, GCC secondary) run as independent
    # diagnostic engines on the containerized multi-version model.
    #
    # **Cache-var contract.** vergil-tooling passes *intent* as CMake cache vars
    # and the consuming project's ``CMakeLists.txt`` translates them
    # per-compiler (this keeps every command portable across both families —
    # e.g. libc++'s ``-stdlib`` is Clang-only, so it must not be a raw flag
    # here):
    #   ``VERGIL_CPP_STD``      — the ``[cpp].std`` value (e.g. ``c++20``)
    #   ``VERGIL_CPP_STDLIB``   — the ``[cpp].stdlib`` value (e.g. ``libstdc++``)
    #   ``VERGIL_CPP_COVERAGE`` — ``ON`` selects coverage instrumentation
    #   ``VERGIL_CPP_SANITIZE`` — sanitizer list (``address,undefined``)
    # ``{std}`` / ``{stdlib}`` are expanded by ``language_commands`` from the
    # parsed ``[cpp]`` block (defaulting to the v1 pins ``c++20`` / ``libstdc++``).
    "cpp": Language(
        name="cpp",
        checks={
            # ``conan.lock`` is a COMMITTED input consumed via ``--lockfile``,
            # never regenerated here. Regenerating it on every run (the old
            # ``conan lock create`` step) dirtied the working tree — the lock is
            # committed, not gitignored, like ``uv.lock`` (gitignore.py) — and
            # defeated reproducibility by re-resolving the graph against current
            # remote state each run. Developers refresh the lock deliberately
            # with ``conan lock create . -s build_type=Debug`` (like ``uv lock``);
            # validation only consumes it. ``-s build_type=Debug`` stays on
            # ``conan install`` so Conan builds deps in the *same* configuration
            # as the CMake coverage/sanitizer builds — the container image only
            # bakes a default profile (build_type=Release), and that mismatch
            # broke the T11 cold rebuild (#2558): ``fmt/format.h`` not found
            # because the Debug config had no matching Conan binary. (#3021;
            # #2572, image side #487)
            CheckKind.INSTALL: [
                [
                    "conan",
                    "install",
                    ".",
                    "-s",
                    "build_type=Debug",
                    "--build=missing",
                    "--lockfile=conan.lock",
                    # Write Conan's generator output (toolchain file, CMakeDeps
                    # config files, env scripts, CMakePresets.json) under the
                    # already-ignored ``build/`` instead of the source root, so
                    # the working tree stays clean after a build. (#2912)
                    f"--output-folder={_CPP_BUILD_DIR}",
                ],
                [
                    "cmake",
                    "-S",
                    ".",
                    "-B",
                    _CPP_BUILD_DIR,
                    "-DCMAKE_BUILD_TYPE=Debug",
                    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                    "-DVERGIL_CPP_STD={std}",
                    "-DVERGIL_CPP_STDLIB={stdlib}",
                    f"-DCMAKE_TOOLCHAIN_FILE={_CPP_TOOLCHAIN_FILE}",
                ],
            ],
            # LINT runs *once* on the primary Clang image (§3.6). No shell is
            # available (commands exec directly), so each tool self-discovers:
            # cppcheck walks ``.``; run-clang-tidy reads the compile DB; only
            # clang-format needs a file list, so it uses an explicit ``sh -c``
            # find/xargs driver.
            #
            # The driver prunes *directories* named ``build``/``build-sanitize``
            # at any depth — not just ``./build`` at the top level — plus
            # ``.worktrees`` entirely. A root-level validation must never read
            # another worktree's tree or a nested CMake output dir: the old
            # ``-path ./build`` prune matched only the top level, so a develop
            # validation run while a sibling ``.worktrees/<name>/`` existed
            # descended into it and linted CMake's generated compiler-probe
            # file (``CMakeCXXCompilerId.cpp``), failing a healthy develop on
            # another worktree's build artifacts (#2906).
            CheckKind.LINT: [
                [
                    "sh",
                    "-c",
                    "find . -type d \\( -name build -o -name build-sanitize "
                    "-o -name .worktrees \\) -prune -o "
                    '-type f \\( -name "*.cpp" -o -name "*.cxx" -o -name "*.cc" '
                    '-o -name "*.hpp" -o -name "*.hxx" -o -name "*.hh" '
                    '-o -name "*.h" \\) -print0 '
                    "| xargs -0 -r clang-format --dry-run --Werror "
                    "--style=file:{configs}/cpp/.clang-format",
                ],
                [
                    "run-clang-tidy",
                    "-p",
                    _CPP_BUILD_DIR,
                    "-quiet",
                    "-config-file={configs}/cpp/.clang-tidy",
                ],
                [
                    "cppcheck",
                    # Curated enable set, NOT ``--enable=all`` (#2585). This drops
                    # the unreliable ``unusedFunction`` check — a systematic false
                    # positive on GoogleTest's static-registered ``TEST()``
                    # functions, which cppcheck reports as dead code because it
                    # cannot see the cross-TU/static-registration usage — plus the
                    # noisy ``information``/``missingInclude``. It is deliberate
                    # tool-tuning of a check that emits systematic false positives,
                    # not a standing suppression-list entry (§ No Standing
                    # Suppression List): a real finding is never silenced here.
                    "--enable=warning,style,performance,portability",
                    "--error-exitcode=1",
                    "--inline-suppr",
                    "--std={std}",
                    # GoogleTest is the documented default framework; without its
                    # library config cppcheck throws a syntaxError on the TEST()
                    # macro (T11 #2558). The images ship googletest.cfg. (#2579)
                    "--library=googletest",
                    "--suppressions-list={configs}/cpp/cppcheck-suppressions.txt",
                    # Never walk CMake's ``build/``/``build-sanitize/`` output —
                    # its compiler-probe file (``CMakeCXXCompilerId.cpp``) trips
                    # ``toomanyconfigs`` (#2585) — nor ``.worktrees/``, whose
                    # sibling worktrees carry their own sources and build trees a
                    # root-level validation must never read (#2906). clang-format
                    # prunes the same dirs via its find driver above; cppcheck
                    # excludes them with -i.
                    "-i",
                    _CPP_BUILD_DIR,
                    "-i",
                    _CPP_SANITIZE_BUILD_DIR,
                    "-i",
                    _WORKTREES_DIR,
                    ".",
                ],
            ],
            # TYPECHECK is "the compiler" — the warnings-to-11 build, per
            # compiler×version. The curated warning set (§3.2) lives here, on
            # the configure line, as one CMAKE_CXX_FLAGS cache value.
            CheckKind.TYPECHECK: [
                [
                    "cmake",
                    "-S",
                    ".",
                    "-B",
                    _CPP_BUILD_DIR,
                    "-DCMAKE_BUILD_TYPE=Debug",
                    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                    "-DVERGIL_CPP_STD={std}",
                    "-DVERGIL_CPP_STDLIB={stdlib}",
                    f"-DCMAKE_CXX_FLAGS={_CPP_WARNINGS_CXX_FLAGS}",
                    f"-DCMAKE_TOOLCHAIN_FILE={_CPP_TOOLCHAIN_FILE}",
                ],
                ["cmake", "--build", _CPP_BUILD_DIR, "--parallel"],
            ],
            # TEST is per compiler×version: a coverage-instrumented build+run
            # held to 100% line via gcovr (image sets the gcov executable —
            # ``gcov`` on GCC, ``llvm-cov gcov`` on Clang), then a *separate*
            # ASan+UBSan build+run (distinct build dir — the instrumentations
            # do not share a build, §4).
            CheckKind.TEST: [
                [
                    "cmake",
                    "-S",
                    ".",
                    "-B",
                    _CPP_BUILD_DIR,
                    "-DCMAKE_BUILD_TYPE=Debug",
                    "-DVERGIL_CPP_STD={std}",
                    "-DVERGIL_CPP_STDLIB={stdlib}",
                    "-DVERGIL_CPP_COVERAGE=ON",
                    f"-DCMAKE_TOOLCHAIN_FILE={_CPP_TOOLCHAIN_FILE}",
                ],
                ["cmake", "--build", _CPP_BUILD_DIR, "--parallel"],
                ["ctest", "--test-dir", _CPP_BUILD_DIR, "--output-on-failure"],
                # ``--root``/``--filter`` are on the command line so they resolve
                # against the repo root (cwd), not the packaged config file's own
                # directory. A config-relative ``root``/``filter`` pointed gcovr
                # at ``{configs}/cpp/`` and filtered *all* coverage out in T11
                # (#2558); the config file now carries only path-independent
                # settings. (#2572)
                [
                    "gcovr",
                    "--config",
                    "{configs}/cpp/gcovr.cfg",
                    "--root",
                    ".",
                    "--filter",
                    "src/",
                    "--fail-under-line",
                    "100",
                ],
                [
                    "cmake",
                    "-S",
                    ".",
                    "-B",
                    _CPP_SANITIZE_BUILD_DIR,
                    "-DCMAKE_BUILD_TYPE=Debug",
                    "-DVERGIL_CPP_STD={std}",
                    "-DVERGIL_CPP_STDLIB={stdlib}",
                    "-DVERGIL_CPP_SANITIZE=address,undefined",
                    # The sanitizer build reads the shared toolchain file that
                    # INSTALL's single ``conan install`` wrote under ``build/`` —
                    # a plain file reference, so pointing the ``build-sanitize``
                    # configure at ``build/conan_toolchain.cmake`` is fine. (#2912)
                    f"-DCMAKE_TOOLCHAIN_FILE={_CPP_TOOLCHAIN_FILE}",
                ],
                ["cmake", "--build", _CPP_SANITIZE_BUILD_DIR, "--parallel"],
                ["ctest", "--test-dir", _CPP_SANITIZE_BUILD_DIR, "--output-on-failure"],
            ],
            # AUDIT runs *once* (§3.6). ``conan audit`` scans the dependency
            # graph against ConanCenter's advisory database, reading its provider
            # token from ``CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`` in the env.
            # This *reverts* the #2572 switch to OSV-Scanner: T11 (#2558) proved
            # OSV.dev carries no ConanCenter package data, so ``osv-scanner`` over
            # ``conan.lock`` reported a false all-clear — it never had the CVE
            # data to find anything. The decision was reversed on
            # vergil-project/.github#209 (#2579). License checking stays
            # best-effort surfacing in v1 (no turnkey Conan allowlist gate);
            # hardened gating against ``_CPP_LICENSES_ALLOWLIST`` is ledger #7.
            #
            # Scope (verified against Conan 2.33, #3553): ``audit scan`` submits
            # every non-root graph node in *both* contexts, so ``test_requires``
            # (host) and ``tool_requires`` (build) are covered. But the graph is
            # the one resolved for the active profile: bare, it resolved the
            # default profile (Release) and silently skipped locked packages a
            # recipe only requires in Debug — the build_type ``conan lock
            # create`` and INSTALL use. So both steps pin ``build_type=Debug``
            # and name the lock explicitly: the implicit ``conan.lock`` lookup
            # falls back to a fresh re-resolution when the lock is missing.
            CheckKind.AUDIT: [
                ["conan", "audit", "scan", ".", "-s", "build_type=Debug", "--lockfile=conan.lock"],
                # Resolve the graph against the COMMITTED conan.lock so the audit
                # reflects the pinned graph, not a fresh re-resolution. (#3021)
                [
                    "conan",
                    "graph",
                    "info",
                    ".",
                    "-s",
                    "build_type=Debug",
                    "--format=json",
                    "--lockfile=conan.lock",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["cmake", "--build", _CPP_BUILD_DIR],
            publish_cmd=None,
            publish_env_var=None,
        ),
        # The committed ``conan.lock`` is resolved with this command — the same
        # one a developer runs to refresh the lock deliberately (like ``uv
        # lock``), and the one the born-green scaffold runs once at repo
        # creation to produce the lock #3021 requires (epic
        # vergil-project/.github#342). ``-s build_type=Debug`` matches the
        # build_type the INSTALL/coverage/sanitizer builds resolve against.
        lock=["conan", "lock", "create", ".", "-s", "build_type=Debug"],
        # LINT + AUDIT are compiler-agnostic → one gate each (§3.6). TYPECHECK
        # and TEST are the two-diagnostics engine → per compiler×version, which
        # is the PER_VERSION default, so they are intentionally omitted here.
        cardinality={
            CheckKind.LINT: Cardinality.ONCE,
            CheckKind.AUDIT: Cardinality.ONCE,
        },
    ),
    # TypeScript (epic vergil-project/.github#284, T4). Node.js runtime with a
    # single canonical typechecker (``tsc``); the expensive matrix axis is the
    # Node major version, so — unlike C++ — TYPECHECK/LINT/AUDIT run **once**
    # and only TEST fans out per Node version (§3.1/§3.6). The per-kind
    # cardinality machinery is inherited from C++ (this entry only *declares*
    # cardinalities, it builds none).
    #
    # **Warnings to 11 live in the shareable base tsconfig.** The curated
    # strictness set — ``strict`` plus ``noUncheckedIndexedAccess``,
    # ``exactOptionalPropertyTypes``, ``noImplicitOverride``,
    # ``noImplicitReturns``, ``noFallthroughCasesInSwitch``,
    # ``noPropertyAccessFromIndexSignature``, ``noUnusedLocals``,
    # ``noUnusedParameters`` (spec §3.2) — is authored and tested in the
    # packaged ``{configs}/typescript/tsconfig.base.json``, which the fleet
    # sync writes into each repo as the managed ``tsconfig.vergil.json`` that
    # the repo's ``tsconfig.json`` extends (checked by the pre-gate stage).
    # TYPECHECK is therefore a bare ``tsc --noEmit`` that reads the
    # consumer's own ``tsconfig.json`` (which extends the base); the strict set
    # is enforced through that inheritance, the direct analog of C++ threading
    # ``-Wall -Wextra -Werror -Wpedantic``-plus-extras onto every compile line.
    # T8 *documents* this already-decided set; it does not re-choose it.
    "typescript": Language(
        name="typescript",
        checks={
            # Clean install from ``package-lock.json``. Install scripts never
            # run (epic vergil-project/.github#1174, spec §6.3): a repo that
            # needs one declares it as an exception.
            #
            # **The repo's own tools.** ESLint, ``tsc`` and Vitest are the
            # repo's devDependencies, run by path from ``node_modules/.bin``,
            # so a tool missing from the repo fails instead of falling back to
            # a global binary (``npx`` falls back to the global ``bin`` even
            # with ``--no-install``). ``vrg-validate``'s pre-gate stage checks
            # them against the accepted majors in
            # ``{configs}/typescript/dev-dependencies.json`` first. Prettier
            # and license-checker stay image tools.
            CheckKind.INSTALL: [["npm", "ci", "--ignore-scripts"]],
            # LINT runs *once* (§3.6): Prettier for format, then ESLint's flat
            # config with type-aware typescript-eslint rules. Both read the
            # repo's own configs, with no staging (epic
            # vergil-project/.github#1174, spec §6.4): Prettier the managed
            # ``.prettierrc.json``, the one Prettier config an editor finds
            # too, and ESLint the repo's ``eslint.config.mjs``, which builds
            # on the managed ``eslint.vergil.config.mjs`` beside it. Its bare
            # imports (``@eslint/js``, ``typescript-eslint``) resolve from
            # the repo's ``node_modules``. The pre-gate stage has already
            # checked that the entry points build on the managed configs and
            # that Prettier has no second config source. Vergil's ESLint config
            # carries ``@typescript-eslint/ban-ts-comment`` (the
            # no-standing-suppression rule, §3.2). Prettier and ESLint ignore
            # ``node_modules`` by default. The ESLint run writes
            # ``quality-eslint.json``; see ``_TYPESCRIPT_ESLINT_SCRIPT``.
            CheckKind.LINT: [
                ["prettier", "--check", ".", "--config", ".prettierrc.json"],
                ["sh", "-c", _TYPESCRIPT_ESLINT_SCRIPT],
            ],
            # TYPECHECK runs *once* (§3.6) — one canonical type engine. The
            # curated "warnings to 11" set lives in the packaged base tsconfig
            # the consumer extends (see the class comment above); this command
            # reads the consumer's ``tsconfig.json`` and never emits JavaScript.
            # Its diagnostics go to ``quality-tsc.txt``; see
            # ``_TYPESCRIPT_TSC_SCRIPT``.
            CheckKind.TYPECHECK: [["sh", "-c", _TYPESCRIPT_TSC_SCRIPT]],
            # TEST runs *per Node version* (§3.6) — the only fan-out stage,
            # because runtime behavior differs across Node majors. Vitest with
            # the V8 coverage provider, held to a **100% line** threshold so the
            # gate fails under 100% (spec §5). Contingency (spec §4 caveat 2): if
            # V8's byte-range→TS-source-map mapping proves imprecise at the 100%
            # bar, switch the provider to ``@vitest/coverage-istanbul`` (which
            # instruments source directly). Decided against the real gate in T10.
            # The run writes ``junit.xml`` and ``coverage.xml``, even when a
            # test fails; see ``_TYPESCRIPT_TEST_SCRIPT``.
            CheckKind.TEST: [["sh", "-c", _TYPESCRIPT_TEST_SCRIPT]],
            # AUDIT runs *once* (§3.6). ``npm audit`` is the CVE scan over the
            # whole locked dependency set, devDependencies included — no
            # ``--omit=dev``, so a vulnerable docs or test tool fails the gate
            # just as a runtime dependency does (#3552, superseding spec §4
            # caveat 1's prod-only scope) — with an explicit severity threshold
            # (``--audit-level=high``). Contingency (spec §4 caveat 1): if
            # ``npm audit``'s signal/noise is unworkable, fall back to
            # OSV-Scanner over ``package-lock.json`` — the switch is proven or
            # rejected against a known CVE in T10, not asserted here. License
            # checking stays best-effort surfacing in v1 (``license-checker
            # --summary``, non-gating — npm license metadata is inconsistent);
            # hardened gating against ``_TYPESCRIPT_LICENSES_ALLOWLIST`` is
            # deferred (spec §9 ledger #7). The ``npm audit`` run writes
            # ``npm-audit.json``; see ``_TYPESCRIPT_AUDIT_SCRIPT``.
            CheckKind.AUDIT: [
                ["sh", "-c", _TYPESCRIPT_AUDIT_SCRIPT],
                ["license-checker", "--production", "--summary"],
            ],
        },
        ecosystem=EcosystemInfo(
            # The release build, run by the release job and, as the BUILD
            # check, by vrg-validate (epic vergil-project/.github#1174, spec
            # §6.5). It installs from the lockfile first, since the release
            # job's Build step runs on a fresh checkout with no node_modules,
            # and, as every npm ci Vergil runs, with no install script. In
            # vrg-validate this reinstalls what INSTALL already installed: the
            # cost is accepted so validate and release run one command. The
            # repo's ``build`` script is required by the typescript-tools
            # pre-gate stage. No npm publishing: publish_cmd stays None.
            build_cmd=["sh", "-c", "npm ci --ignore-scripts && npm run build"],
            publish_cmd=None,
            publish_env_var=None,
            validate_build=True,
        ),
        # TYPECHECK/LINT/AUDIT are runtime-agnostic → one gate each (§3.6). Only
        # TEST is per Node version, which is the PER_VERSION default, so it is
        # intentionally omitted here.
        cardinality={
            CheckKind.TYPECHECK: Cardinality.ONCE,
            CheckKind.LINT: Cardinality.ONCE,
            CheckKind.AUDIT: Cardinality.ONCE,
        },
    ),
}


def supported_languages() -> frozenset[str]:
    return frozenset(_REGISTRY)


def ecosystem_metadata(language: str) -> EcosystemInfo:
    entry = _REGISTRY.get(language)
    if entry is None:
        msg = f"unsupported language: {language}"
        raise ValueError(msg)
    return entry.ecosystem


def language_commands(
    language: str | None,
    kind: CheckKind,
    *,
    cpp_std: str | None = None,
    cpp_stdlib: str | None = None,
    test_parallel: bool = True,
    xdist_available: bool = False,
    test_workers: int | str = "auto",
) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    ``BUILD`` is not looked up in the language's checks: it is the ecosystem's
    ``build_cmd``, the command the release job runs, and only when the
    ecosystem sets ``validate_build``; otherwise it is empty.

    Placeholder expansion applied to every argument:

    - ``{configs}`` → the resolved ``vergil_tooling.configs`` package dir.
    - ``{std}`` / ``{stdlib}`` → the C++ ``[cpp]`` ``std`` / ``stdlib`` values,
      threaded into the CMake cache vars. ``cpp_std`` / ``cpp_stdlib`` default to
      the v1 pins (``config.DEFAULT_CPP_STD`` / ``DEFAULT_CPP_STDLIB``) when not
      supplied, so the two-argument call still yields fully-resolved argv.

    The Python ``TEST`` command is **computed**, not looked up: it is delegated
    to :func:`build_python_test_argv`, which appends xdist work-stealing when
    ``xdist_available and test_parallel`` (epic vergil-project/.github#333,
    Task 6). ``test_parallel`` carries the ``[test].parallel`` intent (default on)
    and ``xdist_available`` is a live probe supplied by the caller; both default
    to a safe serial command for callers that only need the gate flags.
    ``test_workers`` is the resolved xdist worker count (issue #3322).
    """
    if language is None:
        return []
    entry = _REGISTRY.get(language)
    if entry is None:
        return []
    if language == "python" and kind is CheckKind.TEST:
        return [
            build_python_test_argv(
                xdist_available=xdist_available, parallel=test_parallel, workers=test_workers
            )
        ]
    # Local import keeps this module a leaf in the import graph (the same
    # cycle-avoidance idiom used by github_config/repo_init for languages).
    from vergil_tooling.lib.config import DEFAULT_CPP_STD, DEFAULT_CPP_STDLIB

    replacements = {
        "{configs}": str(files("vergil_tooling.configs")),
        "{std}": cpp_std if cpp_std is not None else DEFAULT_CPP_STD,
        "{stdlib}": cpp_stdlib if cpp_stdlib is not None else DEFAULT_CPP_STDLIB,
    }

    def _expand(arg: str) -> str:
        for token, value in replacements.items():
            arg = arg.replace(token, value)
        return arg

    if kind is CheckKind.BUILD:
        build_cmd = entry.ecosystem.build_cmd
        cmds = [build_cmd] if entry.ecosystem.validate_build and build_cmd else []
    else:
        cmds = entry.checks.get(kind, [])
    return [[_expand(arg) for arg in cmd] for cmd in cmds]


def language_lock_command(language: str | None) -> list[str] | None:
    """Return the lock-resolve command for a language, or ``None``.

    The lock command resolves and writes the committed lockfile the validation
    pipeline consumes (cpp → ``conan lock create . -s build_type=Debug``). A
    language that commits no resolved lock — and any unknown/``None`` language —
    yields ``None``. This is the single source of truth shared by the born-green
    scaffold (``lang_scaffold``) and any future deliberate re-lock (epic
    vergil-project/.github#342).

    A fresh ``list`` copy is returned so a caller cannot mutate the registry's
    stored command in place.
    """
    if language is None:
        return None
    entry = _REGISTRY.get(language)
    if entry is None or entry.lock is None:
        return None
    return list(entry.lock)


def check_cardinality(language: str | None, kind: CheckKind) -> Cardinality:
    """Return the gate cardinality for a language's check kind.

    Defaults to :attr:`Cardinality.PER_VERSION` when the language is unknown,
    ``None``, or declares no explicit cardinality for the kind — preserving the
    historical one-gate-per-version behavior for every existing language.

    ``BUILD`` is :attr:`Cardinality.ONCE` for every language: the release build
    runs once, on the primary version, as the release job does.
    """
    if kind is CheckKind.BUILD:
        return Cardinality.ONCE
    if language is None:
        return Cardinality.PER_VERSION
    entry = _REGISTRY.get(language)
    if entry is None:
        return Cardinality.PER_VERSION
    return entry.cardinality.get(kind, Cardinality.PER_VERSION)
