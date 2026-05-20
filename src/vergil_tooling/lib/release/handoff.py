"""Phase 6: Display consumer-refresh commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Read and display the consumer-refresh message from vergil.toml."""
    cfg = config.read_config(ctx.repo_root)
    template = cfg.publish.consumer_refresh

    if template is None:
        print(
            f"No consumer-refresh sequence is configured for {ctx.repo}. "
            f"Add [publish].consumer-refresh to vergil.toml."
        )
        return

    message = template.replace("<VERSION>", ctx.version)
    print()
    print("Consumer refresh commands:")
    print()
    print(message)
