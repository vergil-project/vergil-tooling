"""The ``upstream-version`` package version sorts correctly under dpkg and rpm.

Epic vergil-project/.github#472, spec §5.5: a newer CPython patch sorts higher,
at the same inputs a newer repo release sorts higher, and a new-format version
sorts above the old ``<cpython>+<pbs_tag>`` format already in the index. Each
version is built by :func:`naming.package_version` and compared by the real
tool with a ``-1`` revision appended, as the index serves it.

A missing tool **fails** these tests; it never skips them. ``rpm`` reaches the
dev images through vergil-project/vergil-containers#692.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from vergil_tooling.lib.config import PackageConfig
from vergil_tooling.lib.package import naming


def _version(upstream: str, release: str) -> str:
    pkg = PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="s",
        smoke="true",
        name="vergil-python3.15",
        upstream_version=upstream,
    )
    return naming.package_version(pkg, release)


# (newer, older): each pair must order newer > older.
_PAIRS = [
    # A new-format version sorts above the old format at the same inputs.
    (_version("3.15.1+20261021", "1.0.5"), "3.15.1+20261021"),
    # At the same inputs, a newer repo release sorts higher (numerically).
    (_version("3.15.1+20261021", "1.0.10"), _version("3.15.1+20261021", "1.0.9")),
    # A newer CPython patch sorts higher, whatever the repo release.
    (_version("3.15.2+20261101", "1.0.5"), _version("3.15.1+20261021", "1.0.6")),
]


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.fail(f"{tool} not found in the dev image; see vergil-project/vergil-containers#692")
    return path


def test_pairs_are_the_spec_examples() -> None:
    assert _PAIRS[0] == ("3.15.1+20261021.1.0.5", "3.15.1+20261021")


def test_missing_tool_fails_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _tool: None)
    with pytest.raises(pytest.fail.Exception, match=r"^rpm not found in the dev image; see "):
        _require("rpm")


def test_version_order_dpkg() -> None:
    dpkg = _require("dpkg")
    for newer, older in _PAIRS:
        a, b = f"{newer}-1", f"{older}-1"
        gt = subprocess.run([dpkg, "--compare-versions", a, "gt", b], check=False)
        assert gt.returncode == 0, f"dpkg: expected {a} > {b}"
        lt = subprocess.run([dpkg, "--compare-versions", b, "gt", a], check=False)
        assert lt.returncode != 0, f"dpkg: expected not {b} > {a}"


def test_version_order_rpm() -> None:
    rpm = _require("rpm")
    for newer, older in _PAIRS:
        a, b = f"{newer}-1", f"{older}-1"
        for x, y, want in ((a, b, "1"), (b, a, "-1")):
            out = subprocess.run(
                [rpm, "--eval", f'%{{lua: print(rpm.vercmp("{x}", "{y}"))}}'],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert out == want, f"rpm.vercmp({x!r}, {y!r}) = {out!r}, expected {want}"
