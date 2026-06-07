"""Phase 6: Display consumer-refresh commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Read and display the consumer-refresh message from vergil.toml.

    The message is also stored on ``ctx.consumer_refresh_message`` so
    ``vrg-release`` can re-print it after the progress renderer collapses
    this stage's output (the commands are for the human to act on).
    """
    cfg = config.read_config(ctx.repo_root)
    template = cfg.publish.consumer_refresh

    if template is None:
        message = (
            f"No consumer-refresh sequence is configured for {ctx.repo}. "
            f"Add [publish].consumer-refresh to vergil.toml."
        )
    else:
        commands = template.replace("<VERSION>", ctx.version)
        message = f"Consumer refresh commands:\n\n{commands}"

    ctx.consumer_refresh_message = message
    print()
    print(message)
