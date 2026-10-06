"""Tests for vergil_tooling.lib.package.index.config (``packages.toml``, spec §7.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import config as index_config
from vergil_tooling.lib.package.index.config import IndexConfig

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "packages.toml"
    p.write_text(body)
    return p


def test_load_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, 'vendor = "vergil"\nproducts = ["vergil-project/vergil-tooling"]\n')
    assert index_config.load(p) == IndexConfig(
        "vergil", ["vergil-project/vergil-tooling"], keep=3, lines=2
    )


def test_load_explicit_retention(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        'vendor = "vergil"\nproducts = ["a/b", "c/d"]\n[retention]\nkeep = 5\nlines = 1\n',
    )
    assert index_config.load(p) == IndexConfig("vergil", ["a/b", "c/d"], keep=5, lines=1)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('products = ["a/b"]\n', r"packages\.toml: vendor is required"),
        ('vendor = ""\nproducts = ["a/b"]\n', r"packages\.toml: vendor is required"),
        ('vendor = 3\nproducts = ["a/b"]\n', r"packages\.toml: vendor is required"),
        ('vendor = "v"\nproducts = []\n', r"products must be a non-empty list"),
        ('vendor = "v"\n', r"products must be a non-empty list"),
        ('vendor = "v"\nproducts = "a/b"\n', r"products must be a non-empty list"),
        (
            'vendor = "v"\nproducts = ["a/b", 3]\n',
            r"products entry 3 is not an <owner>/<repo> string",
        ),
        (
            'vendor = "v"\nproducts = ["nope"]\n',
            r"products entry 'nope' is not an <owner>/<repo> string",
        ),
        (
            'vendor = "v"\nproducts = ["a/b", "a/b"]\n',
            r"products lists 'a/b' more than once",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\n[retention]\nkeep = 0\n',
            r"retention\.keep must be an integer >= 1",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\n[retention]\nkeep = true\n',
            r"retention\.keep must be an integer >= 1",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\n[retention]\nlines = "2"\n',
            r"retention\.lines must be an integer >= 1",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\nretention = 3\n',
            r"packages\.toml: \[retention\] must be a table",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\n[retention]\nkept = 3\n',
            r"packages\.toml: unknown key\(s\) in \[retention\]: kept",
        ),
        (
            'vendor = "v"\nproducts = ["a/b"]\nextra = 1\n',
            r"packages\.toml: unknown key\(s\): extra",
        ),
        ('vendor = "v"\nproducts = [\n', r"packages\.toml is not valid TOML"),
    ],
)
def test_load_errors(tmp_path: Path, body: str, match: str) -> None:
    p = _write(tmp_path, body)
    with pytest.raises(PackageError, match=match):
        index_config.load(p)


def test_missing_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"packages\.toml not found"):
        index_config.load(tmp_path / "packages.toml")
