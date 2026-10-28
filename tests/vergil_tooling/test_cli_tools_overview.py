"""Gate: the CLI Tools Overview lists exactly the declared console scripts.

``docs/site/docs/reference/cli-tools-overview.md`` claims to list every CLI
command this package provides. Each command is a ``### vrg-<name>`` heading.
This test cross-checks those headings against ``[project.scripts]`` in
``pyproject.toml`` in both directions, so the page cannot drift (issue #3775).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_PAGE = _REPO_ROOT / "docs" / "site" / "docs" / "reference" / "cli-tools-overview.md"
_COMMAND_HEADING = re.compile(r"^### (vrg-[a-z0-9-]+)\s*$", re.MULTILINE)


def _declared_commands() -> set[str]:
    return set(tomllib.loads(_PYPROJECT.read_text())["project"]["scripts"])


def _documented_commands() -> list[str]:
    return _COMMAND_HEADING.findall(_PAGE.read_text())


def test_page_lists_commands() -> None:
    assert _documented_commands()


def test_every_declared_command_is_documented() -> None:
    missing = sorted(_declared_commands() - set(_documented_commands()))
    assert not missing, f"{_PAGE.name} has no entry for: {', '.join(missing)}"


def test_every_documented_command_is_declared() -> None:
    unknown = sorted(set(_documented_commands()) - _declared_commands())
    assert not unknown, (
        f"{_PAGE.name} documents commands not in [project.scripts]: {', '.join(unknown)}"
    )


def test_no_command_is_documented_twice() -> None:
    documented = _documented_commands()
    duplicates = sorted({name for name in documented if documented.count(name) > 1})
    assert not duplicates, f"{_PAGE.name} documents more than once: {', '.join(duplicates)}"
