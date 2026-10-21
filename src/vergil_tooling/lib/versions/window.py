"""Load the published language version window (epic vergil-project/.github#407).

``vergil-containers`` publishes ``docker/versions/window.json`` at every Vergil
ref. A consumer reads it at its own ``[dependencies].vergil`` ref, at build
time, so a window change reaches it without a tooling release (spec §6.3).

The window is never cached and never replaced by a built-in default. A failed
fetch or a malformed document raises :class:`WindowError`, naming the URL and
the one explicit override: ``VRG_VERSION_WINDOW=<path>`` points at a local
``window.json`` for deliberate offline work.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import _version_sort_key
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

WINDOW_URL = (
    "https://raw.githubusercontent.com/vergil-project/vergil-containers/"
    "{ref}/docker/versions/window.json"
)
WINDOW_ENV = "VRG_VERSION_WINDOW"
WINDOW_SCHEMA = 1
_FETCH_TIMEOUT_SECONDS = 30


class WindowError(VersionsError):
    """The version window could not be read or is malformed."""


@dataclass(frozen=True)
class WindowVersion:
    version: str
    eol: date | None
    in_policy: bool
    runtime_published: bool | None


@dataclass(frozen=True)
class WindowFamily:
    # ``key`` is the image suffix (``python``, ``cpp-clang``, ``ts-node``);
    # ``token_prefix`` is what a ``[ci].versions`` entry carries in front of the
    # version (``""``, ``clang-``, ``node-``).
    key: str
    token_prefix: str
    versions: tuple[WindowVersion, ...]  # newest first

    def token(self, version: WindowVersion) -> str:
        """The ``[ci].versions`` token for *version* (``clang-`` + ``20``)."""
        return f"{self.token_prefix}{version.version}"


@dataclass(frozen=True)
class Window:
    languages: Mapping[str, tuple[WindowFamily, ...]]
    # Where the window came from (the URL, or the override path), for messages.
    source: str = ""


def _http_fetch(url: str) -> bytes:
    """GET *url*. Every failure, a 404 included, propagates to the caller."""
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https base URL
        data: bytes = resp.read()
        return data


def window_url(vergil_ref: str) -> str:
    """The published window URL for a ``[dependencies].vergil`` ref."""
    return WINDOW_URL.format(ref=vergil_ref)


def load_window(
    vergil_ref: str,
    *,
    fetch: Callable[[str], bytes] = _http_fetch,
    env: Mapping[str, str] = os.environ,
) -> Window:
    """Load the window published at *vergil_ref*, or the ``VRG_VERSION_WINDOW`` file.

    The override wins whenever it is set. Otherwise the window is fetched, and
    any failure raises :class:`WindowError` naming the URL and the override.
    """
    if WINDOW_ENV in env:
        return _load_override(env[WINDOW_ENV])
    url = window_url(vergil_ref)
    hint = (
        f"The window is published by vergil-containers at the [dependencies].vergil "
        f"ref ({vergil_ref}); check that ref and the network, or set "
        f"{WINDOW_ENV}=<path> to a local window.json"
    )
    try:
        data = fetch(url)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        msg = f"cannot fetch the version window {url}: {exc}. {hint}"
        raise WindowError(msg) from exc
    return parse_window(data, source=url, hint=hint)


def _load_override(raw_path: str) -> Window:
    if not raw_path:
        msg = f"{WINDOW_ENV} is set but empty. Fix: set it to a window.json path, or unset it"
        raise WindowError(msg)
    path = Path(raw_path)
    hint = f"Fix the file, or unset {WINDOW_ENV} to use the published window"
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read the version window {path} named by {WINDOW_ENV}: {exc}. {hint}"
        raise WindowError(msg) from exc
    return parse_window(data, source=f"{path} ({WINDOW_ENV})", hint=hint)


def parse_window(data: bytes, *, source: str, hint: str = "") -> Window:
    """Parse and validate a schema-1 window document; raise :class:`WindowError`."""
    try:
        raw = json.loads(data)
        languages = _parse_document(raw)
    except (ValueError, _ShapeError) as exc:
        suffix = f". {hint}" if hint else ""
        msg = f"invalid version window {source}: {exc}{suffix}"
        raise WindowError(msg) from exc
    return Window(languages=languages, source=source)


class _ShapeError(Exception):
    """A structural violation, reported through :class:`WindowError`."""


def _require(condition: bool, message: str) -> None:  # noqa: FBT001 - a guard
    if not condition:
        raise _ShapeError(message)


def _parse_document(raw: Any) -> dict[str, tuple[WindowFamily, ...]]:  # noqa: ANN401 - JSON
    _require(isinstance(raw, dict), "the document must be a JSON object")
    schema = raw.get("schema")
    _require(
        type(schema) is int and schema == WINDOW_SCHEMA,
        f"unsupported schema {schema!r} (this tooling reads schema {WINDOW_SCHEMA})",
    )
    languages = raw.get("languages")
    _require(
        isinstance(languages, dict) and bool(languages),
        "'languages' must be a non-empty object",
    )
    return {name: _parse_language(name, entry) for name, entry in languages.items()}


def _parse_language(name: str, entry: Any) -> tuple[WindowFamily, ...]:  # noqa: ANN401 - JSON
    _require(isinstance(entry, dict), f"language {name!r} must be an object")
    families = entry.get("families")
    _require(
        isinstance(families, list) and bool(families),
        f"language {name!r}: 'families' must be a non-empty list",
    )
    parsed = tuple(_parse_family(name, family) for family in families)
    tokens = [family.token(v) for family in parsed for v in family.versions]
    _require(
        len(tokens) == len(set(tokens)),
        f"language {name!r}: a version token is listed twice ({', '.join(tokens)})",
    )
    return parsed


def _parse_family(language: str, raw: Any) -> WindowFamily:  # noqa: ANN401 - JSON
    where = f"language {language!r}"
    _require(isinstance(raw, dict), f"{where}: each family must be an object")
    key = raw.get("key")
    _require(isinstance(key, str) and bool(key), f"{where}: family 'key' must be a string")
    where = f"{where}, family {key!r}"
    prefix = raw.get("token_prefix")
    _require(isinstance(prefix, str), f"{where}: 'token_prefix' must be a string")
    versions = raw.get("versions")
    _require(
        isinstance(versions, list) and bool(versions),
        f"{where}: 'versions' must be a non-empty list",
    )
    parsed = sorted(
        (_parse_version(where, v) for v in versions),
        key=lambda v: _version_sort_key(v.version),
        reverse=True,
    )
    return WindowFamily(key=key, token_prefix=prefix, versions=tuple(parsed))


def _parse_version(where: str, raw: Any) -> WindowVersion:  # noqa: ANN401 - JSON
    _require(isinstance(raw, dict), f"{where}: each version must be an object")
    version = raw.get("version")
    _require(
        isinstance(version, str) and bool(version),
        f"{where}: 'version' must be a non-empty string",
    )
    where = f"{where}, version {version!r}"
    eol = raw.get("eol")
    _require(eol is None or isinstance(eol, str), f"{where}: 'eol' must be null or a date")
    in_policy = raw.get("in_policy")
    _require(isinstance(in_policy, bool), f"{where}: 'in_policy' must be a boolean")
    runtime = raw.get("runtime_published")
    _require(
        runtime is None or isinstance(runtime, bool),
        f"{where}: 'runtime_published' must be null or a boolean",
    )
    try:
        eol_date = date.fromisoformat(eol) if eol is not None else None
    except ValueError as exc:
        msg = f"{where}: 'eol' {eol!r} is not an ISO date"
        raise _ShapeError(msg) from exc
    return WindowVersion(
        version=version,
        eol=eol_date,
        in_policy=in_policy,
        runtime_published=runtime,
    )
