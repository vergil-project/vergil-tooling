"""Read, write, and delete ``.vergil/pr-template.yml`` files.

Uses a minimal YAML-subset parser — no PyYAML dependency. Handles
flat ``key: value`` pairs, quoted values, and ``key: |`` literal
blocks (``|``, ``|-``, ``|+``). YAML folded scalars (``>``) are
deliberately rejected rather than silently mangled — see ``_parse``.
This is sufficient for the PR template format.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.linkage import ALLOWED_LINKAGES

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATE_DIR = ".vergil"
_TEMPLATE_FILE = "pr-template.yml"
_REQUIRED_FIELDS = ("issue", "title", "summary", "notes")
_LITERAL_BLOCK_INDICATORS = ("|", "|-", "|+")


class TemplateError(Exception):
    """Raised when a template file is malformed or carries invalid field values."""


def _validate_linkage(linkage: str) -> None:
    """Raise ``TemplateError`` if the linkage keyword is not allowed."""
    if linkage not in ALLOWED_LINKAGES:
        allowed = ", ".join(ALLOWED_LINKAGES)
        msg = (
            f"PR template linkage '{linkage}' is not allowed; use: {allowed}. "
            "GitHub auto-close keywords (Closes/Fixes/Resolves) are banned "
            "repo-wide — issues stay open until post-merge workflows succeed."
        )
        raise TemplateError(msg)


def _parse(text: str) -> dict[str, str]:
    """Parse the pr-template.yml format."""
    result: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in _LITERAL_BLOCK_INDICATORS:
            block_lines: list[str] = []
            i += 1
            while i < len(lines):
                if lines[i] and not lines[i][0].isspace():
                    break
                block_lines.append(lines[i])
                i += 1
            result[key] = _render_literal_block(block_lines)
        elif value.startswith(">"):
            # YAML folded scalar. The minimal parser cannot fold reliably
            # (single newline -> space, blank line -> newline, with deeper
            # indentation kept literal), and silently mis-parsing it as the
            # string ">" corrupts the field. Reject loudly instead.
            msg = (
                f"PR template field '{key}' uses a YAML folded block scalar ('>'), "
                "which is not supported and would be silently corrupted. Use a "
                "literal block ('|') or a single-line inline value instead."
            )
            raise TemplateError(msg)
        else:
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            result[key] = value
            i += 1
    return result


def _render_literal_block(block_lines: list[str]) -> str:
    """Dedent a literal block by its minimal common indent and join it.

    Blank lines are preserved (as empty lines); the result is stripped of
    leading/trailing whitespace. Chomping indicators (``-``/``+``) need no
    special handling here because the final ``strip`` already removes any
    trailing blank lines.
    """
    indents = [len(line) - len(line.lstrip()) for line in block_lines if line.strip()]
    strip_n = min(indents) if indents else 0
    dedented = ["" if not line.strip() else line[strip_n:] for line in block_lines]
    return "\n".join(dedented).strip()


def _template_path(worktree_root: Path) -> Path:
    return worktree_root / _TEMPLATE_DIR / _TEMPLATE_FILE


def read_template(worktree_root: Path) -> dict[str, str]:
    """Read and validate ``.vergil/pr-template.yml``.

    Raises ``FileNotFoundError`` if the file does not exist.
    Raises ``TemplateError`` if a required field is missing or empty, a
    folded scalar is used, or the linkage keyword is not allowed.
    """
    path = _template_path(worktree_root)
    if not path.exists():
        msg = f"No PR template found at {path}"
        raise FileNotFoundError(msg)
    fields = _parse(path.read_text())
    for field in _REQUIRED_FIELDS:
        if not fields.get(field, "").strip():
            msg = (
                f"PR template field '{field}' is required and must be non-empty. "
                "Provide a substantive value (a literal '|' block for multi-line "
                "prose); empty or placeholder fields produce useless PR bodies."
            )
            raise TemplateError(msg)
    if "linkage" in fields:
        _validate_linkage(fields["linkage"])
    return fields


def write_template(
    worktree_root: Path,
    *,
    issue: str,
    title: str,
    summary: str,
    notes: str,
    linkage: str = "Ref",
) -> Path:
    """Write ``.vergil/pr-template.yml``, warning if it already exists.

    ``issue``, ``title``, ``summary`` and ``notes`` are all required and must
    be non-empty. Raises ``TemplateError`` if any is blank, or if the linkage
    keyword is not allowed — the producer fails loudly before an empty field or
    a forbidden auto-close linkage can reach the template file.
    """
    for fname, fval in (
        ("issue", issue),
        ("title", title),
        ("summary", summary),
        ("notes", notes),
    ):
        if not fval.strip():
            msg = f"PR template field '{fname}' is required and must be non-empty."
            raise TemplateError(msg)
    _validate_linkage(linkage)
    path = _template_path(worktree_root)
    if path.exists():
        print(
            f"WARNING: Overwriting existing PR template at {path}. "
            "A leftover template indicates a previous cycle was not completed.",
            file=sys.stderr,
        )
    lines = [
        "# Generated by agent — review and edit before running vrg-submit-pr",
    ]
    for key, value in [
        ("issue", issue),
        ("title", title),
        ("summary", summary),
        ("linkage", linkage),
        ("notes", notes),
    ]:
        if "\n" in value:
            lines.append(f"{key}: |")
            for vline in value.splitlines():
                lines.append(f"  {vline}")
        elif ":" in value or value.startswith(("'", '"')):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    atomic_write(path, "\n".join(lines) + "\n")
    return path


def delete_template(worktree_root: Path) -> None:
    """Delete the template file if it exists."""
    path = _template_path(worktree_root)
    path.unlink(missing_ok=True)
