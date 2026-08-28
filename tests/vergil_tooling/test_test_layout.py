"""Tests for the test-layout classifier (import-mode safety survey).

``classify_test_layout`` inspects a repo's test tree and reports whether a
switch to pytest's ``--import-mode=importlib`` is safe. The verdict is consumed
by Task 10 (the import-mode gate) and the Phase-0 fleet survey (epic
vergil-project/.github#333, Task 3).

The safety rule encoded here: importlib mode is safe when the tests are fully
packaged (``tests/__init__.py`` — every module has a unique dotted path) *or*
there are no duplicate test-file basenames to collide. An unpackaged tree with
duplicate basenames is the one unsafe shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.test_layout import classify_test_layout

if TYPE_CHECKING:
    from pathlib import Path


def test_packaged_unique_basenames_is_importlib_safe(tmp_path: Path) -> None:
    t = tmp_path / "tests"
    t.mkdir()
    (t / "__init__.py").touch()
    (t / "test_a.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.packaged and v.importlib_safe and v.duplicate_basenames == []


def test_duplicate_basenames_not_importlib_safe(tmp_path: Path) -> None:
    for sub in ("x", "y"):
        d = tmp_path / "tests" / sub
        d.mkdir(parents=True)
        (d / "test_dup.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.importlib_safe is False
    assert v.duplicate_basenames == ["test_dup.py"]
    assert v.packaged is False


def test_unpackaged_unique_basenames_is_importlib_safe(tmp_path: Path) -> None:
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_a.py").touch()
    (t / "test_b.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.packaged is False
    assert v.importlib_safe is True
    assert v.duplicate_basenames == []


def test_packaged_duplicate_basenames_still_safe(tmp_path: Path) -> None:
    # Packaged trees disambiguate duplicates by dotted path, so importlib mode
    # stays safe even with repeated basenames.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").touch()
    for sub in ("x", "y"):
        d = tmp_path / "tests" / sub
        d.mkdir(parents=True)
        (d / "test_dup.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.packaged is True
    assert v.duplicate_basenames == ["test_dup.py"]
    assert v.importlib_safe is True


def test_suffix_style_test_files_are_collected(tmp_path: Path) -> None:
    t = tmp_path / "tests"
    t.mkdir()
    (t / "a_test.py").touch()
    (t / "b_test.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.duplicate_basenames == []
    assert v.importlib_safe is True


def test_singular_test_dir_is_recognized(tmp_path: Path) -> None:
    # Falls back to the singular ``test/`` directory when ``tests/`` is absent.
    d = tmp_path / "test"
    d.mkdir()
    (d / "test_a.py").touch()
    v = classify_test_layout(tmp_path)
    assert v.importlib_safe is True
    assert v.duplicate_basenames == []


def test_no_tests_dir_is_vacuously_safe(tmp_path: Path) -> None:
    v = classify_test_layout(tmp_path)
    assert v.packaged is False
    assert v.duplicate_basenames == []
    assert v.importlib_safe is True
