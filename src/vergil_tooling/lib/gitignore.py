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
