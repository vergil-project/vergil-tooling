"""Classify a repository's test-suite layout for import-mode safety.

pytest's ``--import-mode=importlib`` is faster than the default ``prepend`` mode
but is not universally safe: an *unpackaged* test tree (no ``tests/__init__.py``)
with duplicate test-file basenames collides under importlib, because two files
named ``test_dup.py`` map to the same top-level module. ``classify_test_layout``
reports the shape so Task 10 can gate the flag per repo and the Phase-0 fleet
survey can flag the unsafe repos (epic vergil-project/.github#333, Task 3).

This module is an **import-graph leaf**: it uses only the standard library and
the filesystem and imports nothing else from ``vergil_tooling``. That matches the
``languages.py`` leaf convention so ``languages.py`` (Task 10) can consume this
verdict without an import cycle — ``lib/`` must never import from ``scripts/``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Directory names, in priority order, under which a repo keeps its test suite.
_TEST_DIRS = ("tests", "test")

#: Glob patterns that identify a pytest test module by filename. Mirrors
#: pytest's default ``python_files`` discovery patterns.
_TEST_GLOBS = ("test_*.py", "*_test.py")


@dataclass(frozen=True)
class LayoutVerdict:
    """The classified shape of a repo's test tree.

    ``packaged`` — the top-level test directory has an ``__init__.py`` (tests
    are importable as a package, giving each module a unique dotted path).
    ``duplicate_basenames`` — sorted test-file basenames that appear more than
    once across the tree.
    ``importlib_safe`` — whether ``--import-mode=importlib`` is safe: true when
    the tree is packaged *or* has no duplicate basenames.
    """

    packaged: bool
    duplicate_basenames: list[str]
    importlib_safe: bool


def _find_tests_dir(repo: Path) -> Path | None:
    for name in _TEST_DIRS:
        candidate = repo / name
        if candidate.is_dir():
            return candidate
    return None


def classify_test_layout(repo: Path) -> LayoutVerdict:
    """Classify the test layout of ``repo`` for import-mode safety."""
    tests_dir = _find_tests_dir(repo)
    if tests_dir is None:
        return LayoutVerdict(packaged=False, duplicate_basenames=[], importlib_safe=True)

    packaged = (tests_dir / "__init__.py").is_file()

    paths: set[Path] = set()
    for pattern in _TEST_GLOBS:
        paths.update(tests_dir.rglob(pattern))

    counts = Counter(path.name for path in paths)
    duplicate_basenames = sorted(name for name, n in counts.items() if n > 1)

    importlib_safe = packaged or not duplicate_basenames
    return LayoutVerdict(
        packaged=packaged,
        duplicate_basenames=duplicate_basenames,
        importlib_safe=importlib_safe,
    )
