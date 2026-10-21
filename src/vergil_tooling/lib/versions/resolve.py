"""The single CI version resolver (epic vergil-project/.github#407, spec §6.3).

:func:`resolve` turns a repo's ``vergil.toml`` and the published window into
the versions its CI builds, the primary version, and the image for each. It is
the only implementation of version resolution: the dev container,
``vrg-validate``, the GitHub gates and ``vrg-ci-resolve`` (for the CI setup
step) all call it.

The rules (spec §6.3):

* ``repository-type = "library"`` resolves as a library; every other type
  resolves as an app.
* No ``[ci].versions``: a library builds the whole window, an app the newest
  version. A language whose tokens carry a family (a ``token_prefix``, or more
  than one family) must pin until its lifecycle turn.
* A ``[ci].versions`` pin must lie inside the window. An app pin should name
  one version: more is a warning, or an error once :data:`STRICT_APP_PINS`.
* ``[ci].primary-version`` must name a resolved version; without it the
  primary is the newest resolved version.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import (
    REPOSITORY_TYPE_DEFINITIONS,
    _version_sort_key,
    read_config,
)
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import (
    EXTENSIONS,
    ExtensionContext,
    ResolutionKind,
)
from vergil_tooling.lib.versions.window import load_window

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vergil_tooling.lib.config import VergilConfig
    from vergil_tooling.lib.versions.window import Window, WindowFamily

__all__ = [
    "NO_LANGUAGE_SUFFIX",
    "NO_LANGUAGE_VERSION",
    "STRICT_APP_PINS",
    "Resolution",
    "ResolutionError",
    "ResolutionKind",
    "ci_outputs",
    "matrix_versions",
    "resolution_kind",
    "resolve",
    "resolve_config",
    "resolve_language",
    "resolve_repo",
]

# An app with more than one [ci].versions entry warns while the fleet sweep
# removes the lists, and becomes fatal once it is done (spec §6.3, Task 14).
STRICT_APP_PINS = False

# What a repo with no language builds: the base image at ``latest``, as before.
NO_LANGUAGE_VERSION = "latest"
NO_LANGUAGE_SUFFIX = "base"


class ResolutionError(VersionsError):
    """A repo's version configuration does not resolve against the window."""


@dataclass(frozen=True)
class Resolution:
    language: str
    kind: ResolutionKind
    # Newest first; tokens as written in vergil.toml (e.g. "clang-20").
    versions: tuple[str, ...]
    primary: str
    # version token -> image tag ("clang-20" -> "20", "3.14" -> "3.14").
    container_tags: Mapping[str, str]
    # version token -> image suffix ("clang-20" -> "cpp-clang", "3.14" -> "python").
    container_suffixes: Mapping[str, str]
    warnings: tuple[str, ...] = field(default=())

    @property
    def primary_container_tag(self) -> str:
        return self.container_tags[self.primary]

    @property
    def primary_container_suffix(self) -> str:
        return self.container_suffixes[self.primary]


def resolution_kind(repository_type: str) -> ResolutionKind:
    """``library`` iff ``repository-type == "library"``, else ``app``."""
    return "library" if repository_type == "library" else "app"


def _newest_first(tokens: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(tokens, key=_version_sort_key, reverse=True))


def _is_family_keyed(families: tuple[WindowFamily, ...]) -> bool:
    return len(families) > 1 or any(family.token_prefix for family in families)


def resolve(
    cfg: VergilConfig,
    window: Window,
    *,
    repo_root: Path | None,
    strict_app_pins: bool = STRICT_APP_PINS,
) -> Resolution | None:
    """Resolve *cfg* against *window*; ``None`` for a repo with no language."""
    language = cfg.project.primary_language
    if not language:
        return None
    return resolve_language(
        cfg, window, language, repo_root=repo_root, strict_app_pins=strict_app_pins
    )


def resolve_language(
    cfg: VergilConfig,
    window: Window,
    language: str,
    *,
    repo_root: Path | None,
    strict_app_pins: bool = STRICT_APP_PINS,
) -> Resolution:
    """Resolve *cfg* for *language*, which may be detected rather than declared."""
    families = window.languages.get(language)
    if families is None:
        supported = ", ".join(sorted(window.languages))
        msg = (
            f"{language} is not in the supported window ({window.source}; it covers "
            f"{supported}). Fix: set [project].primary-language to a supported "
            f"language, or update [dependencies].vergil"
        )
        raise ResolutionError(msg)

    suffixes = {family.token(v): family.key for family in families for v in family.versions}
    tags = {family.token(v): v.version for family in families for v in family.versions}
    window_tokens = _newest_first(list(suffixes))
    window_list = ", ".join(window_tokens)
    kind = resolution_kind(cfg.project.repository_type)
    pins = cfg.ci.versions
    warnings: list[str] = []

    if pins is None:
        if _is_family_keyed(families):
            msg = (
                f"{language} must pin [ci].versions until its lifecycle turn: its "
                f"versions carry a family, so there is no single default. Fix: add "
                f'[ci].versions = ["{window_tokens[0]}"] (window: {window_list})'
            )
            raise ResolutionError(msg)
        candidates = window_tokens
    else:
        outside = [pin for pin in pins if pin not in suffixes]
        if outside:
            msg = (
                f"[ci].versions pins {', '.join(outside)}, which is not in the "
                f"{language} window ({window_list}) published at {window.source}. "
                f"Fix: remove it from [ci].versions or update the pin to a window version"
            )
            raise ResolutionError(msg)
        candidates = _newest_first(list(dict.fromkeys(pins)))
        if kind == "app" and len(candidates) > 1:
            msg = (
                f"[ci].versions pins {len(candidates)} versions ({', '.join(candidates)}), "
                f"but repository-type '{cfg.project.repository_type}' resolves as an app, "
                f"which builds exactly one version. Fix: keep one entry in [ci].versions, "
                f"or remove the list to follow the newest window version. A library is a "
                f"repo that {REPOSITORY_TYPE_DEFINITIONS['library']}"
            )
            if strict_app_pins:
                raise ResolutionError(msg)
            warnings.append(msg)

    extension = EXTENSIONS.get(language)
    if extension is not None:
        context = ExtensionContext(
            cfg=cfg,
            window=window,
            language=language,
            kind=kind,
            pinned=pins is not None,
            repo_root=repo_root,
        )
        narrowed = extension(context, candidates)
        unknown = [token for token in narrowed if token not in candidates]
        if unknown or not narrowed:
            msg = (
                f"the {language} resolver extension returned {list(narrowed)} from "
                f"candidates {list(candidates)}; it may only narrow them to a non-empty "
                f"subset. This is a vergil-tooling bug"
            )
            raise ResolutionError(msg)
        candidates = _newest_first(narrowed)

    resolved = candidates if pins is not None or kind == "library" else candidates[:1]

    primary = cfg.ci.primary_version or resolved[0]
    if primary not in resolved:
        msg = (
            f"[ci].primary-version {primary} is not a resolved version "
            f"({', '.join(resolved)}). Fix: set it to one of those, or remove it so the "
            f"primary is the newest resolved version"
        )
        raise ResolutionError(msg)

    return Resolution(
        language=language,
        kind=kind,
        versions=resolved,
        primary=primary,
        container_tags={token: tags[token] for token in resolved},
        container_suffixes={token: suffixes[token] for token in resolved},
        warnings=tuple(warnings),
    )


def resolve_config(
    cfg: VergilConfig,
    *,
    repo_root: Path | None,
    language: str | None = None,
    env: Mapping[str, str] = os.environ,
    strict_app_pins: bool = STRICT_APP_PINS,
) -> Resolution | None:
    """Load the window at *cfg*'s Vergil ref and resolve.

    *language* stands in when ``vergil.toml`` declares none (a language
    detected from the repo's files). With neither, the window is not fetched and
    the result is ``None``.
    """
    lang = cfg.project.primary_language or language
    if not lang:
        return None
    window = load_window(cfg.dependencies["vergil"], env=env)
    return resolve_language(cfg, window, lang, repo_root=repo_root, strict_app_pins=strict_app_pins)


def resolve_repo(
    repo_root: Path,
    *,
    language: str | None = None,
    env: Mapping[str, str] = os.environ,
) -> Resolution | None:
    """Read *repo_root*'s ``vergil.toml`` and resolve it.

    Without a ``vergil.toml`` there is no Vergil ref to read the window at: that
    is ``None`` for a repo with no language and a :class:`ResolutionError` for
    one whose *language* was detected.
    """
    try:
        cfg = read_config(repo_root)
    except FileNotFoundError:
        if language:
            msg = (
                f"no vergil.toml at {repo_root}: the {language} version comes from the "
                f"version window at [dependencies].vergil. Fix: add a vergil.toml"
            )
            raise ResolutionError(msg) from None
        return None
    return resolve_config(cfg, repo_root=repo_root, language=language, env=env)


def matrix_versions(resolution: Resolution | None) -> tuple[str, ...]:
    """The versions a repo's CI matrix runs (``("latest",)`` with no language)."""
    return resolution.versions if resolution is not None else (NO_LANGUAGE_VERSION,)


def ci_outputs(resolution: Resolution | None) -> dict[str, object]:
    """The resolver's machine output, as ``vrg-ci-resolve`` prints it."""
    if resolution is None:
        return {
            "versions": [NO_LANGUAGE_VERSION],
            "primary-version": NO_LANGUAGE_VERSION,
            "primary-container-tag": NO_LANGUAGE_VERSION,
            "container-tags": {NO_LANGUAGE_VERSION: NO_LANGUAGE_VERSION},
            "container-suffixes": {NO_LANGUAGE_VERSION: NO_LANGUAGE_SUFFIX},
        }
    return {
        "versions": list(resolution.versions),
        "primary-version": resolution.primary,
        "primary-container-tag": resolution.primary_container_tag,
        "container-tags": dict(resolution.container_tags),
        "container-suffixes": dict(resolution.container_suffixes),
    }
