"""Gate: every console script's module is named after its command.

See docs/site/docs/standards/development/python/cli-entry-points.md. The
module for ``vrg-foo-bar`` is ``vergil_tooling.bin.vrg_foo_bar`` and its entry
point is ``main``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_SCRIPTS: dict[str, str] = dict(tomllib.loads(_PYPROJECT.read_text())["project"]["scripts"])


def test_scripts_are_declared() -> None:
    assert _SCRIPTS


@pytest.mark.parametrize("command", sorted(_SCRIPTS))
def test_module_name_matches_command(command: str) -> None:
    expected = f"vergil_tooling.bin.{command.replace('-', '_')}:main"
    assert _SCRIPTS[command] == expected
