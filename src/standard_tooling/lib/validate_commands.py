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
        CheckKind.TYPECHECK: ["mypy src/", "ty check src"],
        CheckKind.TEST: ["pytest --cov --cov-branch --cov-fail-under=100"],
        CheckKind.AUDIT: [
            "uv sync --check --frozen --group dev",
            "uv lock --check",
            "pip-audit -r requirements.txt -r requirements-dev.txt",
            "pip-licenses --allow-only=<allowlist>",
        ],
    },
    "go": {
        CheckKind.LINT: ["golangci-lint run ./...", "gocyclo -over 15 ."],
        CheckKind.TYPECHECK: ["go vet ./..."],
        CheckKind.TEST: [
            "go test -race -count=1 -coverprofile=coverage.out ./...",
            "go-test-coverage --config .testcoverage.yml",
        ],
        CheckKind.AUDIT: ["govulncheck ./...", "go-licenses check ./..."],
    },
    "java": {
        CheckKind.LINT: ["./mvnw spotless:check checkstyle:check -B"],
        CheckKind.TYPECHECK: ["./mvnw compile -B"],
        CheckKind.TEST: ["./mvnw verify -B"],
        CheckKind.AUDIT: [
            "./mvnw dependency:tree -B -q",
            "./mvnw org.codehaus.mojo:license-maven-plugin:add-third-party -B",
        ],
    },
    "ruby": {
        CheckKind.LINT: ["bundle exec rubocop"],
        CheckKind.TYPECHECK: ["bundle exec steep check"],
        CheckKind.TEST: ["bundle exec rake"],
        CheckKind.AUDIT: ["bundle exec bundle-audit check --update"],
    },
    "rust": {
        CheckKind.LINT: ["cargo fmt --all -- --check", "cargo clippy -- -D warnings"],
        CheckKind.TYPECHECK: ["cargo check"],
        CheckKind.TEST: ["cargo llvm-cov --fail-under-lines 100"],
        CheckKind.AUDIT: ["cargo deny check"],
    },
}


def language_commands(language: str, kind: CheckKind) -> list[str]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.
    """
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    return list(lang_entry.get(kind, []))
