"""The stage checklist of a workflow, kept in a tracking issue's body.

``vrg-checkpoint`` records how far a multi-session workflow has got as one
delimited GitHub task list per workflow in the tracking issue's body (epic
vergil-project/.github#1546, spec §4.1, §4.2)::

    <!-- vrg-checkpoint:epic-create -->
    - [x] bootstrap
    - [ ] brainstorm
    ...
    <!-- /vrg-checkpoint:epic-create -->

Each workflow's stage list is defined once, in :data:`WORKFLOWS`, and the
block is generated from it. The parse is strict: every line between the
markers must be exactly ``- [ ] <stage>`` or ``- [x] <stage>`` (a hand tick in
the web UI may write ``[X]``), so a blank line, prose or any text after a
stage name makes the block :class:`Malformed`, as does a second block for the
same workflow, or a marker without its partner. Per-stage detail belongs in
progress comments.

This is a sibling of :mod:`vergil_tooling.lib.release.checklist`, not a
generalization of it: that module's lenient parse is what ``vrg-release
--resume`` accepts, and it is left as it is.

Edits keep every byte of the body outside the block: lines keep their own
endings, and a new or rewritten block takes the line ending the body already
uses (:mod:`vergil_tooling.lib.issue_body`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.issue_body import line_content, line_ending, split_lines

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

WORKFLOWS: Mapping[str, tuple[str, ...]] = {
    "epic-create": (
        "bootstrap",
        "brainstorm",
        "spec-pushback",
        "spec-review",
        "plan",
        "plan-pushback",
        "alignment",
        "file-tasks",
        "docs-pr",
    ),
}
"""Each workflow's stages, in the order they run."""

_ITEM_RE = re.compile(r"^- \[([ xX])\] ([a-z][a-z0-9-]*)$")


class Malformed(Exception):  # noqa: N818 - the plan's name, read as "the block is malformed"
    """The body's block for the workflow breaks the strict format."""


class Missing(Exception):  # noqa: N818 - the plan's name, read as "the block is missing"
    """The body holds no block for the workflow."""


@dataclass(frozen=True)
class Skew:
    """How a recorded block differs from the workflow's current stage list."""

    recorded: tuple[str, ...]
    current: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    reordered: bool


def begin(workflow: str) -> str:
    """The block's begin marker."""
    return f"<!-- vrg-checkpoint:{workflow} -->"


def end(workflow: str) -> str:
    """The block's end marker."""
    return f"<!-- /vrg-checkpoint:{workflow} -->"


def render(workflow: str, checked: Iterable[str] = ()) -> str:
    """The block for *workflow*'s current list, markers included, no trailing newline.

    A stage in *checked* is ticked. Raises :class:`KeyError` for an unknown
    workflow and :class:`ValueError` for a *checked* name not in its list.
    """
    stages = WORKFLOWS[workflow]
    done = set(checked)
    unknown = sorted(done - set(stages))
    if unknown:
        msg = f"not a stage of {workflow}: {', '.join(unknown)}"
        raise ValueError(msg)
    lines = [begin(workflow)]
    lines.extend(f"- [{'x' if s in done else ' '}] {s}" for s in stages)
    lines.append(end(workflow))
    return "\n".join(lines)


def _blocks(lines: Sequence[str], workflow: str) -> list[tuple[int, int]]:
    """The (begin, end) line indices of every closed block for *workflow*.

    Raises :class:`Malformed` for a begin marker with no end marker (or a
    second begin marker before the end) and an end marker with no begin
    marker: where such a block starts or ends is unknown.
    """
    opener, closer = begin(workflow), end(workflow)
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        content = line_content(line)
        if content == opener:
            if start is not None:
                msg = f"the {workflow} checkpoint block on line {start + 1} has no end marker"
                raise Malformed(msg)
            start = index
        elif content == closer:
            if start is None:
                msg = (
                    f"the {workflow} checkpoint end marker on line {index + 1} has no begin marker"
                )
                raise Malformed(msg)
            blocks.append((start, index))
            start = None
    if start is not None:
        msg = f"the {workflow} checkpoint block on line {start + 1} has no end marker"
        raise Malformed(msg)
    return blocks


def _region(lines: Sequence[str], workflow: str) -> tuple[int, int]:
    """The one block's (begin, end) indices; :class:`Missing` or :class:`Malformed` otherwise."""
    blocks = _blocks(lines, workflow)
    if not blocks:
        msg = f"no {workflow} checkpoint block in the issue body"
        raise Missing(msg)
    if len(blocks) > 1:
        msg = f"the issue body holds {len(blocks)} blocks for {workflow}; expected one"
        raise Malformed(msg)
    return blocks[0]


def _parse_lines(lines: Sequence[str], workflow: str) -> list[tuple[str, bool]]:
    first, last = _region(lines, workflow)
    pairs: list[tuple[str, bool]] = []
    for index in range(first + 1, last):
        content = line_content(lines[index])
        match = _ITEM_RE.match(content)
        if match is None:
            msg = (
                f"line {index + 1} of the {workflow} checkpoint block is not "
                f"'- [ ] <stage>' or '- [x] <stage>': {content!r}"
            )
            raise Malformed(msg)
        pairs.append((match.group(2), match.group(1) != " "))
    return pairs


def parse(body: str, workflow: str) -> list[tuple[str, bool]]:
    """The block's ``[(stage, ticked)]``, in block order.

    Raises :class:`Missing` with no block, and :class:`Malformed` for a
    second block, an unclosed block, an end marker with no begin, or any line
    between the markers that is not a stage line.
    """
    return _parse_lines(split_lines(body), workflow)


def skew(pairs: Sequence[tuple[str, bool]], workflow: str) -> Skew | None:
    """How the recorded *pairs* differ from *workflow*'s current list, or None.

    *added* is in current order, *removed* in recorded order. *reordered* is
    true when the stages in both lists come in a different order (a stage
    recorded twice counts as reordered).
    """
    recorded = tuple(name for name, _ in pairs)
    current = WORKFLOWS[workflow]
    if recorded == current:
        return None
    added = tuple(s for s in current if s not in recorded)
    removed = tuple(s for s in recorded if s not in current)
    common_recorded = [s for s in recorded if s in current]
    common_current = [s for s in current if s in recorded]
    return Skew(
        recorded=recorded,
        current=current,
        added=added,
        removed=removed,
        reordered=common_recorded != common_current,
    )


def next_stage(pairs: Sequence[tuple[str, bool]]) -> str | None:
    """The first unticked stage, or None when every stage is ticked."""
    return next((name for name, done in pairs if not done), None)


def upsert(body: str, block: str, workflow: str) -> str:
    """*body* with *block* as its *workflow* block.

    Replaces the one block in place, or, with none, appends *block* after a
    blank line (an empty body becomes the block alone). The block takes the
    body's line ending and ends with one. Raises :class:`Malformed` for a
    malformed body (an existing block's lines are not checked), before
    anything is changed.
    """
    lines = split_lines(body)
    newline = line_ending(body)
    framed = block.replace("\n", newline)
    try:
        first, last = _region(lines, workflow)
    except Missing:
        if not body:
            return framed + newline
        separator = newline if body.endswith("\n") else newline + newline
        return body + separator + framed + newline
    ending = lines[last][len(line_content(lines[last])) :]
    return "".join(lines[:first]) + framed + ending + "".join(lines[last + 1 :])


def tick(body: str, workflow: str, stage: str) -> str:
    """*body* with *stage* ticked; *body* itself when it is already ticked.

    Only the stage's own line changes. Raises :class:`Missing` or
    :class:`Malformed` as :func:`parse` does, and :class:`ValueError` for a
    stage not in the block or one with an earlier stage unticked: stages are
    linear.
    """
    lines = split_lines(body)
    pairs = _parse_lines(lines, workflow)
    names = [name for name, _ in pairs]
    if stage not in names:
        msg = f"{stage!r} is not a stage in the {workflow} checkpoint block"
        raise ValueError(msg)
    position = names.index(stage)
    if pairs[position][1]:
        return body
    earlier = [name for name, done in pairs[:position] if not done]
    if earlier:
        msg = f"cannot tick {stage!r}: earlier stages are unticked: {', '.join(earlier)}"
        raise ValueError(msg)
    first, _ = _region(lines, workflow)
    index = first + 1 + position
    content = line_content(lines[index])
    lines[index] = f"- [x] {stage}" + lines[index][len(content) :]
    return "".join(lines)


def remove(body: str, workflow: str) -> str:
    """*body* without any closed *workflow* block, nor the blank line before each.

    Every closed block goes, from its begin marker to its end marker, whatever
    lies between (a malformed or duplicate block included); a body with none
    is returned as it is. Raises :class:`Malformed` for a begin marker with no
    end marker, or an end marker with no begin, before anything is changed.
    """
    lines = split_lines(body)
    blocks = _blocks(lines, workflow)
    if not blocks:
        return body
    kept: list[str] = []
    index = 0
    for first, last in blocks:
        kept.extend(lines[index:first])
        if kept and not line_content(kept[-1]).strip():
            kept.pop()
        index = last + 1
    kept.extend(lines[index:])
    return "".join(kept)
