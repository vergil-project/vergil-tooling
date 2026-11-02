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
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import quote

from vergil_tooling.lib import branch_sweep, epics, github

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

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
        Repo names are compared without case, as GitHub does.
        """
        if "/" in home_repo:
            msg = f"home_repo is a repo name, not owner/name: {home_repo!r}"
            raise ValueError(msg)
        return self.path if _same_repo(self.repo, home_repo) else f"{self.repo}/{self.path}"

    @property
    def url(self) -> str:
        """The ``blob`` URL of the document on its branch."""
        return f"https://github.com/{self.owner}/{self.repo}/blob/{self.branch}/{quote(self.path)}"


Entry = DocLink | str
"""One document's entry: a :class:`DocLink`, or one of the five notes."""


def _same_repo(a: str, b: str) -> bool:
    """True when *a* and *b* name the same repo: GitHub repo names ignore case."""
    return a.lower() == b.lower()


# --- lines and fences --------------------------------------------------------


def _split(body: str) -> list[str]:
    """*body*'s lines, each with its line ending.

    Only ``\\n`` (with an optional ``\\r`` before it) ends a line, as in
    Markdown; :meth:`str.splitlines` would also split on a form feed, U+2028
    and the like, which render inside one line.
    """
    parts = body.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _content(line: str) -> str:
    """*line* without its line ending."""
    return line.rstrip("\r\n")


def _is_marker(content: str, marker: str) -> bool:
    """True when *content* is *marker*, indented at most three spaces.

    Four spaces or a tab start an indented code block, so a marker there is
    an example, not the section.
    """
    text = content.rstrip()
    indent = len(text) - len(text.lstrip(" "))
    return indent <= 3 and text[indent:] == marker


def _newline(body: str) -> str:
    """The body's line ending: that of its first line break, ``\\n`` by default."""
    index = body.find("\n")
    return "\r\n" if index > 0 and body[index - 1] == "\r" else "\n"


class _Fences:
    """Track whether successive lines are inside a fenced code block.

    As in CommonMark, a fence is a run of three or more backticks or tildes
    (a backtick fence's info string has no backtick) indented at most three
    spaces, and only such a run of the same character, at least as long as
    the opener's, with nothing but whitespace after it, closes the block. A
    shorter run, or one with an info string, is content of the block (a
    nested example). A run indented four spaces or more is indented code, not
    a fence, the same limit :func:`_is_marker` applies to the markers.
    """

    def __init__(self) -> None:
        self._open: tuple[str, int] | None = None

    def step(self, content: str) -> bool:
        """Advance over *content*; True when the line is a fence or fenced."""
        stripped = content.lstrip(" ")
        char = stripped[:1]
        fence_char = char in ("`", "~") and len(content) - len(stripped) <= 3
        run = len(stripped) - len(stripped.lstrip(char)) if fence_char else 0
        rest = stripped[run:]
        if self._open is None:
            if run >= 3 and not (char == "`" and "`" in rest):
                self._open = (char, run)
                return True
            return False
        open_char, open_run = self._open
        if char == open_char and run >= open_run and not rest.strip():
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
        if _is_marker(content, BEGIN):
            begins.append(index)
        elif _is_marker(content, END):
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


def _path_candidates(text: str, repo: str, home_repo: str | None) -> list[str]:
    """The paths a link *text* may name in *repo* (see :meth:`DocLink.text`)."""
    prefixed = text[len(repo) + 1 :] if text.startswith(f"{repo}/") else None
    if home_repo is None:
        return [text] if prefixed is None else [text, prefixed]
    if _same_repo(repo, home_repo):
        return [text]
    return [] if prefixed is None else [prefixed]


def _parse_link(value: str, home_repo: str | None) -> DocLink:
    """Parse a rendered link back to its :class:`DocLink`.

    The text names the path (``<repo>/`` first for another repo), which
    splits the URL's ``<branch>/<path>`` tail even for a branch with a slash
    in it. With *home_repo* the text's form is known, so the split is exact.
    Without it, a link whose text fits both forms (another repo's link on a
    branch ending in ``/<repo>``, or a home path starting with the home
    repo's name) is refused as ambiguous rather than guessed.
    """
    match = _LINK_RE.match(value)
    if match is None:
        msg = f"Documents entry is neither a note nor a link: {value!r}"
        raise EpicDocsFormatError(msg)
    text, owner, repo, tail = match.groups()
    splits: list[tuple[str, str]] = []
    for path in _path_candidates(text, repo, home_repo):
        suffix = f"/{quote(path)}"
        if tail.endswith(suffix) and len(tail) > len(suffix):
            splits.append((path, tail[: -len(suffix)]))
    if not splits:
        msg = f"Documents link text does not match its URL: {value!r}"
        raise EpicDocsFormatError(msg)
    if len(splits) > 1:
        msg = f"Documents link is ambiguous without the epic's home repo: {value!r}"
        raise EpicDocsFormatError(msg)
    path, branch = splits[0]
    try:
        return DocLink(owner, repo, path, branch)
    except ValueError as exc:
        raise EpicDocsFormatError(f"bad Documents link {value!r}: {exc}") from exc


def parse(body: str, home_repo: str | None = None) -> dict[str, Entry] | None:
    """The entries of *body*'s Documents section, or None when it has none.

    *home_repo* is the epic's home repo name (see :meth:`DocLink.text`);
    given, every link parses exactly as :func:`render` wrote it. Raises
    :class:`EpicDocsFormatError` when the section is malformed: half a
    section, a missing heading, a missing, extra or out-of-order entry, or an
    entry that is neither a link nor one of the five notes.
    """
    if home_repo is not None and "/" in home_repo:
        msg = f"home_repo is a repo name, not owner/name: {home_repo!r}"
        raise ValueError(msg)
    lines = _split(body)
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
        entries[doc] = value if value in NOTES else _parse_link(value, home_repo)
    return entries


# --- legacy lines and upsert --------------------------------------------------

_LEGACY_HEADER = "Documents:"
_LEGACY_BULLET_RE = re.compile(r"^- (Spec|Plan|Retrospective):")
_NOTE_ALTERNATION = "|".join(re.escape(note) for note in sorted(NOTES, key=len, reverse=True))
_LEGACY_LINE_RE = re.compile(
    r"^(Spec|Plan|Retrospective): *"
    r"(?:`[^`]+`|\[[^\]]*\]\([^)\s]*\)|[\w.-]+(?:/[\w.-]+)*/[\w.-]*\w\.md|"
    rf"{_NOTE_ALTERNATION})"
    r"(?: *\(.*\))?\.?\s*$"
)
"""A standalone document line: its whole value is one backticked path, one
bare ``.md`` path with a directory, one markdown link or one note, with an
optional bracketed remark. A prose line
that merely starts with the label, such as ``Plan: migrate the repos first.``
or ``Plan: `vrg-foo` ships first.``, is kept."""
_CONTINUATION_RE = re.compile(r"^\(.*\)\.?$")


def _legacy_group(contents: list[str], fenced: list[bool], start: int) -> int:
    """The end (exclusive) of a legacy group starting at *start*, or *start* for none.

    A group is a ``Documents:`` line with the Spec/Plan/Retrospective bullets
    after it (blank lines before and between them allowed; the group ends at
    the last bullet), or a standalone ``Spec:``, ``Plan:`` or
    ``Retrospective:`` document line with an optional following line that is
    only a bracketed note. A bullet keeps the indented lines directly after
    it, so a bullet wrapped onto continuation lines goes as one entry.
    """
    line = contents[start]
    if line.strip() == _LEGACY_HEADER:
        end = start
        index = start + 1
        while index < len(contents) and not fenced[index]:
            text = contents[index]
            continuation = end == index > start and text.strip() and text[0] in " \t"
            if continuation or _LEGACY_BULLET_RE.match(text):
                end = index + 1
            elif text.strip():
                break
            index += 1
        return end
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
    and standalone ``Spec:``/``Plan:``/``Retrospective:`` lines whose value
    is a path, a link or a note, with any bracketed-note line after them.
    Nothing inside a fenced code block or the marked section is touched.
    When a removed group stood alone between blank lines (or at the top of
    the body), one blank line after it goes too, so no double gap is left.
    Every other line keeps its exact bytes.
    """
    lines = _split(body)
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
    lines = _split(body)
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
    if "body" not in data:
        msg = f"unexpected API response for {endpoint}: no body field"
        raise ValueError(msg)
    body = data["body"]
    if body is None:
        return ""
    if not isinstance(body, str):
        msg = f"unexpected body in API response for {endpoint}: {type(body).__name__}"
        raise ValueError(msg)  # noqa: TRY004 - a malformed API answer, not a caller type error
    return body


def write_body(owner: str, repo: str, number: int, body: str) -> None:
    """Replace the body of issue *owner*/*repo*#*number* with *body*."""
    github.write_json("PATCH", _issue_endpoint(owner, repo, number), {"body": body})


_NOT_FOUND_RE = re.compile(r"\(HTTP 404\)$", re.MULTILINE)


def is_not_found(exc: github.GitHubAPIError) -> bool:
    """True when *exc* is gh reporting an HTTP 404 status.

    gh ends its error line with the status, ``gh: Not Found (HTTP 404)``, so
    only a stderr line ending in exactly ``(HTTP 404)`` counts; a ``404``
    anywhere else (a path such as ``epics/404-x``, a request id) does not.
    """
    return _NOT_FOUND_RE.search((exc.stderr or "").replace("\r", "")) is not None


def _contents_endpoint(owner: str, repo: str, path: str, branch: str) -> str:
    return f"repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(branch, safe='')}"


def file_exists(owner: str, repo: str, path: str, branch: str) -> bool:
    """True when *path* exists in *owner*/*repo* on *branch* (contents API).

    A 404 is ``False``; any other error propagates.
    """
    try:
        github.read_json("api", _contents_endpoint(owner, repo, path, branch))
    except github.GitHubAPIError as exc:
        if is_not_found(exc):
            return False
        raise
    return True


def list_dir(owner: str, repo: str, path: str, branch: str) -> dict[str, str] | None:
    """The entries of directory *path* in *owner*/*repo* on *branch*, name to type.

    The type is the contents API's (``file``, ``dir``, …). None when *path*
    does not exist (a 404); any other error propagates, and a path that is a
    file, not a directory, raises :class:`ValueError`.
    """
    endpoint = _contents_endpoint(owner, repo, path, branch)
    try:
        data = github.read_json("api", endpoint)
    except github.GitHubAPIError as exc:
        if is_not_found(exc):
            return None
        raise
    if not isinstance(data, list):
        msg = f"{owner}/{repo}:{path} is not a directory on {branch}"
        raise ValueError(msg)  # noqa: TRY004 - a malformed API answer, not a caller type error
    entries: dict[str, str] = {}
    for item in data:
        name = item.get("name") if isinstance(item, dict) else None
        kind = item.get("type") if isinstance(item, dict) else None
        if not isinstance(name, str) or not isinstance(kind, str):
            msg = f"unexpected entry in the contents API response for {endpoint}: {item!r}"
            raise ValueError(msg)
        entries[name] = kind
    return entries


# --- backfill: which epics, entry rules and discovery ---------------------------

SPEC_PLAN_CUTOFF = "2026-07-12"
"""Epics created before this date predate the spec and plan convention (§3)."""
RETRO_CUTOFF = "2026-07-27"
"""Epics created before this date predate the retrospective convention (§3)."""

_ARCHIVED_PREFIX = "[archived]"
_STANDING_TITLE_PREFIX = "Epic (standing):"
_RETIRED_STANDING_LABEL = "standing"
_ARCHIVE_DIR = "archive"
_FILES = {"spec": "spec.md", "plan": "plan.md", "retrospective": "retrospective.md"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def is_skipped(title: str, labels: Iterable[str]) -> bool:
    """True for an epic the Documents section does not apply to.

    A container epic (one :func:`epics.is_standing` recognises, one carrying
    the retired ``standing`` label, or one titled ``Epic (standing): …``) or
    an archived one (titled ``[archived] …``).
    """
    names = set(labels)
    return (
        epics.is_standing(names)
        or _RETIRED_STANDING_LABEL in names
        or title.startswith((_STANDING_TITLE_PREFIX, _ARCHIVED_PREFIX))
    )


def entry_for(
    doc: str, link: DocLink | None, *, state: str, state_reason: str | None, created_at: str
) -> Entry:
    """The entry for *doc* under the §3 rules: the first matching row decides.

    *link* is the discovered document, or None when it is missing. *state*
    and *state_reason* are GitHub's (``OPEN``/``CLOSED``, ``NOT_PLANNED`` and
    so on, in either case); *created_at* is the epic's ISO creation time, and
    only its date is compared with the cutoffs.
    """
    if doc not in DOCS:
        msg = f"not a document: {doc!r}"
        raise ValueError(msg)
    if link is not None:
        return link
    if state.upper() == "OPEN":
        return PENDING
    if state.upper() != "CLOSED":
        msg = f"unexpected issue state: {state!r}"
        raise ValueError(msg)
    if (state_reason or "").upper() == "NOT_PLANNED":
        return NOT_PLANNED
    if _DATE_RE.match(created_at) is None:
        msg = f"unexpected creation time: {created_at!r}"
        raise ValueError(msg)
    created = created_at[:10]
    if doc != "retrospective" and created < SPEC_PLAN_CUTOFF:
        return PREDATES_SPEC_PLAN
    if doc == "retrospective" and created < RETRO_CUTOFF:
        return PREDATES_RETRO
    return NOT_WRITTEN


class Ambiguous(Exception):  # noqa: N818 - the plan's name for "needs a decision"
    """Discovery found more than one candidate; the human decides with ``set``."""

    def __init__(self, what: str, candidates: Iterable[str]) -> None:
        self.what = what
        self.candidates = tuple(candidates)
        super().__init__(f"{len(self.candidates)} candidate {what}: {', '.join(self.candidates)}")


def epic_directories(owner: str, home_repo: str, branch: str) -> frozenset[str]:
    """The directory names under ``epics/`` in the home repo, minus ``archive``.

    Empty when the repo has no ``epics/`` directory. The archive directory is
    left out by name and never listed.
    """
    entries = list_dir(owner, home_repo, "epics", branch)
    if entries is None:
        return frozenset()
    return frozenset(
        name for name, kind in entries.items() if kind == "dir" and name != _ARCHIVE_DIR
    )


_EPIC_DIR_RE = re.compile(r"(?<![\w.-])epics/([\w.-]+)/")
_DOC_PATH_RE = re.compile(
    r"(?<![\w./-])(?:([\w.-]+)/)?(?:([\w.-]+)/)?"
    r"(docs/(?:specs|plans|superpowers/specs|superpowers/plans)/[\w./-]*?[\w-]\.md)"
    r"(?![\w/-]|\.\w)"
)
_PLAN_PREFIXES = ("docs/plans/", "docs/superpowers/plans/")


def _outside_section(body: str) -> str:
    """*body* without its marked Documents section, markers included.

    Discovery reads only what the epic's prose mentions: the section's own
    links name documents already decided, possibly another epic's.
    """
    lines = _split(body)
    region = _region(lines)
    if region is None:
        return body
    return "".join(lines[: region[0]] + lines[region[1] + 1 :])


def discover(
    owner: str,
    home_repo: str,
    number: int,
    body: str,
    branch: str,
    *,
    epic_dirs: Iterable[str] | None = None,
    branches: dict[str, str] | None = None,
    wanted: Iterable[str] = DOCS,
) -> dict[str, DocLink | None]:
    """The documents of epic *number*, each a :class:`DocLink` or None (§5.2).

    Looks, in order, for the directory ``epics/<number>-*/`` in the home repo
    on *branch*, an existing ``epics/<dir>/`` the *body* mentions, and
    existing ``docs/specs/``, ``docs/plans/``, ``docs/superpowers/specs/`` or
    ``docs/superpowers/plans/`` paths the body mentions, in the home repo or
    another repo of *owner*. In a directory, ``spec.md``, ``plan.md`` and
    ``retrospective.md`` are the documents. The body's marked Documents
    section is not read.

    *wanted* names the documents still needing a decision; only they are
    looked for. Raises :class:`Ambiguous` when more than one candidate
    directory holds a wanted document, or a wanted spec or plan has more
    than one candidate. A document not wanted is returned as None.

    *epic_dirs* is :func:`epic_directories`, looked up when not given;
    *branches* caches default branches by repo name across calls.
    """
    wanted = frozenset(wanted)
    if not wanted <= set(DOCS):
        msg = f"not documents: {', '.join(sorted(wanted - set(DOCS)))}"
        raise ValueError(msg)
    if not wanted:
        return dict.fromkeys(DOCS)
    body = _outside_section(body)
    dirs = frozenset(epic_dirs) if epic_dirs is not None else None
    if dirs is None:
        dirs = epic_directories(owner, home_repo, branch)
    found = sorted(name for name in dirs if name.startswith(f"{number}-"))
    if not found:
        found = sorted({name for name in _EPIC_DIR_RE.findall(body) if name in dirs})
    if found:
        return _found_directory(owner, home_repo, found, branch, wanted)
    cache = branches if branches is not None else {}
    cache.setdefault(home_repo, branch)
    return _mentioned_documents(owner, home_repo, body, cache, wanted)


def _found_directory(
    owner: str, repo: str, found: list[str], branch: str, wanted: frozenset[str]
) -> dict[str, DocLink | None]:
    """The wanted documents of the one directory in *found* that holds any."""
    holding: list[tuple[str, dict[str, DocLink | None]]] = []
    for name in found:
        documents = _directory_documents(owner, repo, f"epics/{name}", branch)
        if any(documents[doc] is not None for doc in wanted):
            holding.append((name, documents))
    if len(holding) > 1:
        raise Ambiguous("directories", (f"epics/{name}/" for name, _ in holding))
    if not holding:
        return dict.fromkeys(DOCS)
    documents = holding[0][1]
    return {doc: documents[doc] if doc in wanted else None for doc in DOCS}


def _directory_documents(
    owner: str, repo: str, directory: str, branch: str
) -> dict[str, DocLink | None]:
    entries = list_dir(owner, repo, directory, branch)
    if entries is None:
        msg = f"{owner}/{repo}:{directory} was listed but no longer exists on {branch}"
        raise ValueError(msg)
    return {
        doc: DocLink(owner, repo, f"{directory}/{name}", branch)
        if entries.get(name) == "file"
        else None
        for doc, name in _FILES.items()
    }


def _mentioned_documents(
    owner: str, home_repo: str, body: str, branches: dict[str, str], wanted: frozenset[str]
) -> dict[str, DocLink | None]:
    """The wanted spec and plan among the ``docs/…`` paths *body* mentions that exist."""
    candidates: dict[str, dict[tuple[str, str], DocLink]] = {"spec": {}, "plan": {}}
    for first, second, path in _DOC_PATH_RE.findall(body):
        if second:
            if first.lower() != owner.lower():
                continue  # another org's document: never linked
            repo = second
        else:
            repo = first or home_repo
        doc = "plan" if path.startswith(_PLAN_PREFIXES) else "spec"
        key = (repo.lower(), path)
        if doc not in wanted or key in candidates[doc]:
            continue
        branch = _repo_branch(owner, repo, branches)
        if branch is not None and file_exists(owner, repo, path, branch):
            candidates[doc][key] = DocLink(owner, repo, path, branch)
    result: dict[str, DocLink | None] = dict.fromkeys(DOCS)
    for doc, links in candidates.items():
        if len(links) > 1:
            raise Ambiguous(f"{doc}s", (f"{link.repo}/{link.path}" for link in links.values()))
        result[doc] = next(iter(links.values()), None)
    return result


def _repo_branch(owner: str, repo: str, branches: dict[str, str]) -> str | None:
    """*repo*'s default branch, cached in *branches*; None when the repo does not exist."""
    for name, branch in branches.items():
        if _same_repo(name, repo):
            return branch
    try:
        branch = default_branch(owner, repo)
    except github.GitHubAPIError as exc:
        if is_not_found(exc):
            return None
        raise
    branches[repo] = branch
    return branch


class EpicRecord(TypedDict):
    """One epic as :func:`list_epics` returns it, in ``gh issue list``'s field names.

    ``state`` is ``OPEN`` or ``CLOSED`` and ``stateReason`` upper case, as
    ``gh`` reports them. ``body`` is None for an ``[archived]`` epic, whose
    body is dropped by the API query and never read.
    """

    number: int
    title: str
    state: str
    stateReason: str | None  # noqa: N815 - gh's field name
    createdAt: str  # noqa: N815 - gh's field name
    labels: list[str]
    body: str | None


_LIST_JQ = (
    '[.[] | select(has("pull_request") | not) | {number, title, state, '
    "stateReason: .state_reason, createdAt: .created_at, labels: [.labels[].name], "
    f'body: (if (.title | startswith("{_ARCHIVED_PREFIX}")) then null else (.body // "") end)}}]'
)


def list_epics(home: str, *, apply_skips: bool = True) -> list[EpicRecord]:
    """Every ``epic``-labelled issue in *home* (``owner/repo``), open and closed.

    Read through the paginated issues API, so no epic is lost past a page
    limit. With *apply_skips*, the epics :func:`is_skipped` names are left
    out. Raises :class:`ValueError` for an answer of an unexpected shape.
    """
    raw = github.read_json_paginated(f"repos/{home}/issues?labels=epic&state=all", jq=_LIST_JQ)
    records = [_epic_record(item, home) for item in raw]
    if apply_skips:
        records = [r for r in records if not is_skipped(r["title"], r["labels"])]
    return records


def _epic_record(item: object, home: str) -> EpicRecord:
    if not isinstance(item, dict):
        msg = f"unexpected epic in the {home} issue listing: {item!r}"
        raise ValueError(msg)  # noqa: TRY004 - a malformed API answer, not a caller type error
    fields = cast("dict[str, object]", item)
    number, title, state = fields.get("number"), fields.get("title"), fields.get("state")
    reason, created = fields.get("stateReason"), fields.get("createdAt")
    labels, body = fields.get("labels"), fields.get("body")
    if (
        not isinstance(number, int)
        or not isinstance(title, str)
        or not isinstance(state, str)
        or not (reason is None or isinstance(reason, str))
        or not isinstance(created, str)
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or not (body is None or isinstance(body, str))
    ):
        msg = f"unexpected epic in the {home} issue listing: {item!r}"
        raise ValueError(msg)
    return EpicRecord(
        number=number,
        title=title,
        state=state.upper(),
        stateReason=reason.upper() if reason is not None else None,
        createdAt=created,
        labels=cast("list[str]", labels),
        body=body,
    )
