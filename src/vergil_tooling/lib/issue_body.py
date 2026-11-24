"""Read, write and edit an issue body line by line, keeping its line endings.

The commands that rewrite one marked region of an issue body
(``vrg-epic-docs``, ``vrg-checkpoint``) share this module: the body is read
only through the trust layer (:func:`read_body`), split into lines that keep
their endings (:func:`split_lines`), so a ``\\r\\n`` body (GitHub's web editor
saves those) keeps every byte outside the region, and written back whole
(:func:`write_body`). A command that records its edit in the issue's log
posts the record with :func:`post_comment`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import github, trust

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors


def split_lines(body: str) -> list[str]:
    """*body*'s lines, each with its line ending.

    Only ``\\n`` (with an optional ``\\r`` before it) ends a line, as in
    Markdown; :meth:`str.splitlines` would also split on a form feed, U+2028
    and the like, which render inside one line.
    """
    parts = body.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def line_content(line: str) -> str:
    """*line* without its line ending."""
    return line.rstrip("\r\n")


def line_ending(body: str) -> str:
    """The body's line ending: that of its first line break, ``\\n`` by default."""
    index = body.find("\n")
    return "\r\n" if index > 0 and body[index - 1] == "\r" else "\n"


def _issue_endpoint(owner: str, repo: str, number: int) -> str:
    return f"repos/{owner}/{repo}/issues/{number}"


def read_body(owner: str, repo: str, number: int, *, authors: TrustedAuthors | None = None) -> str:
    """The body of issue *owner*/*repo*#*number*, read only when the trust layer trusts it.

    A command that parses the body and writes it back reads it through
    :func:`vergil_tooling.lib.trust.read_issue` with ``require_body=True``
    (spec §5.2 of epic vergil-project/.github#1467): a body whose author or
    an editor holds no role raises
    :class:`~vergil_tooling.lib.trust.UntrustedContentError` (metadata only),
    and a read the layer cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`. An issue with no body
    gives ``""``. *authors* defaults to the loaded configuration, whose
    errors propagate.
    """
    content = trust.read_issue(f"{owner}/{repo}", number, require_body=True, authors=authors)
    return content.body or ""


def write_body(owner: str, repo: str, number: int, body: str) -> None:
    """Replace the body of issue *owner*/*repo*#*number* with *body*."""
    github.write_json("PATCH", _issue_endpoint(owner, repo, number), {"body": body})


def post_comment(owner: str, repo: str, number: int, body: str) -> None:
    """Post *body* as a comment on issue *owner*/*repo*#*number*, exactly once.

    A comment is a create: a failure may hide one that landed, so the POST is
    never retried (:func:`vergil_tooling.lib.github.post_json_once`), and a
    failure raises :class:`~vergil_tooling.lib.github.GitHubAPIError` for the
    caller to report.
    """
    github.post_json_once(f"{_issue_endpoint(owner, repo, number)}/comments", {"body": body})
