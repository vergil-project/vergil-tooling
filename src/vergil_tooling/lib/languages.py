"""Unified per-language metadata registry.

Defines validation commands, build/publish commands, and credential
metadata for each supported language. This is the single source of
truth for all per-language data — update one place when adding a
language.

Replaces the former ``validate_commands`` module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.resources import files


class CheckKind(Enum):
    INSTALL = "install"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"


@dataclass(frozen=True)
class EcosystemInfo:
    build_cmd: list[str] | None
    publish_cmd: list[str] | None
    credential_secret: str | None


@dataclass(frozen=True)
class Language:
    name: str
    checks: dict[CheckKind, list[list[str]]]
    ecosystem: EcosystemInfo


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
                ["pytest", "--cov=src", "--cov-branch", "--cov-fail-under=100"],
            ],
            CheckKind.AUDIT: [
                ["uv", "sync", "--check", "--frozen", "--group", "dev"],
                ["uv", "lock", "--check"],
                ["pip-audit"],
                ["pip-licenses", f"--allow-only={_PIP_LICENSES_ALLOWLIST}"],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["uv", "build"],
            publish_cmd=["uv", "publish"],
            credential_secret="PYPI_TOKEN",
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
            credential_secret=None,
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
            credential_secret="MAVEN_GPG_PASSPHRASE",
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
            credential_secret="RUBYGEMS_API_KEY",
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
            credential_secret="CRATES_IO_TOKEN",
        ),
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


def language_commands(language: str, kind: CheckKind) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    Any argument containing ``{configs}`` is expanded to the resolved
    path of the ``vergil_tooling.configs`` package directory.
    """
    entry = _REGISTRY.get(language)
    if entry is None:
        return []
    configs_dir = str(files("vergil_tooling.configs"))
    return [
        [arg.replace("{configs}", configs_dir) for arg in cmd]
        for cmd in entry.checks.get(kind, [])
    ]
