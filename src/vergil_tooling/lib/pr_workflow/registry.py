"""The judgment-check registry.

The canonical check IDs (used by the engine to sequence checks) plus the prompt
loader (used by the CLI to inline a check's instructions into the audit directive).
Prompts live as package data under ``prompts/<id>.md`` and are read via
``importlib.resources`` so they resolve whether the package is run from source or
pip/uv-installed. Adding a check is one ``CHECK_IDS`` entry plus one ``.md`` file.
"""

from __future__ import annotations

from importlib.resources import files

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

CHECK_IDS: tuple[str, ...] = (
    "site-docs-reflection",
    "docstring-accuracy",
    "pr-description-fidelity",
    "commit-message-fidelity",
    "scope-coherence",
    "test-adequacy",
)

_PROMPTS = files("vergil_tooling.lib.pr_workflow.prompts")


def check_ids() -> tuple[str, ...]:
    """Return the canonical, ordered tuple of judgment-check IDs."""
    return CHECK_IDS


def check_prompt(check_id: str) -> str:
    """Return the markdown prompt text for ``check_id``.

    Raises ``WorkflowError`` for an unknown id.
    """
    if check_id not in CHECK_IDS:
        raise WorkflowError(f"unknown check id {check_id!r}; known checks: {sorted(CHECK_IDS)}")
    return (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
