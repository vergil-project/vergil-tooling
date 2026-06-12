"""Phase 6: Display consumer-refresh commands."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import config

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Read and display the consumer-refresh message from vergil.toml.

    The message is also stored on ``ctx.consumer_refresh_message`` so
    ``vrg-release`` can re-print it after the progress renderer collapses
    this stage's output (the commands are for the human to act on). The
    version-expanded command block is stored separately on
    ``ctx.consumer_refresh_commands`` so ``vrg-release --install`` can
    execute exactly what the message shows (issue #1643).
    """
    cfg = config.read_config(ctx.repo_root)
    template = cfg.publish.consumer_refresh

    if template is None:
        message = (
            f"No consumer-refresh sequence is configured for {ctx.repo}. "
            f"Add [publish].consumer-refresh to vergil.toml."
        )
        ctx.consumer_refresh_commands = None
    else:
        commands = template.replace("<VERSION>", ctx.version)
        message = f"Consumer refresh commands:\n\n{commands}"
        ctx.consumer_refresh_commands = commands

    ctx.consumer_refresh_message = message
    print()
    print(message)


def run_consumer_refresh(commands: str) -> int:
    """Execute the version-expanded consumer-refresh *commands*, fail-fast.

    Drives the ``--install`` step of the release cascade (issue #1643): the
    same command block the human would otherwise copy/paste is run through a
    single ``bash`` invocation under ``set -e`` so the first failing command
    stops the block and surfaces a non-zero exit. The block runs *after* the
    release has already completed, so a failed install never un-does the
    release — it only reports that the local refresh did not finish.

    Output is inherited (not captured) so ``uv tool install`` / ``vrg-vm
    update`` progress streams live; by this point the release progress
    renderer has already torn down, so there is no nesting (cf. issue #1470).
    """
    print()
    print("--install: running consumer-refresh commands:")
    print()
    print(commands)
    print()
    # `set -e` (fail-fast) plus pipefail so a failure anywhere in the block
    # stops it; `-u` is deliberately omitted so a command referencing an
    # unset shell variable is not turned into a spurious failure.
    script = "set -eo pipefail\n" + commands
    result = subprocess.run(("bash", "-c", script), check=False)  # noqa: S603, S607
    if result.returncode != 0:
        print(
            f"vrg-release: consumer-refresh install commands failed "
            f"(exit {result.returncode}); the release itself completed and is "
            "unaffected. Re-run the commands above by hand to finish the "
            "local refresh.",
            file=sys.stderr,
        )
    return result.returncode
