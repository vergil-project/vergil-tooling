"""Tests for vergil_tooling.lib.package.elf (the glibc floor guard, spec §6.1 step 4)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, BinaryIO

import pytest
from elftools.elf.gnuversions import GNUVerNeedSection

from vergil_tooling.lib.package import PackageError, elf

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeVerNeed(GNUVerNeedSection):
    """A version-needed section yielding fixed needs (no real ELF needed).

    ``needs`` maps each needed library (the verneed ``vn_file``) to its version names.
    """

    def __init__(self, needs: dict[str, list[str]]) -> None:
        self._needs = needs

    def iter_versions(self) -> Iterator[Any]:
        for lib, names in self._needs.items():
            yield (
                SimpleNamespace(name=lib),
                iter(SimpleNamespace(name=n) for n in names),
            )


def _fake_elffile(sections: list[object]) -> Any:
    def make(_fh: BinaryIO) -> SimpleNamespace:
        return SimpleNamespace(iter_sections=lambda: iter(sections))

    return make


def test_non_elf_has_no_requirements(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello")
    assert elf.glibc_requirements(f) == set()


def test_empty_file_has_no_requirements(tmp_path: Path) -> None:
    f = tmp_path / "empty"
    f.write_bytes(b"")
    assert elf.glibc_requirements(f) == set()


def test_real_interpreter_requires_some_glibc() -> None:
    # Validation runs in the Linux dev container, so the interpreter is an ELF binary.
    reqs = elf.glibc_requirements(Path(os.path.realpath(sys.executable)))
    assert reqs
    assert all(r >= (2, 2) for r in reqs)


def test_only_glibc_versions_are_collected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "lib.so"
    f.write_bytes(b"\x7fELF")
    names = ["GLIBC_2.2.5", "GLIBC_2.34", "GLIBC_PRIVATE", "ZLIB_1.2.0", "XGLIBC_9.9"]
    sections = [
        object(),
        _FakeVerNeed({"libc.so.6": names}),
    ]
    monkeypatch.setattr(elf, "ELFFile", _fake_elffile(sections))
    assert elf.glibc_requirements(f) == {(2, 2), (2, 34)}


def test_non_glibc_library_glibc_names_are_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The arm64 python interpreter's needs: libgcc_s.so.1 names __register_frame
    # and __deregister_frame GLIBC_2.0, which is not a need on glibc.
    f = tmp_path / "python3.15"
    f.write_bytes(b"\x7fELF")
    sections: list[object] = [
        _FakeVerNeed(
            {
                "libgcc_s.so.1": ["GLIBC_2.0", "GCC_3.0"],
                "libm.so.6": ["GLIBC_2.17", "GLIBC_2.29"],
                "libc.so.6": ["GLIBC_2.17", "GLIBC_2.34"],
                "ld-linux-aarch64.so.1": ["GLIBC_2.17"],
                "libz.so.1": ["GLIBC_9.99"],
            }
        ),
    ]
    monkeypatch.setattr(elf, "ELFFile", _fake_elffile(sections))
    assert elf.glibc_requirements(f) == {(2, 17), (2, 29), (2, 34)}


def test_only_non_glibc_needs_give_no_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "ext.so"
    f.write_bytes(b"\x7fELF")
    sections: list[object] = [_FakeVerNeed({"libgcc_s.so.1": ["GLIBC_2.0"]})]
    monkeypatch.setattr(elf, "ELFFile", _fake_elffile(sections))
    assert elf.glibc_requirements(f) == set()
    # So the floor guard has nothing to hold against the file.
    elf.check_glibc_floor(tmp_path, (2, 17))


@pytest.mark.parametrize("loader", ["ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1"])
def test_both_loaders_are_glibc(loader: str) -> None:
    assert loader in elf.GLIBC_SONAMES


def test_corrupt_elf_is_fatal(tmp_path: Path) -> None:
    f = tmp_path / "bad.so"
    f.write_bytes(b"\x7fELF" + b"\x00" * 8)
    with pytest.raises(PackageError, match=r"bad\.so: unreadable ELF"):
        elf.glibc_requirements(f)


def test_truncated_elf_is_fatal(tmp_path: Path) -> None:
    f = tmp_path / "trunc.so"
    f.write_bytes(Path(os.path.realpath(sys.executable)).read_bytes()[:200])
    with pytest.raises(PackageError, match=r"trunc\.so: unreadable ELF"):
        elf.glibc_requirements(f)


def test_floor_violation_lists_every_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sub").mkdir()
    for n in ("a.so", "sub/b.so", "ok.so"):
        (tmp_path / n).write_bytes(b"\x7fELF")
    fake = {"a.so": {(2, 38)}, "b.so": {(2, 35), (2, 17)}, "ok.so": {(2, 17)}}
    monkeypatch.setattr(elf, "glibc_requirements", lambda p: fake[p.name])
    with pytest.raises(
        PackageError, match=r"(?s)GLIBC floor 2\.34.*a\.so needs 2\.38.*sub/b\.so needs 2\.35"
    ) as exc:
        elf.check_glibc_floor(tmp_path, (2, 34))
    assert "ok.so" not in str(exc.value)


def test_floor_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ok.so").write_bytes(b"\x7fELF")
    (tmp_path / "plain").write_text("x")
    monkeypatch.setattr(elf, "glibc_requirements", lambda p: {(2, 17)} if p.suffix else set())
    elf.check_glibc_floor(tmp_path, (2, 34))


def test_floor_equal_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "eq.so").write_bytes(b"\x7fELF")
    monkeypatch.setattr(elf, "glibc_requirements", lambda p: {(2, 34)})
    elf.check_glibc_floor(tmp_path, (2, 34))


def test_symlinks_are_not_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "real.so").write_bytes(b"\x7fELF")
    (tmp_path / "link.so").symlink_to("real.so")
    (tmp_path / "dangling.so").symlink_to("missing.so")
    scanned: list[str] = []

    def fake(p: Path) -> set[tuple[int, int]]:
        scanned.append(p.name)
        return set()

    monkeypatch.setattr(elf, "glibc_requirements", fake)
    elf.check_glibc_floor(tmp_path, (2, 34))
    assert scanned == ["real.so"]
