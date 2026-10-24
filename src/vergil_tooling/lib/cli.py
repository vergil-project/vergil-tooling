"""The shared boundary every ``vrg-*`` console script's ``main`` runs inside.

:func:`cli_main` turns the known GitHub authentication failures into a
``<prog>: <message>`` line on stderr and exit status 1, instead of a
traceback. It replaces the import-time ``sys.excepthook`` that
``lib/github.py`` used to install (#3216): the handling is now explicit at
each command's entry point, and importing a library module no longer
mutates interpreter-global state.

A command that already catches one of these errors inside ``main`` (to add
command-specific context) keeps its own message and exit code; the decorator
only sees what ``main`` lets escape.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FunctionType


def cli_main[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Decorate a console script's ``main`` with the GitHub auth-error boundary.

    The program name is the command name, derived from the defining module's
    file (``vrg_foo_bar.py`` is ``vrg-foo-bar``, per the CLI entry-point
    naming standard), so it is right whether the command runs as an
    installed script, under ``python -m``, or in-process from a test.
    """
    code = cast("FunctionType", func).__code__
    prog = Path(code.co_filename).stem.replace("_", "-")

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            message = _auth_error_message(prog, exc)
            if message is None:
                raise
        print(message, file=sys.stderr)
        raise SystemExit(1)

    return wrapper


def _auth_error_message(prog: str, exc: Exception) -> str | None:
    """The clean message for a GitHub auth error, or ``None`` for anything else.

    ``lib.github`` is imported here, on the failure path, rather than at module
    level: commands that never talk to GitHub (notably ``vrg-hook-guard``,
    which runs on every agent tool call) must not pay its import cost just to
    be decorated. An auth error can only exist once ``lib.github`` has been
    imported, so the deferred import never changes which errors are mapped.
    """
    from vergil_tooling.lib import github

    if isinstance(exc, github.AppAuthError):
        return f"{prog}: {github.app_auth_message(exc)}"
    if isinstance(exc, github.NoInstallationError):
        return f"{prog}: {github.no_installation_message(exc)}"
    if isinstance(exc, github.MissingGitHubTokenError):
        return github.missing_token_message(prog)
    # An unresolvable epic home is not an auth error, but it carries a complete
    # one-line message and is raised from many commands' resolution paths
    # (#3603), so it gets the same clean exit.
    from vergil_tooling.lib import epics

    if isinstance(exc, epics.EpicHomeError):
        return f"{prog}: {exc}"
    return None
