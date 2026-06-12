"""Shared ``--yes`` confirmation helper (issue #1644).

A single, consistent definition of the ``--yes`` flag and the yes/no
gate it pre-answers, so every tool that asks "are you sure?" behaves
the same way.

``--yes`` is deliberately narrow:

- It auto-answers a *single, unambiguous* yes/no confirmation.
- It does **not** pick between multiple choices — disambiguation
  prompts (e.g. "which of these PRs?") are still shown.
- It does **not** override a safety gate. Tools keep their own
  ``--force`` / ``--allow-*`` flags for pushing past code-level
  guardrails; ``--yes`` only skips the courtesy "are you sure?".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

YES_FLAG_HELP = (
    "Auto-answer the confirmation prompt with 'yes'. Only bypasses a "
    "single, unambiguous yes/no confirmation; prompts that disambiguate "
    "between multiple choices are still shown, and safety gates are not "
    "overridden (use the relevant --force/--allow-* flag for those)."
)


def add_yes_argument(parser: argparse.ArgumentParser) -> None:
    """Register the standard ``--yes`` / ``-y`` flag on *parser*."""
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=YES_FLAG_HELP,
    )


def confirm(prompt: str, *, assume_yes: bool, default: bool = False) -> bool:
    """Ask *prompt* as a yes/no question; return ``True`` to proceed.

    With *assume_yes* (``--yes``), return ``True`` without reading stdin,
    echoing the auto-answer so the transcript still records the decision.
    Empty input returns *default*. EOF / interrupt is treated as a
    decline (returns ``False``), leaving the caller to print its own
    "Aborted." message.
    """
    hint = "[Y/n]" if default else "[y/N]"
    if assume_yes:
        print(f"{prompt} {hint} y  (--yes)")
        return True
    while True:
        try:
            raw = input(f"{prompt} {hint} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Enter y or n.")
