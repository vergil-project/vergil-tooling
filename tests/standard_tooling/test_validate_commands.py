"""Tests for standard_tooling.lib.validate_commands."""

from __future__ import annotations

from standard_tooling.lib.validate_commands import (
    CheckKind,
    language_commands,
)


def test_python_lint_commands() -> None:
    cmds = language_commands("python", CheckKind.LINT)
    assert cmds == ["ruff check", "ruff format --check ."]


def test_python_typecheck_commands() -> None:
    cmds = language_commands("python", CheckKind.TYPECHECK)
    assert cmds == ["mypy src/"]


def test_python_test_commands() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    assert cmds == ["pytest --cov --cov-branch --cov-fail-under=100"]


def test_python_audit_commands() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    assert cmds == ["uv sync --check --frozen --group dev", "uv lock --check"]


def test_go_lint_commands() -> None:
    cmds = language_commands("go", CheckKind.LINT)
    assert "golangci-lint run" in cmds


def test_go_test_commands() -> None:
    cmds = language_commands("go", CheckKind.TEST)
    assert any("go test" in c for c in cmds)


def test_rust_lint_commands() -> None:
    cmds = language_commands("rust", CheckKind.LINT)
    assert "cargo fmt --check" in cmds
    assert "cargo clippy" in cmds


def test_unknown_language_returns_empty() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


def test_language_with_no_typecheck() -> None:
    cmds = language_commands("go", CheckKind.TYPECHECK)
    assert cmds == []
