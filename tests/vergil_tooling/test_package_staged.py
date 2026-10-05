"""Tests for vergil_tooling.lib.package.staged (the ``staged`` builder, spec §6.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.config import PackageConfig, PackageStagedConfig
from vergil_tooling.lib.package import PackageError, staged
from vergil_tooling.lib.package.build import BuildContext
from vergil_tooling.lib.package.matrix import BuildCell

if TYPE_CHECKING:
    from pathlib import Path

_SHARED_AMD64 = BuildCell(
    "shared-amd64", "amd64", "r", "i", ("deb", "rpm"), ("ubuntu/24.04/amd64",), False, None
)


def _staged_ctx(
    tmp_path: Path, *, build_command: str | None, name: str, with_staged: bool = True
) -> BuildContext:
    pkg = PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="s",
        smoke="true",
        name=name,
        staged=PackageStagedConfig(build_command=build_command) if with_staged else None,
    )
    staging = tmp_path / "s"
    staging.mkdir()
    return BuildContext(tmp_path, pkg, _SHARED_AMD64, name, "1.0.0", staging, tmp_path / "o")


def test_build_command_receives_env_and_populates_root(tmp_path: Path) -> None:
    (tmp_path / "packaging").mkdir()
    script = tmp_path / "packaging/build.sh"
    script.write_text(
        '#!/bin/sh\nset -e\nmkdir -p "$VRG_STAGING_ROOT/opt/vergil/python/3.14.4/bin"\n'
        'echo "$VRG_TARGET_ARCH" > "$VRG_STAGING_ROOT/opt/vergil/python/3.14.4/bin/arch"\n'
    )
    script.chmod(0o755)
    ctx = _staged_ctx(tmp_path, build_command="packaging/build.sh", name="vergil-python3.14.4")
    res = staged.build_staged(ctx)
    assert (ctx.staging_root / "opt/vergil/python/3.14.4/bin/arch").read_text().strip() == "amd64"
    assert {"dst": "/opt/vergil/python", "type": "dir"} in res.contents
    assert {"dst": "/opt/vergil/python/3.14.4", "type": "dir"} in res.contents
    assert not any(e["dst"] == "/opt/vergil" and e.get("type") == "dir" for e in res.contents)
    assert res.depends == {"deb": [], "rpm": []}


def test_build_command_runs_from_repo_root(tmp_path: Path) -> None:
    (tmp_path / "marker").write_text("here")
    ctx = _staged_ctx(tmp_path, build_command='cp marker "$VRG_STAGING_ROOT/m"', name="x")
    res = staged.build_staged(ctx)
    assert [e["dst"] for e in res.contents] == ["/m"]


def test_failing_command_is_fatal(tmp_path: Path) -> None:
    ctx = _staged_ctx(tmp_path, build_command="exit 3", name="x")
    with pytest.raises(PackageError, match=r"build-command failed \(exit 3\): exit 3"):
        staged.build_staged(ctx)


def test_overlay_only_returns_no_contents(tmp_path: Path) -> None:
    ctx = _staged_ctx(tmp_path, build_command=None, name="vergil-archive-keyring")
    assert staged.build_staged(ctx).contents == []


def test_missing_staged_table_runs_nothing(tmp_path: Path) -> None:
    ctx = _staged_ctx(tmp_path, build_command=None, name="x", with_staged=False)
    assert staged.build_staged(ctx).contents == []


def test_command_producing_nothing_returns_no_contents(tmp_path: Path) -> None:
    ctx = _staged_ctx(tmp_path, build_command="true", name="x")
    assert staged.build_staged(ctx).contents == []
