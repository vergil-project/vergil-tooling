"""Tests for standard_tooling.lib.validate_commands."""

from __future__ import annotations

from standard_tooling.lib.validate_commands import (
    CheckKind,
    language_commands,
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


def test_python_audit_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.AUDIT))
    assert any("uv sync --check" in c for c in joined)
    assert any("uv lock --check" in c for c in joined)
    assert "pip-audit" in joined
    assert any("pip-licenses" in c for c in joined)


def test_python_audit_pip_licenses_allowlist_intact() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"]
    assert len(pip_licenses_cmd) == 1
    assert len(pip_licenses_cmd[0]) == 2
    assert pip_licenses_cmd[0][1].startswith("--allow-only=")


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
    assert any("go-licenses" in c for c in joined)


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


def test_shell_language_returns_empty() -> None:
    cmds = language_commands("shell", CheckKind.LINT)
    assert cmds == []


def test_shell_install_commands() -> None:
    cmds = language_commands("shell", CheckKind.INSTALL)
    assert cmds == []


def test_none_language_returns_empty() -> None:
    cmds = language_commands("none", CheckKind.LINT)
    assert cmds == []
