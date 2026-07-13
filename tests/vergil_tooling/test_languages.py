"""Tests for vergil_tooling.lib.languages."""

from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.languages import (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    LICENSES_REPORT,
    PIP_AUDIT_REPORT,
    PYTHON_REPORT_FILES,
    CheckKind,
    EcosystemInfo,
    ecosystem_metadata,
    language_commands,
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


def test_supported_languages_returns_five() -> None:
    langs = supported_languages()
    assert langs == frozenset({"python", "go", "java", "ruby", "rust"})


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
