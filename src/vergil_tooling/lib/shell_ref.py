"""Issue refs as paste-ready shell words.

A tool that prints a command for the human to paste writes every issue ref in
it with :func:`quoted_issue_ref` (or :func:`quoted_slug` for an
:class:`~vergil_tooling.lib.epics.IssueRef`). Unquoted, the ``#`` in
``owner/repo#N`` is a glob operator in zsh with ``EXTENDED_GLOB`` set, so the
pasted command fails with ``no matches found`` (#3962, #3966). Prose and status
lines that only mention a ref are not commands and keep the bare form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.epics import IssueRef


def quoted_issue_ref(repo: str, issue: int) -> str:
    """The issue ref as a paste-ready shell word, ``'owner/repo#N'``.

    The ref's grammar admits no single quote, so plain single quotes are safe.
    """
    return f"'{repo}#{issue}'"


def quoted_slug(ref: IssueRef) -> str:
    """*ref*'s ``owner/repo#N`` slug as a paste-ready shell word."""
    return quoted_issue_ref(f"{ref.owner}/{ref.repo}", ref.number)
