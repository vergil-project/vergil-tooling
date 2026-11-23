"""The ``install`` stage: run the release's ``[publish].install`` commands.

It runs after ``finalize`` and ``update-deps``, and needs ``promote`` done:
an install that follows the rolling ``vX.Y`` tag (vergil-tooling's
``vrg-tooling-install v<MINOR>``, vergil-claude-plugin's marketplace) installs
this release only once ``promote`` has moved the tag. Before it runs anything
it checks that ``vX.Y`` names this release's tag, and refuses if it does not
(a failed ``promote``, or a hand demotion in between). A deferred publish (a
failed CD publish job, the package index) never holds it back (spec §4.4 and
§4.6 of epic vergil-project/.github#1440).

A failed install records nothing done and ends the run non-zero, so
``vrg-release --resume`` runs it again; a resume whose state block records it
done skips it. A repo with no install configured is the normal case: the stage
is ``none configured``, with no warning, and recorded done.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, progress
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext

PHASE = "install"
# The stage install needs done first: it moves vX.Y onto the release.
_PROMOTE = "promote"
_SHORT = 12
# The line an install command prints for what it installed, as
# vrg-tooling-install does: ``installed vX.Y.Z (commit <sha>)``.
_REPORTED_RE = re.compile(
    r"\binstalled v(?P<version>\d+\.\d+\.\d+) \(commit (?P<commit>[0-9a-f]{7,40})\)"
)


def minor(version: str) -> str:
    """The ``X.Y`` line of release *version* ``X.Y.Z``."""
    return ".".join(version.split(".")[:2])


def expand(template: str, version: str) -> str:
    """*template* with ``<VERSION>`` as *version* and ``<MINOR>`` as its ``X.Y`` line."""
    return template.replace("<VERSION>", version).replace("<MINOR>", minor(version))


def install(ctx: ReleaseContext) -> None:
    """Run the release's install commands, rolled up to one line.

    The version-expanded command block is stored on ``ctx.install_commands``.
    ``ctx.install_message`` is set only when the human has something to read
    once the progress renderer has collapsed this stage: a failed run's full
    output. ``vrg-release`` re-prints it below the summary.

    On success the commands' output is not shown, so the stage is the one
    ``✓ install`` line, and what they reported installing, if anything, is
    stored on ``ctx.install_reported`` (:func:`reported_install`). On
    failure the full output is shown and the stage fails with the exit
    status. Either way the run log holds the commands,
    their full output and their exit status (issue #4812).
    """
    template = config.read_config(ctx.repo_root).publish.install
    ctx.install_reported = None
    if template is None:
        ctx.install_commands = None
        ctx.install_message = None
        progress.note("none configured")
        return

    commands = expand(template, ctx.version)
    ctx.install_commands = commands

    _require_promote(ctx)
    check_line_on_release(ctx)

    returncode, output = run_install(commands, cwd=ctx.repo_root)
    if returncode == 0:
        ctx.install_message = None
        ctx.install_reported = reported_install(output)
        return

    ctx.install_message = (
        f"Install for v{ctx.version} failed (exit {returncode}). "
        f"The commands:\n\n{commands}\n\nTheir full output:\n\n{output.rstrip()}"
    )
    print(ctx.install_message)
    # The output stays out of the error: the error is posted on the public
    # tracking issue, and the output is the local machine's install log.
    raise ReleaseError(
        phase=PHASE,
        command=PHASE,
        message=(
            f"the install commands failed (exit {returncode}); the release itself is "
            "unaffected. Their full output is shown below the summary. Once the cause "
            f"is fixed, `vrg-release --resume {ctx.version}` runs the install again."
        ),
    )


def reported_install(output: str) -> tuple[str, str] | None:
    """The ``(X.Y.Z, commit)`` an install's *output* reports installing, or ``None``.

    The report is a line holding ``installed vX.Y.Z (commit <sha>)``, as
    ``vrg-tooling-install`` prints. When several lines report one, the last
    wins, since it is what the commands left installed. Output with no such
    line reports nothing, the normal case for any other install command.
    """
    found = None
    for match in _REPORTED_RE.finditer(output):
        found = (match.group("version"), match.group("commit"))
    return found


def _require_promote(ctx: ReleaseContext) -> None:
    """Refuse unless the state block records ``promote`` done for this release."""
    if ctx.state is not None and _PROMOTE in ctx.state.done:
        return
    raise ReleaseError(
        phase=PHASE,
        command=PHASE,
        message=(
            f"install needs promote done, and the state block does not record it, so "
            f"v{minor(ctx.version)} may not name v{ctx.version}. Once promote succeeds, "
            f"`vrg-release --resume {ctx.version}` runs the install."
        ),
    )


def check_line_on_release(ctx: ReleaseContext) -> None:
    """Refuse unless the rolling tag ``vX.Y`` on origin peels to this release's tag commit.

    One ``git ls-remote origin refs/tags/vX.Y refs/tags/vX.Y^{}``: an
    annotated ``vX.Y`` lists its tag object and then, as ``^{}``, the commit
    it points at, which wins; a lightweight one lists only its commit. The
    commit must be ``ctx.tag_commit``, the commit the release tag peels to.
    Anything else (no ``vX.Y``, another commit, no recorded tag commit) is a
    :class:`ReleaseError` naming both, never an install of another release.
    """
    line = f"v{minor(ctx.version)}"
    ref = f"refs/tags/{line}"
    command = f"git ls-remote origin {ref} {ref}^{{}}"
    if ctx.tag_commit is None:
        raise ReleaseError(
            phase=PHASE,
            command=command,
            message=(
                f"no commit is recorded for v{ctx.version}'s tag, so whether {line} names "
                "it cannot be checked; refusing to install."
            ),
        )
    listing = git.read_output("ls-remote", "origin", ref, f"{ref}^{{}}")
    direct: str | None = None
    peeled: str | None = None
    for row in listing.splitlines():
        fields = row.split()
        if len(fields) != 2:  # noqa: PLR2004
            continue
        if fields[1] == ref:
            direct = fields[0]
        elif fields[1] == f"{ref}^{{}}":
            peeled = fields[0]
    commit = peeled or direct
    if commit is None:
        raise ReleaseError(
            phase=PHASE,
            command=command,
            message=f"{line} does not exist on origin, so it cannot name v{ctx.version}.",
        )
    if commit != ctx.tag_commit:
        raise ReleaseError(
            phase=PHASE,
            command=command,
            message=(
                f"{line} names {commit[:_SHORT]}, not v{ctx.version}'s "
                f"{ctx.tag_commit[:_SHORT]}; refusing to install another release. Once "
                f"{line} names v{ctx.version}, `vrg-release --resume {ctx.version}` runs "
                "the install."
            ),
        )


def run_install(commands: str, *, cwd: Path) -> tuple[int, str]:
    """Run the version-expanded install *commands*, fail-fast.

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
