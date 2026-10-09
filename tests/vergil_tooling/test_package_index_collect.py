"""Tests for vergil_tooling.lib.package.index.collect (spec §7.2)."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.lib import retry
from vergil_tooling.lib.config import PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import PackageError, matrix, repo_setup
from vergil_tooling.lib.package.index import collect
from vergil_tooling.lib.package.index.collect import Artifact
from vergil_tooling.lib.package.index.config import IndexConfig

_RPM_FILE = re.compile(r"(?P<name>.+)-(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?P<arch>[^.]+)\.rpm")


def _cfg(*products: str) -> IndexConfig:
    return IndexConfig("vergil", list(products) or ["o/t"], 3, 2)


def _bytes(name: str) -> bytes:
    return f"bytes of {name}".encode()


class _FakeGh:
    """A ``Run`` fake for ``gh``, ``dpkg-deb``, ``rpm`` and ``rpmkeys``.

    - ``gh release list`` lists ``releases[product]``; tags in ``drafts`` are
      drafts and tags in ``prereleases`` are prereleases.
    - ``gh release view`` returns each release's asset names.
    - ``gh release download`` writes the manifest (``json.dumps``) or
      placeholder bytes into ``--dir``.
    - ``dpkg-deb -f`` / ``rpm -qp`` answer deterministic metadata derived from
      the filename; ``control`` / ``rpm_qf`` override it per filename.
    """

    def __init__(
        self,
        releases: dict[str, list[tuple[str, list[str]]]],
        manifest: Any = None,
        *,
        manifests: dict[str, Any] | None = None,
        drafts: tuple[str, ...] = (),
        prereleases: tuple[str, ...] = (),
        depends: dict[str, list[str]] | None = None,
        control: dict[str, str] | None = None,
        rpm_qf: dict[str, str] | None = None,
        skip_write: tuple[str, ...] = (),
    ) -> None:
        self.releases = releases
        self.manifest = manifest
        self.manifests = manifests or {}
        self.drafts = drafts
        self.prereleases = prereleases
        self.depends = depends or {}
        self.control = control or {}
        self.rpm_qf = rpm_qf or {}
        self.skip_write = skip_write
        self.calls: list[str] = []

    def _assets(self, repo: str, tag: str) -> list[str]:
        return dict(self.releases[repo])[tag]

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(" ".join(argv))
        return subprocess.CompletedProcess(argv, 0, self._answer(list(argv)), "")

    def _answer(self, a: list[str]) -> str:
        if a[:3] == ["gh", "release", "list"]:
            repo = a[a.index("--repo") + 1]
            assert a[a.index("--json") + 1] == "tagName,isDraft,isPrerelease"
            rows = [
                {
                    "tagName": tag,
                    "isDraft": tag in self.drafts,
                    "isPrerelease": tag in self.prereleases,
                }
                for tag, _ in self.releases[repo]
            ]
            return json.dumps(rows)
        if a[:3] == ["gh", "release", "view"]:
            repo = a[a.index("--repo") + 1]
            return json.dumps({"assets": [{"name": n} for n in self._assets(repo, a[3])]})
        if a[:3] == ["gh", "release", "download"]:
            name = a[a.index("--pattern") + 1]
            dest = Path(a[a.index("--dir") + 1])
            assert "--clobber" in a
            if name not in self.skip_write:
                if name == collect.MANIFEST:
                    body = self.manifests.get(a[3], self.manifest)
                    (dest / name).write_text(body if isinstance(body, str) else json.dumps(body))
                else:
                    (dest / name).write_bytes(_bytes(name))
            return ""
        if a[:2] == ["dpkg-deb", "-f"]:
            fname = Path(a[2]).name
            if fname in self.control:
                return self.control[fname]
            name, ver, arch = fname.removesuffix(".deb").split("_")
            out = f"Package: {name}\nVersion: {ver}\nArchitecture: {arch}\n"
            if name in self.depends:
                out += f"Depends: {', '.join(self.depends[name])}\n"
            return out
        if a[:3] == ["rpm", "-qp", "--qf"]:
            fname = Path(a[4]).name
            if fname in self.rpm_qf:
                return self.rpm_qf[fname]
            m = _RPM_FILE.fullmatch(fname)
            assert m is not None
            return f"{m['name']}\n{m['ver']}\n{m['rel']}\n{m['arch']}\n"
        if a[:3] == ["rpm", "-qp", "--requires"]:
            m = _RPM_FILE.fullmatch(Path(a[3]).name)
            assert m is not None
            reqs = [*self.depends.get(m["name"], []), "rpmlib(PayloadIsZstd) <= 5.4.18-1"]
            reqs.append("/bin/sh")
            return "".join(f"{r}\n" for r in reqs)
        raise AssertionError(f"unexpected command: {a}")  # pragma: no cover


def _fake_gh(releases: dict[str, list[tuple[str, list[str]]]], **kw: Any) -> _FakeGh:
    return _FakeGh(releases, **kw)


def _entry(fmt: str, arch: str, suite: str | None = None) -> dict[str, Any]:
    return {"fmt": fmt, "arch": arch, "suite": suite}


def _a(product: str, tag: str, name: str, version: str, fmt: str = "deb") -> Artifact:
    path = Path(f"/x/{name}_{version}-1_amd64.{fmt}")
    return Artifact(product, tag, path, fmt, name, version, "1", "amd64", (), "s")


# --- default runner ------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        collect.stable_tags,
        collect.deb_metadata,
        collect.rpm_metadata,
        collect.collect,
        collect.verify,
    ],
)
def test_default_runner_is_repo_setup_local_run(fn: Any) -> None:
    # Run/local_run live in repo_setup (the canonical home); collect only uses them.
    assert inspect.signature(fn).parameters["gh"].default is repo_setup.local_run


# --- collect -----------------------------------------------------------------


def test_release_without_manifest_is_ignored(  # Review Focus 2
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _fake_gh(releases={"o/t": [("v2.0.0", [])]})  # no assets at all
    assert collect.collect(IndexConfig("vergil", ["o/t"], 3, 2), tmp_path, gh=gh) == []
    assert (
        "o/t@v2.0.0: no packages-manifest.json (pre-packaging release) — skipped"
        in capsys.readouterr().out
    )
    assert not any("download" in c for c in gh.calls)


def test_partial_release_is_fatal(tmp_path: Path) -> None:  # Review Focus 2
    manifest = {"artifacts": [_entry("deb", "amd64"), _entry("deb", "arm64")]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", ["packages-manifest.json", "t_2.1.0-1_amd64.deb"])]},
        manifest=manifest,
    )
    with pytest.raises(PackageError, match=r"o/t@v2\.1\.0 is missing deb/arm64"):
        collect.collect(IndexConfig("vergil", ["o/t"], 3, 2), tmp_path, gh=gh)


def test_partial_native_release_names_the_suite(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "amd64"), _entry("deb", "amd64", "resolute")]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, "t_2.1.0-1_amd64.deb"])]},
        manifest=manifest,
    )
    with pytest.raises(PackageError, match=r"o/t@v2\.1\.0 is missing deb/amd64/resolute"):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_only_stable_tags_and_metadata(tmp_path: Path) -> None:
    deb, rpm = "t_2.1.0-1_amd64.deb", "t-2.1.0-1.x86_64.rpm"
    manifest = {"artifacts": [_entry("deb", "amd64"), _entry("rpm", "amd64")]}
    gh = _fake_gh(
        releases={
            "o/t": [
                ("v2.2.0-rc1", [collect.MANIFEST]),
                ("develop-v2.1.0", [collect.MANIFEST]),
                ("v2.3.0", [collect.MANIFEST]),
                ("v2.1.0", [collect.MANIFEST, deb, rpm, "t-2.1.0.tar.gz", "sbom.json"]),
            ]
        },
        manifest=manifest,
        drafts=("v2.3.0",),
        prereleases=("v2.2.0-rc1", "develop-v2.1.0"),
        depends={"t": ["vergil-python3.14.4 (>= 20261001)", "libc6 (>= 2.34) | libc6-dev:any"]},
    )
    arts = collect.collect(_cfg(), tmp_path, gh=gh)
    dest = tmp_path / "o" / "t" / "v2.1.0"
    assert arts == [
        Artifact(
            "o/t",
            "v2.1.0",
            dest / deb,
            "deb",
            "t",
            "2.1.0",
            "1",
            "amd64",
            ("vergil-python3.14.4", "libc6", "libc6-dev"),
            hashlib.sha256(_bytes(deb)).hexdigest(),
        ),
        Artifact(
            "o/t",
            "v2.1.0",
            dest / rpm,
            "rpm",
            "t",
            "2.1.0",
            "1",
            "amd64",
            ("vergil-python3.14.4", "libc6"),  # rpm keeps the first token per line
            hashlib.sha256(_bytes(rpm)).hexdigest(),
        ),
    ]
    viewed = [c for c in gh.calls if c.startswith("gh release view ")]
    assert viewed == ["gh release view v2.1.0 --repo o/t --json assets"]
    downloaded = {c.split("--pattern ")[1].split(" ")[0] for c in gh.calls if "download" in c}
    assert downloaded == {collect.MANIFEST, deb, rpm}  # non-package assets are not fetched
    assert arts[0].full_version == "2.1.0-1"


def test_collects_every_product_and_release(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "arm64")]}
    gh = _fake_gh(
        releases={
            "o/a": [
                ("v1.1.0", [collect.MANIFEST, "a_1.1.0-1_arm64.deb"]),
                ("v1.0.0", [collect.MANIFEST, "a_1.0.0-1_arm64.deb"]),
            ],
            "o/b": [("v3.0.0", [collect.MANIFEST, "b_3.0.0-1_arm64.deb"])],
        },
        manifest=manifest,
    )
    arts = collect.collect(_cfg("o/a", "o/b"), tmp_path, gh=gh)
    assert [(a.product, a.tag, a.name, a.arch) for a in arts] == [
        ("o/a", "v1.1.0", "a", "arm64"),
        ("o/a", "v1.0.0", "a", "arm64"),
        ("o/b", "v3.0.0", "b", "arm64"),
    ]


def test_native_suffixes_and_noarch(tmp_path: Path) -> None:
    files = [
        "t_2.1.0-1_amd64.deb",
        "t_2.1.0-1~resolute_amd64.deb",
        "t-2.1.0-1.el10.aarch64.rpm",
        "k_1.0.0-1_all.deb",
        "k-1.0.0-1.noarch.rpm",
    ]
    manifest = {
        "artifacts": [
            _entry("deb", "amd64"),
            _entry("deb", "amd64", "resolute"),
            _entry("rpm", "arm64", "el10"),
            _entry("deb", "all"),
            _entry("rpm", "all"),
        ]
    }
    gh = _fake_gh(releases={"o/t": [("v2.1.0", [collect.MANIFEST, *files])]}, manifest=manifest)
    arts = collect.collect(_cfg(), tmp_path, gh=gh)
    assert [(a.path.name, a.name, a.version, a.release, a.arch) for a in arts] == [
        ("t_2.1.0-1_amd64.deb", "t", "2.1.0", "1", "amd64"),
        ("t_2.1.0-1~resolute_amd64.deb", "t", "2.1.0", "1~resolute", "amd64"),
        ("t-2.1.0-1.el10.aarch64.rpm", "t", "2.1.0", "1.el10", "arm64"),
        ("k_1.0.0-1_all.deb", "k", "1.0.0", "1", "all"),
        ("k-1.0.0-1.noarch.rpm", "k", "1.0.0", "1", "all"),
    ]


def test_manifest_from_real_matrix_shape(tmp_path: Path) -> None:
    """The manifest ``matrix.manifest`` emits for a default product is accepted."""
    pkg = PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="true",
        python=PackagePythonConfig(runtime="3.14.4"),
    )
    manifest = matrix.manifest(matrix.resolve(pkg))
    files = [
        "t_2.1.0-1_amd64.deb",
        "t-2.1.0-1.x86_64.rpm",
        "t_2.1.0-1_arm64.deb",
        "t-2.1.0-1.aarch64.rpm",
    ]
    gh = _fake_gh(releases={"o/t": [("v2.1.0", [collect.MANIFEST, *files])]}, manifest=manifest)
    assert sorted(a.path.name for a in collect.collect(_cfg(), tmp_path, gh=gh)) == sorted(files)


def test_shared_pattern_does_not_claim_a_native_file(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "amd64")]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, "t_2.1.0-1~noble_amd64.deb"])]},
        manifest=manifest,
    )
    with pytest.raises(PackageError, match=r"o/t@v2\.1\.0 is missing deb/amd64"):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_unlisted_package_is_fatal(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "amd64")]}
    gh = _fake_gh(
        releases={
            "o/t": [("v2.1.0", [collect.MANIFEST, "t_2.1.0-1_amd64.deb", "t-2.1.0-1.x86_64.rpm"])]
        },
        manifest=manifest,
    )
    with pytest.raises(
        PackageError,
        match=r"o/t@v2\.1\.0 attaches package\(s\) not in its packages-manifest\.json: "
        r"t-2\.1\.0-1\.x86_64\.rpm",
    ):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_two_packages_for_one_entry_is_fatal(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "amd64")]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, "b_1-1_amd64.deb", "a_1-1_amd64.deb"])]},
        manifest=manifest,
    )
    with pytest.raises(
        PackageError, match=r"o/t@v2\.1\.0 has 2 packages for deb/amd64: a_1-1_amd64\.deb, b_1"
    ):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_repeated_manifest_entry_is_fatal(tmp_path: Path) -> None:
    manifest = {"artifacts": [_entry("deb", "amd64"), _entry("deb", "amd64")]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, "t_1-1_amd64.deb"])]}, manifest=manifest
    )
    with pytest.raises(PackageError, match=r"t_1-1_amd64\.deb is named by more than one"):
        collect.collect(_cfg(), tmp_path, gh=gh)


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        ("{not json", r"packages-manifest\.json is malformed: Expecting"),
        ([1, 2], r"expected a non-empty 'artifacts' list"),
        ({"artifacts": []}, r"expected a non-empty 'artifacts' list"),
        ({"artifacts": "deb"}, r"expected a non-empty 'artifacts' list"),
        ({"artifacts": ["deb"]}, r"bad artifact entry 'deb'"),
        ({"artifacts": [_entry("apk", "amd64")]}, r"bad artifact entry"),
        ({"artifacts": [_entry("deb", "riscv64")]}, r"bad artifact entry"),
        ({"artifacts": [{"fmt": "deb", "arch": "amd64", "suite": 3}]}, r"bad artifact entry"),
    ],
)
def test_malformed_manifest_is_fatal(tmp_path: Path, manifest: Any, match: str) -> None:
    gh = _fake_gh(releases={"o/t": [("v2.1.0", [collect.MANIFEST])]}, manifest=manifest)
    with pytest.raises(PackageError, match=r"o/t@v2\.1\.0: .*" + match):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_manifest_suite_may_be_omitted(tmp_path: Path) -> None:
    manifest = {"artifacts": [{"fmt": "deb", "arch": "amd64"}]}
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, "t_1-1_amd64.deb"])]}, manifest=manifest
    )
    assert [a.release for a in collect.collect(_cfg(), tmp_path, gh=gh)] == ["1"]


@pytest.mark.parametrize(
    ("control", "match"),
    [
        (
            "Package: t\nVersion: 2.1.0-1\nArchitecture: arm64\n",
            r"t_2\.1\.0-1_amd64\.deb declares arch 'arm64' release '1', but its "
            r"packages-manifest\.json entry deb/amd64 expects arch 'amd64' release '1'",
        ),
        (
            "Package: t\nVersion: 2.1.0-1~noble\nArchitecture: amd64\n",
            r"declares arch 'amd64' release '1~noble'",
        ),
    ],
)
def test_metadata_disagreeing_with_manifest_is_fatal(
    tmp_path: Path, control: str, match: str
) -> None:
    deb = "t_2.1.0-1_amd64.deb"
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, deb])]},
        manifest={"artifacts": [_entry("deb", "amd64")]},
        control={deb: control},
    )
    with pytest.raises(PackageError, match=match):
        collect.collect(_cfg(), tmp_path, gh=gh)


def test_download_without_a_file_is_fatal(tmp_path: Path) -> None:
    deb = "t_2.1.0-1_amd64.deb"
    gh = _fake_gh(
        releases={"o/t": [("v2.1.0", [collect.MANIFEST, deb])]},
        manifest={"artifacts": [_entry("deb", "amd64")]},
        skip_write=(deb,),
    )
    with pytest.raises(
        PackageError, match=r"downloading t_2\.1\.0-1_amd64\.deb from o/t@v2\.1\.0 produced no file"
    ):
        collect.collect(_cfg(), tmp_path, gh=gh)


# --- release listing -----------------------------------------------------------


def _stdout(out: str) -> repo_setup.Run:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


def _failing(stdout: str = "", stderr: str = "", code: int = 1) -> repo_setup.Run:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(code, argv, stdout, stderr)

    return run


def test_listing_failure_is_fatal_with_stderr() -> None:
    with pytest.raises(PackageError, match=r"listing releases of o/t failed: HTTP 404"):
        collect.stable_tags("o/t", _failing(stderr="HTTP 404\n"))


def test_listing_failure_falls_back_to_stdout_then_exit_code() -> None:
    with pytest.raises(PackageError, match=r"failed: out text$"):
        collect.stable_tags("o/t", _failing(stdout="out text"))
    with pytest.raises(PackageError, match=r"failed: exit 4$"):
        collect.stable_tags("o/t", _failing(code=4))


def test_listing_invalid_json_is_fatal() -> None:
    with pytest.raises(PackageError, match=r"listing releases of o/t returned invalid JSON"):
        collect.stable_tags("o/t", _stdout("nope"))


def test_listing_not_a_list_is_fatal() -> None:
    with pytest.raises(PackageError, match=r"listing releases of o/t: expected a JSON list"):
        collect.stable_tags("o/t", _stdout("{}"))


def test_listing_at_the_limit_is_fatal_not_truncated() -> None:
    rows = [{"tagName": f"v1.0.{i}", "isDraft": False, "isPrerelease": False} for i in range(1000)]
    with pytest.raises(PackageError, match=r"1000 releases reached the listing limit"):
        collect.stable_tags("o/t", _stdout(json.dumps(rows)))


# --- metadata ------------------------------------------------------------------


def test_deb_metadata_handles_continuation_and_no_depends(tmp_path: Path) -> None:
    out = "Package: t\nVersion: 1:2.1.0-1\nArchitecture: amd64\nDepends: a,\n b (>= 1)\n"
    assert collect.deb_metadata(tmp_path / "t.deb", _stdout(out)) == (
        "t",
        "1:2.1.0",
        "1",
        "amd64",
        ("a", "b"),
    )
    out = "Package: t\nVersion: 2.1.0-1\nArchitecture: all\n"
    assert collect.deb_metadata(tmp_path / "t.deb", _stdout(out))[4] == ()


def test_deb_metadata_ignores_leading_continuation_and_noise(tmp_path: Path) -> None:
    out = " stray\nnoise\nPackage: t\nVersion: 2.1.0-1\nArchitecture: amd64\n"
    assert collect.deb_metadata(tmp_path / "t.deb", _stdout(out))[0] == "t"


@pytest.mark.parametrize(
    ("out", "match"),
    [
        ("Package: t\n", r"t\.deb: control data lacks Version, Architecture"),
        ("Package: t\nVersion: 2.1.0\nArchitecture: amd64\n", r"Version '2\.1\.0' has no revision"),
        ("Package: t\nVersion: 2.1.0-\nArchitecture: amd64\n", r"has no revision"),
    ],
)
def test_deb_metadata_errors(tmp_path: Path, out: str, match: str) -> None:
    with pytest.raises(PackageError, match=match):
        collect.deb_metadata(tmp_path / "t.deb", _stdout(out))


def test_deb_metadata_command_failure(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"reading t\.deb failed: not a debian archive"):
        collect.deb_metadata(tmp_path / "t.deb", _failing(stderr="not a debian archive"))


@pytest.mark.parametrize(
    ("out", "match"),
    [
        ("t\n2.1.0\n1\n", r"unexpected rpm header query output"),
        ("t\n\n1\nx86_64\n", r"unexpected rpm header query output"),
        ("t\n2.1.0\n1\nppc64le\n", r"t\.rpm: unsupported rpm arch 'ppc64le'"),
    ],
)
def test_rpm_metadata_errors(tmp_path: Path, out: str, match: str) -> None:
    with pytest.raises(PackageError, match=match):
        collect.rpm_metadata(tmp_path / "t.rpm", _stdout(out))


def test_rpm_requires_keeps_names_only(tmp_path: Path) -> None:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        if "--requires" in argv:
            out = "a >= 1\n\nrpmlib(X) <= 1\n/bin/sh\na\nlibc.so.6()(64bit)\n"
        else:
            out = "t\n2.1.0\n1\naarch64\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    assert collect.rpm_metadata(tmp_path / "t.rpm", run) == (
        "t",
        "2.1.0",
        "1",
        "arm64",
        ("a", "libc.so.6()(64bit)"),
    )


# --- verify --------------------------------------------------------------------


def test_verify_pins_signer_workflow_and_ref(tmp_path: Path) -> None:
    calls: list[str] = []

    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        calls.append(" ".join(a))
        return subprocess.CompletedProcess(a, 0, "digests signatures OK", "")

    art = _a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")
    collect.verify([art], tmp_path / "k.asc", gh=gh)
    assert calls == [
        f"gh attestation verify {art.path} --repo o/t --signer-workflow "
        "vergil-project/vergil-actions/.github/workflows/cd-release.yml "
        "--source-ref refs/heads/main",
        f"rpmkeys --import {tmp_path / 'k.asc'}",
        f"rpmkeys --checksig {art.path}",
    ]


def test_verify_uses_a_private_rpmdb_when_given(tmp_path: Path) -> None:
    """The publish-index runner is not root: the key goes into a private rpm DB."""
    calls: list[str] = []

    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        calls.append(" ".join(a))
        return subprocess.CompletedProcess(a, 0, "digests signatures OK", "")

    db = tmp_path / "work" / "rpmdb"
    art = _a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")
    collect.verify([art], tmp_path / "k.asc", gh=gh, rpmdb=db)
    assert db.is_dir()
    assert calls[1:] == [
        f"rpmkeys --dbpath {db.resolve()} --import {tmp_path / 'k.asc'}",
        f"rpmkeys --dbpath {db.resolve()} --checksig {art.path}",
    ]


def test_verify_debs_only_never_touches_rpmkeys(tmp_path: Path) -> None:
    calls: list[str] = []

    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        calls.append(" ".join(a))
        return subprocess.CompletedProcess(a, 0, "", "")

    collect.verify(
        [_a("o/t", "v2.1.0", "t", "2.1.0"), _a("o/u", "v1.0.0", "u", "1.0.0")],
        tmp_path / "k.asc",
        gh=gh,
    )
    assert [c.split(" ")[0] for c in calls] == ["gh", "gh"]
    assert "--repo o/u " in calls[1]


def test_verify_failure_is_fatal(tmp_path: Path) -> None:
    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, a, "", "no matching attestations")

    with pytest.raises(
        PackageError, match=r"attestation verification failed for .*: no matching attestations"
    ):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0")], tmp_path / "k.asc", gh=gh)


def _rpm_gh(checksig: subprocess.CompletedProcess[str] | Exception) -> repo_setup.Run:
    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        if a[:2] == ("rpmkeys", "--checksig"):
            if isinstance(checksig, Exception):
                raise checksig
            return checksig
        return subprocess.CompletedProcess(a, 0, "", "")

    return gh


def test_unsigned_rpm_is_fatal(tmp_path: Path) -> None:
    gh = _rpm_gh(subprocess.CompletedProcess([], 0, "t.rpm: digests SIGNATURES NOT OK\n", ""))
    with pytest.raises(
        PackageError,
        match=r"rpm signature check failed for t_2\.1\.0-1_amd64\.rpm \(o/t@v2\.1\.0\): "
        r"t\.rpm: digests SIGNATURES NOT OK$",
    ):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")], tmp_path / "k.asc", gh=gh)


def test_digests_only_rpm_is_fatal(tmp_path: Path) -> None:
    """An unsigned rpm passes ``--checksig`` with exit 0 and only "digests OK"."""
    gh = _rpm_gh(subprocess.CompletedProcess([], 0, "t.rpm: digests OK\n", ""))
    with pytest.raises(PackageError, match=r"rpm signature check failed"):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")], tmp_path / "k.asc", gh=gh)


def test_bad_rpm_signature_exit_is_fatal(tmp_path: Path) -> None:
    gh = _rpm_gh(subprocess.CalledProcessError(1, [], "t.rpm: SIGNATURES NOT OK", ""))
    with pytest.raises(
        PackageError, match=r"rpm signature check failed for .*: t\.rpm: SIGNATURES NOT OK"
    ):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")], tmp_path / "k.asc", gh=gh)


def test_rpm_key_import_failure_is_fatal(tmp_path: Path) -> None:
    def gh(*a: str) -> subprocess.CompletedProcess[str]:
        if a[:2] == ("rpmkeys", "--import"):
            raise subprocess.CalledProcessError(1, a, "", "key file not found")
        return subprocess.CompletedProcess(a, 0, "", "")

    with pytest.raises(
        PackageError, match=r"importing .*k\.asc into the rpm keyring failed: key file not found"
    ):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0", fmt="rpm")], tmp_path / "k.asc", gh=gh)


# --- transient GitHub errors (#3153) -------------------------------------------

_GRAPHQL_BLIP = "GraphQL: Something went wrong while executing your query."


class _Flaky:
    """Wrap a ``Run``: commands starting with ``prefix`` fail ``times`` times first."""

    def __init__(self, inner: repo_setup.Run, prefix: str, times: int, stderr: str) -> None:
        self.inner = inner
        self.prefix = prefix
        self.left = times
        self.stderr = stderr
        self.attempts = 0

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        if " ".join(argv).startswith(self.prefix):
            self.attempts += 1
            if self.left:
                self.left -= 1
                raise subprocess.CalledProcessError(1, argv, "", self.stderr)
        return self.inner(*argv)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("vergil_tooling.lib.retry.time.sleep", slept.append)
    return slept


def _one_release_gh() -> _FakeGh:
    return _fake_gh(
        releases={"o/t": [("v2.1.0", ["packages-manifest.json", "t_2.1.0-1_amd64.deb"])]},
        manifest={"artifacts": [_entry("deb", "amd64")]},
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "gh release list",
        "gh release view",
        "gh release download v2.1.0 --repo o/t --pattern packages-manifest.json",
        "gh release download v2.1.0 --repo o/t --pattern t_2.1.0-1_amd64.deb",
    ],
)
@pytest.mark.parametrize("stderr", [_GRAPHQL_BLIP, "HTTP 502: Bad Gateway"])
def test_transient_gh_release_blip_is_retried_then_succeeds(
    tmp_path: Path, sleeps: list[float], prefix: str, stderr: str
) -> None:
    gh = _Flaky(_one_release_gh(), prefix, 2, stderr)
    arts = collect.collect(_cfg(), tmp_path, gh=gh)
    assert [a.path.name for a in arts] == ["t_2.1.0-1_amd64.deb"]
    assert gh.attempts == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize("prefix", ["gh release list", "gh release view", "gh release download"])
def test_persistent_transient_gh_release_failure_is_fatal_after_budget(
    tmp_path: Path, sleeps: list[float], prefix: str
) -> None:
    gh = _Flaky(_one_release_gh(), prefix, 99, _GRAPHQL_BLIP)
    with pytest.raises(PackageError, match=r"failed: GraphQL: Something went wrong"):
        collect.collect(_cfg(), tmp_path, gh=gh)
    assert gh.attempts == retry.MAX_RETRIES + 1
    assert len(sleeps) == retry.MAX_RETRIES


@pytest.mark.parametrize("prefix", ["gh release list", "gh release view", "gh release download"])
def test_answer_type_gh_release_failure_is_not_retried(
    tmp_path: Path, sleeps: list[float], prefix: str
) -> None:
    gh = _Flaky(_one_release_gh(), prefix, 1, "release not found")
    with pytest.raises(PackageError, match=r"failed: release not found"):
        collect.collect(_cfg(), tmp_path, gh=gh)
    assert gh.attempts == 1
    assert sleeps == []


def test_non_gh_command_is_never_retried(tmp_path: Path, sleeps: list[float]) -> None:
    gh = _Flaky(_one_release_gh(), "dpkg-deb", 1, "unexpected EOF")
    with pytest.raises(PackageError, match=r"reading t_2\.1\.0-1_amd64\.deb failed"):
        collect.collect(_cfg(), tmp_path, gh=gh)
    assert gh.attempts == 1
    assert sleeps == []


def test_transient_attestation_blip_is_retried(tmp_path: Path, sleeps: list[float]) -> None:
    gh = _Flaky(_stdout(""), "gh attestation verify", 1, "HTTP 503")
    collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0")], tmp_path / "k.asc", gh=gh)
    assert gh.attempts == 2
    assert len(sleeps) == 1


def test_persistent_transient_attestation_failure_is_fatal(
    tmp_path: Path, sleeps: list[float]
) -> None:
    gh = _Flaky(_stdout(""), "gh attestation verify", 99, "HTTP 503")
    with pytest.raises(PackageError, match=r"attestation verification failed for .*: HTTP 503"):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0")], tmp_path / "k.asc", gh=gh)
    assert gh.attempts == retry.MAX_RETRIES + 1
    assert len(sleeps) == retry.MAX_RETRIES


def test_failed_attestation_is_not_retried(tmp_path: Path, sleeps: list[float]) -> None:
    gh = _Flaky(_stdout(""), "gh attestation verify", 1, "no matching attestations")
    with pytest.raises(PackageError, match=r"attestation verification failed"):
        collect.verify([_a("o/t", "v2.1.0", "t", "2.1.0")], tmp_path / "k.asc", gh=gh)
    assert gh.attempts == 1
    assert sleeps == []
