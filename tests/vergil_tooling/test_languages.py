"""Tests for vergil_tooling.lib.languages."""

from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.languages import (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    LICENSES_REPORT,
    PIP_AUDIT_REPORT,
    PYTHON_REPORT_FILES,
    Cardinality,
    CheckKind,
    EcosystemInfo,
    Language,
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


def test_python_typecheck_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.TYPECHECK))
    assert "mypy src/" in joined
    assert "ty check src tests" in joined


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
        "coverage.xml",
        "junit.xml",
        "pip-audit.json",
        "licenses.json",
    )
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
    # A conan.lock is produced so AUDIT (OSV-Scanner) has a lockfile to scan.
    assert any("conan lock create" in c for c in joined)
    # Conan resolves deps in the same build_type (Debug) as the CMake
    # coverage/sanitizer builds — a Release/Debug mismatch broke the cold
    # rebuild in T11 (#2558): fmt/format.h not found. (#2572)
    assert "conan install . -s build_type=Debug --build=missing" in joined
    # Both conan steps pin build_type=Debug to stay consistent with the build.
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
    assert any(
        "cppcheck" in c and "--enable=all" in c and "--error-exitcode=1" in c for c in joined
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


def test_cpp_audit_commands() -> None:
    joined = _joined(language_commands("cpp", CheckKind.AUDIT))
    # OSV-Scanner over conan.lock is the CVE scan — tokenless and scalable,
    # replacing conan audit (SaaS token, rate-limited). Decision: #209. (#2572)
    assert "osv-scanner scan source --lockfile=conan.lock" in joined
    # conan audit is retired here.
    assert not any("conan audit" in c for c in joined)
    # Best-effort license-metadata surfacing (hardened gating deferred, §9 #7).
    assert any("conan graph info" in c for c in joined)


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
    assert langs == frozenset({"python", "go", "java", "ruby", "rust", "cpp"})


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
    gates byte-identical after the cardinality concept was introduced. C++ is
    the one matrix language that legitimately declares ONCE kinds, so it is
    excluded here and covered by the C++-specific cardinality tests.
    """
    for lang in supported_languages() - {"cpp"}:
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
