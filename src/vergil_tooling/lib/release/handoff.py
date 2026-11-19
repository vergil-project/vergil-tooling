"""The consumer-refresh stage: run the [publish].consumer-refresh commands."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, progress
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext

PHASE = "consumer-refresh"


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Run the release's consumer-refresh commands, rolled up to one line.

    The stage runs right after ``confirm-main``, as soon as the release tag
    exists (epic vergil-project/.github#1350, spec §4.6). It needs only the
    tag, so no later stage holds it, and a deferred publish (a CD job or the
    package index) does not either: the consumers get the release even if a
    follow-on stage fails.

    The version-expanded command block is stored on
    ``ctx.consumer_refresh_commands``. ``ctx.consumer_refresh_message`` is set
    only when the human has something to read once the progress renderer has
    collapsed this stage: the commands to run by hand (``--no-install``), the
    notice that none is configured, or a failed run's full output.
    ``vrg-release`` re-prints it below the summary.

    On success the commands' output is not shown, so the stage is the one
    ``✓ consumer-refresh`` line. On failure the full output is shown and the
    stage fails with the exit status.
    """
    cfg = config.read_config(ctx.repo_root)
    template = cfg.publish.consumer_refresh

    if template is None:
        ctx.consumer_refresh_commands = None
        ctx.consumer_refresh_message = (
            f"No consumer-refresh sequence is configured for {ctx.repo}; nothing to run. "
            "Add [publish].consumer-refresh to vergil.toml."
        )
        print(ctx.consumer_refresh_message)
        progress.note("none configured")
        return

    commands = template.replace("<VERSION>", ctx.version)
    ctx.consumer_refresh_commands = commands

    if not ctx.install:
        ctx.consumer_refresh_message = (
            f"Consumer refresh commands for v{ctx.version} (not run: --no-install):\n\n{commands}"
        )
        print(ctx.consumer_refresh_message)
        progress.note("not run (--no-install)")
        return

    returncode, output = run_consumer_refresh(commands, cwd=ctx.repo_root)
    if returncode == 0:
        ctx.consumer_refresh_message = None
        return

    ctx.consumer_refresh_message = (
        f"Consumer refresh for v{ctx.version} failed (exit {returncode}). "
        f"The commands:\n\n{commands}\n\nTheir full output:\n\n{output.rstrip()}"
    )
    print(ctx.consumer_refresh_message)
    # The output stays out of the error: the error is posted on the public
    # tracking issue, and the output is the local machine's install log.
    raise ReleaseError(
        phase=PHASE,
        command=PHASE,
        message=(
            f"the consumer-refresh commands failed (exit {returncode}); the release "
            f"itself is unaffected. Their full output is shown below the summary: fix "
            "the cause and re-run the commands by hand to finish the refresh."
        ),
    )


def run_consumer_refresh(commands: str, *, cwd: Path) -> tuple[int, str]:
    """Run the version-expanded consumer-refresh *commands*, fail-fast.

    The block runs through one ``bash`` invocation under ``set -eo pipefail``,
    so the first failing command stops it and the exit status is non-zero
    (issues #1643, #1184). ``-u`` is left out so a command naming an unset
    shell variable is not turned into a spurious failure. Its standard output
    and error are captured together, in order, and returned with the exit
    status; nothing is printed here. Standard input is closed, so a command
    can never wait on the terminal under the progress renderer.
    """
    script = "set -eo pipefail\n" + commands
    result = subprocess.run(  # noqa: S603
        ("bash", "-c", script),  # noqa: S607
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        check=False,
    )
    return result.returncode, result.stdout or ""
