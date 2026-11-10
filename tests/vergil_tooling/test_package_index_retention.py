"""Tests for vergil_tooling.lib.package.index.retention (spec §7.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import retention
from vergil_tooling.lib.package.index.collect import Artifact


def _a(
    product: str,
    tag: str,
    name: str,
    version: str,
    arch: str = "amd64",
    fmt: str = "deb",
    depends: tuple[str, ...] = (),
    sha: str = "s",
    release: str = "1",
) -> Artifact:
    return Artifact(
        product,
        tag,
        Path(f"/x/{name}_{version}_{arch}.{fmt}"),
        fmt,
        name,
        version,
        release,
        arch,
        depends,
        sha + version,
    )


def test_full_version() -> None:
    assert _a("o/t", "v1.0.0", "t", "1.0.0", release="1~noble").full_version == "1.0.0-1~noble"


def test_select_tags_keep_per_line_newest_lines() -> None:
    tags = ["v1.0.0", "v2.0.0", "v2.0.1", "v2.1.0", "v2.1.1", "v2.1.2"]
    assert retention.select_tags(tags, keep=2, lines=2) == ["v2.1.2", "v2.1.1", "v2.0.1", "v2.0.0"]


def test_select_tags_lines_compare_numerically() -> None:
    assert retention.select_tags(["v2.9.0", "v2.10.0"], keep=1, lines=1) == ["v2.10.0"]


def test_select_tags_counts_only_given_tags() -> None:
    assert retention.select_tags(["v2.0.5", "v2.1.0"], keep=1, lines=2) == ["v2.1.0", "v2.0.5"]


def test_select_tags_rejects_a_non_stable_tag() -> None:
    with pytest.raises(PackageError, match="develop-1"):
        retention.select_tags(["develop-1"], keep=1, lines=1)


def test_drop_retired_removes_every_fmt_and_arch() -> None:
    rt_deb = _a("o/py", "v1.0.0", "rt", "1.0.0")
    rt_rpm = _a("o/py", "v1.0.0", "rt", "1.0.0", arch="arm64", fmt="rpm")
    app = _a("o/t", "v2.0.0", "app", "2.0.0")
    assert retention.drop_retired([rt_deb, app, rt_rpm], frozenset({"rt"})) == [app]
