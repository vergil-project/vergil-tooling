"""vergil.toml's ``[publish].consumer-refresh``: the Mac's locked install (vergil-tooling#4827)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.vergil_tooling.tooling_install_stubs import Run, run_lines, stub_bin, write_stub
from vergil_tooling.lib.tooling_install import GIT_URL

_ROOT = Path(__file__).resolve().parents[2]
_VERSION = "2.1.10"
_GUIDES = (
    _ROOT / "docs/site/docs/getting-started.md",
    _ROOT / "docs/site/docs/guides/consuming-repo-setup.md",
    _ROOT / "docs/site/docs/guides/releasing.md",
)
# The documented install by hand: an unlocked bootstrap, then the locked
# reinstall. It is the first install on a fresh host, and the recovery when the
# installed vrg-tooling-install is broken.
_FIRST_INSTALL = (
    "VERSION=X.Y.Z\n"
    "env -u UV_PYTHON -u UV_PYTHON_PREFERENCE -u UV_NO_MANAGED_PYTHON \\\n"
    "  uv tool install --force --no-config --python 3.15 --managed-python \\\n"
    f'  "vergil-tooling @ git+{GIT_URL}@v${{VERSION}}"\n'
    'vrg-tooling-install "v${VERSION}"\n'
)


def _template() -> str:
    data = tomllib.loads((_ROOT / "vergil.toml").read_text())
    refresh = data["publish"]["consumer-refresh"]
    assert isinstance(refresh, str)
    return refresh


def _run(tmp_path: Path, **env: str) -> Run:
    bin_dir = stub_bin(tmp_path)
    for name in ("vrg-tooling-install", "vrg-vm"):
        write_stub(
            bin_dir,
            name,
            "#!/bin/bash\n"
            f"printf '{name}\\t%s\\n' \"$*\" >> \"$STUB_LOG\"\n"
            f'exit "${{STUB_{name.upper().replace("-", "_")}_RC:-0}}"\n',
        )
    # vrg-release runs the block under `set -eo pipefail` (handoff.run_consumer_refresh).
    script = "set -eo pipefail\n" + _template().replace("<VERSION>", _VERSION)
    return run_lines(script, tmp_path, env=env, path=[bin_dir])


def test_consumer_refresh_installs_the_exact_tag(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.of("vrg-tooling-install") == [[f"v{_VERSION}"]]
    assert run.of("vrg-vm") == [["update --all"]]


def test_a_failed_install_stops_the_refresh(tmp_path: Path) -> None:
    run = _run(tmp_path, STUB_VRG_TOOLING_INSTALL_RC="1")
    assert run.returncode != 0
    assert run.of("vrg-vm") == []


def test_consumer_refresh_has_no_inline_install_shell() -> None:
    template = _template()
    for fragment in ("trap", "mktemp", "uv tool install", "uv export", "git "):
        assert fragment not in template
    assert "raw.githubusercontent.com" not in template
    assert template.rstrip().endswith("vrg-vm update --all")


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.name)
def test_guides_document_the_install_by_hand(guide: Path) -> None:
    text = guide.read_text()
    assert _FIRST_INSTALL in text
    assert "trap '" not in text
