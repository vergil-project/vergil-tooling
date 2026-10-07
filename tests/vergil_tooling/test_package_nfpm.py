"""Tests for vergil_tooling.lib.package.nfpm (spec §5.2, §6.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from vergil_tooling.lib.config import PackageConfig, PackageStagedConfig
from vergil_tooling.lib.package import PackageError, nfpm
from vergil_tooling.lib.package.build import BuildContext, BuildResult
from vergil_tooling.lib.package.matrix import BuildCell


def _pkg(**kw: Any) -> PackageConfig:
    base: dict[str, Any] = {
        "builder": "staged",
        "vendor": "vergil",
        "summary": "Shared tooling",
        "smoke": "true",
        "name": "vergil-tooling",
        "staged": PackageStagedConfig(build_command="true"),
    }
    base.update(kw)
    return PackageConfig(**base)


def _ctx(tmp_path: Path, cell: BuildCell | None = None, **pkgkw: Any) -> BuildContext:
    cell = cell or BuildCell(
        "shared-amd64", "amd64", "r", "i", ("deb", "rpm"), ("ubuntu/24.04/amd64",), False, None
    )
    return BuildContext(
        tmp_path, _pkg(**pkgkw), cell, "vergil-tooling", "2.1.240", tmp_path / "s", tmp_path / "o"
    )


def _overlay(repo: Path, text: str) -> None:
    (repo / "packaging").mkdir(exist_ok=True)
    (repo / "packaging/nfpm.overlay.yaml").write_text(text)


def _script(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- render -----------------------------------------------------------------------------


def test_render_identity_and_depends(tmp_path: Path) -> None:
    res = BuildResult(
        contents=[{"dst": "/opt/vergil/vergil-tooling", "type": "dir"}],
        depends={
            "deb": ["vergil-python3.14.4 (>= 3.14.4+20261001)"],
            "rpm": ["vergil-python3.14.4 >= 3.14.4+20261001"],
        },
    )
    cfg = nfpm.render(_ctx(tmp_path), res, "rpm", {})
    assert cfg["name"] == "vergil-tooling"
    assert cfg["version"] == "2.1.240"
    assert cfg["release"] == "1"
    assert cfg["version_schema"] == "none"
    assert cfg["arch"] == "amd64"
    assert cfg["platform"] == "linux"
    assert cfg["maintainer"] == "vergil packages <packages@vergil.invalid>"
    assert cfg["description"] == "Shared tooling"
    assert cfg["vendor"] == "vergil"
    assert cfg["depends"] == ["vergil-python3.14.4 >= 3.14.4+20261001"]
    assert cfg["contents"] == res.contents
    assert "scripts" not in cfg
    assert not any(k in cfg for k in ("provides", "conflicts", "replaces"))


def test_render_native_release(tmp_path: Path) -> None:
    cell = BuildCell("native-rhel-9-arm64", "arm64", "r", "i", ("rpm",), (), True, "el9")
    cfg = nfpm.render(_ctx(tmp_path, cell), BuildResult(contents=[]), "rpm", {})
    assert cfg["release"] == "1.el9"
    assert cfg["arch"] == "arm64"


def test_noarch_renders_all(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, noarch=True)
    assert nfpm.render(ctx, BuildResult(contents=[]), "deb", {})["arch"] == "all"


def test_overlay_appends_contents_and_depends(tmp_path: Path) -> None:
    overlay = {
        "contents": [{"src": "x", "dst": "/etc/x"}],
        "overrides": {"deb": {"depends": ["adduser"]}},
        "scripts": {"postinstall": "packaging/postinst.sh"},
    }
    cfg = nfpm.render(
        _ctx(tmp_path), BuildResult(contents=[{"dst": "/a", "type": "dir"}]), "deb", overlay
    )
    assert cfg["contents"][0] == {"dst": "/a", "type": "dir"}
    assert cfg["contents"][-1]["dst"] == "/etc/x"
    assert cfg["depends"] == ["adduser"]
    assert cfg["scripts"]["postinstall"] == str(tmp_path / "packaging/postinst.sh")
    # The deb override never leaks into the rpm config.
    assert nfpm.render(_ctx(tmp_path), BuildResult(contents=[]), "rpm", overlay)["depends"] == []


def test_overlay_contents_src_is_repo_relative(tmp_path: Path) -> None:
    overlay = {
        "contents": [
            {"src": "keys/k.asc", "dst": "/k"},
            {"src": "/abs/file", "dst": "/abs"},
            {"src": "../relative/target", "dst": "/l", "type": "symlink"},
            {"dst": "/var/lib/vergil/x", "type": "dir"},
        ]
    }
    cfg = nfpm.render(_ctx(tmp_path), BuildResult(contents=[]), "deb", overlay)
    assert cfg["contents"] == [
        {"src": str(tmp_path / "keys/k.asc"), "dst": "/k"},
        {"src": "/abs/file", "dst": "/abs"},
        {"src": "../relative/target", "dst": "/l", "type": "symlink"},
        {"dst": "/var/lib/vergil/x", "type": "dir"},
    ]
    # The overlay itself is not mutated.
    assert overlay["contents"][0]["src"] == "keys/k.asc"


def test_glob_escape_quotes_exactly_the_nfpm_metacharacters() -> None:
    # The set gobwas/glob's ``syntax.Special`` (used by nFPM 2.47.0) treats as meta.
    assert nfpm.glob_escape(r"a*b?c[d]e{f}g\h") == r"a\*b\?c\[d\]e\{f\}g\\h"
    assert nfpm.glob_escape("/plain/path-1,2!x.txt") == "/plain/path-1,2!x.txt"
    assert nfpm.has_glob_meta("x{y") is True
    assert nfpm.has_glob_meta("/plain/path-1,2!x.txt") is False


def test_overlay_relative_src_under_metachar_repo_root_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "re{po}"
    repo.mkdir()
    overlay = {"contents": [{"src": "keys/k.asc", "dst": "/k"}]}
    with pytest.raises(PackageError, match=r"src 'keys/k\.asc'.*re\{po\}.*glob metacharacter"):
        nfpm.render(_ctx(repo), BuildResult(contents=[]), "deb", overlay)
    over_only = {"overrides": {"rpm": {"contents": [{"src": "r.conf", "dst": "/etc/r"}]}}}
    with pytest.raises(PackageError, match=r"r\.conf"):
        nfpm.render(_ctx(repo), BuildResult(contents=[]), "rpm", over_only)


def test_overlay_non_glob_entries_under_metachar_repo_root_are_fine(tmp_path: Path) -> None:
    repo = tmp_path / "re{po}"
    repo.mkdir()
    entries = [
        {"src": "/abs/file", "dst": "/abs"},
        {"src": "../t", "dst": "/l", "type": "symlink"},
        {"dst": "/var/lib/x", "type": "dir"},
    ]
    cfg = nfpm.render(_ctx(repo), BuildResult(contents=[]), "deb", {"contents": entries})
    assert cfg["contents"] == entries


def test_overlay_relations_and_per_format_overrides_merge(tmp_path: Path) -> None:
    overlay = {
        "provides": ["thing"],
        "conflicts": ["old-thing"],
        "scripts": {"postinstall": "packaging/post.sh", "preremove": "packaging/prerm.sh"},
        "overrides": {
            "rpm": {
                "conflicts": ["old-thing-rpm"],
                "recommends": ["extra"],
                "contents": [{"src": "rpm.conf", "dst": "/etc/rpm.conf"}],
                "scripts": {"postinstall": "packaging/post-rpm.sh"},
            }
        },
    }
    cfg = nfpm.render(_ctx(tmp_path), BuildResult(contents=[]), "rpm", overlay)
    assert cfg["provides"] == ["thing"]
    assert cfg["conflicts"] == ["old-thing", "old-thing-rpm"]
    assert cfg["recommends"] == ["extra"]
    assert "suggests" not in cfg
    assert cfg["contents"] == [{"src": str(tmp_path / "rpm.conf"), "dst": "/etc/rpm.conf"}]
    assert cfg["scripts"] == {
        "postinstall": str(tmp_path / "packaging/post-rpm.sh"),
        "preremove": str(tmp_path / "packaging/prerm.sh"),
    }
    assert "overrides" not in cfg
    deb = nfpm.render(_ctx(tmp_path), BuildResult(contents=[]), "deb", overlay)
    assert deb["conflicts"] == ["old-thing"]
    assert deb["scripts"]["postinstall"] == str(tmp_path / "packaging/post.sh")


# --- load_overlay -------------------------------------------------------------------------


def test_load_overlay_absent_is_empty(tmp_path: Path) -> None:
    assert nfpm.load_overlay(tmp_path) == {}


def test_load_overlay_empty_file_is_empty(tmp_path: Path) -> None:
    _overlay(tmp_path, "")
    assert nfpm.load_overlay(tmp_path) == {}


def test_load_overlay_returns_mapping(tmp_path: Path) -> None:
    text = (
        "contents:\n  - src: a\n    dst: /a\n"
        "scripts:\n  postinstall: p.sh\n"
        "provides: [x]\n"
        "overrides:\n  deb:\n    depends: [adduser]\n  rpm:\n    scripts:\n      preremove: r.sh\n"
    )
    _overlay(tmp_path, text)
    assert nfpm.load_overlay(tmp_path) == yaml.safe_load(text)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (": : :", r"not valid YAML"),
        ("- a\n- b\n", r"must be a YAML mapping"),
        ("depends: [x]\n", r"top-level 'depends' is not allowed"),
        ("contents: x\n", r"contents must be a list of mappings"),
        ("contents:\n  - src: a\n", r"contents entries must be mappings with a string 'dst'"),
        ("contents:\n  - a\n", r"contents entries must be mappings"),
        ("scripts: [a]\n", r"scripts must map script names"),
        ("scripts:\n  postinstall: 3\n", r"scripts must map script names"),
        ("provides: x\n", r"provides must be a list of strings"),
        ("provides: [1]\n", r"provides must be a list of strings"),
        ("overrides: [deb]\n", r"overrides must be a mapping"),
        ("overrides:\n  apk: {}\n", r"overrides\.apk: unknown format"),
        ("overrides:\n  deb: [x]\n", r"overrides\.deb must be a mapping"),
        ("overrides:\n  deb:\n    name: x\n", r"overrides\.deb may not set name"),
        ("overrides:\n  rpm:\n    depends: x\n", r"overrides\.rpm\.depends must be a list"),
        ("overrides:\n  rpm:\n    contents: [1]\n", r"overrides\.rpm\.contents entries"),
    ],
)
def test_load_overlay_bad_shape_is_fatal(tmp_path: Path, text: str, error: str) -> None:
    _overlay(tmp_path, text)
    with pytest.raises(PackageError, match=r"packaging/nfpm\.overlay\.yaml: .*" + error):
        nfpm.load_overlay(tmp_path)


# --- check_maintainer_scripts ---------------------------------------------------------


def test_raw_systemctl_in_maintainer_script_is_fatal(tmp_path: Path) -> None:
    _script(tmp_path, "packaging/postinst.sh", "#!/bin/sh\nsystemctl enable --now x.service\n")
    with pytest.raises(PackageError, match=r"postinst\.sh:2: raw 'systemctl'"):
        nfpm.check_maintainer_scripts(tmp_path, {"scripts": {"postinst": "packaging/postinst.sh"}})


@pytest.mark.parametrize(
    "line",
    [
        "/usr/bin/systemctl daemon-reload",
        "if true; then systemctl start x; fi",
        'echo "#"; systemctl stop x',
        "x=$(systemctl is-active x)",
    ],
)
def test_raw_systemctl_variants_are_fatal(tmp_path: Path, line: str) -> None:
    _script(tmp_path, "packaging/prerm.sh", f"#!/bin/sh\n{line}\n")
    with pytest.raises(PackageError, match=r"prerm\.sh:2: raw 'systemctl'"):
        nfpm.check_maintainer_scripts(tmp_path, {"scripts": {"preremove": "packaging/prerm.sh"}})


def test_helper_idioms_and_comments_are_allowed(tmp_path: Path) -> None:
    _script(
        tmp_path,
        "packaging/postinst.sh",
        "#!/bin/sh\n# never call systemctl directly\n"
        "deb-systemd-helper enable x.service\ndeb-systemd-invoke start x.service  # not systemctl\n"
        "[ -x /usr/lib/systemd/systemd-update-helper ] && "
        "/usr/lib/systemd/systemd-update-helper install-system-units x.service\n"
        "echo my-systemctl-wrapper systemctl-ish\n",
    )
    nfpm.check_maintainer_scripts(tmp_path, {"scripts": {"postinstall": "packaging/postinst.sh"}})


def test_override_scripts_are_checked(tmp_path: Path) -> None:
    _script(tmp_path, "packaging/ok.sh", "#!/bin/sh\ntrue\n")
    _script(tmp_path, "packaging/rpm-post.sh", "#!/bin/sh\nsystemctl enable x\n")
    overlay = {
        "scripts": {"postinstall": "packaging/ok.sh"},
        "overrides": {
            "deb": {"depends": ["x"]},
            "rpm": {"scripts": {"postinstall": "packaging/rpm-post.sh"}},
        },
    }
    with pytest.raises(PackageError, match=r"rpm-post\.sh:2: raw 'systemctl'"):
        nfpm.check_maintainer_scripts(tmp_path, overlay)


def test_no_scripts_is_ok(tmp_path: Path) -> None:
    nfpm.check_maintainer_scripts(tmp_path, {})


def test_missing_maintainer_script_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"maintainer script packaging/gone\.sh not found"):
        nfpm.check_maintainer_scripts(tmp_path, {"scripts": {"postinstall": "packaging/gone.sh"}})


# --- package --------------------------------------------------------------------------


def _fake_nfpm(
    calls: list[list[str]], produce: list[str], returncode: int = 0, stderr: str = ""
) -> Any:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        config = yaml.safe_load(Path(cmd[cmd.index("--config") + 1]).read_text())
        calls.append([f"config-name={config.get('name')}"])
        target = Path(cmd[cmd.index("--target") + 1])
        for name in produce:
            (target / name).write_bytes(b"pkg")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return fake_run


def test_package_runs_nfpm_and_returns_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(nfpm.shutil, "which", lambda _: "/usr/bin/nfpm")
    monkeypatch.setattr(
        nfpm.subprocess, "run", _fake_nfpm(calls, ["vergil-tooling_2.1.240-1_amd64.deb"])
    )
    out = tmp_path / "o"
    out.mkdir()
    (out / "vergil-tooling_2.1.240-1_amd64.deb").write_bytes(b"stale")
    got = nfpm.package(
        {"name": "vergil-tooling", "version": "2.1.240", "release": "1", "arch": "amd64"},
        "deb",
        out,
    )
    assert got == out / "vergil-tooling_2.1.240-1_amd64.deb"
    assert got.read_bytes() == b"pkg"
    assert calls[0][:2] == ["nfpm", "package"]
    assert calls[0][calls[0].index("--packager") + 1] == "deb"
    assert calls[1] == ["config-name=vergil-tooling"]
    # Nothing but the artifact lands in the output directory.
    assert sorted(p.name for p in out.iterdir()) == ["vergil-tooling_2.1.240-1_amd64.deb"]


def test_package_creates_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nfpm.shutil, "which", lambda _: "/usr/bin/nfpm")
    monkeypatch.setattr(nfpm.subprocess, "run", _fake_nfpm([], ["x-1-1.x86_64.rpm"]))
    got = nfpm.package({"name": "x"}, "rpm", tmp_path / "new/dir")
    assert got == tmp_path / "new/dir/x-1-1.x86_64.rpm"


def test_missing_nfpm_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nfpm.shutil, "which", lambda _: None)
    # The hint must name the setup action that actually installs nFPM.
    with pytest.raises(
        PackageError, match=r"nfpm not found on PATH \(CI installs it via actions/package/setup\)"
    ):
        nfpm.package({}, "deb", tmp_path)


def test_nfpm_failure_is_fatal_with_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nfpm.shutil, "which", lambda _: "/usr/bin/nfpm")
    monkeypatch.setattr(
        nfpm.subprocess, "run", _fake_nfpm([], [], returncode=1, stderr="bad config\n")
    )
    with pytest.raises(PackageError, match=r"(?s)--packager deb failed \(exit 1\):\nbad config"):
        nfpm.package({"name": "x"}, "deb", tmp_path)


@pytest.mark.parametrize("produce", [[], ["a.deb", "b.deb"], ["a.rpm"]])
def test_nfpm_must_produce_exactly_one_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, produce: list[str]
) -> None:
    monkeypatch.setattr(nfpm.shutil, "which", lambda _: "/usr/bin/nfpm")
    monkeypatch.setattr(nfpm.subprocess, "run", _fake_nfpm([], produce))
    with pytest.raises(PackageError, match=r"nfpm produced \d+ \.deb files, expected exactly 1"):
        nfpm.package({"name": "x"}, "deb", tmp_path / "o")
