"""Render, parse, and tick the release progress checklist in an issue body.

The checklist lives in an HTML-comment-delimited block so writes never disturb
the human-written parts of the tracking-issue body. The block is the resume
cursor for vrg-release --resume (issue #1612). Stage names are supplied by the
caller; this module has no knowledge of the pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

BEGIN = "<!-- vrg-release:progress -->"
END = "<!-- /vrg-release:progress -->"


class ChecklistError(Exception):
    """The checklist block is missing, malformed, or version-skewed."""


def render(stages: Sequence[str], checked: Iterable[str] = ()) -> str:
    """Return the delimited checklist block for *stages*.

    A stage in *checked* is rendered as ``[x]``, otherwise ``[ ]``.
    """
    done = set(checked)
    lines = [BEGIN]
    lines.extend(f"- [{'x' if s in done else ' '}] {s}" for s in stages)
    lines.append(END)
    return "\n".join(lines)


_ITEM_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(\S+)\s*$")


def _block_inner(body: str) -> str:
    if BEGIN not in body or END not in body:
        msg = "no vrg-release progress block found in issue body"
        raise ChecklistError(msg)
    start = body.index(BEGIN) + len(BEGIN)
    return body[start : body.index(END)]


def parse(body: str) -> list[tuple[str, bool]]:
    """Return ``[(stage, checked)]`` parsed from the block in *body*."""
    pairs: list[tuple[str, bool]] = []
    for line in _block_inner(body).splitlines():
        match = _ITEM_RE.match(line)
        if match:
            pairs.append((match.group(2), match.group(1).lower() == "x"))
    return pairs


def upsert(body: str, stages: Sequence[str], checked: Iterable[str] = ()) -> str:
    """Return *body* with the checklist block inserted or replaced.

    If a block is already present it is replaced in place; otherwise the block
    is appended after a blank line.
    """
    block = render(stages, checked)
    if BEGIN in body and END in body:
        pre = body[: body.index(BEGIN)]
        post = body[body.index(END) + len(END) :]
        return pre + block + post
    return body.rstrip() + "\n\n" + block + "\n"


def first_unchecked(body: str, expected_stages: Sequence[str]) -> str | None:
    """Return the first unchecked stage, or None if all are checked.

    Raises ``ChecklistError`` if the block's stages do not match
    *expected_stages* — a mismatch means the checklist was written by a
    different tooling version, and resume must refuse rather than guess.
    """
    pairs = parse(body)
    names = [name for name, _ in pairs]
    if names != list(expected_stages):
        msg = (
            "release checklist was written by a different vrg-release version "
            f"(found {names}, expected {list(expected_stages)}); complete the "
            "release with the original version or finish the remaining stages "
            "manually"
        )
        raise ChecklistError(msg)
    for name, checked in pairs:
        if not checked:
            return name
    return None
