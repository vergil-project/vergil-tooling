"""Validate the repository configuration and README structure.

Checks that ``standard-tooling.toml`` is valid (required fields,
enum values, co-author format, dependencies), then validates
README.md structural conventions (exactly one H1, a Table of
Contents heading, no heading-level skips).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from standard_tooling.lib.config import CONFIG_FILE, ConfigError, read_config

_CODE_FENCE_RE = re.compile(r"^(```|~~~)")
_TOC_RE = re.compile(r"^## Table of Contents\s*$")
_HEADING_RE = re.compile(r"^(#{1,6}) ")


def _structural_check(file_path: str) -> bool:
    """Validate README structural conventions. Return True if valid."""
    in_code = False
    toc_found = False
    h1_count = 0
    last_level = 0
    errors: list[str] = []

    lines = Path(file_path).read_text(encoding="utf-8").splitlines()
    for line_num, line in enumerate(lines, start=1):
        if _CODE_FENCE_RE.match(line):
            in_code = not in_code

        if in_code:
            continue

        if _TOC_RE.match(line):
            toc_found = True

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if level == 1:
                h1_count += 1
            if last_level > 0 and level > last_level + 1:
                errors.append(
                    f"ERROR: Heading level skips from {last_level} to {level} "
                    f"({file_path}:{line_num})"
                )
            last_level = level

    if h1_count != 1:
        errors.append(f"ERROR: expected exactly one H1 heading, found {h1_count} ({file_path})")

    if not toc_found:
        errors.append(f"ERROR: missing ## Table of Contents ({file_path})")

    for error in errors:
        print(error, file=sys.stderr)

    return len(errors) == 0


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    try:
        read_config(Path.cwd())
    except FileNotFoundError:
        print(
            f"ERROR: {CONFIG_FILE} not found",
            file=sys.stderr,
        )
        return 2
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    readme = Path("README.md")
    if readme.is_file() and not _structural_check(str(readme)):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
