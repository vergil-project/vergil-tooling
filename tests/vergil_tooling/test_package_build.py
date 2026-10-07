"""Tests for vergil_tooling.lib.package.build (spec §6)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.package import PackageError, build, nfpm
from vergil_tooling.lib.package import staged as _staged
from vergil_tooling.lib.package.matrix import BuildCell

if TYPE_CHECKING:
    from pathlib import Path

_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def _staged_toml(*, build_command: str | None, extra: str = "") -> str:
    text = (
        _BASE_TOML
        + """
[package]
builder = "staged"
vendor = "vergil"
name = "vergil-thing"
summary = "A thing"
smoke = "true"
"""
        + extra
    )
    if build_command is not None:
        text += f'\n[package.staged]\nbuild-command = "{build_command}"\n'
    return text


def _write_overlay(repo: Path, text: str) -> None:
    (repo / "packaging").mkdir(exist_ok=True)
    (repo / "packaging/nfpm.overlay.yaml").write_text(text)


def _fake_package(
    calls: list[tuple[dict[str, Any], str, Path]],
) -> Any:
    def fake(config: dict[str, Any], fmt: str, out_dir: Path) -> Path:
        calls.append((config, fmt, out_dir))
        return out_dir / f"{config['name']}.{fmt}"

    return fake


# --- contents_from_tree ---------------------------------------------------------


def test_contents_owns_dirs_below_prefix_files_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    (root / "opt/vergil/x/bin").mkdir(parents=True)
    (root / "opt/vergil/x/bin/tool").write_text("#!/bin/sh\n")
    (root / "opt/vergil/x/bin/tool").chmod(0o755)
    (root / "etc/vergil").mkdir(parents=True)
    (root / "etc/vergil/x.conf").write_text("a=1\n")
    (root / "etc/vergil/x.conf").chmod(0o644)
    (root / "opt/vergil/x/bin/link").symlink_to("tool")
    got = build.contents_from_tree(root, own_below="/opt/vergil")
    assert {"dst": "/opt/vergil/x", "type": "dir"} in got
    assert {"dst": "/opt/vergil/x/bin", "type": "dir"} in got
    assert not any(
        e.get("dst") in ("/", "/opt", "/opt/vergil", "/etc", "/etc/vergil")
        and e.get("type") == "dir"
        for e in got
    )
    tool = next(e for e in got if e["dst"] == "/opt/vergil/x/bin/tool")
    assert tool["file_info"]["mode"] == 0o755
    assert tool["src"] == str(root / "opt/vergil/x/bin/tool")
    conf = next(e for e in got if e["dst"] == "/etc/vergil/x.conf")
    assert conf["file_info"]["mode"] == 0o644
    assert {"src": "tool", "dst": "/opt/vergil/x/bin/link", "type": "symlink"} in got


def test_contents_own_below_accepts_trailing_slash_and_ignores_prefix_lookalikes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "stage"
    (root / "opt/vergil-other/y").mkdir(parents=True)
    (root / "opt/vergil/z").mkdir(parents=True)
    got = build.contents_from_tree(root, own_below="/opt/vergil/")
    dirs = [e["dst"] for e in got if e.get("type") == "dir"]
    assert dirs == ["/opt/vergil/z"]


def test_contents_hidden_top_level_dir_keeps_its_name(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    (root / ".hidden").mkdir(parents=True)
    (root / ".hidden/f").write_text("x")
    got = build.contents_from_tree(root, own_below="/opt/vergil")
    assert [e["dst"] for e in got] == ["/.hidden/f"]


def test_contents_symlink_to_dir_is_a_symlink_not_descended(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    (root / "opt/vergil/x/real").mkdir(parents=True)
    (root / "opt/vergil/x/real/f").write_text("x")
    (root / "opt/vergil/x/alias").symlink_to("real")
    got = build.contents_from_tree(root, own_below="/opt/vergil")
    assert {"src": "real", "dst": "/opt/vergil/x/alias", "type": "symlink"} in got
    assert not any(e["dst"].startswith("/opt/vergil/x/alias/") for e in got)
    assert {"dst": "/opt/vergil/x/real", "type": "dir"} in got


def test_contents_entries_are_sorted_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    (root / "opt/vergil/b").mkdir(parents=True)
    (root / "opt/vergil/a").mkdir(parents=True)
    for n in ("z", "m", "a"):
        (root / "opt/vergil/a" / n).write_text(n)
    got = [e["dst"] for e in build.contents_from_tree(root, own_below="/opt/vergil")]
    assert got == [
        "/opt/vergil/a",
        "/opt/vergil/a/a",
        "/opt/vergil/a/m",
        "/opt/vergil/a/z",
        "/opt/vergil/b",
    ]


def test_contents_special_file_is_fatal(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    (root / "opt/vergil/x").mkdir(parents=True)
    os.mkfifo(root / "opt/vergil/x/pipe")
    with pytest.raises(PackageError, match=r"non-regular file at /opt/vergil/x/pipe"):
        build.contents_from_tree(root, own_below="/opt/vergil")


def test_contents_of_empty_tree_is_empty(tmp_path: Path) -> None:
    (tmp_path / "stage").mkdir()
    assert build.contents_from_tree(tmp_path / "stage", own_below="/opt/vergil") == []


def test_contents_escapes_glob_metacharacters_in_file_sources(tmp_path: Path) -> None:
    # nFPM globs every file ``src``: a literal ``{``/``[``/``*``/``?``/``\`` must be
    # escaped, and the escaped src then needs the *parent* dir (trailing slash) as
    # ``dst`` or nFPM nests the file under a directory of its own name (#3125).
    root = tmp_path / "stage"
    base = root / "opt/vergil/x"
    (base / "{dir}").mkdir(parents=True)
    for name, mode in (
        ("{name}.tmpl", 0o755),
        ("[x]*?.txt", 0o600),
        ("{dir}/inner.txt", 0o640),
        ("back\\slash.txt", 0o644),
        ("normal.txt", 0o644),
    ):
        (base / name).write_text(name)
        (base / name).chmod(mode)
    (base / "{link}").symlink_to("{name}.tmpl")
    esc = nfpm.glob_escape(str(base))
    got = build.contents_from_tree(root, own_below="/opt/vergil")
    assert got == [
        {"dst": "/opt/vergil/x", "type": "dir"},
        {"src": esc + r"/\[x\]\*\?.txt", "dst": "/opt/vergil/x/", "file_info": {"mode": 0o600}},
        {"src": esc + r"/back\\slash.txt", "dst": "/opt/vergil/x/", "file_info": {"mode": 0o644}},
        {
            "src": str(base / "normal.txt"),
            "dst": "/opt/vergil/x/normal.txt",
            "file_info": {"mode": 0o644},
        },
        # A symlink's src is the link target text, not a glob: nFPM stores it verbatim.
        {"src": "{name}.tmpl", "dst": "/opt/vergil/x/{link}", "type": "symlink"},
        {"src": esc + r"/\{name\}.tmpl", "dst": "/opt/vergil/x/", "file_info": {"mode": 0o755}},
        # A ``type: dir`` entry has no src, and nFPM never globs a dst.
        {"dst": "/opt/vergil/x/{dir}", "type": "dir"},
        {
            "src": esc + r"/\{dir\}/inner.txt",
            "dst": "/opt/vergil/x/{dir}/",
            "file_info": {"mode": 0o640},
        },
    ]


def test_contents_metacharacter_in_staging_root_escapes_every_file(tmp_path: Path) -> None:
    root = tmp_path / "st[a]ge"
    (root / "opt/vergil").mkdir(parents=True)
    (root / "opt/vergil/f").write_text("x")
    (root / "top").write_text("y")
    got = build.contents_from_tree(root, own_below="/opt/vergil")
    esc = nfpm.glob_escape(str(root))
    assert esc.endswith(r"st\[a\]ge")
    assert [(e["src"], e["dst"]) for e in got] == [
        (esc + "/top", "/"),
        (esc + "/opt/vergil/f", "/opt/vergil/"),
    ]


# --- release, floor, registry -------------------------------------------------------


def test_package_release_revision() -> None:
    shared = BuildCell("shared-amd64", "amd64", "r", "i", ("deb", "rpm"), (), False, None)
    native = BuildCell(
        "native-ubuntu-26.04-amd64", "amd64", "r", "i", ("deb",), (), True, "resolute"
    )
    native_rpm = BuildCell("native-rhel-10-amd64", "amd64", "r", "i", ("rpm",), (), True, "el10")
    assert build.package_release(shared, "deb") == "1"
    assert build.package_release(shared, "rpm") == "1"
    assert build.package_release(native, "deb") == "1~resolute"
    assert build.package_release(native_rpm, "rpm") == "1.el10"


def test_glibc_floor_is_lowest_among_cell_targets() -> None:
    cell = BuildCell(
        "shared-amd64",
        "amd64",
        "r",
        "i",
        ("deb", "rpm"),
        ("rhel/10/amd64", "rhel/9/amd64", "ubuntu/24.04/amd64", "ubuntu/26.04/amd64"),
        False,
        None,
    )
    assert build.glibc_floor(cell) == (2, 34)
    ub26 = ("ubuntu/26.04/amd64",)
    only_ubuntu = BuildCell("native-u26", "amd64", "r", "i", ("deb",), ub26, True, "x")
    assert build.glibc_floor(only_ubuntu) == (2, 43)


def test_register_adds_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "BUILDERS", {})

    @build.register("fake")
    def fake(ctx: build.BuildContext) -> build.BuildResult:
        return build.BuildResult(contents=[])

    assert {"fake": fake} == build.BUILDERS
    assert build.BuildResult(contents=[]).depends == {"deb": [], "rpm": []}


def test_staged_builder_is_registered() -> None:
    assert build.BUILDERS["staged"] is _staged.build_staged


# --- run_build ------------------------------------------------------------------------


def test_run_build_overlay_only_packages_every_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command=None))
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys/k.asc").write_text("key")
    _write_overlay(tmp_path, "contents:\n  - src: keys/k.asc\n    dst: /usr/share/keyrings/k.asc\n")
    calls: list[tuple[dict[str, Any], str, Path]] = []
    monkeypatch.setattr(nfpm, "package", _fake_package(calls))
    out = tmp_path / "dist"
    got = build.run_build(tmp_path, "shared-amd64", "2.1.240", out, tmp_path / "stage")
    assert [fmt for _, fmt, _ in calls] == ["deb", "rpm"]
    assert got == [out / "vergil-thing.deb", out / "vergil-thing.rpm"]
    cfg = calls[0][0]
    assert cfg["name"] == "vergil-thing"
    assert cfg["version"] == "2.1.240"
    assert cfg["arch"] == "amd64"
    assert cfg["contents"] == [
        {"src": str(tmp_path / "keys/k.asc"), "dst": "/usr/share/keyrings/k.asc"}
    ]
    assert out.is_dir()


def test_run_build_staged_command_and_version_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = "d=$VRG_STAGING_ROOT/opt/vergil/thing && mkdir -p $d && echo hi > $d/f"
    (tmp_path / "vergil.toml").write_text(
        _staged_toml(
            build_command=cmd,
            extra='version = "3.14.4+20261001"\nnative = ["ubuntu/26.04/arm64"]\n',
        )
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "stale").write_text("left over from a previous build")
    calls: list[tuple[dict[str, Any], str, Path]] = []
    monkeypatch.setattr(nfpm, "package", _fake_package(calls))
    build.run_build(tmp_path, "native-ubuntu-26.04-arm64", "2.1.240", tmp_path / "o", stage)
    assert len(calls) == 1
    cfg, fmt, _ = calls[0]
    assert fmt == "deb"
    assert cfg["version"] == "3.14.4+20261001"
    assert cfg["release"] == "1~resolute"
    assert cfg["arch"] == "arm64"
    assert not (stage / "stale").exists()
    assert {"dst": "/opt/vergil/thing", "type": "dir"} in cfg["contents"]


def test_run_build_without_package_section_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_BASE_TOML)
    with pytest.raises(PackageError, match=r"no \[package\] section"):
        build.run_build(tmp_path, "shared-amd64", "1.0.0", tmp_path / "o", tmp_path / "s")


def test_unknown_cell_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command="true"))
    with pytest.raises(PackageError, match=r"unknown build cell 'shared-mips'.*shared-amd64"):
        build.run_build(tmp_path, "shared-mips", "1.0.0", tmp_path / "o", tmp_path / "s")


def test_unregistered_builder_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command="true"))
    monkeypatch.setattr(build, "BUILDERS", {})
    with pytest.raises(PackageError, match=r"no builder registered.*'staged'"):
        build.run_build(tmp_path, "shared-amd64", "1.0.0", tmp_path / "o", tmp_path / "s")


def test_empty_package_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command="true"))
    calls: list[tuple[dict[str, Any], str, Path]] = []
    monkeypatch.setattr(nfpm, "package", _fake_package(calls))
    with pytest.raises(PackageError, match=r"empty package for vergil-thing \(cell shared-amd64\)"):
        build.run_build(tmp_path, "shared-amd64", "1.0.0", tmp_path / "o", tmp_path / "s")
    assert calls == []


def test_run_build_applies_glibc_floor_of_the_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command="touch $VRG_STAGING_ROOT/f"))
    seen: list[tuple[Path, tuple[int, int]]] = []
    monkeypatch.setattr(
        build.elf, "check_glibc_floor", lambda root, floor: seen.append((root, floor))
    )
    monkeypatch.setattr(nfpm, "package", _fake_package([]))
    build.run_build(tmp_path, "shared-arm64", "1.0.0", tmp_path / "o", tmp_path / "s")
    assert seen == [(tmp_path / "s", (2, 34))]


def test_run_build_rejects_raw_systemctl_before_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vergil.toml").write_text(_staged_toml(build_command="touch $VRG_STAGING_ROOT/f"))
    _write_overlay(tmp_path, "scripts:\n  postinstall: packaging/postinst.sh\n")
    (tmp_path / "packaging/postinst.sh").write_text("#!/bin/sh\nsystemctl start x\n")
    calls: list[tuple[dict[str, Any], str, Path]] = []
    monkeypatch.setattr(nfpm, "package", _fake_package(calls))
    with pytest.raises(PackageError, match=r"raw 'systemctl'"):
        build.run_build(tmp_path, "shared-amd64", "1.0.0", tmp_path / "o", tmp_path / "s")
    assert calls == []
