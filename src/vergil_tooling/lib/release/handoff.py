"""The consumer-refresh stage: run the [publish].consumer-refresh commands."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, progress
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import publish_deferred_remedy

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext

PHASE = "consumer-refresh"

# Failed stages that never hold the refresh back (issue #4828). update-deps
# prepares the next develop version, after the release is fully out: its
# failure has its own handling (the PR stays open, a fix task is filed, the
# next vrg-release is gated) and leaves the published release valid.
OPTIONAL_STAGES = frozenset({"update-deps"})


def blocking_failures(ctx: ReleaseContext) -> list[str]:
    """The failed earlier stages that hold the refresh back, in pipeline order.

    Every failed stage in ``ctx.deferred_failures`` except the optional ones
    (:data:`OPTIONAL_STAGES`), whose failure leaves the published release valid.
    """
    return [name for name in ctx.deferred_failures if name not in OPTIONAL_STAGES]


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Run the release's consumer-refresh commands, rolled up to one line.

    The stage is the pipeline's last, after ``promote``, ``close-finalize``
    and ``update-deps``: a consumer that follows the rolling ``vX.Y`` tag
    (vergil-claude-plugin's marketplace) sees the release only once
    ``promote`` has moved it. While any publish is deferred (a CD job seen at
    ``confirm-main``, or ``package-index``) it holds: nothing runs, and the
    message warns not to advertise the version. After an earlier stage that
    failed, nothing runs either, and the commands are shown to run by hand.
    A failed ``update-deps`` alone does not hold it (issue #4828): that stage
    prepares the next version, so the release just published is still valid,
    and its failure is still reported and still ends the run non-zero.

    The version-expanded command block is stored on
    ``ctx.consumer_refresh_commands``. ``ctx.consumer_refresh_message`` is set
    only when the human has something to read once the progress renderer has
    collapsed this stage: the hold warning, the commands to run by hand
    (after a failed stage, or with ``--no-install``), the notice that none is
    configured, or a failed run's full output. ``vrg-release`` re-prints it
    below the summary.

    On success the commands' output is not shown, so the stage is the one
    ``✓ consumer-refresh`` line. On failure the full output is shown and the
    stage fails with the exit status. Either way the run log holds the
    commands, their full output and their exit status (issue #4812).
    """
    if ctx.deferred_publish_failures:
        remedy = publish_deferred_remedy(ctx, ctx.deferred_publish_failures)
        ctx.consumer_refresh_commands = None
        ctx.consumer_refresh_message = (
            "⚠ Consumer refresh held: artifact publish was deferred "
            f"({', '.join(ctx.deferred_publish_failures)}). Do NOT advertise "
            f"v{ctx.version} to consumers until the artifacts are delivered: {remedy}."
        )
        print(ctx.consumer_refresh_message)
        progress.note("held: publish deferred")
        return

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

    # A failed earlier stage (promote among them, which leaves vX.Y behind)
    # never triggers the install, as the post-pipeline install never ran after
    # a failed pipeline: the commands are shown to run once it is fixed. An
    # optional stage's failure (update-deps) does not count (issue #4828).
    blocking = blocking_failures(ctx)
    if blocking:
        ctx.consumer_refresh_message = (
            f"Consumer refresh commands for v{ctx.version} (not run: earlier stages "
            f"failed: {', '.join(blocking)}). Fix the cause, then run "
            f"them yourself:\n\n{commands}"
        )
        print(ctx.consumer_refresh_message)
        progress.note("not run: earlier stages failed")
        return

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
    status. Standard input is closed, so a command can never wait on the
    terminal under the progress renderer.

    Nothing is shown on the terminal here, but the run goes to the run log
    as it happens, success or failure (issue #4812): the commands, each
    output line as it arrives (as :func:`progress.run` streams any other
    command's output), and the exit status.
    """
    progress.log_only(f"{PHASE}: running the commands:")
    for command in commands.splitlines():
        progress.log_only(f"  {command}")
    script = "set -eo pipefail\n" + commands
    proc = subprocess.Popen(  # noqa: S603
        ("bash", "-c", script),  # noqa: S607
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
    )
    lines: list[str] = []
    try:
        for line in proc.stdout or ():
            lines.append(line)
            progress.log_only(line.rstrip("\n"))
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise
    progress.log_only(f"{PHASE}: the commands exited {returncode}")
    return returncode, "".join(lines)
