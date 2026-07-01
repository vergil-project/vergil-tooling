"""Gate: every human-facing ``vrg-*`` console script answers ``--help``.

Part of epic vergil-project/.github#72 ("Every vrg-* tool answers --help,
enforced"). See docs/site/docs/standards/development/cli-help-convention.md.

The gate runs each *covered* console script with ``--help`` in a subprocess and
asserts it exits 0 with non-empty output. ``--help`` short-circuits before any
of a tool's real work, so the check is hermetic. Tools that do not yet answer
``--help`` are listed in ``KNOWN_GAPS`` and are never executed; each is fixed by
a later task of the epic, and ``test_gap_set_matches_reality`` forces that list
to stay in sync with the source.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# vrg-hook-guard is a Claude Code PreToolUse hook: the hook system invokes it
# with a JSON event on stdin, never a human at a prompt, so it has no --help
# contract.
EXEMPT = frozenset({"vrg-hook-guard"})

# Tools that do not yet answer --help. Each is fixed by a later task of epic
# vergil-project/.github#72; removing a tool from this set is part of its fix.
# When the set is empty the gate is fully enforcing. Do NOT add a new tool here
# to silence the gate — give the tool a --help instead.
KNOWN_GAPS = frozenset(
    {
        "vrg-container-docs",
        "vrg-gh",
        "vrg-git",
    }
)


def _scripts() -> dict[str, str]:
    """Map console-script name -> ``module:attr`` entry from pyproject."""
    data = tomllib.loads(_PYPROJECT.read_text())
    return dict(data["project"]["scripts"])


def _module_of(entry: str) -> str:
    # "vergil_tooling.bin.vrg_foo:main" -> "vergil_tooling.bin.vrg_foo"
    return entry.split(":", 1)[0]


def _declares_help(module: str) -> bool:
    """True if the module builds an argparse parser or handles --help itself."""
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None, module
    src = Path(spec.origin).read_text()
    return "ArgumentParser" in src or "--help" in src


_SCRIPTS = _scripts()
_COVERED = sorted(n for n in _SCRIPTS if n not in EXEMPT and n not in KNOWN_GAPS)


def test_exempt_and_known_gaps_are_real_scripts() -> None:
    """EXEMPT and KNOWN_GAPS may only name scripts that actually exist."""
    names = set(_SCRIPTS)
    assert names >= EXEMPT, f"stale EXEMPT entries: {sorted(EXEMPT - names)}"
    assert names >= KNOWN_GAPS, f"stale KNOWN_GAPS entries: {sorted(KNOWN_GAPS - names)}"


def test_gap_set_matches_reality() -> None:
    """Documented gaps must equal the tools that actually lack ``--help``.

    Fix a tool (add argparse or --help handling) and it leaves the real gap set
    — remove it from KNOWN_GAPS. Add a tool without help and it enters the real
    gap set — give it a --help, or (for a non-human hook) add it to EXEMPT.
    """
    actual = {
        n for n in _SCRIPTS if n not in EXEMPT and not _declares_help(_module_of(_SCRIPTS[n]))
    }
    assert actual == set(KNOWN_GAPS), (
        "help-gap set drifted from source; symmetric difference: "
        f"{sorted(actual ^ set(KNOWN_GAPS))}"
    )


@pytest.mark.parametrize("name", _COVERED)
def test_tool_answers_help(name: str) -> None:
    """Every covered tool exits 0 and prints something for ``--help``."""
    module = _module_of(_SCRIPTS[name])
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"{name} (--help) exited {result.returncode}; stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"{name} (--help) produced no stdout"
