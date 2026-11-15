"""A bounded retry of the Python audit gate's ``pip-audit`` call.

``pip-audit`` queries PyPI's vulnerability feed for every pinned package, so
one slow PyPI response fails the whole audit stage, and with it a merge, on
nothing more than a network blip (issue #4423: a ``ReadTimeoutError`` on one
leg of a dependency update whose other legs passed on the same lock).

:func:`run_pip_audit` re-runs the command only when its failure is a network
or timeout error, named by its stderr. A run that reports a vulnerability
finding is never retried, even when its stderr also carries a network
message, and any other failure (a bad requirements file, a hash mismatch)
fails at once. When every attempt fails on a network error, the stage fails
loudly with that error and the attempt count: there is no silent pass.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import progress

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# The waits, in seconds, before each retry: two retries, three attempts.
RETRY_DELAYS: tuple[float, ...] = (10.0, 30.0)

# stderr fragments that mark a failure as a network or timeout error: the
# requests/urllib3 exceptions an uncaught PyPI timeout raises, and the
# messages pip-audit's own ``ConnectionError`` carries.
_NETWORK_ERROR_PATTERN = re.compile(
    r"ReadTimeout|ConnectTimeout|ConnectionError|Max retries exceeded|"
    r"Read timed out|Connection reset|Connection refused|RemoteDisconnected|"
    r"Temporary failure in name resolution|Name or service not known|"
    r"Could not connect to PyPI|PyPI is not redirecting properly"
)

# pip-audit's summary line when it found vulnerabilities. Its presence means
# the run completed and reported a finding, which must never be retried.
_FINDING_PATTERN = re.compile(r"Found \d+ known vulnerabilit")


def network_error(stderr: str) -> str | None:
    """Return the line of *stderr* naming a network error, or ``None``.

    ``None`` whenever *stderr* reports a vulnerability finding, so a finding
    is never classified as transient.
    """
    if _FINDING_PATTERN.search(stderr):
        return None
    for line in stderr.splitlines():
        if _NETWORK_ERROR_PATTERN.search(line):
            return line.strip()
    return None


def run_pip_audit(
    cmd: Sequence[str],
    *,
    delays: Sequence[float] = RETRY_DELAYS,
    run: Callable[..., int] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Run the ``pip-audit`` *cmd*, retrying only on a network error.

    Makes at most ``len(delays) + 1`` attempts, waiting ``delays[i]`` seconds
    before retry ``i + 1``. Returns ``0`` on a passing attempt, or the exit
    code of the last attempt otherwise. *sleep* defaults to
    :func:`time.sleep` and *run* to
    :func:`vergil_tooling.lib.progress.run`, which streams the output and,
    with ``check=True``, raises ``CalledProcessError`` carrying stderr.
    """
    runner = run or progress.run
    wait = sleep or time.sleep
    attempts = len(delays) + 1
    attempt = 0
    while True:
        attempt += 1
        try:
            return runner(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            error = network_error(exc.stderr or "")
            if error is None:
                return exc.returncode
            if attempt == attempts:
                progress.emit(
                    f"ERROR: pip-audit failed on a network error on all {attempts} "
                    f"attempts; last error: {error}"
                )
                return exc.returncode
            delay = delays[attempt - 1]
            progress.emit(
                f"WARNING: pip-audit attempt {attempt} of {attempts} failed on a "
                f"network error ({error}); retrying in {delay:g}s"
            )
            wait(delay)
