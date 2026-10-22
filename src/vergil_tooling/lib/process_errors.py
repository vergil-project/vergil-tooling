"""User-facing messages for a failed subprocess, built from its own fields.

``str(subprocess.CalledProcessError)`` is the standard library's format, not
ours, and it changes between Python versions (3.15 dropped the quotes around an
argv list, #3437). Every message about a failed command is therefore built here
from the exception's ``cmd``, ``returncode``, ``stderr`` and ``stdout``, never
from its default stringification.
"""

from __future__ import annotations

import subprocess


def command_text(cmd: object) -> str:
    """A CalledProcessError's ``cmd`` (a string or an argv sequence) as one string."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


def failure_message(exc: subprocess.CalledProcessError) -> str:
    """One line naming the failed command and how it ended, without its output."""
    command = command_text(exc.cmd)
    if exc.returncode < 0:
        return f"Command '{command}' died with signal {-exc.returncode}."
    return f"Command '{command}' returned non-zero exit status {exc.returncode}."


def _stream_text(stream: str | bytes | None) -> str:
    text = stream.decode(errors="replace") if isinstance(stream, bytes) else stream
    return (text or "").strip()


def failure_detail(exc: subprocess.CalledProcessError) -> str:
    """:func:`failure_message` followed by the labelled stderr and stdout, if any."""
    lines = [failure_message(exc)]
    for label, stream in (("stderr", exc.stderr), ("stdout", exc.stdout)):
        text = _stream_text(stream)
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def error_text(exc: BaseException, *, output: bool = True) -> str:
    """The text to show for a caught *exc* of any type.

    A ``CalledProcessError`` gets :func:`failure_detail` (or, with *output*
    false because the caller shows the output itself, :func:`failure_message`)
    unless its class defines its own ``__str__`` (``GitHubAPIError``,
    ``KnownResourceNotFoundError``): that message is ours and already says what
    failed. Any other exception is its own ``str``.
    """
    if (
        isinstance(exc, subprocess.CalledProcessError)
        and type(exc).__str__ is subprocess.CalledProcessError.__str__
    ):
        return failure_detail(exc) if output else failure_message(exc)
    return str(exc)
