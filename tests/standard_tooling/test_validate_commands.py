"""Tests for standard_tooling.lib.validate_commands."""

from __future__ import annotations

from standard_tooling.lib.validate_commands import (
    CheckKind,
    language_commands,
)

# -- Python ------------------------------------------------------------------


def test_python_install_commands() -> None:
    cmds = language_commands("python", CheckKind.INSTALL)
    assert cmds == ["uv sync --frozen --group dev"]


def test_python_lint_commands() -> None:
    cmds = language_commands("python", CheckKind.LINT)
    assert cmds == ["ruff check src/ tests/", "ruff format --check src/ tests/"]


def test_python_typecheck_commands() -> None:
    cmds = language_commands("python", CheckKind.TYPECHECK)
    assert "mypy src/ tests/" in cmds
    assert "ty check src tests" in cmds


def test_python_test_commands() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    assert any("pytest" in c for c in cmds)
    assert any("--cov=src" in c for c in cmds)


def test_python_audit_commands() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    assert any("uv sync --check" in c for c in cmds)
    assert any("uv lock --check" in c for c in cmds)
    assert "pip-audit" in cmds
    assert any("pip-licenses" in c for c in cmds)


# -- Go ----------------------------------------------------------------------


def test_go_install_commands() -> None:
    cmds = language_commands("go", CheckKind.INSTALL)
    assert cmds == ["go mod download"]


def test_go_lint_commands() -> None:
    cmds = language_commands("go", CheckKind.LINT)
    assert "golangci-lint run ./..." in cmds
    assert any("gocyclo" in c for c in cmds)


def test_go_typecheck_commands() -> None:
    cmds = language_commands("go", CheckKind.TYPECHECK)
    assert "go vet ./..." in cmds


def test_go_test_commands() -> None:
    cmds = language_commands("go", CheckKind.TEST)
    assert any("go test" in c for c in cmds)
    assert any("go-test-coverage" in c for c in cmds)


def test_go_audit_commands() -> None:
    cmds = language_commands("go", CheckKind.AUDIT)
    assert any("govulncheck" in c for c in cmds)
    assert any("go-licenses" in c for c in cmds)


# -- Java ---------------------------------------------------------------------


def test_java_install_commands() -> None:
    cmds = language_commands("java", CheckKind.INSTALL)
    assert cmds == ["./mvnw dependency:resolve -B"]


def test_java_lint_commands() -> None:
    cmds = language_commands("java", CheckKind.LINT)
    assert any("spotless:check" in c for c in cmds)
    assert any("checkstyle:check" in c for c in cmds)


def test_java_typecheck_commands() -> None:
    cmds = language_commands("java", CheckKind.TYPECHECK)
    assert any("compile" in c for c in cmds)


def test_java_test_commands() -> None:
    cmds = language_commands("java", CheckKind.TEST)
    assert any("verify" in c for c in cmds)


def test_java_audit_commands() -> None:
    cmds = language_commands("java", CheckKind.AUDIT)
    assert any("dependency:tree" in c for c in cmds)
    assert any("license-maven-plugin" in c for c in cmds)


# -- Ruby ---------------------------------------------------------------------


def test_ruby_install_commands() -> None:
    cmds = language_commands("ruby", CheckKind.INSTALL)
    assert cmds == ["bundle install --jobs 4"]


def test_ruby_lint_commands() -> None:
    cmds = language_commands("ruby", CheckKind.LINT)
    assert any("rubocop" in c for c in cmds)


def test_ruby_typecheck_commands() -> None:
    cmds = language_commands("ruby", CheckKind.TYPECHECK)
    assert any("steep check" in c for c in cmds)


def test_ruby_test_commands() -> None:
    cmds = language_commands("ruby", CheckKind.TEST)
    assert any("rake" in c for c in cmds)


def test_ruby_audit_commands() -> None:
    cmds = language_commands("ruby", CheckKind.AUDIT)
    assert any("bundle-audit" in c for c in cmds)


# -- Rust ---------------------------------------------------------------------


def test_rust_install_commands() -> None:
    cmds = language_commands("rust", CheckKind.INSTALL)
    assert cmds == ["cargo fetch"]


def test_rust_lint_commands() -> None:
    cmds = language_commands("rust", CheckKind.LINT)
    assert any("cargo fmt" in c for c in cmds)
    assert any("cargo clippy" in c for c in cmds)


def test_rust_typecheck_commands() -> None:
    cmds = language_commands("rust", CheckKind.TYPECHECK)
    assert "cargo check" in cmds


def test_rust_test_commands() -> None:
    cmds = language_commands("rust", CheckKind.TEST)
    assert any("cargo llvm-cov" in c for c in cmds)


def test_rust_audit_commands() -> None:
    cmds = language_commands("rust", CheckKind.AUDIT)
    assert "cargo deny check" in cmds


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
