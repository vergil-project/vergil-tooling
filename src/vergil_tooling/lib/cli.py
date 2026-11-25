"""The shared boundary every ``vrg-*`` console script's ``main`` runs inside.

:func:`cli_main` turns the known GitHub authentication failures into a
``<prog>: <message>`` line on stderr and exit status 1, instead of a
traceback. It replaces the import-time ``sys.excepthook`` that
``lib/github.py`` used to install (#3216): the handling is now explicit at
each command's entry point, and importing a library module no longer
mutates interpreter-global state.

It does the same for the trust layer's failures (an untrusted-content stop,
a trust read failure, a missing trusted-authors configuration), whose
messages are metadata only, for a gh the tools cannot use (missing,
unreadable, or older than the minimum, #5046), and for every refusal to
write to an epic (``epics.EpicRefusalError``, a closed epic for one).

A command that already catches one of these errors inside ``main`` (to add
command-specific context) keeps its own message and exit code; the decorator
only sees what ``main`` lets escape.
"""

from __future__ import annotations

import functools
import os
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
    # (#3603), so it gets the same clean exit. So does a standing epic created
    # but not confirmed listed: its message names the new epic to check before
    # retrying, and every command that can create one lets it reach here (#3820).
    # So does every refusal to write to an epic (a closed epic, say): each is an
    # EpicRefusalError with a complete message (epic vergil-project/.github#1609).
    from vergil_tooling.lib import epics

    if isinstance(
        exc, (epics.EpicHomeError, epics.StandingEpicNotVisibleError, epics.EpicRefusalError)
    ):
        return f"{prog}: {exc}"
    # A gh that is missing, unreadable or older than the minimum is refused
    # before any gh call (github.require_gh, #5046); its message names the
    # version required, as vrg-gh's own refusal does.
    from vergil_tooling.lib import gh_version

    if isinstance(exc, gh_version.GhVersionError):
        return f"{prog}: {exc}"
    return _trust_error_message(prog, exc)


def _trust_error_message(prog: str, exc: Exception) -> str | None:
    """The clean message for a trust-layer failure, or ``None`` for anything else.

    A stop on untrusted content, a read the trust layer could not complete,
    and a missing trusted-authors configuration each carry a one-line,
    metadata-only message (epic vergil-project/.github#1467), so a command
    that lets one escape exits with it rather than a traceback. Under GitHub
    Actions, whose logs are public, a stop names only its cause, never the
    login (spec §5.3).
    """
    from vergil_tooling.lib import trust
    from vergil_tooling.lib.trusted_authors import (
        GITHUB_ACTIONS_ENV,
        TrustedAuthorsNotConfiguredError,
    )

    if isinstance(exc, trust.UntrustedContentError):
        if os.environ.get(GITHUB_ACTIONS_ENV) == "true":
            return f"{prog}: stopped before any write on untrusted content ({exc.item.cause})"
        return f"{prog}: {exc}"
    if isinstance(exc, (trust.TrustReadError, TrustedAuthorsNotConfiguredError)):
        return f"{prog}: {exc}"
    return None
