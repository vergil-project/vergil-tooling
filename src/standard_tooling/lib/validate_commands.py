"""Per-language validation command registry.

Defines the canonical commands for lint, typecheck, test, and audit
per supported language. These are not configurable per-repo — the
standard defines them centrally.
"""

from __future__ import annotations

from enum import Enum


class CheckKind(Enum):
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"


_REGISTRY: dict[str, dict[CheckKind, list[str]]] = {
    "python": {
        CheckKind.LINT: ["ruff check", "ruff format --check ."],
        CheckKind.TYPECHECK: ["mypy src/"],
        CheckKind.TEST: ["pytest --cov --cov-branch --cov-fail-under=100"],
        CheckKind.AUDIT: [
            "uv sync --check --frozen --group dev",
            "uv lock --check",
        ],
    },
    "go": {
        CheckKind.LINT: ["golangci-lint run", "gocyclo -over 15 ."],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["go test -coverprofile=coverage.out ./..."],
        CheckKind.AUDIT: ["govulncheck ./..."],
    },
    "java": {
        CheckKind.LINT: ["./mvnw spotless:check checkstyle:check"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["./mvnw verify"],
        CheckKind.AUDIT: [],
    },
    "ruby": {
        CheckKind.LINT: ["rubocop"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["rake"],
        CheckKind.AUDIT: [],
    },
    "rust": {
        CheckKind.LINT: ["cargo fmt --check", "cargo clippy"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["cargo llvm-cov --fail-under-lines 100"],
        CheckKind.AUDIT: ["cargo audit"],
    },
}


def language_commands(language: str, kind: CheckKind) -> list[str]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is unknown or has no commands
    for the given kind.
    """
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    return list(lang_entry.get(kind, []))
