"""Per-language validation command registry.

Defines the canonical commands for install, lint, typecheck, test,
and audit per supported language. These are not configurable
per-repo — the standard defines them centrally.
"""

from __future__ import annotations

from enum import Enum
from importlib.resources import files


class CheckKind(Enum):
    INSTALL = "install"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"


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

_REGISTRY: dict[str, dict[CheckKind, list[list[str]]]] = {
    "python": {
        CheckKind.INSTALL: [["uv", "sync", "--frozen", "--group", "dev"]],
        CheckKind.LINT: [
            ["ruff", "check", "src/", "tests/"],
            ["ruff", "format", "--check", "src/", "tests/"],
        ],
        CheckKind.TYPECHECK: [["mypy", "src/"], ["ty", "check", "src", "tests"]],
        CheckKind.TEST: [["pytest", "--cov=src", "--cov-branch", "--cov-fail-under=100"]],
        CheckKind.AUDIT: [
            ["uv", "sync", "--check", "--frozen", "--group", "dev"],
            ["uv", "lock", "--check"],
            ["pip-audit"],
            ["pip-licenses", f"--allow-only={_PIP_LICENSES_ALLOWLIST}"],
        ],
    },
    "go": {
        CheckKind.INSTALL: [["go", "mod", "download"]],
        CheckKind.LINT: [["golangci-lint", "run", "./..."], ["gocyclo", "-over", "15", "."]],
        CheckKind.TYPECHECK: [["go", "vet", "./..."]],
        CheckKind.TEST: [
            ["go", "test", "-race", "-count=1", "-coverprofile=coverage.out", "./..."],
            ["go-test-coverage", "--config", ".testcoverage.yml"],
        ],
        CheckKind.AUDIT: [
            ["govulncheck", "./..."],
            ["go-licenses", "check", "./...", f"--allowed_licenses={_GO_LICENSES_ALLOWLIST}"],
        ],
    },
    "java": {
        CheckKind.INSTALL: [["./mvnw", "dependency:resolve", "-B"]],
        CheckKind.LINT: [["./mvnw", "spotless:check", "checkstyle:check", "-B"]],
        CheckKind.TYPECHECK: [["./mvnw", "compile", "-B"]],
        CheckKind.TEST: [["./mvnw", "verify", "-B"]],
        CheckKind.AUDIT: [
            ["./mvnw", "dependency:tree", "-B", "-q"],
            ["./mvnw", "org.codehaus.mojo:license-maven-plugin:add-third-party", "-B"],
        ],
    },
    "ruby": {
        CheckKind.INSTALL: [["bundle", "install", "--jobs", "4"]],
        CheckKind.LINT: [["bundle", "exec", "rubocop"]],
        CheckKind.TYPECHECK: [["bundle", "exec", "steep", "check"]],
        CheckKind.TEST: [["bundle", "exec", "rake"]],
        CheckKind.AUDIT: [
            ["bundle", "exec", "bundle-audit", "check", "--update"],
            ["license_finder", "--decisions-file={configs}/ruby/license_finder.yml"],
        ],
    },
    "rust": {
        CheckKind.INSTALL: [["cargo", "fetch"]],
        CheckKind.LINT: [
            ["cargo", "fmt", "--all", "--", "--check"],
            ["cargo", "clippy", "--", "-D", "warnings"],
        ],
        CheckKind.TYPECHECK: [["cargo", "check"]],
        CheckKind.TEST: [["cargo", "llvm-cov", "--fail-under-lines", "100"]],
        CheckKind.AUDIT: [["cargo", "deny", "check"]],
    },
}


def language_commands(language: str, kind: CheckKind) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    Any argument containing ``{configs}`` is expanded to the resolved
    path of the ``standard_tooling.configs`` package directory.
    """
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    configs_dir = str(files("standard_tooling.configs"))
    return [
        [arg.replace("{configs}", configs_dir) for arg in cmd] for cmd in lang_entry.get(kind, [])
    ]
