"""The epic Documents section: its one format, parser and body editor.

Every epic body opens with a marked section that links the epic's spec, plan
and retrospective, or carries a note saying why one does not exist (epic
vergil-project/.github#819)::

    <!-- vrg-epic-docs -->
    **Documents**

    - Spec: [`epics/819-x/spec.md`](https://github.com/o/.github/blob/develop/epics/819-x/spec.md)
    - Plan: pending
    - Retrospective: pending
    <!-- /vrg-epic-docs -->

This module is the only place that format lives. ``vrg-epic-docs`` writes the
section through :func:`render` and :func:`upsert`; the audit reads it through
:func:`parse`.

Bodies are edited line by line with their original line endings kept, so a
``\\r\\n`` body (GitHub's web editor saves those) keeps every byte outside the
marked region and the removed legacy lines. Marker and legacy-line detection
ignore lines inside fenced code blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from vergil_tooling.lib import branch_sweep, github

if TYPE_CHECKING:
    from collections.abc import Mapping

DOCS = ("spec", "plan", "retrospective")
"""The three documents, in the order the section lists them."""

BEGIN = "<!-- vrg-epic-docs -->"
END = "<!-- /vrg-epic-docs -->"
HEADING = "**Documents**"

PENDING = "pending"
NOT_PLANNED = "not written (closed as not planned)"
PREDATES_SPEC_PLAN = "not written (predates the spec and plan convention)"
PREDATES_RETRO = "not written (predates the retrospective convention)"
NOT_WRITTEN = "not written"

NOTES = frozenset({PENDING, NOT_PLANNED, PREDATES_SPEC_PLAN, PREDATES_RETRO, NOT_WRITTEN})
"""Every note an entry may carry in place of a link."""

_LABELS = {"spec": "Spec", "plan": "Plan", "retrospective": "Retrospective"}


class EpicDocsFormatError(ValueError):
    """A body's Documents section is malformed: half a section, or a bad entry."""


@dataclass(frozen=True)
class DocLink:
    """A link to one document: *path* in *owner*/*repo*, on *branch*.

    *branch* is the repo's default branch, queried from GitHub by the caller
    (:func:`default_branch`), never assumed.
    """

    owner: str
    repo: str
    path: str
    branch: str

    def __post_init__(self) -> None:
        for name in ("owner", "repo", "path", "branch"):
            value = getattr(self, name)
            if not value or any(c.isspace() or c == "`" for c in value):
                msg = f"DocLink {name} must be non-empty, without spaces or backticks: {value!r}"
                raise ValueError(msg)
        if self.path.startswith("/"):
            msg = f"DocLink path must be repo-relative: {self.path!r}"
            raise ValueError(msg)

    def text(self, home_repo: str) -> str:
        """The link text: the path, or ``<repo>/<path>`` outside *home_repo*.

        *home_repo* is the epic's home repo **name** (``.github``), not
        ``owner/name``; a document is only ever linked within the epic's org.
        """
        if "/" in home_repo:
            msg = f"home_repo is a repo name, not owner/name: {home_repo!r}"
            raise ValueError(msg)
        return self.path if self.repo == home_repo else f"{self.repo}/{self.path}"

    @property
    def url(self) -> str:
        """The ``blob`` URL of the document on its branch."""
        return f"https://github.com/{self.owner}/{self.repo}/blob/{self.branch}/{quote(self.path)}"


Entry = DocLink | str
"""One document's entry: a :class:`DocLink`, or one of the five notes."""


# --- lines and fences --------------------------------------------------------


def _content(line: str) -> str:
    """*line* without its line ending."""
    return line.rstrip("\r\n")


def _newline(body: str) -> str:
    """The body's line ending: that of its first line break, ``\\n`` by default."""
    index = body.find("\n")
    return "\r\n" if index > 0 and body[index - 1] == "\r" else "\n"


class _Fences:
    """Track whether successive lines are inside a fenced code block."""

    def __init__(self) -> None:
        self._open: str | None = None

    def step(self, content: str) -> bool:
        """Advance over *content*; True when the line is a fence or fenced."""
        stripped = content.lstrip(" ")
        marker = next((m for m in ("```", "~~~") if stripped.startswith(m)), None)
        if self._open is None:
            if marker is not None:
                self._open = marker
                return True
            return False
        if marker == self._open:
            self._open = None
        return True


def _region(lines: list[str]) -> tuple[int, int] | None:
    """The indices of the BEGIN and END marker lines, or None for no section.

    Raises :class:`EpicDocsFormatError` for half a section: only one marker,
    END before BEGIN, or more than one of either.
    """
    fences = _Fences()
    begins: list[int] = []
    ends: list[int] = []
    for index, line in enumerate(lines):
        content = _content(line)
        if fences.step(content):
            continue
        if content.strip() == BEGIN:
            begins.append(index)
        elif content.strip() == END:
            ends.append(index)
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        msg = (
            f"malformed Documents section: expected one {BEGIN} followed by one {END}, "
            f"found {len(begins)} and {len(ends)}"
            + (" with the end marker first" if begins and ends and ends[0] < begins[0] else "")
        )
        raise EpicDocsFormatError(msg)
    return begins[0], ends[0]


# --- render and parse --------------------------------------------------------


def _render_entry(entry: Entry, home_repo: str) -> str:
    if isinstance(entry, DocLink):
        return f"[`{entry.text(home_repo)}`]({entry.url})"
    if entry not in NOTES:
        msg = f"not a Documents note: {entry!r}"
        raise ValueError(msg)
    return entry


def render(entries: Mapping[str, Entry], home_repo: str) -> str:
    """The whole Documents block, markers included, with no trailing newline.

    *entries* maps each of :data:`DOCS` to its entry; *home_repo* is the epic's
    home repo name (see :meth:`DocLink.text`).
    """
    if set(entries) != set(DOCS):
        msg = f"entries must name exactly {', '.join(DOCS)}: got {', '.join(sorted(entries))}"
        raise ValueError(msg)
    lines = [BEGIN, HEADING, ""]
    lines += [f"- {_LABELS[doc]}: {_render_entry(entries[doc], home_repo)}" for doc in DOCS]
    lines.append(END)
    return "\n".join(lines)


_ENTRY_RE = re.compile(r"^- (Spec|Plan|Retrospective): (.+)$")
_LINK_RE = re.compile(r"^\[`([^`]+)`\]\(https://github\.com/([^/\s]+)/([^/\s]+)/blob/(\S+)\)$")


def _parse_link(value: str) -> DocLink:
    """Parse a rendered link back to its :class:`DocLink`.

    The text names the path (``<repo>/`` first for another repo), which
    splits the URL's ``<branch>/<path>`` tail unambiguously even for a
    branch with a slash in it.
    """
    match = _LINK_RE.match(value)
    if match is None:
        msg = f"Documents entry is neither a note nor a link: {value!r}"
        raise EpicDocsFormatError(msg)
    text, owner, repo, tail = match.groups()
    candidates = [text]
    if text.startswith(f"{repo}/"):
        candidates.append(text[len(repo) + 1 :])
    for path in candidates:
        suffix = f"/{quote(path)}"
        if tail.endswith(suffix) and len(tail) > len(suffix):
            try:
                return DocLink(owner, repo, path, tail[: -len(suffix)])
            except ValueError as exc:
                raise EpicDocsFormatError(f"bad Documents link {value!r}: {exc}") from exc
    msg = f"Documents link text does not match its URL: {value!r}"
    raise EpicDocsFormatError(msg)


def parse(body: str) -> dict[str, Entry] | None:
    """The entries of *body*'s Documents section, or None when it has none.

    Raises :class:`EpicDocsFormatError` when the section is malformed: half a
    section, a missing heading, a missing, extra or out-of-order entry, or an
    entry that is neither a link nor one of the five notes.
    """
    lines = body.splitlines(keepends=True)
    region = _region(lines)
    if region is None:
        return None
    inner = [_content(line).strip() for line in lines[region[0] + 1 : region[1]]]
    inner = [line for line in inner if line]
    if not inner or inner[0] != HEADING:
        msg = f"Documents section does not start with {HEADING}"
        raise EpicDocsFormatError(msg)
    rows = inner[1:]
    expected = [_LABELS[doc] for doc in DOCS]
    matches = [_ENTRY_RE.match(row) for row in rows]
    labels = [m.group(1) if m else None for m in matches]
    if labels != expected:
        msg = f"Documents section must list exactly {', '.join(expected)} in order: got {rows!r}"
        raise EpicDocsFormatError(msg)
    entries: dict[str, Entry] = {}
    for doc, match in zip(DOCS, matches, strict=True):
        value = cast("re.Match[str]", match).group(2).strip()
        entries[doc] = value if value in NOTES else _parse_link(value)
    return entries


# --- legacy lines and upsert --------------------------------------------------

_LEGACY_HEADER = "Documents:"
_LEGACY_BULLET_RE = re.compile(r"^- (Spec|Plan|Retrospective):")
_LEGACY_LINE_RE = re.compile(r"^(Spec|Plan|Retrospective):")
_CONTINUATION_RE = re.compile(r"^\(.*\)\.?$")


def _legacy_group(contents: list[str], fenced: list[bool], start: int) -> int:
    """The end (exclusive) of a legacy group starting at *start*, or *start* for none.

    A group is a ``Documents:`` line with the Spec/Plan/Retrospective bullets
    after it (blank lines between allowed), or a standalone ``Spec:``,
    ``Plan:`` or ``Retrospective:`` line with an optional following line
    that is only a bracketed note.
    """
    line = contents[start]
    if line.strip() == _LEGACY_HEADER:
        index = start + 1
        while index < len(contents) and not fenced[index] and not contents[index].strip():
            index += 1
        bullets = index
        while (
            index < len(contents) and not fenced[index] and _LEGACY_BULLET_RE.match(contents[index])
        ):
            index += 1
        return index if index > bullets else start
    if _LEGACY_LINE_RE.match(line):
        index = start + 1
        if (
            index < len(contents)
            and not fenced[index]
            and _CONTINUATION_RE.match(contents[index].strip())
        ):
            index += 1
        return index
    return start


def strip_legacy(body: str) -> tuple[str, list[str]]:
    """*body* without its hand-written document lines, plus the lines removed.

    Removes a ``Documents:`` line with its Spec/Plan/Retrospective bullets,
    and standalone ``Spec:``/``Plan:``/``Retrospective:`` lines with any
    bracketed-note line after them. Nothing inside a fenced code block or the
    marked section is touched. When a removed group stood alone between blank
    lines (or at the top of the body), one blank line after it goes too, so
    no double gap is left. Every other line keeps its exact bytes.
    """
    lines = body.splitlines(keepends=True)
    contents = [_content(line) for line in lines]
    fences = _Fences()
    fenced = [fences.step(content) for content in contents]
    region = _region(lines)
    if region is not None:
        for index in range(region[0], region[1] + 1):
            fenced[index] = True
    kept: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(lines):
        end = index if fenced[index] else _legacy_group(contents, fenced, index)
        if end == index:
            kept.append(lines[index])
            index += 1
            continue
        removed += [contents[i] for i in range(index, end) if contents[i].strip()]
        alone = not kept or not _content(kept[-1]).strip()
        if alone and end < len(lines) and not contents[end].strip():
            end += 1
        index = end
    return "".join(kept), removed


def upsert(body: str, block: str) -> tuple[str, list[str]]:
    """*body* with *block* as its Documents section, plus the legacy lines removed.

    Replaces the marked region in place when there is one; otherwise
    prepends *block* and a blank line (an empty body becomes *block* alone).
    The block takes the body's line ending. Legacy document lines are removed
    (:func:`strip_legacy`). Raises :class:`EpicDocsFormatError` on half a
    section, before anything is changed.
    """
    lines = body.splitlines(keepends=True)
    region = _region(lines)
    newline = _newline(body)
    framed = block.replace("\n", newline)
    if region is not None:
        begin, end = region
        tail_ending = lines[end][len(_content(lines[end])) :]
        replaced = "".join(lines[:begin]) + framed + tail_ending + "".join(lines[end + 1 :])
        return strip_legacy(replaced)
    rest, removed = strip_legacy(body)
    rest = rest.lstrip("\r\n")
    if not rest.strip():
        return framed, removed
    return framed + newline + newline + rest, removed


# --- GitHub reads and writes --------------------------------------------------


def default_branch(owner: str, repo: str) -> str:
    """The default branch of *owner*/*repo*, queried from GitHub."""
    return branch_sweep.default_branch(f"{owner}/{repo}")


def _issue_endpoint(owner: str, repo: str, number: int) -> str:
    return f"repos/{owner}/{repo}/issues/{number}"


def read_body(owner: str, repo: str, number: int) -> str:
    """The body of issue *owner*/*repo*#*number*; an issue with no body gives ``""``."""
    endpoint = _issue_endpoint(owner, repo, number)
    data = github.read_json("api", endpoint)
    if not isinstance(data, dict):
        msg = f"unexpected API response for {endpoint}: {type(data).__name__}"
        raise ValueError(msg)  # noqa: TRY004 - a malformed API answer, not a caller type error
    body = data.get("body")
    if body is None:
        return ""
    if not isinstance(body, str):
        msg = f"unexpected body in API response for {endpoint}: {type(body).__name__}"
        raise ValueError(msg)  # noqa: TRY004 - a malformed API answer, not a caller type error
    return body


def write_body(owner: str, repo: str, number: int, body: str) -> None:
    """Replace the body of issue *owner*/*repo*#*number* with *body*."""
    github.write_json("PATCH", _issue_endpoint(owner, repo, number), {"body": body})
