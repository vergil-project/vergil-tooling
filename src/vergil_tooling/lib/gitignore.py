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
