"""The draft relay: a drafted reply carried to the human over a reserved git ref.

The agent drafts the human's reply; the human posts it. The draft travels
through GitHub, never a shared filesystem, so the handoff works the same from a
Lima VM and from a cloud VM. It reuses the relay mechanism of
``vrg-pr-workflow report-ready`` (:mod:`vergil_tooling.lib.pr_workflow.github_transport`):

- :func:`push_draft` writes the reply as a **single-file commit** holding
  ``reply.md``, built with git plumbing (``hash-object`` -> ``mktree`` ->
  ``commit-tree``), and force-pushes it to
  ``refs/vergil/ruling-draft/<issue>/<request comment ID>`` on the clone's
  remote. It never runs ``git commit`` and never touches HEAD, the index or the
  working tree.
- :func:`fetch_draft` reads the draft back through the REST API (the human
  tool runs without the agent's clone), and :func:`delete_draft_api` removes
  the ref once the round is over.

The ref is world-readable, like the report-ready relay: a draft holds only
what the reply will make public anyway, and only plain text
(:mod:`.plain_text`), so it is checked before anything is pushed.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import re
import subprocess
import sys
from typing import Any

from vergil_tooling.lib import git, github
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.plain_text import check_plain_text

DRAFT_FILE = "reply.md"
_REF_NAMESPACE = "vergil/ruling-draft"
_COMMIT_MESSAGE = "vergil: ruling draft"
# The regular-file mode git uses for a blob entry in a tree.
_BLOB_MODE = "100644"

# gh ends its error line with the status, ``gh: Not Found (HTTP 404)``.
_NOT_FOUND_RE = re.compile(r"\(HTTP 404\)$", re.MULTILINE)
# GitHub answers DELETE on a ref that is not there with 422 and this message.
_NO_REFERENCE = "Reference does not exist"

# A remote URL naming a repo on github.com: ``https://`` (optionally with a
# user), ``ssh://`` (optionally with a port) or scp-style ``git@github.com:``.
_GITHUB_REPO_URL_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def draft_ref(issue: int, request_id: int) -> str:
    """The reserved ref carrying the draft reply to request ``request_id``."""
    return f"refs/{_REF_NAMESPACE}/{issue}/{request_id}"


def reply_command(repo: str, issue: int, request_id: int) -> str:
    """The human's paste-ready command that posts the drafted reply."""
    return f"vrg-ruling-reply --issue {repo}#{issue} --request {request_id}"


def _repo_of_url(url: str) -> str | None:
    match = _GITHUB_REPO_URL_RE.fullmatch(url.strip())
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def require_remote_is(repo: str, *, remote: str = "origin") -> None:
    """Refuse unless every push URL of ``remote`` names ``repo`` on github.com.

    The draft is pushed through ``remote``, so a remote pointing anywhere else
    would publish it to the wrong repo. Every push URL is checked (``git remote
    get-url --push --all``, with ``insteadOf`` rewrites applied), because a push
    goes to all of them. A refusal never echoes a URL: it could carry a token.
    """
    try:
        output = git.read_output("remote", "get-url", "--push", "--all", remote)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"cannot read the push URL of the '{remote}' remote (run vrg-ruling draft"
            f" in a clone of {repo}): git exited {exc.returncode}"
        )
        raise RulingError(msg) from exc
    urls = [line for line in output.splitlines() if line.strip()]
    if not urls:
        msg = f"the '{remote}' remote has no push URL; run vrg-ruling draft in a clone of {repo}"
        raise RulingError(msg)
    for url in urls:
        found = _repo_of_url(url)
        if found is None:
            msg = (
                f"a push URL of the '{remote}' remote is not a github.com repo;"
                f" run vrg-ruling draft in a clone of {repo}"
            )
            raise RulingError(msg)
        if found.casefold() != repo.casefold():
            msg = (
                f"the '{remote}' remote pushes to {found}, not the issue's repo {repo};"
                f" run vrg-ruling draft in a clone of {repo}"
            )
            raise RulingError(msg)


def push_draft(issue: int, request_id: int, text: str, *, remote: str = "origin") -> None:
    """Build the single-file draft commit with plumbing and force-push it to its ref.

    INVARIANT: never runs ``git commit``; never touches HEAD, the index or the
    working tree. ``text`` must be plain text, checked here before anything is
    written. The push's chatter goes to stderr, keeping stdout for the caller.
    """
    check_plain_text(text, what="the draft reply")
    try:
        blob = git.read_output_stdin(text, "hash-object", "-w", "--stdin")
        tree = git.read_output_stdin(f"{_BLOB_MODE} blob {blob}\t{DRAFT_FILE}\n", "mktree")
        commit = git.read_output("commit-tree", tree, "-m", _COMMIT_MESSAGE)
        with contextlib.redirect_stdout(sys.stderr):
            git.run("push", "--force", remote, f"{commit}:{draft_ref(issue, request_id)}")
    except subprocess.CalledProcessError as exc:
        # git's own output has already gone to stderr (read_output prints it on
        # failure, and run streams it), so it is not repeated here.
        ref = draft_ref(issue, request_id)
        msg = f"pushing the draft to {ref} failed: git exited {exc.returncode}"
        raise RulingError(msg) from exc


def _is_not_found(exc: github.GitHubAPIError) -> bool:
    return _NOT_FOUND_RE.search((exc.stderr or "").replace("\r", "")) is not None


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        msg = f"{what} is not a JSON object"
        raise RulingError(msg)
    return data


def _string(data: dict[str, Any], key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no '{key}'"
        raise RulingError(msg)
    return value


def fetch_draft(repo: str, issue: int, request_id: int) -> str | None:
    """The draft reply to request ``request_id`` on ``repo#issue``, or ``None``.

    Reads the ref, its commit, the commit's tree and the ``reply.md`` blob
    through the REST API. A ref that is not there (HTTP 404) is ``None``; a
    ref that is not a single-file draft commit, or a blob that is not UTF-8,
    raises :class:`RulingError`. Any other API failure propagates.
    """
    ref = draft_ref(issue, request_id)
    what = f"the draft ref {ref} on {repo}"
    try:
        ref_data = github.read_json("api", f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}")
    except github.GitHubAPIError as exc:
        if _is_not_found(exc):
            return None
        raise
    blob_sha = _draft_blob_sha(repo, ref, _object(ref_data, what), what)
    return _blob_text(repo, blob_sha, what)


def _api_object(repo: str, path: str, what: str) -> dict[str, Any]:
    return _object(github.read_json("api", f"repos/{repo}/git/{path}"), what)


def _draft_blob_sha(repo: str, ref: str, ref_obj: dict[str, Any], what: str) -> str:
    """The SHA of the ``reply.md`` blob the draft ref's commit holds, every shape checked."""
    if ref_obj.get("ref") != ref:
        msg = f"{what}: GitHub returned a different ref"
        raise RulingError(msg)
    target = _object(ref_obj.get("object"), f"{what} object")
    if target.get("type") != "commit":
        msg = f"{what} does not point at a commit"
        raise RulingError(msg)
    commit = _api_object(repo, f"commits/{_string(target, 'sha', what)}", f"{what} commit")
    tree_sha = _string(_object(commit.get("tree"), f"{what} commit tree"), "sha", f"{what} commit")
    tree = _api_object(repo, f"trees/{tree_sha}", f"{what} tree")
    entries = tree.get("tree")
    if tree.get("truncated") or not isinstance(entries, list) or len(entries) != 1:
        msg = f"{what} does not hold exactly one file"
        raise RulingError(msg)
    entry = _object(entries[0], f"{what} tree entry")
    shape = (entry.get("path"), entry.get("type"), entry.get("mode"))
    if shape != (DRAFT_FILE, "blob", _BLOB_MODE):
        msg = f"{what} does not hold a single {DRAFT_FILE} file"
        raise RulingError(msg)
    return _string(entry, "sha", f"{what} tree entry")


def _blob_text(repo: str, blob_sha: str, what: str) -> str:
    """The blob's content as UTF-8 text, checked against the blob's own SHA."""
    blob = _api_object(repo, f"blobs/{blob_sha}", f"{what} blob")
    content = blob.get("content")
    if blob.get("encoding") != "base64" or not isinstance(content, str):
        msg = f"{what}: the blob is not base64 content"
        raise RulingError(msg)
    try:
        raw = base64.b64decode(content.replace("\n", ""), validate=True)
    except binascii.Error as exc:
        msg = f"{what}: the blob's base64 content does not decode"
        raise RulingError(msg) from exc
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- a git
    # blob ID is SHA-1 by definition; this checks integrity, not security.
    if hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest() != blob_sha:
        msg = f"{what}: the blob's content does not match its SHA"
        raise RulingError(msg)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{what}: the draft is not UTF-8 (byte {exc.start})"
        raise RulingError(msg) from exc


def delete_draft_api(repo: str, issue: int, request_id: int) -> None:
    """Delete the draft ref of request ``request_id`` on ``repo``; absent is not an error.

    GitHub answers a DELETE on a missing ref with 404 or with 422 "Reference
    does not exist"; both mean the draft is already gone. Any other failure
    propagates.
    """
    endpoint = f"repos/{repo}/git/refs/{_REF_NAMESPACE}/{issue}/{request_id}"
    try:
        github.delete_if_exists(endpoint)
    except github.GitHubAPIError as exc:
        # ``gh api -i`` puts the status line and body on stdout, its error on stderr.
        detail = (exc.stderr or "") + (exc.stdout or "")
        if "422" in detail and _NO_REFERENCE in detail:
            return
        raise
