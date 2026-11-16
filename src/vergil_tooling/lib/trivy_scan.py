"""The Trivy scan of PR CI's ``security / trivy`` gate, run by vrg-validate.

The scan mirrors vergil-actions' ``actions/shared/security/trivy/scan.sh``,
which the gate runs as ``scan-type: fs`` with the action's defaults (scan
ref ``.``, scanners ``vuln``, severity ``CRITICAL,HIGH``, exit code 1, no
``trivyignores`` input, so Trivy reads the ``.trivyignore`` and
``trivy.yaml`` in the directory it scans from):

1. ``trivy fs`` writes the raw JSON report, with exit code 0 and the
   pip-vendored CycloneDX SBOM skipped;
2. ``trivy convert`` prints the table of findings, with exit code 0;
3. ``trivy convert`` writes the SARIF report, with the gate's exit code.

Its three outcomes are scan.sh's: *passed* (the SARIF was written and the
last step exited 0), *findings* (the SARIF was written and it exited
non-zero), and *error* (no SARIF: Trivy stopped before it could report, a
failed vulnerability-database download being the usual cause). An error is
retried with the same backoff, and a scan that never writes a report never
passes.

The one addition is ``--cache-dir`` on the scan, so the vulnerability
database is cached between runs, outside the working tree
(:func:`cache_dir`). ``vrg-container-run`` mounts the named volume
:data:`CACHE_VOLUME` at :data:`CONTAINER_CACHE_DIR` and points
:data:`CACHE_ENV` at it, so the cache outlives each ``--rm`` container.
Concurrent runs sharing the cache take turns (:func:`cache_lock`).
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

# The gate's inputs (vergil-actions actions/shared/security/trivy/action.yml
# defaults, which ci-security.yml's trivy job does not override).
SCANNERS = "vuln"
SEVERITY = "CRITICAL,HIGH"
EXIT_CODE = "1"
SCAN_REF = "."

# scan.sh skips the CycloneDX SBOM pip vendors (pip/_vendor/bom.cdx.json):
# Trivy would otherwise report pip's vendored internals as findings.
SKIP_FILES = "**/pip/_vendor/bom.cdx.json"

# scan.sh's retry of a scan that produced no report: TRIVY_SCAN_ATTEMPTS and
# TRIVY_SCAN_RETRY_DELAY defaults (attempt N waits N times the delay).
SCAN_ATTEMPTS = 3
RETRY_DELAY_SECS = 15

# Where the vulnerability database is cached. vrg-container-run mounts the
# named volume at the container path and sets the env var to it.
CACHE_ENV = "VRG_TRIVY_CACHE_DIR"
CACHE_VOLUME = "vergil-trivy-cache"
CONTAINER_CACHE_DIR = "/var/cache/vergil/trivy"

_LOCK_NAME = "vrg-validate.lock"
RAW_REPORT = "trivy-raw.json"
SARIF_REPORT = "trivy-results.sarif"

Outcome = Literal["passed", "findings", "error"]


class TrivyError(Exception):
    """The scan cannot be set up (an unusable cache location)."""


@dataclass(frozen=True)
class ScanOutcome:
    """How the scan ended: its outcome, and the exit status scan.sh gives it."""

    result: Outcome
    returncode: int


def cache_dir(repo_root: Path, environ: Mapping[str, str], home: Path | None = None) -> Path:
    """The vulnerability database cache directory, never inside *repo_root*.

    :data:`CACHE_ENV` names it when set (``vrg-container-run`` sets it to the
    mounted volume); it must be a non-empty absolute path. Otherwise it is
    ``vergil/trivy`` under ``$XDG_CACHE_HOME`` (when absolute) or
    ``~/.cache``. Raises :class:`TrivyError` for an unusable value, or for a
    directory in the working tree, where the database would be written into
    the repository.
    """
    raw = environ.get(CACHE_ENV)
    if raw is not None:
        if not raw.strip():
            msg = f"{CACHE_ENV} is set but empty"
            raise TrivyError(msg)
        path = Path(raw)
        if not path.is_absolute():
            msg = f"{CACHE_ENV} must be an absolute path, not {raw!r}"
            raise TrivyError(msg)
    else:
        xdg = environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else (home or Path.home()) / ".cache"
        path = base / "vergil" / "trivy"
    resolved = path.resolve()
    root = repo_root.resolve()
    if resolved == root or resolved.is_relative_to(root):
        msg = (
            f"the Trivy vulnerability database cache {resolved} is inside the working "
            f"tree {root}; set {CACHE_ENV} to a directory outside it"
        )
        raise TrivyError(msg)
    return resolved


def scan_commands(workdir: Path, cache: Path) -> list[list[str]]:
    """scan.sh's three Trivy commands, writing the reports under *workdir*."""
    raw = str(workdir / RAW_REPORT)
    return [
        [
            "trivy",
            "fs",
            "--scanners",
            SCANNERS,
            "--severity",
            SEVERITY,
            "--exit-code",
            "0",
            "--format",
            "json",
            "--output",
            raw,
            "--skip-files",
            SKIP_FILES,
            "--cache-dir",
            str(cache),
            SCAN_REF,
        ],
        ["trivy", "convert", "--severity", SEVERITY, "--format", "table", "--exit-code", "0", raw],
        [
            "trivy",
            "convert",
            "--severity",
            SEVERITY,
            "--format",
            "sarif",
            "--exit-code",
            EXIT_CODE,
            "--output",
            str(workdir / SARIF_REPORT),
            raw,
        ],
    ]


@contextlib.contextmanager
def cache_lock(cache: Path, log: Callable[[str], None]) -> Generator[None]:
    """Hold an exclusive lock on *cache* for one scan, creating the directory.

    Parallel validation runs share the cache volume; taking turns keeps two
    of them from downloading the database into it at once.
    """
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / _LOCK_NAME).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"trivy: waiting for another scan to release the database cache {cache}")
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_scan(
    target: Path,
    workdir: Path,
    cache: Path,
    *,
    run: Callable[[Sequence[str], Path], int],
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = SCAN_ATTEMPTS,
    delay: int = RETRY_DELAY_SECS,
) -> ScanOutcome:
    """Scan *target* as scan.sh does and return its outcome.

    *run* runs one command from the given working directory (the scan runs
    from *target*, as the gate's runs from the checkout) and returns its
    exit status. The commands stop at the first that fails, as scan.sh's
    ``set -e`` does. The outcome rests on whether the SARIF report was
    written, never on the exit status alone.
    """
    commands = scan_commands(workdir, cache)
    report = workdir / SARIF_REPORT
    for attempt in range(1, attempts + 1):
        # A report left by an earlier attempt must not stand in for this one's.
        report.unlink(missing_ok=True)
        (workdir / RAW_REPORT).unlink(missing_ok=True)
        rc = 0
        for cmd in commands:
            rc = run(cmd, target)
            if rc != 0:
                break
        if report.is_file():
            return ScanOutcome("passed", 0) if rc == 0 else ScanOutcome("findings", rc)
        log(
            f"WARNING: trivy: the scan produced no report (exit {rc}, attempt {attempt}/{attempts})"
        )
        if attempt < attempts:
            backoff = attempt * delay
            log(f"trivy: retrying the scan in {backoff}s")
            sleep(backoff)
    return ScanOutcome("error", 1)
