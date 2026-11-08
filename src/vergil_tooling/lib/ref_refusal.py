"""Explain a bare 422 from GitHub's REST refs API as a likely ruleset refusal.

When a repository ruleset blocks a ref write made through the REST refs API
(``POST``, ``PATCH`` or ``DELETE`` on ``repos/{owner}/{repo}/git/refs…``),
GitHub answers only ``422 Reference update failed``, with no rule name or
reason. The same write done as a ``git push`` prints the rule text (``GH013:
Repository rule violations found for refs/heads/v0.0. - Cannot create ref due
to creations being restricted.``), vergil-tooling#3997.

The 422 is generic: a stale ``sha`` or a non-fast-forward update gives the same
message, so :func:`ruleset_hint` says *likely*, names the ref and the repo, and
tells the operator how to see the rules. It never changes what failed; the
error is still raised.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

# GitHub's whole message for the refusal, in the JSON body gh puts on stdout
# and in gh's own ``gh: Reference update failed (HTTP 422)`` line on stderr.
_REFERENCE_UPDATE_FAILED = "reference update failed"

# A REST refs endpoint: the collection (a create names the ref in its body) or
# one ref under it. ``git/ref/`` (singular) is the read-only lookup.
_REFS_ENDPOINT_RE = re.compile(r"^/?repos/([^/]+/[^/]+)/git/refs(?:/(.+))?$")

_OPERATIONS = {"POST": "creation", "PATCH": "update", "DELETE": "deletion"}


def _stream_text(stream: str | bytes | None) -> str:
    return stream.decode(errors="replace") if isinstance(stream, bytes) else (stream or "")


def _refs_write(cmd: object) -> tuple[str, str | None, str] | None:
    """The repo, ref path and operation of a ``gh api`` refs write, or ``None``."""
    if not isinstance(cmd, (list, tuple)):
        return None
    args = [str(part) for part in cmd]
    if args[:2] != ["gh", "api"]:
        return None
    match = next((m for arg in args[2:] if (m := _REFS_ENDPOINT_RE.match(arg))), None)
    if match is None:
        return None
    method = "GET"
    for flag, value in zip(args, args[1:], strict=False):
        if flag in ("-X", "--method"):
            method = value.upper()
    operation = _OPERATIONS.get(method)
    if operation is None:
        return None
    return match.group(1), match.group(2), operation


def ruleset_hint(exc: subprocess.CalledProcessError) -> str | None:
    """A note on a REST refs write refused with a bare 422, or ``None`` for any other failure."""
    found = _refs_write(exc.cmd)
    if found is None:
        return None
    detail = (_stream_text(exc.stderr) + _stream_text(exc.stdout)).lower()
    if "422" not in detail or _REFERENCE_UPDATE_FAILED not in detail:
        return None
    repo, path, operation = found
    ref = f"refs/{urllib.parse.unquote(path)}" if path else "the ref named in the request"
    lines = [
        f"Likely cause: a repository ruleset blocks the {operation} of {ref} in {repo}.",
        "GitHub's REST refs API reports a ruleset refusal as a bare 422 'Reference update "
        "failed' with no rule text (vergil-tooling#3997); a git push of the same ref prints it.",
    ]
    if path and path.startswith("heads/"):
        lines.append(
            f"See the rules on that branch: gh api repos/{repo}/rules/branches/"
            f"{path.removeprefix('heads/')}"
        )
    lines.append(
        f"See the repo's rulesets: gh api repos/{repo}/rulesets, "
        f"or compare them with the desired set: vrg-github-repo-config audit --repo {repo}"
    )
    return "\n".join(lines)
