"""Read per-repo configuration from ``standard-tooling.toml``."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_FILE = "standard-tooling.toml"

_COAUTHOR_RE = re.compile(r"^Co-Authored-By:\s+.+\s+<.+>$")

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
    """Raised when standard-tooling.toml has invalid content."""


@dataclass
class ProjectConfig:
    repository_type: str
    versioning_scheme: str
    branching_model: str
    release_model: str
    primary_language: str
    co_authors: dict[str, str]


@dataclass
class MarkdownlintConfig:
    ignore: list[str]


@dataclass
class CiConfig:
    versions: list[str]
    integration_tests: bool


@dataclass
class GithubOverrides:
    skip_rulesets: bool


@dataclass
class PublishConfig:
    release: bool
    docs: bool


@dataclass
class StConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig | None
    github: GithubOverrides
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

    co_authors: dict[str, str] = {}
    co_authors_raw = project_raw.get("co-authors", {})
    for name, trailer in co_authors_raw.items():
        if not _COAUTHOR_RE.match(trailer):
            msg = f"{CONFIG_FILE}: malformed co-author trailer for '{name}': {trailer!r}"
            raise ConfigError(msg)
        co_authors[name] = trailer

    deps = raw.get("dependencies", {})
    if "standard-tooling" not in deps:
        msg = f"{CONFIG_FILE}: [dependencies] must contain 'standard-tooling'"
        raise ConfigError(msg)

    ml_raw = raw.get("markdownlint", {})
    ml_ignore = ml_raw.get("ignore", [])
    if not isinstance(ml_ignore, list) or not all(isinstance(p, str) for p in ml_ignore):
        msg = f"{CONFIG_FILE}: [markdownlint].ignore must be a list of strings"
        raise ConfigError(msg)
    markdownlint = MarkdownlintConfig(ignore=ml_ignore)

    ci_raw = raw.get("ci")
    ci: CiConfig | None = None
    if ci_raw is not None:
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

    github_raw = raw.get("github", {})
    github_overrides = GithubOverrides(
        skip_rulesets=bool(github_raw.get("skip-rulesets", False)),
    )

    publish_raw = raw.get("publish", {})
    publish = PublishConfig(
        release=bool(publish_raw.get("release", False)),
        docs=bool(publish_raw.get("docs", True)),
    )

    project = ProjectConfig(
        repository_type=project_raw["repository-type"],
        versioning_scheme=project_raw["versioning-scheme"],
        branching_model=project_raw["branching-model"],
        release_model=project_raw["release-model"],
        primary_language=project_raw["primary-language"],
        co_authors=co_authors,
    )
    return StConfig(
        project=project,
        dependencies=dict(deps),
        markdownlint=markdownlint,
        ci=ci,
        github=github_overrides,
        publish=publish,
    )


def read_config(repo_root: Path) -> StConfig:
    """Parse, validate, and return ``standard-tooling.toml``."""
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


def st_install_tag(repo_root: Path) -> str:
    """Return the ``[dependencies].standard-tooling`` value for runtime install.

    Checks ``ST_DOCKER_INSTALL_TAG`` env var first (override).
    """
    override = os.environ.get("ST_DOCKER_INSTALL_TAG")
    if override:
        return override
    cfg = read_config(repo_root)
    return cfg.dependencies["standard-tooling"]
