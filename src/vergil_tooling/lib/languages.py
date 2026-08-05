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


@dataclass(frozen=True)
class Language:
    name: str
    checks: dict[CheckKind, list[list[str]]]
    ecosystem: EcosystemInfo
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
COVERAGE_REPORT = "coverage.xml"
JUNIT_REPORT = "junit.xml"
PIP_AUDIT_REPORT = "pip-audit.json"
LICENSES_REPORT = "licenses.json"

# The workspace-root report files the Python gate commands emit. Exposed as a
# single source of truth so consumers (and tests) can assert the contract
# without re-deriving the individual filenames.
PYTHON_REPORT_FILES = (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    PIP_AUDIT_REPORT,
    LICENSES_REPORT,
)


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
        "MIT License",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later",
        "PSF-2.0",
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

_MAVEN_LICENSES_ALLOWLIST = "|".join(
    [
        "Apache 2.0",
        "Apache-2.0",
        "The Apache License, Version 2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-or-later",
        "ISC",
        "MIT License",
        "MPL-2.0",
    ]
)

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

# Recognized C++ compiler families, in declared-primary precedence order. The
# `[ci].versions` tags carry the compiler family as a `<family>-<version>`
# prefix (e.g. ``clang-20``), per spec vergil-project/.github#207 §6.
_CPP_FAMILIES = ("clang", "gcc")


def parse_cpp_version_tag(tag: str) -> tuple[str, str] | None:
    """Split a C++ compiler-family version tag into ``(family, version)``.

    C++ carries its compiler-family × version axis on ``[ci].versions`` as a
    ``clang-``/``gcc-`` prefixed tag (e.g. ``clang-20`` → ``("clang", "20")``,
    ``gcc-15`` → ``("gcc", "15")``), per spec vergil-project/.github#207 §6.
    Returns ``None`` when *tag* carries no recognized family prefix.

    This is the single source of truth for the tag format, shared by the
    container image-resolution path (:mod:`container`) and the CI scaffolding
    path (:mod:`repo_init`) so the two can never drift on the naming.
    """
    for family in _CPP_FAMILIES:
        prefix = f"{family}-"
        if tag.startswith(prefix):
            return family, tag[len(prefix) :]
    return None


# -- Registry -----------------------------------------------------------------

_REGISTRY: dict[str, Language] = {
    "python": Language(
        name="python",
        checks={
            CheckKind.INSTALL: [["uv", "sync", "--frozen", "--group", "dev"]],
            CheckKind.LINT: [
                ["ruff", "check", "src/", "tests/"],
                ["ruff", "format", "--check", "src/", "tests/"],
            ],
            CheckKind.TYPECHECK: [["mypy", "src/"], ["ty", "check", "src", "tests"]],
            CheckKind.TEST: [
                [
                    "pytest",
                    "--cov=src",
                    "--cov-branch",
                    "--cov-fail-under=100",
                    "--cov-report=term-missing",
                    f"--cov-report=xml:{COVERAGE_REPORT}",
                    f"--junitxml={JUNIT_REPORT}",
                ],
            ],
            CheckKind.AUDIT: [
                ["uv", "sync", "--check", "--frozen", "--group", "dev"],
                ["uv", "lock", "--check"],
                ["pip-audit", "--format=json", f"--output={PIP_AUDIT_REPORT}"],
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
            publish_env_var="PYPI_TOKEN",
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
    "java": Language(
        name="java",
        checks={
            CheckKind.INSTALL: [["./mvnw", "dependency:resolve", "-B"]],
            CheckKind.LINT: [["./mvnw", "spotless:check", "checkstyle:check", "-B"]],
            CheckKind.TYPECHECK: [["./mvnw", "compile", "-B"]],
            CheckKind.TEST: [["./mvnw", "verify", "-B"]],
            CheckKind.AUDIT: [
                ["./mvnw", "dependency:tree", "-B", "-q"],
                [
                    "./mvnw",
                    "org.codehaus.mojo:license-maven-plugin:add-third-party",
                    "-Dlicense.excludedScopes=test",
                    "-Dlicense.failIfWarning=true",
                    f"-Dlicense.includedLicenses={_MAVEN_LICENSES_ALLOWLIST}",
                    "-B",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["./mvnw", "package", "-B"],
            publish_cmd=["./mvnw", "deploy", "-B"],
            publish_env_var="MAVEN_GPG_PASSPHRASE",
        ),
    ),
    "ruby": Language(
        name="ruby",
        checks={
            CheckKind.INSTALL: [["bundle", "install", "--jobs", "4"]],
            CheckKind.LINT: [["bundle", "exec", "rubocop"]],
            CheckKind.TYPECHECK: [["bundle", "exec", "steep", "check"]],
            CheckKind.TEST: [["bundle", "exec", "rake"]],
            CheckKind.AUDIT: [
                ["bundle", "exec", "bundle-audit", "check", "--update"],
                ["license_finder", "--decisions-file={configs}/ruby/license_finder.yml"],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["gem", "build"],
            publish_cmd=["gem", "push"],
            publish_env_var="RUBYGEMS_API_KEY",
        ),
    ),
    "rust": Language(
        name="rust",
        checks={
            CheckKind.INSTALL: [["cargo", "fetch"]],
            CheckKind.LINT: [
                ["cargo", "fmt", "--all", "--", "--check"],
                ["cargo", "clippy", "--", "-D", "warnings"],
            ],
            CheckKind.TYPECHECK: [["cargo", "check"]],
            CheckKind.TEST: [["cargo", "llvm-cov", "--fail-under-lines", "100"]],
            CheckKind.AUDIT: [["cargo", "deny", "check"]],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["cargo", "build", "--release"],
            publish_cmd=["cargo", "publish"],
            publish_env_var="CRATES_IO_TOKEN",
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
            # ``conan lock create`` resolves the dependency graph and writes a
            # ``conan.lock`` that pins it for the Debug builds; ``conan install``
            # then materializes the binaries. Both pin ``build_type=Debug`` so
            # Conan builds deps in the *same* configuration as the CMake
            # coverage/sanitizer builds — the container image only bakes a
            # default profile (build_type=Release), and that mismatch broke the
            # T11 cold rebuild (#2558): ``fmt/format.h`` not found because the
            # Debug config had no matching Conan binary. (#2572, image side #487)
            CheckKind.INSTALL: [
                ["conan", "lock", "create", ".", "-s", "build_type=Debug"],
                ["conan", "install", ".", "-s", "build_type=Debug", "--build=missing"],
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
                ],
            ],
            # LINT runs *once* on the primary Clang image (§3.6). No shell is
            # available (commands exec directly), so each tool self-discovers:
            # cppcheck walks ``.``; run-clang-tidy reads the compile DB; only
            # clang-format needs a file list, so it uses an explicit ``sh -c``
            # find/xargs driver.
            CheckKind.LINT: [
                [
                    "sh",
                    "-c",
                    "find . -path ./build -prune -o -path ./build-sanitize -prune -o "
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
                    "--enable=all",
                    "--error-exitcode=1",
                    "--inline-suppr",
                    "--std={std}",
                    # GoogleTest is the documented default framework; without its
                    # library config cppcheck throws a syntaxError on the TEST()
                    # macro (T11 #2558). The images ship googletest.cfg. (#2579)
                    "--library=googletest",
                    "--suppressions-list={configs}/cpp/cppcheck-suppressions.txt",
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
            CheckKind.AUDIT: [
                ["conan", "audit", "scan", "."],
                ["conan", "graph", "info", ".", "--format=json"],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["cmake", "--build", _CPP_BUILD_DIR],
            publish_cmd=None,
            publish_env_var=None,
        ),
        # LINT + AUDIT are compiler-agnostic → one gate each (§3.6). TYPECHECK
        # and TEST are the two-diagnostics engine → per compiler×version, which
        # is the PER_VERSION default, so they are intentionally omitted here.
        cardinality={
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
) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    Placeholder expansion applied to every argument:

    - ``{configs}`` → the resolved ``vergil_tooling.configs`` package dir.
    - ``{std}`` / ``{stdlib}`` → the C++ ``[cpp]`` ``std`` / ``stdlib`` values,
      threaded into the CMake cache vars. ``cpp_std`` / ``cpp_stdlib`` default to
      the v1 pins (``config.DEFAULT_CPP_STD`` / ``DEFAULT_CPP_STDLIB``) when not
      supplied, so the two-argument call still yields fully-resolved argv.
    """
    if language is None:
        return []
    entry = _REGISTRY.get(language)
    if entry is None:
        return []
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

    return [[_expand(arg) for arg in cmd] for cmd in entry.checks.get(kind, [])]


def check_cardinality(language: str | None, kind: CheckKind) -> Cardinality:
    """Return the gate cardinality for a language's check kind.

    Defaults to :attr:`Cardinality.PER_VERSION` when the language is unknown,
    ``None``, or declares no explicit cardinality for the kind — preserving the
    historical one-gate-per-version behavior for every existing language.
    """
    if language is None:
        return Cardinality.PER_VERSION
    entry = _REGISTRY.get(language)
    if entry is None:
        return Cardinality.PER_VERSION
    return entry.cardinality.get(kind, Cardinality.PER_VERSION)
