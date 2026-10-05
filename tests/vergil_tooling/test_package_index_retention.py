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


def test_keep_per_line_newest_two_lines() -> None:
    tags = ["v2.0.1", "v2.0.2", "v2.1.1", "v2.1.2", "v2.1.3", "v2.1.4", "v2.2.0"]
    arts = [_a("o/t", t, "t", t[1:]) for t in tags]
    kept = {a.tag for a in retention.select(arts, keep=3, lines=2)}
    assert kept == {"v2.1.2", "v2.1.3", "v2.1.4", "v2.2.0"}


def test_lines_compare_numerically_not_lexically() -> None:
    tags = ["v2.9.0", "v2.10.0", "v2.10.1", "v10.0.0"]
    arts = [_a("o/t", t, "t", t[1:]) for t in tags]
    kept = {a.tag for a in retention.select(arts, keep=1, lines=2)}
    assert kept == {"v10.0.0", "v2.10.1"}


def test_retention_is_per_product() -> None:
    arts = [
        _a("o/a", "v1.0.0", "a", "1.0.0"),
        _a("o/a", "v1.1.0", "a", "1.1.0"),
        _a("o/b", "v5.0.0", "b", "5.0.0"),
    ]
    kept = {(a.product, a.tag) for a in retention.select(arts, keep=1, lines=1)}
    assert kept == {("o/a", "v1.1.0"), ("o/b", "v5.0.0")}


def test_every_artifact_of_a_retained_release_is_kept() -> None:
    arts = [
        _a("o/t", "v1.0.0", "t", "1.0.0", arch="amd64"),
        _a("o/t", "v1.0.0", "t", "1.0.0", arch="arm64"),
        _a("o/t", "v1.0.0", "t", "1.0.0", fmt="rpm"),
    ]
    assert retention.select(arts, keep=1, lines=1) == arts


def test_dependency_closure_keeps_old_runtime() -> None:
    rt_old = _a("o/py", "v1.0.0", "vergil-python3.14.3", "3.14.3+20260801")
    rt_new = _a("o/py", "v1.1.0", "vergil-python3.14.4", "3.14.4+20261001")
    rt_newer = _a("o/py", "v1.2.0", "vergil-python3.14.5", "3.14.5+20261201")
    rt_newest = _a("o/py", "v1.3.0", "vergil-python3.14.6", "3.14.6+20270101")
    app = _a("o/t", "v2.1.4", "t", "2.1.4", depends=("vergil-python3.14.3",))
    kept = retention.select([rt_old, rt_new, rt_newer, rt_newest, app], keep=1, lines=2)
    # evicted by line retention (lines=2 keeps 1.3, 1.2) but kept by closure
    assert rt_old in kept
    assert rt_new not in kept
    assert {rt_newer, rt_newest, app} <= set(kept)


def test_closure_keeps_newest_provider_per_fmt_and_arch() -> None:
    old_deb = _a("o/py", "v1.0.0", "rt", "3.14.3+1", arch="amd64")
    new_deb = _a("o/py", "v1.1.0", "rt", "3.14.3+2", arch="amd64")
    new_deb_arm = _a("o/py", "v1.1.0", "rt", "3.14.3+2", arch="arm64")
    old_rpm = _a("o/py", "v1.0.0", "rt", "3.14.3+1", fmt="rpm")
    # the runtime product itself keeps only v2.0.0, which no longer ships "rt"
    other = _a("o/py", "v2.0.0", "rt2", "3.15.0+1")
    app_deb = _a("o/t", "v1.0.0", "t", "1.0.0", depends=("rt",))
    app_rpm = _a("o/t", "v1.0.0", "t", "1.0.0", fmt="rpm", depends=("rt",))
    kept = retention.select(
        [old_deb, new_deb, new_deb_arm, old_rpm, other, app_deb, app_rpm], keep=1, lines=1
    )
    assert new_deb in kept
    assert old_deb not in kept
    assert new_deb_arm not in kept  # no retained arm64 artifact depends on it
    assert old_rpm in kept  # the only rpm provider


def test_closure_accepts_arch_all_provider_and_is_transitive() -> None:
    lib = _a("o/l", "v1.0.0", "lib", "1.0.0", arch="all")
    lib_new = _a("o/l", "v2.0.0", "lib-ng", "2.0.0", arch="all")
    rt = _a("o/py", "v1.0.0", "rt", "1.0.0", depends=("lib",))
    rt_new = _a("o/py", "v2.0.0", "rt-ng", "2.0.0")
    app = _a("o/t", "v1.0.0", "t", "1.0.0", depends=("rt", "libc6"))
    kept = retention.select([lib, lib_new, rt, rt_new, app], keep=1, lines=1)
    assert lib in kept and rt in kept


def test_closure_ignores_names_nobody_provides_and_self_dependency() -> None:
    app = _a("o/t", "v1.0.0", "t", "1.0.0", depends=("libc6", "t"))
    assert retention.select([app], keep=1, lines=1) == [app]


def test_closure_version_order_is_numeric() -> None:
    v9 = _a("o/py", "v1.0.0", "rt", "3.14.9+1")
    v10 = _a("o/py", "v1.1.0", "rt", "3.14.10+1")
    gone = _a("o/py", "v2.0.0", "rt-ng", "1.0.0")
    app = _a("o/t", "v1.0.0", "t", "1.0.0", depends=("rt",))
    kept = retention.select([v10, v9, gone, app], keep=1, lines=1)
    assert v10 in kept and v9 not in kept


def test_duplicate_name_version_arch_with_different_bytes_is_fatal() -> None:  # Review Focus 1
    a = _a("o/py", "v1.0.0", "vergil-python3.14.4", "3.14.4+20261001", sha="aaa")
    b = _a("o/py", "v1.0.1", "vergil-python3.14.4", "3.14.4+20261001", sha="bbb")
    with pytest.raises(
        PackageError,
        match=(
            r"vergil-python3\.14\.4 3\.14\.4\+20261001-1 amd64 \(deb\) differs between "
            r"o/py@v1\.0\.0 and o/py@v1\.0\.1"
        ),
    ):
        retention.select([a, b], keep=3, lines=2)


def test_duplicate_conflict_is_fatal_even_when_retention_would_evict_one() -> None:
    a = _a("o/py", "v1.0.0", "p", "1", sha="aaa")
    b = _a("o/py", "v2.0.0", "p", "1", sha="bbb")
    with pytest.raises(PackageError, match=r"differs between o/py@v1\.0\.0 and o/py@v2\.0\.0"):
        retention.select([a, b], keep=1, lines=1)


def test_identical_duplicate_is_collapsed() -> None:
    a = _a("o/py", "v1.0.0", "p", "1", sha="same")
    b = _a("o/py", "v1.0.1", "p", "1", sha="same")
    kept = [x for x in retention.select([a, b], keep=3, lines=2) if x.name == "p"]
    assert kept == [a]


def test_identical_duplicate_survives_when_only_the_later_release_is_retained() -> None:
    a = _a("o/py", "v1.0.0", "p", "1", sha="same")
    b = _a("o/py", "v2.0.0", "p", "1", sha="same")
    assert retention.select([a, b], keep=1, lines=1) == [b]


def test_same_name_version_on_different_arch_or_fmt_is_not_a_duplicate() -> None:
    arts = [
        _a("o/t", "v1.0.0", "t", "1.0.0", arch="amd64", sha="x"),
        _a("o/t", "v1.0.0", "t", "1.0.0", arch="arm64", sha="y"),
        _a("o/t", "v1.0.0", "t", "1.0.0", fmt="rpm", sha="z"),
    ]
    assert retention.select(arts, keep=1, lines=1) == arts


def test_non_stable_tag_is_rejected() -> None:
    with pytest.raises(PackageError, match=r"o/t@v1\.0\.0-rc1 is not a stable vX\.Y\.Z tag"):
        retention.select([_a("o/t", "v1.0.0-rc1", "t", "1.0.0")], keep=1, lines=1)


def test_empty_input() -> None:
    assert retention.select([], keep=3, lines=2) == []
