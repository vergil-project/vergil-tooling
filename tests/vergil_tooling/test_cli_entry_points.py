"""Gate: every console script's module is named after its command.

See docs/site/docs/standards/development/python/cli-entry-points.md. The
module for ``vrg-foo-bar`` is ``vergil_tooling.bin.vrg_foo_bar`` and its entry
point is ``main``. In every ``bin/`` module that defines ``main``, ``main`` is
the first top-level function, so the module reads top-down from its entry point.
Every console script's ``main`` is decorated with
:func:`vergil_tooling.lib.cli.cli_main`, so GitHub auth errors exit cleanly.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from vergil_tooling.lib.cli import cli_main

if TYPE_CHECKING:
    from types import FunctionType

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_SCRIPTS: dict[str, str] = dict(tomllib.loads(_PYPROJECT.read_text())["project"]["scripts"])


def test_scripts_are_declared() -> None:
    assert _SCRIPTS


@pytest.mark.parametrize("command", sorted(_SCRIPTS))
def test_module_name_matches_command(command: str) -> None:
    expected = f"vergil_tooling.bin.{command.replace('-', '_')}:main"
    assert _SCRIPTS[command] == expected


_BIN_DIR = Path(__file__).resolve().parents[2] / "src" / "vergil_tooling" / "bin"
_BIN_MODULES_WITH_MAIN = sorted(
    path
    for path in _BIN_DIR.glob("*.py")
    if any(
        isinstance(node, ast.FunctionDef) and node.name == "main"
        for node in ast.parse(path.read_text()).body
    )
)


def test_bin_modules_with_main_are_found() -> None:
    assert _BIN_MODULES_WITH_MAIN


@pytest.mark.parametrize("path", _BIN_MODULES_WITH_MAIN, ids=lambda p: p.name)
def test_main_is_first_function(path: Path) -> None:
    tree = ast.parse(path.read_text())
    functions = [
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert functions[0] == "main"


@pytest.mark.parametrize("command", sorted(_SCRIPTS))
def test_main_runs_inside_the_cli_boundary(command: str) -> None:
    # Every console script maps GitHub auth errors to a clean message (#3216):
    # main must be decorated with lib.cli.cli_main, not a look-alike.
    module = importlib.import_module(_SCRIPTS[command].split(":")[0])
    probe = cast("FunctionType", cli_main(lambda: None))
    assert module.main.__code__ is probe.__code__
    assert module.main.__wrapped__.__module__ == module.__name__
