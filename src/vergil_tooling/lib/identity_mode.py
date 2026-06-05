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
from pathlib import Path


class IdentityMode(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


_AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

_ENV_VAR = "VRG_IDENTITY_MODE"

_MODE_FILE = "~/.config/vergil/identity-mode"
_APP_KEY_FILE = "~/.config/vergil/app.pem"


def _parse_mode(raw: str) -> IdentityMode | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    try:
        return IdentityMode(raw)
    except ValueError:
        return None


def current_mode() -> IdentityMode:
    """Detect the current identity mode from the environment.

    Resolution order:

    1. ``VRG_IDENTITY_MODE`` environment variable (valid value)
    2. ``~/.config/vergil/identity-mode`` written by VM provisioning
    3. presence of ``~/.config/vergil/app.pem`` (provisioned App
       credential) implies an agent VM and resolves to USER
    4. ``VRG_APP_ID`` environment variable resolves to USER
    5. HUMAN
    """
    mode = _parse_mode(os.environ.get(_ENV_VAR, ""))
    if mode is not None:
        return mode
    mode_file = Path(_MODE_FILE).expanduser()
    if mode_file.exists():
        mode = _parse_mode(mode_file.read_text())
        if mode is not None:
            return mode
    if Path(_APP_KEY_FILE).expanduser().exists():
        return IdentityMode.USER
    if os.environ.get("VRG_APP_ID"):
        return IdentityMode.USER
    return IdentityMode.HUMAN


def is_agent() -> bool:
    """Return True if running as any agent identity."""
    return current_mode() in _AGENT_MODES


def is_human() -> bool:
    """Return True if running as the human (Chief Steward)."""
    return current_mode() == IdentityMode.HUMAN
