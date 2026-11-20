"""vergil.toml's ``[publish].consumer-refresh``: the Mac's locked install (spec §4.5)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.vergil_tooling.tooling_install_stubs import Run, run_lines, stub_bin, write_stub
from vergil_tooling.lib.tooling_install import GIT_URL

_ROOT = Path(__file__).resolve().parents[2]
_VERSION = "2.1.10"
_SPEC = f"vergil-tooling @ git+{GIT_URL}@v{_VERSION}"
_GUIDES = (
    _ROOT / "docs/site/docs/getting-started.md",
    _ROOT / "docs/site/docs/guides/consuming-repo-setup.md",
    _ROOT / "docs/site/docs/guides/releasing.md",
)


def _template() -> str:
    data = tomllib.loads((_ROOT / "vergil.toml").read_text())
    refresh = data["publish"]["consumer-refresh"]
    assert isinstance(refresh, str)
    return refresh


def _refresh() -> str:
    return _template().replace("<VERSION>", _VERSION)


def _install_block() -> str:
    """The template's install subshell, ``( … )``, with ``<VERSION>`` left in."""
    match = re.search(r"^\(\n.*?^\)$", _template(), re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group(0)


def _run(tmp_path: Path, **env: str) -> Run:
    bin_dir = stub_bin(tmp_path)
    write_stub(bin_dir, "vrg-vm", '#!/bin/bash\nprintf \'vrg-vm\\t%s\\n\' "$*" >> "$STUB_LOG"\n')
    # vrg-release runs the block under `set -eo pipefail` (handoff.run_consumer_refresh).
    return run_lines("set -eo pipefail\n" + _refresh(), tmp_path, env=env, path=[bin_dir])


def test_consumer_refresh_installs_the_exact_tag_with_its_lock(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.returncode == 0, run.stderr
    git = run.of("git")
    assert git[0][:2] == ["init", "-q"]
    assert git[1][2:] == ["fetch", "-q", "--depth", "1", GIT_URL, f"refs/tags/v{_VERSION}"]
    assert git[2][2:] == ["checkout", "-q", "FETCH_HEAD"]
    assert not any(c[0] == "ls-remote" for c in git)
    export, install = run.of("uv")
    assert export[0] == "export"
    for flag in ("--no-config", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"):
        assert flag in export
    constraints = export[export.index("-o") + 1]
    assert install[:2] == ["tool", "install"]
    for flag in ("--force", "--no-config", "--managed-python"):
        assert flag in install
    assert install[install.index("--python") + 1] == "3.15"
    assert install[install.index("-c") + 1] == constraints
    assert install[-1] == _SPEC
    for index in (0, 1):
        env = dict(item.split("=", 1) for item in run.of("uv-env")[index])
        assert env["UV_PYTHON"] == "<unset>"
        assert env["UV_PYTHON_PREFERENCE"] == "<unset>"
        assert env["UV_NO_MANAGED_PYTHON"] == "<unset>"
    assert not list(tmp_path.glob("tmp.*"))


@pytest.mark.parametrize("failing", ["STUB_FETCH_RC", "STUB_EXPORT_RC", "STUB_INSTALL_RC"])
def test_consumer_refresh_fails_on_any_error(tmp_path: Path, failing: str) -> None:
    run = _run(tmp_path, **{failing: "3"})
    assert run.returncode != 0
    assert run.of("vrg-vm") == []
    assert not list(tmp_path.glob("tmp.*"))
    if failing != "STUB_INSTALL_RC":
        assert [c for c in run.of("uv") if c[0] == "tool"] == []


def test_consumer_refresh_runs_no_vergil_tooling_to_install_itself() -> None:
    block = _install_block()
    assert "vrg-" not in block
    assert "raw.githubusercontent.com" not in _template()


def test_consumer_refresh_keeps_the_vm_refresh(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.of("vrg-vm") == [["update --all"]]
    assert _template().rstrip().endswith("vrg-vm update --all")


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.name)
def test_guides_document_the_same_install_lines(guide: Path) -> None:
    block = _install_block().replace("v<VERSION>", "v${VERSION}")
    assert block in guide.read_text()
