"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_CHECKPOINT_RE = re.compile(r"chore\(init\): step (\d+) -")


@dataclass
class RepoInitContext:
    """Mutable state carried through the wizard."""

    org: str
    name: str
    adopt: bool = False
    visibility: str = "public"
    description: str = ""
    work_dir: Path | None = None
    completed_steps: set[int] = field(default_factory=set)

    # vergil.toml fields (populated by step 3 prompts)
    repository_type: str = ""
    primary_language: str = ""
    branching_model: str = ""
    versioning_scheme: str = ""
    release_model: str = ""
    ci_versions: list[str] = field(default_factory=list)
    integration_tests: bool = False
    publish_release: bool = False
    publish_docs: bool = True
    vergil_version: str = "v2.0"
    license_type: str = "GPL-3.0"

    @property
    def repo(self) -> str:
        return f"{self.org}/{self.name}"


def detect_completed_steps(log_output: str) -> set[int]:
    """Parse git log output for checkpoint markers."""
    steps: set[int] = set()
    for line in log_output.splitlines():
        m = _CHECKPOINT_RE.search(line)
        if m:
            steps.add(int(m.group(1)))
    return steps


def prompt_choice(label: str, options: list[str], *, default: str = "") -> str:
    """Present a numbered list of options and return the chosen value."""
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")

    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"  Choice{hint}: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(options)}.")


def prompt_yes_no(label: str, *, default: bool | None = None) -> bool:
    """Prompt for a yes/no answer."""
    hint_map = {True: " [Y/n]", False: " [y/N]", None: " [y/n]"}
    hint = hint_map[default]
    while True:
        raw = input(f"{label}{hint}: ").strip().lower()
        if not raw and default is not None:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Enter y or n.")


def prompt_free_text(
    label: str,
    *,
    default: str = "",
    required: bool = True,
) -> str:
    """Prompt for free-text input."""
    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"{label}{hint}: ").strip()
        if not raw and default:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print("  This field is required.")
