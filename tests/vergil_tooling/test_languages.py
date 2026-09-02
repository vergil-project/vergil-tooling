"""Tests for vergil_tooling.lib.languages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vergil_tooling.lib.languages import (
    _TYPESCRIPT_LICENSES_ALLOWLIST,
    COVERAGE_REPORT,
    JUNIT_REPORT,
    LICENSES_REPORT,
    MYPY_REPORT,
    PIP_AUDIT_REPORT,
    PYTHON_REPORT_FILES,
    RUFF_REPORT,
    Cardinality,
    CheckKind,
    EcosystemInfo,
    Language,
    build_python_test_argv,
    check_cardinality,
    ecosystem_metadata,
    language_commands,
    parse_cpp_version_tag,
    supported_languages,
)


def _joined(cmds: list[list[str]]) -> list[str]:
    return [" ".join(c) for c in cmds]


# -- Python ------------------------------------------------------------------


def test_python_install_commands() -> None:
    cmds = language_commands("python", CheckKind.INSTALL)
    assert cmds == [["uv", "sync", "--frozen", "--group", "dev"]]


def test_python_lint_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.LINT))
    assert "ruff check src/ tests/" in joined
    assert "ruff format --check src/ tests/" in joined


def test_python_lint_emits_ruff_json_report() -> None:
    """A ``ruff check`` invocation writes the machine-readable findings file."""
    cmds = language_commands("python", CheckKind.LINT)
    report_cmd = [c for c in cmds if c[:2] == ["ruff", "check"] and "--output-format=json" in c]
    assert len(report_cmd) == 1
    assert "--output-format=json" in report_cmd[0]
    assert f"--output-file={RUFF_REPORT}" in report_cmd[0]
    # The developer-facing console invocations are preserved alongside it.
    assert ["ruff", "check", "src/", "tests/"] in cmds
    assert ["ruff", "format", "--check", "src/", "tests/"] in cmds


def test_python_typecheck_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.TYPECHECK))
    assert "mypy src/ --junit-xml quality-mypy.xml" in joined
    assert "ty check src tests" in joined


def test_python_typecheck_mypy_emits_junit_xml() -> None:
    """The mypy gate writes a JUnit-XML diagnostic report file."""
    cmds = language_commands("python", CheckKind.TYPECHECK)
    mypy_cmd = [c for c in cmds if c[0] == "mypy"]
    assert len(mypy_cmd) == 1
    assert "--junit-xml" in mypy_cmd[0]
    junit_idx = mypy_cmd[0].index("--junit-xml")
    assert mypy_cmd[0][junit_idx + 1] == MYPY_REPORT
    # ty stays stdout-only (unchanged).
    assert ["ty", "check", "src", "tests"] in cmds


def test_python_test_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.TEST))
    assert any("pytest" in c for c in joined)
    assert any("--cov=src" in c for c in joined)


def test_python_test_enforcement_flag_intact() -> None:
    """The coverage gate (``--cov-fail-under=100``) must survive report wiring."""
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"]
    assert len(pytest_cmd) == 1
    assert "--cov-fail-under=100" in pytest_cmd[0]


def test_python_test_emits_coverage_xml() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"][0]
    assert f"--cov-report=xml:{COVERAGE_REPORT}" in pytest_cmd


def test_python_test_emits_junit_xml() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"][0]
    assert f"--junitxml={JUNIT_REPORT}" in pytest_cmd


# -- build_python_test_argv (epic vergil-project/.github#333, Task 6) ----------
#
# The computed Python TEST command. ``-n auto --dist worksteal`` (pytest-xdist
# work-stealing scheduler) is appended IFF xdist is available AND parallelism is
# not opted out via ``[test].parallel = false``; the coverage gate flags are
# present in every case. Task 4 (COVERAGE_CORE=sysmon) and Task 10
# (--import-mode=importlib) were both dropped, so neither may ever appear here.


@pytest.mark.parametrize(
    ("xdist_available", "parallel", "expect_xdist"),
    [
        (True, True, True),  # both true → work-stealing xdist
        (True, False, False),  # opt-out honored → serial
        (False, True, False),  # xdist missing → serial, no error
        (False, False, False),  # neither → serial
    ],
)
def test_build_python_test_argv_truth_table(
    *, xdist_available: bool, parallel: bool, expect_xdist: bool
) -> None:
    argv = build_python_test_argv(xdist_available=xdist_available, parallel=parallel)

    # A plain pytest argv (list[str]), never an (argv, env) tuple.
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)
    assert argv[0] == "pytest"

    has_xdist = "-n" in argv and "auto" in argv and "--dist" in argv and "worksteal" in argv
    assert has_xdist is expect_xdist

    # The coverage gate is present in every case.
    assert "--cov=src" in argv
    assert "--cov-branch" in argv
    assert "--cov-fail-under=100" in argv

    # Dropped levers must never appear: no import-mode (Task 10), no
    # sysmon/COVERAGE_CORE overlay (Task 4).
    assert "--import-mode=importlib" not in argv
    joined = " ".join(argv)
    assert "COVERAGE_CORE" not in joined
    assert "sysmon" not in joined
    assert "--import-mode" not in joined


def test_build_python_test_argv_xdist_flags_order() -> None:
    """When enabled, the four xdist tokens are appended as ``-n auto --dist worksteal``."""
    argv = build_python_test_argv(xdist_available=True, parallel=True)
    assert argv[-4:] == ["-n", "auto", "--dist", "worksteal"]


def test_build_python_test_argv_serial_equals_base() -> None:
    """Serial output is exactly the base gate argv with nothing appended."""
    serial = build_python_test_argv(xdist_available=False, parallel=True)
    assert serial[-1] == f"--junitxml={JUNIT_REPORT}"  # last base flag, no xdist tail


def test_language_commands_python_test_is_computed_parallel() -> None:
    """(python, TEST) routes through build_python_test_argv; xdist+parallel → work-steal."""
    cmds = language_commands("python", CheckKind.TEST, test_parallel=True, xdist_available=True)
    assert cmds == [build_python_test_argv(xdist_available=True, parallel=True)]
    assert cmds[0][-4:] == ["-n", "auto", "--dist", "worksteal"]


def test_language_commands_python_test_serial_when_xdist_missing() -> None:
    cmds = language_commands("python", CheckKind.TEST, test_parallel=True, xdist_available=False)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]


def test_language_commands_python_test_serial_when_opted_out() -> None:
    cmds = language_commands("python", CheckKind.TEST, test_parallel=False, xdist_available=True)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]


def test_language_commands_python_test_defaults_serial() -> None:
    """A bare call (no probes) is serial and still carries the coverage gate."""
    cmds = language_commands("python", CheckKind.TEST)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]
    assert "--import-mode=importlib" not in cmds[0]


def test_python_audit_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.AUDIT))
    assert any("uv sync --check" in c for c in joined)
    assert any("uv lock --check" in c for c in joined)
    assert any(c.startswith("pip-audit") for c in joined)
    assert any("pip-licenses" in c for c in joined)


def test_python_audit_pip_audit_emits_json_report() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_audit_cmd = [c for c in cmds if c[0] == "pip-audit"]
    assert len(pip_audit_cmd) == 1
    assert "--format=json" in pip_audit_cmd[0]
    assert f"--output={PIP_AUDIT_REPORT}" in pip_audit_cmd[0]


def test_python_audit_pip_licenses_allowlist_intact() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"]
    assert len(pip_licenses_cmd) == 1
    assert any(arg.startswith("--allow-only=") for arg in pip_licenses_cmd[0])


def test_python_audit_pip_licenses_emits_json_report() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"][0]
    assert "--format=json" in pip_licenses_cmd
    assert f"--output-file={LICENSES_REPORT}" in pip_licenses_cmd


def test_python_report_files_contract() -> None:
    """The shared report-path constants match the T8 path contract."""
    assert PYTHON_REPORT_FILES == (
        "quality-ruff.json",
        "quality-mypy.xml",
        "coverage.xml",
        "junit.xml",
        "pip-audit.json",
        "licenses.json",
    )
    assert RUFF_REPORT == "quality-ruff.json"
    assert MYPY_REPORT == "quality-mypy.xml"
    assert COVERAGE_REPORT == "coverage.xml"
    assert JUNIT_REPORT == "junit.xml"
    assert PIP_AUDIT_REPORT == "pip-audit.json"
    assert LICENSES_REPORT == "licenses.json"


# -- Go ----------------------------------------------------------------------


def test_go_install_commands() -> None:
    cmds = language_commands("go", CheckKind.INSTALL)
    assert cmds == [["go", "mod", "download"]]


def test_go_lint_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.LINT))
    assert "golangci-lint run ./..." in joined
    assert any("gocyclo" in c for c in joined)


def test_go_typecheck_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.TYPECHECK))
    assert "go vet ./..." in joined


def test_go_test_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.TEST))
    assert any("go test" in c for c in joined)
    assert any("go-test-coverage" in c for c in joined)


def test_go_audit_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.AUDIT))
    assert any("govulncheck" in c for c in joined)
    assert any("go-licenses" in c and "--allowed_licenses=" in c for c in joined)


def test_go_audit_go_licenses_allowlist_intact() -> None:
    cmds = language_commands("go", CheckKind.AUDIT)
    go_licenses_cmd = [c for c in cmds if c[0] == "go-licenses"]
    assert len(go_licenses_cmd) == 1
    flag = go_licenses_cmd[0][-1]
    assert flag.startswith("--allowed_licenses=")
    licenses = flag.split("=", 1)[1].split(",")
    assert "MIT" in licenses
    assert "Apache-2.0" in licenses
    assert len(licenses) == 7


# -- Java ---------------------------------------------------------------------


def test_java_install_commands() -> None:
    cmds = language_commands("java", CheckKind.INSTALL)
    assert cmds == [["./mvnw", "dependency:resolve", "-B"]]


def test_java_lint_commands() -> None:
    joined = _joined(language_commands("java", CheckKind.LINT))
    assert any("spotless:check" in c for c in joined)
    assert any("checkstyle:check" in c for c in joined)


def test_java_typecheck_commands() -> None:
    joined = _joined(language_commands("java", CheckKind.TYPECHECK))
    assert any("compile" in c for c in joined)


def test_java_test_commands() -> None:
    joined = _joined(language_commands("java", CheckKind.TEST))
    assert any("verify" in c for c in joined)


def test_java_audit_commands() -> None:
    joined = _joined(language_commands("java", CheckKind.AUDIT))
    assert any("dependency:tree" in c for c in joined)
    assert any("license-maven-plugin" in c for c in joined)
    assert any("-Dlicense.failIfWarning=true" in c for c in joined)
    assert any("-Dlicense.includedLicenses=" in c for c in joined)
    assert any("-Dlicense.excludedScopes=test" in c for c in joined)


def test_java_audit_maven_licenses_allowlist_intact() -> None:
    cmds = language_commands("java", CheckKind.AUDIT)
    license_cmd = [c for c in cmds if any("license-maven-plugin" in arg for arg in c)]
    assert len(license_cmd) == 1
    flag = [arg for arg in license_cmd[0] if arg.startswith("-Dlicense.includedLicenses=")]
    assert len(flag) == 1
    licenses = flag[0].split("=", 1)[1].split("|")
    assert "MIT License" in licenses
    assert "Apache-2.0" in licenses
    assert len(licenses) == 9


# -- Ruby ---------------------------------------------------------------------


def test_ruby_install_commands() -> None:
    cmds = language_commands("ruby", CheckKind.INSTALL)
    assert cmds == [["bundle", "install", "--jobs", "4"]]


def test_ruby_lint_commands() -> None:
    joined = _joined(language_commands("ruby", CheckKind.LINT))
    assert any("rubocop" in c for c in joined)


def test_ruby_typecheck_commands() -> None:
    joined = _joined(language_commands("ruby", CheckKind.TYPECHECK))
    assert any("steep check" in c for c in joined)


def test_ruby_test_commands() -> None:
    joined = _joined(language_commands("ruby", CheckKind.TEST))
    assert any("rake" in c for c in joined)


def test_ruby_audit_commands() -> None:
    joined = _joined(language_commands("ruby", CheckKind.AUDIT))
    assert any("bundle-audit" in c for c in joined)
    assert any("license_finder" in c for c in joined)


def test_ruby_audit_license_finder_decisions_file() -> None:
    cmds = language_commands("ruby", CheckKind.AUDIT)
    license_finder_cmds = [c for c in cmds if c[0] == "license_finder"]
    assert len(license_finder_cmds) == 1
    decisions_arg = license_finder_cmds[0][1]
    assert decisions_arg.startswith("--decisions-file=")
    path = decisions_arg.split("=", 1)[1]
    assert path.endswith("ruby/license_finder.yml")
    assert "{configs}" not in decisions_arg


# -- Rust ---------------------------------------------------------------------


def test_rust_install_commands() -> None:
    cmds = language_commands("rust", CheckKind.INSTALL)
    assert cmds == [["cargo", "fetch"]]


def test_rust_lint_commands() -> None:
    joined = _joined(language_commands("rust", CheckKind.LINT))
    assert any("cargo fmt" in c for c in joined)
    assert any("cargo clippy" in c for c in joined)


def test_rust_typecheck_commands() -> None:
    joined = _joined(language_commands("rust", CheckKind.TYPECHECK))
    assert "cargo check" in joined


def test_rust_test_commands() -> None:
    joined = _joined(language_commands("rust", CheckKind.TEST))
    assert any("cargo llvm-cov" in c for c in joined)


def test_rust_audit_commands() -> None:
    joined = _joined(language_commands("rust", CheckKind.AUDIT))
    assert "cargo deny check" in joined


# -- C++ (epic vergil-project/.github#207, T5) --------------------------------


def test_cpp_is_supported() -> None:
    assert "cpp" in supported_languages()


def test_cpp_install_commands() -> None:
    joined = _joined(language_commands("cpp", CheckKind.INSTALL))
    # conan.lock is a COMMITTED input consumed via --lockfile, not regenerated
    # on every run — validation must never regenerate the lock (it dirtied the
    # tree and defeated reproducibility). (#3021)
    assert not any("conan lock create" in c for c in joined)
    # Conan resolves deps in the same build_type (Debug) as the CMake
    # coverage/sanitizer builds — a Release/Debug mismatch broke the cold
    # rebuild in T11 (#2558): fmt/format.h not found. (#2572)
    assert "conan install . -s build_type=Debug --build=missing --lockfile=conan.lock" in joined
    # The conan step pins build_type=Debug to stay consistent with the build.
    for cmd in language_commands("cpp", CheckKind.INSTALL):
        if cmd and cmd[0] == "conan":
            assert "build_type=Debug" in cmd
    # cmake configure exports the compile DB that feeds clang-tidy and threads
    # the [cpp] std/stdlib in as cache vars.
    cmake_cmd = [c for c in language_commands("cpp", CheckKind.INSTALL) if c[0] == "cmake"]
    assert len(cmake_cmd) == 1
    assert "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in cmake_cmd[0]
    assert "-DVERGIL_CPP_STD=c++20" in cmake_cmd[0]
    assert "-DVERGIL_CPP_STDLIB=libstdc++" in cmake_cmd[0]


def test_cpp_lint_commands() -> None:
    cmds = language_commands("cpp", CheckKind.LINT)
    joined = _joined(cmds)
    assert any("clang-format" in c and "--dry-run --Werror" in c for c in joined)
    assert any("run-clang-tidy" in c for c in joined)
    # clang-format's find driver prunes build/build-sanitize *and* .worktrees as
    # directories at any depth — a root-level validation must never lint a
    # sibling worktree's tree or a nested CMake output dir (#2906). The old
    # top-level-only ``-path ./build`` prune is gone.
    clang_format_cmd = next(c for c in joined if "clang-format" in c)
    assert "-name build -o -name build-sanitize -o -name .worktrees" in clang_format_cmd
    assert "-path ./build" not in clang_format_cmd
    # cppcheck runs with --library=googletest so it parses GoogleTest's TEST()
    # macro instead of throwing a syntaxError on it — GoogleTest is the
    # documented default framework and the images ship googletest.cfg. (#2579)
    #
    # The enable set is *curated*, not ``--enable=all`` (#2585): it drops the
    # unreliable ``unusedFunction`` check (a systematic false positive on
    # GoogleTest's static-registered ``TEST()`` functions — cppcheck cannot see
    # cross-TU usage) plus the noisy ``information``/``missingInclude``. The
    # build tree is excluded with ``-i build -i build-sanitize`` so cppcheck
    # never walks CMake's compiler-probe file and trips ``toomanyconfigs``;
    # ``.worktrees`` is excluded too so it never reads a sibling worktree (#2906).
    assert any(
        "cppcheck" in c
        and "--enable=warning,style,performance,portability" in c
        and "--enable=all" not in c
        and "--error-exitcode=1" in c
        and "--library=googletest" in c
        and "-i build " in c
        and "-i build-sanitize " in c
        and "-i .worktrees " in c
        for c in joined
    )


def test_cpp_lint_uses_packaged_configs() -> None:
    """Every LINT tool points at a packaged {configs}/cpp/* file that exists."""
    cmds = language_commands("cpp", CheckKind.LINT)
    flat = [arg for cmd in cmds for arg in cmd]
    # No unresolved placeholder survives expansion.
    assert all("{configs}" not in arg for arg in flat)
    # clang-format, clang-tidy and cppcheck each reference a real packaged file.
    referenced = [arg for arg in flat if "/cpp/" in arg]
    for arg in referenced:
        # Extract the path token (may be embedded in a --flag=path or sh script).
        for token in arg.split():
            if "/cpp/" in token:
                path = token.split("=", 1)[-1].split(":")[-1]
                assert Path(path).exists(), f"packaged config missing: {path}"


def test_cpp_typecheck_is_the_warnings_build() -> None:
    cmds = language_commands("cpp", CheckKind.TYPECHECK)
    joined = _joined(cmds)
    # A configure carrying the warning flags, then a build.
    assert any(c.startswith("cmake -S . -B build") for c in joined)
    assert any("cmake --build build" in c for c in joined)


def test_cpp_typecheck_carries_curated_warning_set() -> None:
    """The floor + curated extras all land on one CMAKE_CXX_FLAGS cache value."""
    cmds = language_commands("cpp", CheckKind.TYPECHECK)
    cxx_flags = [arg for cmd in cmds for arg in cmd if arg.startswith("-DCMAKE_CXX_FLAGS=")]
    assert len(cxx_flags) == 1
    flags = cxx_flags[0].split("=", 1)[1].split()
    # Floor (spec §3.2).
    for floor in ("-Wall", "-Wextra", "-Werror", "-Wpedantic"):
        assert floor in flags, f"missing floor flag {floor}"
    # Curated extras owned + tested by this task (T5); T9 only documents them.
    for extra in (
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
    ):
        assert extra in flags, f"missing curated extra {extra}"


def test_cpp_test_commands_coverage_ctest_and_sanitizers() -> None:
    cmds = language_commands("cpp", CheckKind.TEST)
    joined = _joined(cmds)
    # CTest as the framework-agnostic runner.
    assert any(c.startswith("ctest") and "--output-on-failure" in c for c in joined)
    # Coverage held to 100% line via gcovr. The root/filter that anchor the
    # search live on the command line so they resolve against the repo root
    # (cwd), not the packaged config's own dir — a config-relative root/filter
    # filtered all coverage out in T11 (#2558). (#2572)
    assert any(
        "gcovr" in c and "--fail-under-line 100" in c and "--root ." in c and "--filter src/" in c
        for c in joined
    )
    # A separate ASan+UBSan build+run in its own build dir.
    assert any("-DVERGIL_CPP_SANITIZE=address,undefined" in c for c in joined)
    assert any("cmake --build build-sanitize" in c for c in joined)
    assert any("ctest --test-dir build-sanitize" in c for c in joined)


def _cpp_config_path(name: str) -> Path:
    """Resolve a packaged ``configs/cpp/<name>`` path.

    Derives the packaged ``cpp/`` config directory from the gcovr ``--config``
    argument in the TEST commands, then joins *name* — so it resolves the
    packaged config file's content without hard-coding the package layout.
    """
    for cmd in language_commands("cpp", CheckKind.TEST):
        for arg in cmd:
            if arg.endswith("/cpp/gcovr.cfg"):
                return Path(arg).parent / name
    msg = "no TEST arg references the cpp configs dir"
    raise AssertionError(msg)


def test_cpp_gcovr_config_ignores_source_not_found() -> None:
    """The packaged gcovr config ignores gcov ``source_not_found`` so third-party
    (Conan) dependency headers (e.g. gtest under ``/opt/conan2``) that gcov
    cannot resolve to a source file do not abort the coverage gate. Only
    ``source_not_found`` is ignored — real gcov errors still fail the gate — and
    the config still carries no ``root``/``filter`` settings (those stay on the
    command line so they resolve against the repo root, #2572). (#3026)
    """
    text = _cpp_config_path("gcovr.cfg").read_text()
    assert "gcov-ignore-errors = source_not_found" in text
    # The path-anchoring ``root``/``filter`` stay on the command line, never as
    # config settings here — assert on actual setting keys, not comment prose.
    setting_keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert "root" not in setting_keys
    assert "filter" not in setting_keys


def test_cpp_audit_commands() -> None:
    joined = _joined(language_commands("cpp", CheckKind.AUDIT))
    # conan audit is the CVE scan — it reads ConanCenter advisories using the
    # CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER env token. OSV-Scanner was reverted
    # because OSV.dev carries no ConanCenter data, so it produced a false
    # all-clear (T11 #2558; decision reversal #209). (#2579)
    assert "conan audit scan ." in joined
    # OSV-Scanner is retired here.
    assert not any("osv-scanner" in c for c in joined)
    # Best-effort license-metadata surfacing (hardened gating deferred, §9 #7).
    # The graph is resolved against the COMMITTED conan.lock via --lockfile so
    # the audit reflects the pinned graph, not a fresh re-resolution. (#3021)
    assert "conan graph info . --format=json --lockfile=conan.lock" in joined


def test_cpp_ecosystem_metadata() -> None:
    info = ecosystem_metadata("cpp")
    assert info.build_cmd == ["cmake", "--build", "build"]
    # C++ has no v1 publish target (non-goal §8).
    assert info.publish_cmd is None
    assert info.publish_env_var is None


def test_cpp_cardinality_lint_and_audit_run_once() -> None:
    assert check_cardinality("cpp", CheckKind.LINT) is Cardinality.ONCE
    assert check_cardinality("cpp", CheckKind.AUDIT) is Cardinality.ONCE


def test_cpp_cardinality_typecheck_and_test_are_per_version() -> None:
    assert check_cardinality("cpp", CheckKind.TYPECHECK) is Cardinality.PER_VERSION
    assert check_cardinality("cpp", CheckKind.TEST) is Cardinality.PER_VERSION


def test_cpp_std_stdlib_default_to_v1_pins() -> None:
    """The two-argument call resolves {std}/{stdlib} to the v1 pins."""
    for kind in (CheckKind.INSTALL, CheckKind.TYPECHECK, CheckKind.TEST, CheckKind.LINT):
        for cmd in language_commands("cpp", kind):
            for arg in cmd:
                assert "{std}" not in arg and "{stdlib}" not in arg


def test_cpp_std_stdlib_are_threaded_from_config() -> None:
    """An explicit [cpp] std/stdlib overrides the pins in the cache vars."""
    cmds = language_commands("cpp", CheckKind.INSTALL, cpp_std="c++17", cpp_stdlib="libstdc++")
    cmake_cmd = [c for c in cmds if c[0] == "cmake"][0]
    assert "-DVERGIL_CPP_STD=c++17" in cmake_cmd
    # cppcheck --std is threaded too.
    lint = language_commands("cpp", CheckKind.LINT, cpp_std="c++17")
    cppcheck_cmd = [c for c in lint if c[0] == "cppcheck"][0]
    assert "--std=c++17" in cppcheck_cmd


# -- TypeScript (epic vergil-project/.github#284, T4) -------------------------


def _ts_config_path(name: str) -> Path:
    """Resolve a packaged ``configs/typescript/<name>`` path.

    Derives the packaged ``typescript/`` config directory from a LINT argument
    that carries the expanded ``{configs}`` placeholder, then joins *name* — so
    it resolves files (like ``tsconfig.base.json``) that no command references
    directly, without hard-coding the package layout.
    """
    for cmd in language_commands("typescript", CheckKind.LINT):
        for arg in cmd:
            token = arg.split("=", 1)[-1]
            if "/typescript/" in token:
                return Path(token).parent / name
    msg = "no LINT arg references the typescript configs dir"
    raise AssertionError(msg)


def test_typescript_is_supported() -> None:
    assert "typescript" in supported_languages()


def test_typescript_install_commands() -> None:
    cmds = language_commands("typescript", CheckKind.INSTALL)
    assert cmds == [["npm", "ci"]]


def test_typescript_lint_commands() -> None:
    cmds = language_commands("typescript", CheckKind.LINT)
    joined = _joined(cmds)
    # Prettier format check + ESLint, each against a packaged config.
    assert any(c.startswith("prettier --check .") for c in joined)
    # ESLint runs inside an ``sh -c`` staging wrapper (see below); the
    # invocation itself is still ``eslint . --config <staged .mjs>``.
    assert any("eslint . --config" in c for c in joined)
    # Both reference a packaged {configs}/typescript/* config.
    assert any("/typescript/prettier.config.json" in c for c in joined)
    assert any("/typescript/eslint.config.mjs" in c for c in joined)


def test_typescript_eslint_staged_into_repo_for_esm_resolution() -> None:
    """ESLint's ESM flat config is staged into the repo so its bare imports
    (@eslint/js, typescript-eslint) resolve against the consumer's repo-local
    node_modules rather than the packaged path with no adjacent node_modules
    (#2771)."""
    cmds = language_commands("typescript", CheckKind.LINT)
    eslint = [c for c in cmds if "eslint" in " ".join(c)]
    assert len(eslint) == 1
    wrapper = eslint[0]
    # A shell wrapper is required to copy-in / copy-out the config.
    assert wrapper[:2] == ["sh", "-c"]
    script = wrapper[2]
    staged = "./.vergil-eslint.config.mjs"
    # The packaged config is copied into the repo tree, eslint points at the
    # staged copy (not the packaged path), and the staged file is cleaned up
    # on exit via a trap so nothing is left in the consumer tree.
    assert "cp " in script and "/typescript/eslint.config.mjs" in script
    assert 'eslint . --config "$cfg"' in script
    assert f"cfg={staged}" in script
    assert "trap 'rm -f" in script
    # The staged name is dot-prefixed and .mjs so it is loaded as ESM and is
    # matched by no config entry (never linted as a source file).
    assert staged.endswith(".mjs")


def test_typescript_lint_ban_ts_comment_rule_present() -> None:
    """The no-standing-suppression rule is wired via the packaged ESLint config."""
    text = _ts_config_path("eslint.config.mjs").read_text()
    assert "@typescript-eslint/ban-ts-comment" in text
    # Bare @ts-ignore / @ts-nocheck are banned; @ts-expect-error needs a reason.
    assert '"ts-ignore": true' in text
    assert '"ts-nocheck": true' in text
    assert '"ts-expect-error": "allow-with-description"' in text
    # Type-aware linting is enabled.
    assert "recommendedTypeChecked" in text
    assert "projectService" in text


def test_typescript_lint_uses_packaged_configs() -> None:
    """Every LINT tool points at a packaged {configs}/typescript/* file that exists."""
    cmds = language_commands("typescript", CheckKind.LINT)
    flat = [arg for cmd in cmds for arg in cmd]
    # No unresolved placeholder survives expansion.
    assert all("{configs}" not in arg for arg in flat)
    # Both packaged configs the LINT stage relies on must exist on disk.
    for name in ("prettier.config.json", "eslint.config.mjs"):
        assert _ts_config_path(name).exists(), f"packaged config missing: {name}"
    # Each config is referenced by exactly one LINT tool — prettier by its
    # ``--config`` arg directly, eslint by path inside the staging wrapper.
    assert any("/typescript/prettier.config.json" in arg for arg in flat)
    assert any("/typescript/eslint.config.mjs" in arg for arg in flat)


def test_typescript_typecheck_commands() -> None:
    cmds = language_commands("typescript", CheckKind.TYPECHECK)
    # One canonical type engine; strict set lives in the base tsconfig consumers
    # extend, so the command is a bare --noEmit typecheck.
    assert cmds == [["tsc", "--noEmit"]]


def test_typescript_base_tsconfig_curated_extras() -> None:
    """The curated 'warnings to 11' set is authored in the packaged base tsconfig.

    This task (T4) owns and tests the concrete strictness set (spec §3.2); T8
    only documents it. Every option must be present and enabled, and each is a
    real ``tsc`` 5.x compiler option.
    """
    base = _ts_config_path("tsconfig.base.json")
    # tsconfig.base.json is authored as pure JSON (no comments) so it is both
    # tsc-loadable and directly parseable here — the file *is* the source of
    # truth for the strict set.
    opts = json.loads(base.read_text())["compilerOptions"]
    curated = (
        "strict",
        "noUncheckedIndexedAccess",
        "exactOptionalPropertyTypes",
        "noImplicitOverride",
        "noImplicitReturns",
        "noFallthroughCasesInSwitch",
        "noPropertyAccessFromIndexSignature",
        "noUnusedLocals",
        "noUnusedParameters",
    )
    for opt in curated:
        assert opts.get(opt) is True, f"base tsconfig missing/disabled: {opt}"


def test_typescript_test_commands() -> None:
    cmds = language_commands("typescript", CheckKind.TEST)
    joined = _joined(cmds)
    # Vitest with the V8 coverage provider.
    assert any(c.startswith("vitest run --coverage") for c in joined)
    assert any("--coverage.provider=v8" in c for c in joined)


def test_typescript_test_enforces_100_line_coverage() -> None:
    """The gate must fail under 100% line coverage (spec §5)."""
    cmds = language_commands("typescript", CheckKind.TEST)
    vitest_cmd = [c for c in cmds if c[0] == "vitest"]
    assert len(vitest_cmd) == 1
    assert "--coverage.thresholds.lines=100" in vitest_cmd[0]


def test_typescript_audit_commands() -> None:
    cmds = language_commands("typescript", CheckKind.AUDIT)
    joined = _joined(cmds)
    # npm audit is the CVE scan, scoped to prod deps with an explicit severity
    # threshold (spec §4 caveat 1).
    npm_audit = [c for c in cmds if c[:2] == ["npm", "audit"]]
    assert len(npm_audit) == 1
    assert "--omit=dev" in npm_audit[0]
    assert any(arg.startswith("--audit-level=") for arg in npm_audit[0])
    # OSV-Scanner is the documented contingency, not the v1 default.
    assert not any("osv-scanner" in c for c in joined)
    # Best-effort license-metadata surfacing (hardened gating deferred, §9 #7).
    assert any("license-checker" in c for c in joined)


def test_typescript_audit_license_allowlist_reviewed_set() -> None:
    """The TS license allowlist constant carries the reviewed permissive set."""
    licenses = _TYPESCRIPT_LICENSES_ALLOWLIST.split(";")
    assert "MIT" in licenses
    assert "Apache-2.0" in licenses
    assert "ISC" in licenses
    # Semicolon-separated (the form license-checker --onlyAllow consumes).
    assert ";" in _TYPESCRIPT_LICENSES_ALLOWLIST


def test_typescript_ecosystem_metadata() -> None:
    info = ecosystem_metadata("typescript")
    # v1 mandates no bundler/emit and no publish target (spec §8, ledger #6).
    assert info.build_cmd is None
    assert info.publish_cmd is None
    assert info.publish_env_var is None


def test_typescript_cardinality_typecheck_lint_audit_run_once() -> None:
    assert check_cardinality("typescript", CheckKind.TYPECHECK) is Cardinality.ONCE
    assert check_cardinality("typescript", CheckKind.LINT) is Cardinality.ONCE
    assert check_cardinality("typescript", CheckKind.AUDIT) is Cardinality.ONCE


def test_typescript_cardinality_test_is_per_version() -> None:
    # Only TEST fans out per Node version (§3.6).
    assert check_cardinality("typescript", CheckKind.TEST) is Cardinality.PER_VERSION


def test_typescript_no_unresolved_placeholders() -> None:
    for kind in CheckKind:
        for cmd in language_commands("typescript", kind):
            for arg in cmd:
                assert "{configs}" not in arg, f"Unresolved placeholder in: {arg}"


# -- Edge cases ---------------------------------------------------------------


def test_unknown_language_returns_empty() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


def test_none_language_returns_empty() -> None:
    cmds = language_commands(None, CheckKind.LINT)
    assert cmds == []


def test_none_language_install_returns_empty() -> None:
    cmds = language_commands(None, CheckKind.INSTALL)
    assert cmds == []


def test_configs_placeholder_is_resolved() -> None:
    """Commands containing {configs} must resolve to a real path."""
    cmds = language_commands("ruby", CheckKind.AUDIT)
    for cmd in cmds:
        for arg in cmd:
            assert "{configs}" not in arg, f"Unresolved placeholder in: {arg}"


def test_configs_placeholder_resolves_to_existing_directory() -> None:
    """The resolved {configs} path must point to a real file."""
    cmds = language_commands("ruby", CheckKind.AUDIT)
    license_finder_cmds = [c for c in cmds if c[0] == "license_finder"]
    if not license_finder_cmds:
        return
    decisions_arg = license_finder_cmds[0][1]
    path = decisions_arg.split("=", 1)[1]
    assert Path(path).exists(), f"Resolved path does not exist: {path}"


# -- New API ------------------------------------------------------------------


def test_supported_languages_includes_cpp() -> None:
    langs = supported_languages()
    assert langs == frozenset({"python", "go", "java", "ruby", "rust", "cpp", "typescript"})


def test_supported_languages_is_frozen() -> None:
    langs = supported_languages()
    assert isinstance(langs, frozenset)


def test_ecosystem_metadata_python() -> None:
    info = ecosystem_metadata("python")
    assert isinstance(info, EcosystemInfo)
    assert info.build_cmd is not None
    assert info.publish_cmd is not None
    assert info.publish_env_var is not None


def test_ecosystem_metadata_go() -> None:
    info = ecosystem_metadata("go")
    assert isinstance(info, EcosystemInfo)
    assert info.publish_env_var is None


def test_ecosystem_metadata_all_languages_have_entries() -> None:
    for lang in supported_languages():
        info = ecosystem_metadata(lang)
        assert isinstance(info, EcosystemInfo), f"Missing ecosystem for {lang}"


def test_ecosystem_metadata_unknown_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        ecosystem_metadata("unknown")


def test_language_commands_still_works_for_unknown() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


# -- Check cardinality --------------------------------------------------------


def test_single_image_languages_default_to_per_version_cardinality() -> None:
    """Every single-image language defaults to per-version for every check kind.

    This backward-compatibility guarantee is what keeps their generated CI
    gates byte-identical after the cardinality concept was introduced. C++ and
    TypeScript are the matrix languages that legitimately declare ONCE kinds, so
    they are excluded here and covered by their own cardinality tests.
    """
    for lang in supported_languages() - {"cpp", "typescript"}:
        for kind in CheckKind:
            assert check_cardinality(lang, kind) is Cardinality.PER_VERSION


def test_check_cardinality_unknown_language_defaults_per_version() -> None:
    assert check_cardinality("unknown", CheckKind.LINT) is Cardinality.PER_VERSION


def test_check_cardinality_none_language_defaults_per_version() -> None:
    assert check_cardinality(None, CheckKind.LINT) is Cardinality.PER_VERSION


def test_language_cardinality_defaults_to_empty_mapping() -> None:
    """A Language that declares no cardinality carries an empty mapping."""
    lang = Language(
        name="x",
        checks={},
        ecosystem=EcosystemInfo(build_cmd=None, publish_cmd=None, publish_env_var=None),
    )
    assert lang.cardinality == {}


def test_language_may_declare_once_cardinality() -> None:
    """A language can declare a kind as run-once (e.g. a matrix language)."""
    lang = Language(
        name="x",
        checks={},
        ecosystem=EcosystemInfo(build_cmd=None, publish_cmd=None, publish_env_var=None),
        cardinality={CheckKind.LINT: Cardinality.ONCE},
    )
    assert lang.cardinality[CheckKind.LINT] is Cardinality.ONCE


def test_cardinality_enum_values() -> None:
    assert Cardinality.PER_VERSION.value == "per-version"
    assert Cardinality.ONCE.value == "once"


# -- parse_cpp_version_tag ----------------------------------------------------


def test_parse_cpp_version_tag_clang() -> None:
    assert parse_cpp_version_tag("clang-20") == ("clang", "20")


def test_parse_cpp_version_tag_gcc() -> None:
    assert parse_cpp_version_tag("gcc-15") == ("gcc", "15")


def test_parse_cpp_version_tag_unrecognized_prefix_returns_none() -> None:
    # A tag with no clang-/gcc- prefix carries no compiler family.
    assert parse_cpp_version_tag("latest") is None
    assert parse_cpp_version_tag("20") is None


def test_parse_cpp_version_tag_empty_returns_none() -> None:
    assert parse_cpp_version_tag("") is None
