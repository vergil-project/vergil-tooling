"""Composable .gitignore fragment data.

The monolithic ``data/gitignore.baseline`` is split into a language-agnostic
``data/gitignore/base`` fragment plus one per-language fragment under
``data/gitignore/<language>``. Each fragment file holds one ignore pattern per
line with no comments or blank lines. ``compose()`` stitches ``base`` together
with a language fragment into an order-stable, de-duplicated pattern list; the
split is lossless — the union of ``base`` and every fragment reconstitutes the
current baseline's pattern set (epic vergil-project/.github#325).
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass

#: Prefix that opens a vergil-managed fence. The full begin marker embeds a
#: human-readable descriptor and a trailing ``>>>``; only the prefix is stable
#: enough to detect the marker.
MANAGED_BEGIN_PREFIX = "# >>> vergil-managed:"

#: Exact line that closes a vergil-managed fence.
MANAGED_END = "# <<< vergil-managed <<<"

#: The known baseline *comment* scaffolding a bootstrap must strip alongside the
#: managed patterns. Frozen verbatim from ``data/gitignore.baseline`` (every
#: comment line: the "single source of truth" preamble and every ``# section``
#: header) so the set survives the monolith's eventual deletion, plus the header
#: an earlier sweep appended to some repos (vergil-project/.github#322). A repo
#: whose ``.gitignore`` *was* repo-init's fully-commented baseline would
#: otherwise convert to a fence trailed by a now-false preamble and a stack of
#: emptied section headers (issue #2939). Matched on rstripped exact equality;
#: genuine repo-local comments are not in this set and survive.
_MANAGED_COMMENTS: frozenset[str] = frozenset(
    {
        "# Vergil baseline .gitignore — single source of truth (epic vergil-project/.github#311).",
        "# Every managed repo's .gitignore must be a SUPERSET of the non-comment lines",
        "# below (verbatim). Repos may add their own local entries. Do not edit a",
        "# consuming repo to diverge — change this file in vergil-tooling and release.",
        "# Editors",
        "# OS",
        "# Environment / secrets",
        "# Logs",
        "# Vergil internals",
        "# Build / packaging output",
        "# C++ TEST runs its ASan/UBSan build out-of-tree in a *separate* dir so the",
        "# sanitizer instrumentation never shares objects with the coverage build; it is",
        "# generated output like build/ and must be ignored too, or a cpp dev build",
        "# leaves the worktree dirty and un-sweepable by finalize cleanup (#2906).",
        "# Python bytecode / caches",
        "# Coverage / validation / CI-gate evidence",
        "# Docs (mkdocs build output; mkdocs.yml lives at docs/site/)",
        "# Node / TypeScript",
        "# Go (test/coverage output; binaries usually have no extension)",
        "# Ruby",
        "# C/C++ object & archive output",
        "# C/C++ Conan 2 generator output (written into the source tree by `conan install`).",
        "# The -debug-armv8 style suffix carries build type + arch, so these need wildcards",
        "# to match Release builds and x86_64 hosts too. conan.lock is intentionally NOT",
        "# ignored — it is committed for reproducible resolution, like uv.lock.",
        "# Conan CMakeDeps per-package files. Package-named (e.g. GTest → FindGTest.cmake,",
        "# GTestConfig.cmake, GTest-Target-debug.cmake), so matched by shape rather than an",
        "# enumerable list. Conan writes these loose at the source root; a repo's own CMake",
        "# modules belong under cmake/ or are generated into build/, never loose at the",
        "# root, so these globs do not collide with tracked project files in practice.",
        "# --- Vergil baseline sync (vergil-project/.github#322): "
        "lines added to match gitignore.baseline ---",
    }
)

FRAGMENT_LANGS: tuple[str, ...] = (
    "python",
    "cpp",
    "go",
    "ruby",
    "rust",
    "java",
    "typescript",
)


def _load(name: str) -> list[str]:
    """Read a fragment file's non-blank, right-stripped lines."""
    text = (
        importlib.resources.files("vergil_tooling.data")
        .joinpath("gitignore", name)
        .read_text(encoding="utf-8")
    )
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def load_base() -> list[str]:
    """Return the language-agnostic base fragment's pattern lines."""
    return _load("base")


def load_fragment(lang: str | None) -> list[str]:
    """Return a language fragment's pattern lines.

    An unknown, ``None``, or empty ``lang`` yields an empty list — there is no
    fragment to load, which is not an error (a repo may have no language-specific
    ignores).
    """
    if not lang or lang not in FRAGMENT_LANGS:
        return []
    return _load(lang)


def compose(lang: str | None) -> list[str]:
    """Compose base + a language fragment into an ordered, de-duplicated list.

    Base patterns come first, then the fragment's, preserving first-seen order
    and dropping duplicates. With no matching fragment the result is base-only.
    """
    seen: set[str] = set()
    composed: list[str] = []
    for pattern in load_base() + load_fragment(lang):
        if pattern not in seen:
            seen.add(pattern)
            composed.append(pattern)
    return composed


def managed_vocabulary() -> set[str]:
    """Return every pattern the fleet manages: base ∪ all language fragments."""
    vocab: set[str] = set(load_base())
    for lang in FRAGMENT_LANGS:
        vocab.update(load_fragment(lang))
    return vocab


def render_block(lang: str | None) -> str:
    """Render the full fenced managed block for ``lang``, with a trailing newline.

    The block is a begin marker, the ``compose(lang)`` pattern lines (one per
    line), and the end marker. The begin marker's descriptor is ``base + <lang>``
    when the language contributes a fragment, or ``base`` for a base-only block
    (``None``, empty, unknown, or a known-but-empty language such as ``rust``).
    """
    descriptor = f"base + {lang}" if load_fragment(lang) else "base"
    begin = f"{MANAGED_BEGIN_PREFIX} {descriptor} (managed by vrg-gitignore-sync; do not edit) >>>"
    return "\n".join([begin, *compose(lang), MANAGED_END]) + "\n"


def parse(text: str) -> tuple[list[str], str | None]:
    """Split ``text`` into repo-local lines and the managed fence (if any).

    Returns ``(repo_local_lines, fence)`` where ``fence`` is the exact text from
    the begin-marker line through the ``MANAGED_END`` line inclusive (with a
    trailing newline), and ``repo_local_lines`` is every line outside that fence
    in order. With no begin marker, or a begin marker that never closes
    (malformed), the fence is ``None`` and every line is repo-local.
    """
    lines = text.splitlines()
    begin_idx = next(
        (i for i, line in enumerate(lines) if line.startswith(MANAGED_BEGIN_PREFIX)),
        None,
    )
    if begin_idx is None:
        return lines, None
    end_idx = next(
        (i for i in range(begin_idx + 1, len(lines)) if lines[i] == MANAGED_END),
        None,
    )
    if end_idx is None:
        return lines, None
    fence = "\n".join(lines[begin_idx : end_idx + 1]) + "\n"
    repo_local = lines[:begin_idx] + lines[end_idx + 1 :]
    return repo_local, fence


@dataclass
class Compliance:
    """The result of a managed-block compliance check."""

    compliant: bool
    reasons: list[str]


def check(text: str, lang: str | None) -> Compliance:
    """Check ``text`` against the managed block expected for ``lang``.

    Compliant iff a well-formed fence is present, that fence equals
    ``render_block(lang)`` (modulo a single trailing newline), and no
    managed-vocabulary pattern appears among the repo-local (outside-fence)
    lines. Each failed condition contributes a human-readable reason.
    """
    repo_local, fence = parse(text)
    reasons: list[str] = []

    if fence is None:
        reasons.append("no well-formed vergil-managed fence found")
    elif fence.rstrip("\n") != render_block(lang).rstrip("\n"):
        reasons.append("the vergil-managed fence does not match the expected rendered block")

    vocab = managed_vocabulary()
    stray = [line for line in repo_local if line.rstrip() in vocab]
    if stray:
        reasons.append("managed patterns appear outside the fence: " + ", ".join(stray))

    return Compliance(compliant=not reasons, reasons=reasons)


@dataclass
class SyncResult:
    """The result of syncing a ``.gitignore``'s managed block.

    ``text`` is the new file content, ``changed`` is whether it differs from the
    input, and ``removed`` lists the managed-vocabulary lines that a bootstrap
    dropped from a loose monolith (always empty for an update).
    """

    text: str
    changed: bool
    removed: list[str]


def _normalize_blanks(lines: list[str]) -> list[str]:
    """Strip leading/trailing blank lines and collapse consecutive blank runs.

    Blank means empty after ``strip``. Interior runs of blank lines collapse to
    a single blank; leading and trailing blanks are removed entirely.
    """
    normalized: list[str] = []
    for line in lines:
        if not line.strip():
            if normalized and normalized[-1].strip():
                normalized.append("")
            continue
        normalized.append(line)
    while normalized and not normalized[-1].strip():
        normalized.pop()
    return normalized


def _assemble(fence_block: str, repo_local: list[str]) -> str:
    """Join a rendered fence block with its repo-local section, fence first.

    With no repo-local lines the result is the bare fence block; otherwise the
    fence is followed by the repo-local lines, each on its own line with a
    trailing newline.
    """
    if not repo_local:
        return fence_block
    return fence_block + "\n".join(repo_local) + "\n"


def sync(text: str, lang: str | None) -> SyncResult:
    """Bring ``text`` into compliance with the managed block for ``lang``.

    Two modes, selected by whether ``text`` already carries a well-formed fence:

    * **Bootstrap** (no fence): the loose monolith is replaced by a fresh
      ``render_block(lang)``, and the repo-local section keeps only lines that
      are *not* in ``managed_vocabulary()`` and *not* in ``_MANAGED_COMMENTS``.
      This deliberately drops the language's own managed lines, any
      foreign-language managed lines, and the known baseline comment scaffolding
      (the "single source of truth" preamble and the emptied section headers),
      then normalizes blank lines, leaving only genuinely repo-specific content
      below a blank separator (the section is omitted entirely when nothing
      genuine remains). Every dropped managed *pattern* is reported in
      ``removed``; stripped comments are not.
    * **Update** (fence present): the fence is rewritten to the canonical
      ``render_block(lang)`` and the parsed repo-local section is trusted as-is
      (never re-filtered); ``removed`` is empty.

    ``changed`` reports whether the new text differs from the input. ``sync`` is
    idempotent: syncing its own output is always a no-op.
    """
    repo_local, fence = parse(text)
    fence_block = render_block(lang)

    if fence is None:
        vocab = managed_vocabulary()
        removed = [line for line in repo_local if line.rstrip() in vocab]
        kept = _normalize_blanks(
            [
                line
                for line in repo_local
                if line.rstrip() not in vocab and line.rstrip() not in _MANAGED_COMMENTS
            ]
        )
        section = ["", *kept] if kept else []
        new_text = _assemble(fence_block, section)
    else:
        removed = []
        new_text = _assemble(fence_block, repo_local)

    return SyncResult(text=new_text, changed=new_text != text, removed=removed)
