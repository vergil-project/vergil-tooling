"""Read per-repo configuration from ``vergil.toml``."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from vergil_tooling.lib.vm_spec import validate_instance_name

CONFIG_FILE = "vergil.toml"

# The default in-container validation entry point. Repos may override this in
# [validation].container-command (e.g. the self-repo, which activates its local
# dev version via "uv run vrg-validate"). vrg-container-run resolves the
# override from the target repo so cross-repo agents pick it up at execution
# time, independent of which CLAUDE.md their session loaded (issue #1433).
DEFAULT_VALIDATION_COMMAND = "vrg-validate"

_ENUMS: dict[str, set[str]] = {
    "repository-type": {"library", "application", "infrastructure", "tooling", "documentation"},
    "versioning-scheme": {"library", "semver", "application", "none"},
    "branching-model": {"library-release", "application-promotion", "docs-single-branch"},
    "release-model": {"artifact-publishing", "tagged-release", "environment-promotion", "none"},
    "primary-language": {"python", "go", "java", "ruby", "rust"},
}

_REQUIRED_PROJECT_FIELDS = (
    "repository-type",
    "versioning-scheme",
    "branching-model",
    "release-model",
)

_PROJECT_FIELDS = (*_REQUIRED_PROJECT_FIELDS, "primary-language", "ghas")

_KNOWN_SECTIONS = frozenset(
    {"project", "dependencies", "markdownlint", "ci", "publish", "container", "validation", "vm"},
)

_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "project": frozenset(_PROJECT_FIELDS),
    "dependencies": frozenset({"vergil"}),
    "markdownlint": frozenset({"ignore"}),
    "ci": frozenset({"versions", "integration-tests"}),
    "publish": frozenset({"release", "docs", "consumer-refresh"}),
    "container": frozenset({"env-prefixes"}),
    "validation": frozenset({"container-command"}),
}


class ConfigError(Exception):
    """Raised when vergil.toml has invalid content."""


@dataclass
class ProjectConfig:
    repository_type: str
    versioning_scheme: str
    branching_model: str
    release_model: str
    primary_language: str | None
    ghas: bool | None = None


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


@dataclass
class ContainerConfig:
    env_prefixes: list[str]


@dataclass
class ValidationConfig:
    container_command: str


@dataclass
class VergilConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig
    publish: PublishConfig
    container: ContainerConfig
    validation: ValidationConfig
    vm: VmStanza | None = None


@dataclass
class RoleOverlay:
    packages: list[str]
    cpus: int | None
    memory: str | None
    disk: str | None
    stale_days: int | None
    apt_repos: list[dict[str, str]]
    vagrant_plugins: list[str]
    port_forwards: list[str]
    nested: bool | None = None
    # Off-platform (cloud) backend keys (vergil-vm #199 / #1706). Scalars, last-wins
    # through the [vm] ⊕ [vm.<identity>] cascade. They carry no driver behaviour at
    # this layer; backend = "off-platform" is what flips the downstream dispatcher
    # from Lima to OpenTofu. compose_vm_spec validates the required-key set.
    backend: str | None = None
    provider: str | None = None
    region: str | None = None
    instance: str | None = None
    volume: str | None = None
    zone: str | None = None
    # Named-instance overlays (vergil-tooling #1831). Each value is itself a
    # RoleOverlay parsed from [vm.<identity>.instances.<name>]; an instance overlay
    # never carries its own nested instances. Empty for the common (unnamed) case.
    instances: dict[str, RoleOverlay] = field(default_factory=dict)


@dataclass
class VmStanza:
    packages: list[str]
    cpus: int | None
    memory: str | None
    disk: str | None
    stale_days: int | None
    apt_repos: list[dict[str, str]]
    vagrant_plugins: list[str]
    port_forwards: list[str]
    roles: dict[str, RoleOverlay]
    nested: bool | None = None
    shared_from: tuple[str, str] | None = None
    # Off-platform (cloud) backend keys — see RoleOverlay.
    backend: str | None = None
    provider: str | None = None
    region: str | None = None
    instance: str | None = None
    volume: str | None = None
    zone: str | None = None


# Recognized keys in a [vm] / [vm.<role>] table. apt_repos is a list of tables
# (key + apt source line); vagrant_plugins is a list of plugin names. The
# vergil-vm template owns *how* these install — repos never supply a script.
# port_forwards is a list of "<port>|<host:port>" records the template relays
# via systemd-socket-proxyd (vergil-vm #170). nested enables Lima nested
# virtualization for the profile (issue #1447); default off, requires macOS 15+
# on M3-or-later Apple silicon at create time.
_VM_KEYS = frozenset(
    {
        "cpus",
        "memory",
        "disk",
        "stale_days",
        "packages",
        "apt_repos",
        "vagrant_plugins",
        "port_forwards",
        "nested",
        "backend",
        "provider",
        "region",
        "instance",
        "volume",
        "zone",
    }
)

# The off-platform string scalars. Type is checked at parse time (each must be a
# string when present); the *required-when-off-platform* contract and the value
# enums/formats are enforced at composition (compose_vm_spec), where the cascade
# is resolved to one effective value per key.
_VM_STR_SCALARS = ("backend", "provider", "region", "instance", "volume", "zone")


def _vm_str_scalar(raw: dict[str, Any], key: str, ctx: str, source: str) -> str | None:
    """Return a [vm]/[vm.<role>] string scalar, erroring if present but non-string."""
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str):
        msg = f"{source}: {ctx} '{key}' must be a string (got {type(value).__name__})"
        raise ConfigError(msg)
    return value


_SHARED_FROM_KEY = "shared_from"
_ORG_REPO_PARTS = 2


def _parse_shared_from(value: Any, source: str) -> tuple[str, str]:
    """Validate a ``[vm].shared_from`` value and return ``(org, repo)``."""
    if not isinstance(value, str):
        msg = f"{source}: [vm].shared_from must be a string"
        raise ConfigError(msg)
    if any(c.isspace() for c in value):
        msg = f"{source}: [vm].shared_from must not contain whitespace (got {value!r})"
        raise ConfigError(msg)
    parts = value.split("/")
    if len(parts) != _ORG_REPO_PARTS or not all(parts):
        msg = f"{source}: [vm].shared_from must be 'org/repo' (got {value!r})"
        raise ConfigError(msg)
    return (parts[0], parts[1])


def _parse_role_overlay(
    name: str, raw: dict[str, Any], source: str = CONFIG_FILE, *, allow_instances: bool = True
) -> RoleOverlay:
    if _SHARED_FROM_KEY in raw:
        msg = f"{source}: shared_from is not allowed in a role overlay [vm.{name}]"
        raise ConfigError(msg)
    for key in raw:
        if key == "instances" and allow_instances:
            continue
        if key == "instances" and not allow_instances:
            msg = f"{source}: [vm.{name}.instances.{name}] must not contain nested instances"
            raise ConfigError(msg)
        if key not in _VM_KEYS:
            print(f"{source}: unrecognized key '{key}' in [vm.{name}]", file=sys.stderr)
    scalars = {k: _vm_str_scalar(raw, k, f"[vm.{name}]", source) for k in _VM_STR_SCALARS}
    instances: dict[str, RoleOverlay] = {}
    if allow_instances:
        raw_instances = raw.get("instances", {})
        if not isinstance(raw_instances, dict):
            msg = f"{source}: [vm.{name}].instances must be a table"
            raise ConfigError(msg)
        for iname, itable in raw_instances.items():
            try:
                validate_instance_name(iname)
            except ValueError as exc:
                msg = f"{source}: [vm.{name}.instances.{iname}]: {exc}"
                raise ConfigError(msg) from exc
            if not isinstance(itable, dict):
                msg = f"{source}: [vm.{name}.instances.{iname}] must be a table"
                raise ConfigError(msg)
            instances[iname] = _parse_role_overlay(iname, itable, source, allow_instances=False)
    return RoleOverlay(
        packages=list(raw.get("packages", [])),
        cpus=raw.get("cpus"),
        memory=raw.get("memory"),
        disk=raw.get("disk"),
        stale_days=raw.get("stale_days"),
        apt_repos=list(raw.get("apt_repos", [])),
        vagrant_plugins=list(raw.get("vagrant_plugins", [])),
        port_forwards=list(raw.get("port_forwards", [])),
        nested=raw.get("nested"),
        instances=instances,
        **scalars,
    )


def parse_vm_stanza(raw: dict[str, Any], source: str = CONFIG_FILE) -> VmStanza | None:
    """Parse the repo ``[vm]`` cascade. Returns None when no ``[vm]`` section exists."""
    vm_raw = raw.get("vm")
    if vm_raw is None:
        return None
    roles: dict[str, RoleOverlay] = {}
    fields: dict[str, Any] = {}
    shared_from: tuple[str, str] | None = None
    for key, value in vm_raw.items():
        if key == _SHARED_FROM_KEY:
            shared_from = _parse_shared_from(value, source)
        elif isinstance(value, dict):
            if key == "instances":
                msg = (
                    f"{source}: no all-identity [vm.instances] tier — declare named "
                    f"instances under [vm.<identity>.instances.<name>]"
                )
                raise ConfigError(msg)
            roles[key] = _parse_role_overlay(key, value, source)
        elif key in _VM_KEYS:
            fields[key] = value
        else:
            print(f"{source}: unrecognized key '{key}' in [vm]", file=sys.stderr)

    if shared_from is not None and (fields or roles):
        offenders = sorted([*fields, *(f"[vm.{r}]" for r in roles)])
        msg = (
            f"{source}: [vm].shared_from cannot be combined with other [vm] keys "
            f"({', '.join(offenders)}); a repo either describes a VM or borrows one"
        )
        raise ConfigError(msg)

    scalars = {k: _vm_str_scalar(fields, k, "[vm]", source) for k in _VM_STR_SCALARS}
    return VmStanza(
        packages=list(fields.get("packages", [])),
        cpus=fields.get("cpus"),
        memory=fields.get("memory"),
        disk=fields.get("disk"),
        stale_days=fields.get("stale_days"),
        apt_repos=list(fields.get("apt_repos", [])),
        vagrant_plugins=list(fields.get("vagrant_plugins", [])),
        port_forwards=list(fields.get("port_forwards", [])),
        roles=roles,
        nested=fields.get("nested"),
        shared_from=shared_from,
        **scalars,
    )


def _warn_unrecognized_keys(raw: dict[str, Any], source: str = CONFIG_FILE) -> None:
    for section in raw:
        if section not in _KNOWN_SECTIONS:
            print(f"{source}: unrecognized section [{section}]", file=sys.stderr)
            continue
        if not isinstance(raw[section], dict):
            continue
        if section == "vm":
            continue  # [vm] keys (incl. [vm.<role>] subtables) are validated in parse_vm_stanza
        known = _KNOWN_KEYS.get(section, frozenset())
        for key in raw[section]:
            if key not in known:
                print(
                    f"{source}: unrecognized key '{key}' in [{section}]",
                    file=sys.stderr,
                )


def _parse_raw_config(raw: dict[str, Any], source: str = CONFIG_FILE) -> VergilConfig:
    """Parse and validate a raw TOML dict into VergilConfig.

    ``source`` labels warnings and errors with the config's origin
    (e.g. a resolved file path) so multi-repo scans stay diagnosable.
    """
    _warn_unrecognized_keys(raw, source)
    project_raw = raw.get("project", {})

    for required_field in _REQUIRED_PROJECT_FIELDS:
        if required_field not in project_raw or not project_raw[required_field]:
            msg = f"{source}: missing or empty required field '{required_field}'"
            raise ConfigError(msg)

    for required_field in _REQUIRED_PROJECT_FIELDS:
        value = project_raw[required_field]
        if value not in _ENUMS[required_field]:
            allowed = ", ".join(sorted(_ENUMS[required_field]))
            msg = f"{source}: invalid {required_field} '{value}' (allowed: {allowed})"
            raise ConfigError(msg)

    raw_lang = project_raw.get("primary-language", "")
    if raw_lang and raw_lang not in _ENUMS["primary-language"]:
        allowed = ", ".join(sorted(_ENUMS["primary-language"]))
        print(
            f"warning: {source}: unrecognized primary-language '{raw_lang}'"
            f" (known: {allowed}); treating as unset",
            file=sys.stderr,
        )
        raw_lang = ""

    raw_ghas = project_raw.get("ghas")
    if raw_ghas is not None and not isinstance(raw_ghas, bool):
        msg = f"{source}: [project].ghas must be a boolean"
        raise ConfigError(msg)

    deps = raw.get("dependencies", {})
    if "vergil" not in deps:
        msg = f"{source}: [dependencies] must contain 'vergil'"
        raise ConfigError(msg)

    ml_raw = raw.get("markdownlint", {})
    ml_ignore = ml_raw.get("ignore", [])
    if not isinstance(ml_ignore, list) or not all(isinstance(p, str) for p in ml_ignore):
        msg = f"{source}: [markdownlint].ignore must be a list of strings"
        raise ConfigError(msg)
    markdownlint = MarkdownlintConfig(ignore=ml_ignore)

    ci_raw = raw.get("ci")
    if ci_raw is None:
        msg = f"{source}: missing required section [ci]"
        raise ConfigError(msg)
    versions = ci_raw.get("versions")
    if versions is None:
        msg = f"{source}: [ci] missing required field 'versions'"
        raise ConfigError(msg)
    if not isinstance(versions, list) or not versions:
        msg = f"{source}: [ci].versions must be a list with at least one entry"
        raise ConfigError(msg)
    if not all(isinstance(v, str) for v in versions):
        msg = f"{source}: [ci].versions entries must be strings"
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
    )

    container_raw = raw.get("container")
    if container_raw is not None:
        env_prefixes = container_raw.get("env-prefixes")
        if env_prefixes is None:
            msg = f"{source}: [container] missing required field 'env-prefixes'"
            raise ConfigError(msg)
        if not isinstance(env_prefixes, list) or not all(isinstance(p, str) for p in env_prefixes):
            msg = f"{source}: [container].env-prefixes must be a list of strings"
            raise ConfigError(msg)
        container = ContainerConfig(env_prefixes=env_prefixes)
    else:
        container = ContainerConfig(env_prefixes=[])

    validation_raw = raw.get("validation")
    if validation_raw is not None:
        container_command = validation_raw.get("container-command")
        if container_command is None:
            msg = f"{source}: [validation] missing required field 'container-command'"
            raise ConfigError(msg)
        if not isinstance(container_command, str) or not container_command.strip():
            msg = f"{source}: [validation].container-command must be a non-empty string"
            raise ConfigError(msg)
        validation = ValidationConfig(container_command=container_command)
    else:
        validation = ValidationConfig(container_command=DEFAULT_VALIDATION_COMMAND)

    project = ProjectConfig(
        repository_type=project_raw["repository-type"],
        versioning_scheme=project_raw["versioning-scheme"],
        branching_model=project_raw["branching-model"],
        release_model=project_raw["release-model"],
        primary_language=raw_lang or None,
        ghas=raw_ghas,
    )
    return VergilConfig(
        project=project,
        dependencies=dict(deps),
        markdownlint=markdownlint,
        ci=ci,
        publish=publish,
        container=container,
        validation=validation,
        vm=parse_vm_stanza(raw, source),
    )


def read_config(repo_root: Path) -> VergilConfig:
    """Parse, validate, and return ``vergil.toml``."""
    config_path = repo_root / CONFIG_FILE
    if not config_path.is_file():
        msg = f"{CONFIG_FILE} not found at {repo_root}"
        raise FileNotFoundError(msg)

    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{config_path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc

    return _parse_raw_config(raw, source=str(config_path))


def vrg_install_tag(repo_root: Path) -> str:
    """Return the ``[dependencies].vergil`` value for runtime install.

    Checks ``VRG_DOCKER_INSTALL_TAG`` env var first (override).
    """
    override = os.environ.get("VRG_DOCKER_INSTALL_TAG")
    if override:
        return override
    cfg = read_config(repo_root)
    return cfg.dependencies["vergil"]


def container_env_prefixes(repo_root: Path) -> list[str]:
    """Return ``[container].env-prefixes`` from vergil.toml, or ``[]``."""
    try:
        cfg = read_config(repo_root)
    except FileNotFoundError:
        return []
    return cfg.container.env_prefixes


def validation_container_command(repo_root: Path) -> str:
    """Return ``[validation].container-command`` from vergil.toml.

    Falls back to :data:`DEFAULT_VALIDATION_COMMAND` (``"vrg-validate"``) when
    the repo declares no override or has no ``vergil.toml``. ``ConfigError``
    from a malformed config propagates, matching :func:`container_env_prefixes`.
    """
    try:
        cfg = read_config(repo_root)
    except FileNotFoundError:
        return DEFAULT_VALIDATION_COMMAND
    return cfg.validation.container_command
