"""Tests for vergil_tooling.lib.package.index.published (#3309)."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from vergil_tooling.lib.config import (
    PackageConfig,
    PackagePythonConfig,
    PackageStagedConfig,
    VergilConfig,
)
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import published
from vergil_tooling.lib.package.index.collect import MANIFEST, Identity

if TYPE_CHECKING:
    from pathlib import Path

_REPO = "vergil-project/vergil-python"

# vergil-python v1.0.0's real assets (vrg-gh release view v1.0.0 --json assets).
_V100 = [
    MANIFEST,
    "v1.0.0-ci-evidence-manifest.json",
    "v1.0.0-ci-evidence.tar.gz",
    "vergil-python3.14.8-3.14.8+20261003-1.aarch64.rpm",
    "vergil-python3.14.8-3.14.8+20261003-1.x86_64.rpm",
    "vergil-python3.14.8_3.14.8+20261003-1_amd64.deb",
    "vergil-python3.14.8_3.14.8+20261003-1_arm64.deb",
]
# v1.0.1 after recovery: its package assets and manifest were deleted.
_V101_RECOVERED = ["v1.0.1-ci-evidence-manifest.json", "v1.0.1-ci-evidence.tar.gz"]


def _vergil_python(version: str = "3.14.8+20261003") -> PackageConfig:
    """vergil-python's [package] at v1.0.1: staged, explicit name and version."""
    return PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="Pinned CPython",
        smoke="true",
        name="vergil-python3.14.8",
        version=version,
        staged=PackageStagedConfig(build_command="packaging/build.sh"),
    )


def _cfg(*pkgs: PackageConfig) -> VergilConfig:
    """A config carrying only ``packages``, the one field ``published`` reads."""
    return cast("VergilConfig", SimpleNamespace(packages=pkgs))


def _staged_series(name: str, upstream: str) -> PackageConfig:
    """A vergil-python-shaped series entry with ``upstream-version`` (epic .github#472)."""
    return PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="CPython",
        smoke="true",
        name=name,
        upstream_version=upstream,
        staged=PackageStagedConfig(build_command="packaging/build.sh"),
    )


class _FakeGh:
    """``gh release list`` / ``gh release view`` over ``releases`` (listed newest first)."""

    def __init__(
        self,
        releases: list[tuple[str, list[str]]],
        *,
        drafts: tuple[str, ...] = (),
        prereleases: tuple[str, ...] = (),
        fail: str = "",
    ) -> None:
        self.releases = releases
        self.drafts = drafts
        self.prereleases = prereleases
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        cmd = " ".join(argv)
        self.calls.append(cmd)
        if self.fail and cmd.startswith(self.fail):
            raise subprocess.CalledProcessError(1, list(argv), "", "HTTP 404: Not Found")
        a = list(argv)
        assert a[a.index("--repo") + 1] == _REPO
        if a[:3] == ["gh", "release", "list"]:
            rows = [
                {"tagName": t, "isDraft": t in self.drafts, "isPrerelease": t in self.prereleases}
                for t, _ in self.releases
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        assert a[:3] == ["gh", "release", "view"]
        assets = [{"name": n} for n in dict(self.releases)[a[3]]]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"assets": assets}), "")


def _parsed(names: list[str]) -> list[Identity]:
    return [published.parse_filename(n) for n in names if n.endswith((".deb", ".rpm"))]


# --- what a release would publish -------------------------------------------


def test_planned_vergil_python_matches_its_published_file_names(tmp_path: Path) -> None:
    got = published.planned(tmp_path, _cfg(_vergil_python()), "1.0.1")
    assert sorted(got) == sorted(_parsed(_V100))
    assert [str(i) for i in got] == [
        "vergil-python3.14.8 3.14.8+20261003-1 amd64 (deb)",
        "vergil-python3.14.8 3.14.8+20261003-1 amd64 (rpm)",
        "vergil-python3.14.8 3.14.8+20261003-1 arm64 (deb)",
        "vergil-python3.14.8 3.14.8+20261003-1 arm64 (rpm)",
    ]


def test_planned_defaults_to_the_release_version_and_pyproject_name(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    pkg = PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="true",
        python=PackagePythonConfig(runtime="3.14.4"),
        targets=["ubuntu/24.04/amd64", "rhel/9/amd64"],
        native=["rhel/9/amd64"],
    )
    assert published.planned(tmp_path, _cfg(pkg), "2.1.246") == [
        Identity("deb", "vergil-tooling", "2.1.246-1", "amd64"),
        Identity("rpm", "vergil-tooling", "2.1.246-1.el9", "amd64"),
    ]


def test_planned_noarch_is_arch_all(tmp_path: Path) -> None:
    pkg = PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="s",
        smoke="true",
        name="vergil-keyring",
        version="1.0",
        noarch=True,
    )
    assert published.planned(tmp_path, _cfg(pkg), "9.9.9") == [
        Identity("deb", "vergil-keyring", "1.0-1", "all"),
        Identity("rpm", "vergil-keyring", "1.0-1", "all"),
    ]


def test_single_package_identities_unchanged(tmp_path: Path) -> None:
    """vergil-tooling's own [package] plans exactly the identities it did before #3631."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    pkg = PackageConfig(
        builder="python",
        vendor="vergil",
        summary="Shared development tooling for Vergil-managed repositories",
        smoke="vrg-whoami --mode",
    )
    assert published.planned(tmp_path, _cfg(pkg), "2.1.267") == [
        Identity("deb", "vergil-tooling", "2.1.267-1", "amd64"),
        Identity("rpm", "vergil-tooling", "2.1.267-1", "amd64"),
        Identity("deb", "vergil-tooling", "2.1.267-1", "arm64"),
        Identity("rpm", "vergil-tooling", "2.1.267-1", "arm64"),
    ]


def test_planned_multi_entries(tmp_path: Path) -> None:
    cfg = _cfg(
        _staged_series("vergil-python3.14", "3.14.9+20261021"),
        _staged_series("vergil-python3.15", "3.15.1+20261021"),
    )
    got = published.planned(tmp_path, cfg, "1.0.5")
    assert [str(i) for i in got] == [
        "vergil-python3.14 3.14.9+20261021.1.0.5-1 amd64 (deb)",
        "vergil-python3.14 3.14.9+20261021.1.0.5-1 amd64 (rpm)",
        "vergil-python3.14 3.14.9+20261021.1.0.5-1 arm64 (deb)",
        "vergil-python3.14 3.14.9+20261021.1.0.5-1 arm64 (rpm)",
        "vergil-python3.15 3.15.1+20261021.1.0.5-1 amd64 (deb)",
        "vergil-python3.15 3.15.1+20261021.1.0.5-1 amd64 (rpm)",
        "vergil-python3.15 3.15.1+20261021.1.0.5-1 arm64 (deb)",
        "vergil-python3.15 3.15.1+20261021.1.0.5-1 arm64 (rpm)",
    ]


def test_new_repo_release_is_a_new_identity_for_upstream_version(tmp_path: Path) -> None:
    """An engine-only release (same inputs) still publishes new identities (spec §5.5)."""
    pkg = _staged_series("vergil-python3.15", "3.15.1+20261021")
    v105 = [f"vergil-python3.15_3.15.1+20261021.1.0.5-1_{a}.deb" for a in ("amd64", "arm64")]
    gh = _FakeGh([("v1.0.5", [MANIFEST, *v105])])
    got = published.check_release(tmp_path, _cfg(pkg), _REPO, "1.0.6", gh)
    assert {i.full_version for i in got} == {"3.15.1+20261021.1.0.6-1"}


# --- reading a published file name ------------------------------------------


@pytest.mark.parametrize(
    ("filename", "identity"),
    [
        ("p_1.2+3-1_amd64.deb", Identity("deb", "p", "1.2+3-1", "amd64")),
        ("p_1.2-1~noble_arm64.deb", Identity("deb", "p", "1.2-1~noble", "arm64")),
        ("p_1.2-1_all.deb", Identity("deb", "p", "1.2-1", "all")),
        ("a-b-c-1.2+3-1.x86_64.rpm", Identity("rpm", "a-b-c", "1.2+3-1", "amd64")),
        ("p-1.2-1.el9.aarch64.rpm", Identity("rpm", "p", "1.2-1.el9", "arm64")),
        ("p-1.2-1.noarch.rpm", Identity("rpm", "p", "1.2-1", "all")),
    ],
)
def test_parse_filename(filename: str, identity: Identity) -> None:
    assert published.parse_filename(filename) == identity


@pytest.mark.parametrize("filename", ["p-1.2-1.s390x.rpm", "p_1.2-1.deb", "p-1.2.rpm", "x.txt"])
def test_parse_filename_rejects_what_it_cannot_read(filename: str) -> None:
    with pytest.raises(PackageError, match="cannot read a package identity"):
        published.parse_filename(filename)


# --- the check ----------------------------------------------------------------


def test_vergil_python_v101_rebuild_is_refused_naming_v100(tmp_path: Path) -> None:
    """The 2026-10-16 incident: v1.0.1 rebuilt v1.0.0's 3.14.8+20261003-1."""
    gh = _FakeGh([("v1.0.0", _V100)])
    with pytest.raises(PackageError) as info:
        published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)
    msg = str(info.value)
    assert msg.startswith(
        f"{_REPO}@v1.0.1 would re-publish 4 package(s) at a version an earlier release "
        "already published:\n"
    )
    assert (
        f"  - vergil-python3.14.8 3.14.8+20261003-1 amd64 (deb): already published by "
        f"{_REPO}@v1.0.0\n"
    ) in msg
    assert "arm64 (rpm): already published" in msg
    assert "not byte-reproducible" in msg
    assert "Bump the package version" in msg
    assert "a new [package].upstream-version in vergil.toml" in msg
    assert "a new repo release for an upstream-version package" in msg
    assert "for a [package].version override, set a new version" in msg


def test_bumped_version_passes_and_returns_the_planned_identities(tmp_path: Path) -> None:
    gh = _FakeGh([("v1.0.1", _V101_RECOVERED), ("v1.0.0", _V100)])
    pkg = _vergil_python("3.14.8+20261013")
    got = published.check_release(tmp_path, _cfg(pkg), _REPO, "1.0.2", gh)
    assert len(got) == 4
    assert all(i.full_version == "3.14.8+20261013-1" for i in got)


def test_partial_overlap_lists_only_the_clashes(tmp_path: Path) -> None:
    only_deb_amd64 = [MANIFEST, "vergil-python3.14.8_3.14.8+20261003-1_amd64.deb"]
    gh = _FakeGh([("v1.0.0", only_deb_amd64)])
    with pytest.raises(PackageError, match=r"re-publish 1 package\(s\)") as info:
        published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)
    assert "rpm" not in str(info.value).split("\n")[1]


def test_the_release_being_checked_is_never_an_earlier_release(tmp_path: Path) -> None:
    gh = _FakeGh([("v1.0.1", _V100)])
    published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)
    assert not any("release view" in c for c in gh.calls)


def test_release_without_manifest_is_not_published(tmp_path: Path) -> None:
    """Mirrors the index: a release without a manifest is never collected."""
    no_manifest = [n for n in _V100 if n != MANIFEST]
    gh = _FakeGh([("v1.0.0", no_manifest)])
    published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)


def test_drafts_prereleases_and_unstable_tags_are_not_published(tmp_path: Path) -> None:
    gh = _FakeGh(
        [("v1.0.0", _V100), ("v0.9.0", _V100), ("develop-v1.0.0", _V100)],
        drafts=("v1.0.0",),
        prereleases=("v0.9.0",),
    )
    published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)
    assert not any("release view" in c for c in gh.calls)


def test_the_earliest_publishing_release_is_named(tmp_path: Path) -> None:
    gh = _FakeGh([("v1.0.10", _V100), ("v1.0.9", _V100), ("v1.0.2", [MANIFEST])])
    assert published.published(_REPO, "v2.0.0", gh) == dict.fromkeys(_parsed(_V100), "v1.0.9")


def test_unreadable_published_file_name_refuses(tmp_path: Path) -> None:
    gh = _FakeGh([("v1.0.0", [MANIFEST, "weird.deb"])])
    with pytest.raises(PackageError, match=rf"^{_REPO}@v1.0.0: cannot read .*'weird.deb'"):
        published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)


@pytest.mark.parametrize("fail", ["gh release list", "gh release view"])
def test_unreadable_published_state_refuses_never_skips(tmp_path: Path, fail: str) -> None:
    gh = _FakeGh([("v1.0.0", _V100)], fail=fail)
    with pytest.raises(PackageError, match="failed: HTTP 404"):
        published.check_release(tmp_path, _cfg(_vergil_python()), _REPO, "1.0.1", gh)


def test_repo_without_package_section_checks_nothing(tmp_path: Path) -> None:
    def _no_gh(*argv: str) -> Any:
        raise AssertionError(argv)

    assert published.check_release(tmp_path, _cfg(), _REPO, "1.0.1", _no_gh) == []
