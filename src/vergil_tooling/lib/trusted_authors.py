"""The GitHub logins whose issues and comments automation may trust.

The trusted logins are **configuration, never hard-coded** in a skill, the
doctrine or a tool. They live in an optional ``[trusted_authors]`` stanza of the
host's ``~/.config/vergil/identities.toml``::

    [trusted_authors]
    human = ["<human-login>"]
    agents = ["<agent-login>", "<agent-login>"]

``vrg-vm`` delivers the stanza into every agent VM as
``~/.config/vergil/trusted-authors.toml`` (the same two keys at top level), and
removes that file when the host has no stanza. Everything reads the list through
:func:`load_trusted_authors` (the ``vrg-trusted-authors`` command), which fails
loudly when nothing is configured and never falls back to a default.

Logins are stored bare. GitHub reports an issue opened by a GitHub App as
``app/<login>`` but that App's comments as ``<login>``, so a caller strips a
leading ``app/`` before comparing; an ``app/`` entry in the configuration is
rejected rather than silently normalized.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

STANZA = "trusted_authors"
VM_FILE_NAME = "trusted-authors.toml"
_APP_PREFIX = "app/"


class TrustedAuthorsNotConfiguredError(Exception):
    """Neither the VM file nor the ``identities.toml`` stanza configures trusted authors."""


@dataclass(frozen=True)
class TrustedAuthors:
    human: tuple[str, ...]
    agents: tuple[str, ...]


def _parse_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    if key not in raw:
        msg = f"trusted_authors: '{key}' is missing (it must list at least one GitHub login)"
        raise ValueError(msg)
    value = raw[key]
    if not isinstance(value, list):
        msg = f"trusted_authors: '{key}' must be a list of GitHub logins, got {value!r}"
        raise ValueError(msg)
    if not value:
        msg = f"trusted_authors: '{key}' is empty (it must list at least one GitHub login)"
        raise ValueError(msg)
    logins: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            msg = f"trusted_authors: '{key}' entries must be non-empty strings, got {entry!r}"
            raise ValueError(msg)
        if entry != entry.strip():
            msg = f"trusted_authors: '{key}' entry {entry!r} has surrounding whitespace"
            raise ValueError(msg)
        if entry.startswith(_APP_PREFIX):
            msg = (
                f"trusted_authors: '{key}' entry {entry!r} has the 'app/' prefix;"
                f" store the bare login {entry[len(_APP_PREFIX) :]!r}"
            )
            raise ValueError(msg)
        logins.append(entry)
    return tuple(logins)


def parse_trusted_authors(raw: Mapping[str, object]) -> TrustedAuthors:
    """Validate a ``[trusted_authors]`` mapping and return it.

    Raises :class:`ValueError` naming the problem when ``human`` or ``agents``
    is missing or empty, an entry is not a non-empty string, an entry carries
    the ``app/`` prefix, or a login appears in both lists.
    """
    human = _parse_list(raw, "human")
    agents = _parse_list(raw, "agents")
    both = sorted(set(human) & set(agents))
    if both:
        msg = f"trusted_authors: login(s) in both 'human' and 'agents': {', '.join(both)}"
        raise ValueError(msg)
    return TrustedAuthors(human=human, agents=agents)


def vm_file_path() -> Path:
    """The trusted-authors file ``vrg-vm`` delivers into an agent VM."""
    return Path.home() / ".config" / "vergil" / VM_FILE_NAME


def identities_path() -> Path:
    """The host's ``identities.toml``."""
    return Path.home() / ".config" / "vergil" / "identities.toml"


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: invalid TOML: {exc}"
        raise ValueError(msg) from exc


def load_trusted_authors() -> TrustedAuthors:
    """Load the trusted authors: the VM file if it exists, else the host stanza.

    Raises :class:`TrustedAuthorsNotConfiguredError` naming both paths when
    neither configures them, and :class:`ValueError` when the configuration
    that is present is invalid. Never returns a default.
    """
    vm_file = vm_file_path()
    if vm_file.exists():
        raw = _read_toml(vm_file)
        try:
            return parse_trusted_authors(raw)
        except ValueError as exc:
            msg = f"{vm_file}: {exc}"
            raise ValueError(msg) from exc

    identities = identities_path()
    if identities.exists():
        stanza = _read_toml(identities).get(STANZA)
        if stanza is not None:
            if not isinstance(stanza, dict):
                msg = f"{identities}: [{STANZA}] must be a table"
                raise ValueError(msg)
            try:
                return parse_trusted_authors(stanza)
            except ValueError as exc:
                msg = f"{identities}: {exc}"
                raise ValueError(msg) from exc

    msg = (
        f"trusted authors are not configured: no {vm_file} and no [{STANZA}] stanza in {identities}"
    )
    raise TrustedAuthorsNotConfiguredError(msg)


def render_vm_file(authors: TrustedAuthors) -> str:
    """Render the VM's ``trusted-authors.toml`` (top-level ``human``/``agents``)."""

    def _array(logins: tuple[str, ...]) -> str:
        # A JSON string literal is a valid TOML basic string.
        return "[" + ", ".join(json.dumps(login, ensure_ascii=False) for login in logins) + "]"

    return (
        "# Delivered by vrg-vm from the host's identities.toml [trusted_authors]."
        " Do not edit.\n"
        f"human = {_array(authors.human)}\n"
        f"agents = {_array(authors.agents)}\n"
    )
