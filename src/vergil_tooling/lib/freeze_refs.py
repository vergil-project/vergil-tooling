"""Freeze and validate internal action references in workflow YAML files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    text: str


def collect_yaml_files(dirs: list[Path]) -> list[Path]:
    """Collect .yml and .yaml files from the given directories."""
    seen: set[Path] = set()
    result: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for pattern in ("**/*.yml", "**/*.yaml"):
            for p in sorted(d.glob(pattern)):
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(p)
    return sorted(result)


def freeze_references(content: str, owner_repo: str, tag: str) -> str:
    """Apply reference freezing transformations to file content.

    Two transformations, applied only to lines containing ``uses:``:
    1. ``./actions/<path>`` → ``<owner_repo>/actions/<path>@<tag>``
    2. ``<owner_repo>/<path>@develop`` → ``<owner_repo>/<path>@<tag>``
    """
    escaped_owner = re.escape(owner_repo)
    lines: list[str] = []
    for line in content.split("\n"):
        if "uses:" in line:
            line = re.sub(
                r"\./actions/(\S+)",
                rf"{owner_repo}/actions/\1@{tag}",
                line,
            )
            line = re.sub(
                rf"({escaped_owner}/\S+)@develop",
                rf"\1@{tag}",
                line,
            )
        lines.append(line)
    return "\n".join(lines)


def validate_no_unfrozen(content: str, filename: str, owner_repo: str) -> list[Finding]:
    """Check for remaining unfrozen references in file content."""
    escaped_owner = re.escape(owner_repo)
    findings: list[Finding] = []
    for i, line in enumerate(content.splitlines(), 1):
        if "uses:" not in line:
            continue
        if re.search(r"uses:\s+\./actions/", line) or re.search(
            rf"{escaped_owner}/\S+@develop", line
        ):
            findings.append(Finding(file=filename, line=i, text=line.strip()))
    return findings
