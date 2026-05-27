"""TTY-aware output formatting for CI and interactive use.

Detection: ``sys.stdout.isatty()``. When stdout is a TTY, output is
formatted for human reading. When not (CI, piped), output uses
GitHub Actions workflow commands.
"""

from __future__ import annotations

import os
import sys


def is_ci() -> bool:
    return not sys.stdout.isatty()


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
        with open(output_path, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"{key}: {value}")


def write_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY") if is_ci() else None
    if summary_path:
        with open(summary_path, "a") as f:
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
