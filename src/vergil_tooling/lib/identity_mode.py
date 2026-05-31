"""Agent identity-mode detection from VM environment.

The VM provisioning sets ``VRG_IDENTITY_MODE`` alongside the GitHub App
credentials. The mode determines which allowlists and behaviors apply
in identity-aware tools (``vrg-gh``, ``vrg-submit-pr``, ``vrg-git``).

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


class IdentityMode(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


_AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

_ENV_VAR = "VRG_IDENTITY_MODE"


def current_mode() -> IdentityMode:
    """Detect the current identity mode from the environment."""
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    if raw:
        try:
            return IdentityMode(raw)
        except ValueError:
            pass
    if os.environ.get("VRG_APP_ID"):
        return IdentityMode.USER
    return IdentityMode.HUMAN


def is_agent() -> bool:
    """Return True if running as any agent identity."""
    return current_mode() in _AGENT_MODES


def is_human() -> bool:
    """Return True if running as the human (Chief Steward)."""
    return current_mode() == IdentityMode.HUMAN
