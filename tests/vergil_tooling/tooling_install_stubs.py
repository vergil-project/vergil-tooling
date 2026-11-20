"""Stub ``git``, ``uv`` and ``vrg-whoami`` for running rendered install lines under bash.

Each stub appends one line per call to ``$STUB_LOG``: the command, then its
arguments, tab-separated. ``uv`` also logs the uv environment it saw and its
working directory. Behaviour is set through ``STUB_*`` variables.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The commit ``git rev-parse`` reports in a stubbed checkout, unless
# ``STUB_REV_PARSE`` overrides it.
STUB_SHA = "0123456789abcdef0123456789abcdef01234567"

_LOG_ARGS ="log() { (IFS=$'\\t'; printf '%s\\n' \"$*\") >> \"$STUB_LOG\"; }"

_GIT = f"""#!/bin/bash
{_LOG_ARGS}
log git "$@"
if [ "$1" = ls-remote ]; then
  [ -n "${{STUB_LS_REMOTE_RC:-}}" ] && exit "$STUB_LS_REMOTE_RC"
  printf '%s' "${{STUB_LS_REMOTE:-}}"
  exit 0
fi
if [ "$1" = init ]; then mkdir -p "${{@: -1}}"; exit 0; fi
if [ "$1" = -C ] && [ "$3" = fetch ]; then exit "${{STUB_FETCH_RC:-0}}"; fi
if [ "$1" = -C ] && [ "$3" = rev-parse ]; then
  printf '%s\\n' "${{STUB_REV_PARSE-{STUB_SHA}}}"
  exit 0
fi
exit 0
"""

_UV = f"""#!/bin/bash
{_LOG_ARGS}
log uv "$@"
log uv-env "UV_PYTHON=${{UV_PYTHON-<unset>}}" \
  "UV_PYTHON_PREFERENCE=${{UV_PYTHON_PREFERENCE-<unset>}}" \
  "UV_NO_MANAGED_PYTHON=${{UV_NO_MANAGED_PYTHON-<unset>}}" \
  "UV_PYTHON_INSTALL_DIR=${{UV_PYTHON_INSTALL_DIR-<unset>}}" \
  "UV_PYTHON_DOWNLOADS=${{UV_PYTHON_DOWNLOADS-<unset>}}" \
  "UV_TOOL_DIR=${{UV_TOOL_DIR-<unset>}}" \
  "UV_TOOL_BIN_DIR=${{UV_TOOL_BIN_DIR-<unset>}}" \
  "PWD=$PWD"
if [ "$1" = export ]; then
  [ -n "${{STUB_EXPORT_RC:-}}" ] && {{ echo "error: no uv.lock" >&2; exit "$STUB_EXPORT_RC"; }}
  out=""
  while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
  printf '%s' "${{STUB_EXPORT-rich==15.0.0
}}" > "$out"
  exit 0
fi
if [ "$1" = tool ]; then exit "${{STUB_INSTALL_RC:-0}}"; fi
exit 0
"""

_WHOAMI = """#!/bin/bash
exit "${STUB_WHOAMI_RC:-0}"
"""


@dataclass
class Run:
    """One bash run of rendered lines: exit code, output and the stub call log."""

    returncode: int
    stdout: str
    stderr: str
    calls: list[list[str]]

    def of(self, command: str) -> list[list[str]]:
        """The argument lists of every call to *command*."""
        return [c[1:] for c in self.calls if c[0] == command]


def write_stub(directory: Path, name: str, body: str) -> Path:
    """Write an executable stub *name* with *body* into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def stub_bin(tmp_path: Path) -> Path:
    """A directory holding the ``git``, ``uv`` and ``vrg-whoami`` stubs."""
    bin_dir = tmp_path / "stub-bin"
    write_stub(bin_dir, "git", _GIT)
    write_stub(bin_dir, "uv", _UV)
    write_stub(bin_dir, "vrg-whoami", _WHOAMI)
    return bin_dir


def run_lines(
    script: str,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    path: list[Path] | None = None,
    cwd: Path | None = None,
) -> Run:
    """Run *script* under ``bash`` with the stubs first on ``PATH`` (or *path*)."""
    log = tmp_path / "stub.log"
    log.write_text("")
    dirs = path if path is not None else [stub_bin(tmp_path)]
    full_env = {
        "PATH": os.pathsep.join([*(str(d) for d in dirs), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path / "home"),
        "STUB_LOG": str(log),
        "TMPDIR": str(tmp_path),
        # Set, to show the lines clear them.
        "UV_PYTHON": "/opt/hostile/python",
        "UV_PYTHON_PREFERENCE": "only-system",
        "UV_NO_MANAGED_PYTHON": "1",
        **(env or {}),
    }
    result = subprocess.run(  # noqa: S603
        ("bash", "-c", script),  # noqa: S607
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd or tmp_path,
        check=False,
    )
    calls = [line.split("\t") for line in log.read_text().splitlines()]
    return Run(result.returncode, result.stdout, result.stderr, calls)
