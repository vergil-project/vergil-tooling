"""CI-aware output formatting for CI and interactive use.

Detection: ``$GITHUB_ACTIONS == "true"`` (owned by ``lib/progress.py``).
When actually running under GitHub Actions, output uses GitHub Actions
workflow commands; otherwise output is formatted for human reading.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from vergil_tooling.lib.progress import is_github_actions


def is_ci() -> bool:
    """True when actually running under GitHub Actions.

    Detection is owned by lib/progress.py; this fixes the old behavior where
    merely piped output (local pipes, agent runs) got ::error:: annotations.
    """
    return is_github_actions()


def emit_error(msg: str, *, file: str | None = None, line: int | None = None) -> None:
    if is_ci():
        params = _annotation_params(file=file, line=line)
        print(f"::error {params}::{msg}", file=sys.stderr)
    else:
        print(f"ERROR: {msg}", file=sys.stderr)


def emit_warning(msg: str, *, file: str | None = None, line: int | None = None) -> None:
    if is_ci():
        params = _annotation_params(file=file, line=line)
        print(f"::warning {params}::{msg}", file=sys.stderr)
    else:
        print(f"WARNING: {msg}", file=sys.stderr)


def write_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT") if is_ci() else None
    if output_path:
        with Path(output_path).open("a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"{key}: {value}")


def write_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY") if is_ci() else None
    if summary_path:
        with Path(summary_path).open("a") as f:
            f.write(markdown if markdown.endswith("\n") else markdown + "\n")
    else:
        print(markdown)


def _annotation_params(*, file: str | None, line: int | None) -> str:
    parts: list[str] = []
    if file is not None:
        parts.append(f"file={file}")
    if line is not None:
        parts.append(f"line={line}")
    return ",".join(parts)
