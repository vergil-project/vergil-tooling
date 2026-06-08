"""Agent identity-mode detection from VM environment.

VM provisioning (``lima.inject_credentials``) derives the mode from the
identity's name in ``identities.toml``, writes it to
``~/.config/vergil/identity-mode``, and adds a ``~/.bashrc`` line that
exports it as ``VRG_IDENTITY_MODE``. The mode determines which
allowlists and behaviors apply in identity-aware tools (``vrg-gh``,
``vrg-submit-pr``, ``vrg-git``).

Detection falls back from the environment variable to the mode file to
the presence of provisioned App credentials, so an agent VM resolves to
an agent mode even when the shell never sourced the export — bare
credentials with no recorded mode resolve to USER (the most-gated
mode), never HUMAN.

``VRG_IDENTITY_MODE`` is a Layer 1 ergonomic, not a security control.
An adversarial agent that becomes root inside the VM can set or unset
it; doing so only relaxes soft-gate allowlists. The real identity
boundary is the GitHub App credential provisioned to the VM, which no
value of this variable can change. See the design spec's Three-Layer
Security Model.

This module covers in-VM *runtime* mode detection. It is distinct from
``vergil_tooling.lib.identity``, which parses the host-side
``identities.toml`` provisioning configuration.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from pathlib import Path


class IdentityMode(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


class Signal(enum.Enum):
    """A source the resolver consults, in fallback order."""

    ENV_VAR = "env-var"
    MODE_FILE = "mode-file"
    APP_KEY = "app-pem"
    APP_ID = "app-id"
    DEFAULT = "default"


_AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

_ENV_VAR = "VRG_IDENTITY_MODE"
_APP_ID_VAR = "VRG_APP_ID"

_MODE_FILE = "~/.config/vergil/identity-mode"
_APP_KEY_FILE = "~/.config/vergil/app.pem"


@dataclass(frozen=True)
class SignalReading:
    """The state of one resolution signal at a point in time.

    ``present`` is True when the signal exists at all (env var set,
    file on disk). ``implied`` is the mode that signal asserts when it
    is both present and recognizable — ``None`` for an absent signal or
    a present-but-unparseable value (e.g. a garbage mode file), which is
    why the resolver falls through it.
    """

    signal: Signal
    detail: str
    present: bool
    implied: IdentityMode | None


@dataclass(frozen=True)
class Resolution:
    """The resolved mode plus the full evidence it was resolved from."""

    mode: IdentityMode
    resolved_by: Signal
    readings: tuple[SignalReading, ...]

    @property
    def disagreement(self) -> bool:
        """True when present signals imply more than one distinct mode.

        These are exactly the conditions that precede a misread — e.g.
        an env var saying ``human`` while a provisioned ``app.pem``
        implies ``user``.
        """
        implied = {r.implied for r in self.readings if r.present and r.implied is not None}
        return len(implied) > 1


def _parse_mode(raw: str) -> IdentityMode | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    try:
        return IdentityMode(raw)
    except ValueError:
        return None


def _read_signals() -> list[SignalReading]:
    """Gather every resolution signal's current state, in fallback order."""
    env_raw = os.environ.get(_ENV_VAR, "")
    mode_file = Path(_MODE_FILE).expanduser()
    file_present = mode_file.exists()
    app_key_present = Path(_APP_KEY_FILE).expanduser().exists()
    app_id_raw = os.environ.get(_APP_ID_VAR, "")
    return [
        SignalReading(
            signal=Signal.ENV_VAR,
            detail=f"${_ENV_VAR}",
            present=bool(env_raw.strip()),
            implied=_parse_mode(env_raw),
        ),
        SignalReading(
            signal=Signal.MODE_FILE,
            detail=_MODE_FILE,
            present=file_present,
            implied=_parse_mode(mode_file.read_text()) if file_present else None,
        ),
        SignalReading(
            signal=Signal.APP_KEY,
            detail=_APP_KEY_FILE,
            present=app_key_present,
            implied=IdentityMode.USER if app_key_present else None,
        ),
        SignalReading(
            signal=Signal.APP_ID,
            detail=f"${_APP_ID_VAR}",
            present=bool(app_id_raw.strip()),
            implied=IdentityMode.USER if app_id_raw.strip() else None,
        ),
    ]


def resolve() -> Resolution:
    """Resolve the identity mode and record the evidence behind it.

    This is the single source of truth for identity resolution; both
    :func:`current_mode` and the ``vrg-whoami`` CLI consult it. The
    fallback order is:

    1. ``VRG_IDENTITY_MODE`` environment variable (valid value)
    2. ``~/.config/vergil/identity-mode`` written by VM provisioning
    3. presence of ``~/.config/vergil/app.pem`` (provisioned App
       credential) implies an agent VM and resolves to USER
    4. ``VRG_APP_ID`` environment variable resolves to USER
    5. HUMAN

    The first present, recognizable signal wins. An unset env var or a
    present-but-unparseable value is *not* a vote for HUMAN — it means
    "fall through to the next signal."
    """
    readings = _read_signals()
    for reading in readings:
        if reading.present and reading.implied is not None:
            return Resolution(
                mode=reading.implied,
                resolved_by=reading.signal,
                readings=tuple(readings),
            )
    return Resolution(
        mode=IdentityMode.HUMAN,
        resolved_by=Signal.DEFAULT,
        readings=tuple(readings),
    )


def current_mode() -> IdentityMode:
    """Detect the current identity mode from the environment.

    Thin wrapper over :func:`resolve`; see it for the resolution order.
    """
    return resolve().mode


def is_agent() -> bool:
    """Return True if running as any agent identity."""
    return current_mode() in _AGENT_MODES


def is_human() -> bool:
    """Return True if running as the human (Chief Steward)."""
    return current_mode() == IdentityMode.HUMAN
