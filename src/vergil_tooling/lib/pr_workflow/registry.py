"""The judgment-check registry.

Phase 1 holds only the canonical check IDs — the engine uses them to list the
checks in an audit directive and to validate a review payload. The prompts that
define how each check is performed are authored in Phase 2; adding a check is a
one-line edit here plus one prompt, with no engine change.
"""

from __future__ import annotations

CHECK_IDS: tuple[str, ...] = (
    "site-docs-reflection",
    "docstring-accuracy",
    "pr-description-fidelity",
    "commit-message-fidelity",
    "scope-coherence",
    "test-adequacy",
)


def check_ids() -> tuple[str, ...]:
    """Return the canonical, ordered tuple of judgment-check IDs."""
    return CHECK_IDS
