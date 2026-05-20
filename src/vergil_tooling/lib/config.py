"""Read per-repo configuration from ``vergil.toml``."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_FILE = "vergil.toml"

_ENUMS: dict[str, set[str]] = {
    "repository-type": {"library", "application", "infrastructure", "tooling", "documentation"},
    "versioning-scheme": {"library", "semver", "application", "none"},
    "branching-model": {"library-release", "application-promotion", "docs-single-branch"},
    "release-model": {"artifact-publishing", "tagged-release", "environment-promotion", "none"},
    "primary-language": {"python", "go", "java", "ruby", "rust", "shell", "none", "claude-plugin"},
}

_PROJECT_FIELDS = (
    "repository-type",
    "versioning-scheme",
    "branching-model",
    "release-model",
    "primary-language",
)


class ConfigError(Exception):
    """Raised when vergil.toml has invalid content."""


@dataclass
class ProjectConfig:
    repository_type: str
    versioning_scheme: str
    branching_model: str
    release_model: str
    primary_language: str


@dataclass
class MarkdownlintConfig:
    ignore: list[str]


@dataclass
class CiConfig:
    versions: list[str]
    integration_tests: bool


@dataclass
class PublishConfig:
    release: bool
    docs: bool
    consumer_refresh: str | None
    docs_workflow: str


@dataclass
class StConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig
    publish: PublishConfig


def _parse_raw_config(raw: dict[str, Any]) -> StConfig:
    """Parse and validate a raw TOML dict into StConfig."""
    project_raw = raw.get("project", {})

    for field in _PROJECT_FIELDS:
        if field not in project_raw or not project_raw[field]:
            msg = f"{CONFIG_FILE}: missing or empty required field '{field}'"
            raise ConfigError(msg)

    for field in _PROJECT_FIELDS:
        value = project_raw[field]
        if value not in _ENUMS[field]:
            allowed = ", ".join(sorted(_ENUMS[field]))
            msg = f"{CONFIG_FILE}: invalid {field} '{value}' (allowed: {allowed})"
            raise ConfigError(msg)

    deps = raw.get("dependencies", {})
    if "vergil" not in deps:
        msg = f"{CONFIG_FILE}: [dependencies] must contain 'vergil'"
        raise ConfigError(msg)

    ml_raw = raw.get("markdownlint", {})
    ml_ignore = ml_raw.get("ignore", [])
    if not isinstance(ml_ignore, list) or not all(isinstance(p, str) for p in ml_ignore):
        msg = f"{CONFIG_FILE}: [markdownlint].ignore must be a list of strings"
        raise ConfigError(msg)
    markdownlint = MarkdownlintConfig(ignore=ml_ignore)

    ci_raw = raw.get("ci")
    if ci_raw is None:
        msg = f"{CONFIG_FILE}: missing required section [ci]"
        raise ConfigError(msg)
    versions = ci_raw.get("versions")
    if versions is None:
        msg = f"{CONFIG_FILE}: [ci] missing required field 'versions'"
        raise ConfigError(msg)
    if not isinstance(versions, list) or not versions:
        msg = f"{CONFIG_FILE}: [ci].versions must be a list with at least one entry"
        raise ConfigError(msg)
    if not all(isinstance(v, str) for v in versions):
        msg = f"{CONFIG_FILE}: [ci].versions entries must be strings"
        raise ConfigError(msg)
    ci = CiConfig(
        versions=versions,
        integration_tests=bool(ci_raw.get("integration-tests", False)),
    )

    publish_raw = raw.get("publish", {})
    publish = PublishConfig(
        release=bool(publish_raw.get("release", False)),
        docs=bool(publish_raw.get("docs", True)),
        consumer_refresh=publish_raw.get("consumer-refresh"),
        docs_workflow=str(publish_raw.get("docs-workflow", "Documentation")),
    )

    project = ProjectConfig(
        repository_type=project_raw["repository-type"],
        versioning_scheme=project_raw["versioning-scheme"],
        branching_model=project_raw["branching-model"],
        release_model=project_raw["release-model"],
        primary_language=project_raw["primary-language"],
    )
    return StConfig(
        project=project,
        dependencies=dict(deps),
        markdownlint=markdownlint,
        ci=ci,
        publish=publish,
    )


def read_config(repo_root: Path) -> StConfig:
    """Parse, validate, and return ``vergil.toml``."""
    config_path = repo_root / CONFIG_FILE
    if not config_path.is_file():
        msg = f"{CONFIG_FILE} not found at {repo_root}"
        raise FileNotFoundError(msg)

    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{CONFIG_FILE} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc

    return _parse_raw_config(raw)


def vrg_install_tag(repo_root: Path) -> str:
    """Return the ``[dependencies].vergil`` value for runtime install.

    Checks ``VRG_DOCKER_INSTALL_TAG`` env var first (override).
    """
    override = os.environ.get("VRG_DOCKER_INSTALL_TAG")
    if override:
        return override
    cfg = read_config(repo_root)
    return cfg.dependencies["vergil"]
