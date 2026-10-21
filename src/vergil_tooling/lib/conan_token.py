"""Resolve the ConanCenter audit token at its point of use (issue #2902).

``conan audit scan`` reads its provider token from
``CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`` inside the dev container. Agent VMs
used to make it available only through a ``~/.bashrc`` source line, so it was
absent under a zsh login shell and in any non-interactive shell. Instead of
trusting the ambient environment, ``vrg-container-run`` resolves the token from
its canonical file and injects it into the container itself.

Precedence, first match wins:

1. ``CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER`` set non-blank in the environment
   (CI secrets, or an operator's deliberate override). The files are not read.
2. ``~/.config/vergil/conan.env`` — the file ``vrg-vm`` writes into agent VMs.
3. ``conan_audit_token_path`` in ``~/.config/vergil/identities.toml`` — the
   host-side bare-token file the VM copy is made from.
4. Nothing: no token is injected, and ``vrg-validate`` skips ``conan audit
   scan`` with a notice (#2895).

A source that exists but cannot be read, is malformed, or holds an empty token
raises :class:`ConanTokenError`. It never falls through to a lower source,
because a silent fallback would hide a broken configuration. Error messages name
the file and the problem, never the token or the offending line.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import identity

if TYPE_CHECKING:
    from collections.abc import Mapping

CONAN_AUDIT_TOKEN_ENV = "CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER"  # noqa: S105


class ConanTokenError(Exception):
    """A configured token source is unreadable or malformed."""


@dataclass(frozen=True)
class ResolvedToken:
    """A resolved token and a description of where it came from (safe to print)."""

    value: str = field(repr=False)
    source: str


def conan_env_path() -> Path:
    """The env file ``vrg-vm`` writes into agent VMs."""
    return Path.home() / ".config" / "vergil" / "conan.env"


def identities_path() -> Path:
    """The host identity config that may name a ``conan_audit_token_path``."""
    return identity.default_config_path()


def resolve_conan_audit_token(
    environ: Mapping[str, str],
    *,
    conan_env: Path | None = None,
    identities: Path | None = None,
) -> ResolvedToken | None:
    """Resolve the token by the module's precedence, or ``None`` when absent."""
    explicit = environ.get(CONAN_AUDIT_TOKEN_ENV, "").strip()
    if explicit:
        return ResolvedToken(explicit, "environment")

    env_file = conan_env if conan_env is not None else conan_env_path()
    if env_file.exists():
        return ResolvedToken(_parse_conan_env(env_file), str(env_file))

    ids_file = identities if identities is not None else identities_path()
    if ids_file.exists():
        return _token_from_identities(ids_file)
    return None


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        msg = f"cannot read {what} {path}: {exc.strerror or type(exc).__name__}"
        raise ConanTokenError(msg) from None


def _parse_conan_env(path: Path) -> str:
    """Parse the ``export NAME=<shell-quoted value>`` line ``vrg-vm`` writes."""
    lines = _read(path, "Conan audit env file").splitlines()
    value: str | None = None
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            msg = f"{path}: line {number} is not valid shell (unbalanced quoting)"
            raise ConanTokenError(msg) from None
        if words[:1] == ["export"]:
            words = words[1:]
        prefix = f"{CONAN_AUDIT_TOKEN_ENV}="
        if len(words) != 1 or not words[0].startswith(prefix):
            msg = f"{path}: line {number} is not `export {CONAN_AUDIT_TOKEN_ENV}=<token>`"
            raise ConanTokenError(msg)
        if value is not None:
            msg = f"{path}: {CONAN_AUDIT_TOKEN_ENV} is assigned more than once"
            raise ConanTokenError(msg)
        value = words[0].removeprefix(prefix).strip()
        if not value:
            msg = f"{path}: line {number} assigns an empty {CONAN_AUDIT_TOKEN_ENV}"
            raise ConanTokenError(msg)
    if value is None:
        msg = f"{path}: no {CONAN_AUDIT_TOKEN_ENV} assignment"
        raise ConanTokenError(msg)
    return value


def _token_from_identities(path: Path) -> ResolvedToken | None:
    """Read the token file named by ``conan_audit_token_path`` in *path*.

    The token is a ConanCenter credential, not a GitHub identity, so the
    identities are expected to share one file. Differing paths are ambiguous
    and fail loudly rather than picking one.
    """
    config = identity.load_config(path)
    paths = sorted(
        {
            Path(ident.conan_audit_token_path).expanduser()
            for ident in config.identities.values()
            if ident.conan_audit_token_path
        }
    )
    if not paths:
        return None
    if len(paths) > 1:
        msg = (
            f"{path}: identities configure different conan_audit_token_path files; "
            f"point them at one file, or set {CONAN_AUDIT_TOKEN_ENV} explicitly"
        )
        raise ConanTokenError(msg)
    token_file = paths[0]
    token = _read(token_file, f"Conan audit token file (conan_audit_token_path in {path})")
    token = token.strip()
    if not token:
        msg = f"Conan audit token file {token_file} is empty"
        raise ConanTokenError(msg)
    return ResolvedToken(token, str(token_file))
