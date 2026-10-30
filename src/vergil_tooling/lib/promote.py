"""Rolling-tag management: move vX.Y to an existing release vX.Y.Z.

Moving a rolling tag is a human maintainer action. :func:`plan_promotion`
refuses any agent identity, fail-closed, before it runs a single git command,
so every caller (the ``vrg-promote`` CLI and ``vrg-release``'s promote stage)
refuses alike.

The target of a move is checked: the release tag must exist on origin, its
commit must be on origin's ``main`` (the primary guard, which limits the
target to merged work), and it must have a published GitHub Release (an
additional layer). Every check, and the push, act on the one repo origin's
fetch and push URLs both name.

The push sends the release tag that was checked (verified locally against
the commit origin reported) straight to the rolling tag's ref, as a
compare-and-swap (``--force-with-lease`` on the rolling tag's raw value on
origin). The tag that is overwritten is exactly the one that was planned, or
the push is refused.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from vergil_tooling.lib import git, github, identity_mode, retry
from vergil_tooling.lib.process_errors import error_text

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
# A github.com remote URL, in the forms git accepts: https (optionally with a
# user), ssh:// (any user, optional port) or scp-style ``user@github.com:``.
_ORIGIN_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/"
    r"|ssh://(?:[^@/]+@)?github\.com(?::\d+)?/"
    r"|[^@/:]+@github\.com:)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_MAIN_REF = "refs/remotes/origin/main"
_SHORT = 12


class PromoteError(ValueError):
    """A promotion was refused, or its plan could not be resolved."""


@dataclass(frozen=True)
class Promotion:
    """A resolved rolling-tag move.

    ``old_ref`` is the rolling tag's raw value on origin (for an annotated tag,
    the tag object's id) and is used only for the push lease. ``old_commit`` is
    the commit it peels to. Both are ``""`` when the rolling tag does not exist
    yet. ``new_commit`` is the commit the release tag peels to.
    """

    origin_repo: str
    rolling_tag: str
    release_tag: str
    old_ref: str
    old_commit: str
    new_commit: str


def _git_read(*args: str) -> str:
    """``git.read_output``, with a failure raised as :class:`PromoteError`.

    ``git.read_output`` already echoes the command's stderr, so the message
    names the command and its exit status only.
    """
    try:
        return git.read_output(*args)
    except subprocess.CalledProcessError as exc:
        raise PromoteError(error_text(exc, output=False)) from exc


def _remote_tag(tag: str) -> tuple[str, str]:
    """``(raw, peeled)`` for origin's *tag*, or ``("", "")`` if it is absent.

    *raw* is the ``refs/tags/<tag>`` line (an annotated tag's object id), and
    *peeled* the commit it resolves to (the ``^{}`` line, or *raw* itself for a
    lightweight tag). A failed ``ls-remote`` raises rather than reading as an
    absent tag.
    """
    raw = peeled = ""
    for line in _git_read("ls-remote", "origin", f"refs/tags/{tag}").splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            raw = sha
    return raw, peeled or raw


def _peeled_commit(tag: str) -> str:
    """The commit origin's *tag* resolves to, or '' if the tag is absent."""
    return _remote_tag(tag)[1]


def _repo_of(url: str, which: str) -> str:
    """``owner/repo`` of a github.com remote *url*.

    The refusal never echoes *url*: a remote URL can carry credentials.
    """
    m = _ORIGIN_RE.match(url.strip())
    if not m:
        msg = (
            f"origin's {which} URL is not a GitHub repository URL "
            "(expected https://github.com/<owner>/<repo> or git@github.com:<owner>/<repo>)"
        )
        raise PromoteError(msg)
    return m.group("repo")


def _origin_repo() -> str:
    """origin's ``owner/repo``, refusing unless its push URLs name the same repo.

    ``ls-remote`` and ``fetch`` use the fetch URL, and ``push`` the push
    URL(s), so a push URL that names another repo would move a tag that none
    of the checks looked at.
    """
    repo = _repo_of(_git_read("remote", "get-url", "origin"), "fetch")
    push_urls = _git_read("remote", "get-url", "--push", "--all", "origin").splitlines()
    push_repos = {_repo_of(u, "push").lower() for u in push_urls if u.strip()}
    if push_repos != {repo.lower()}:
        msg = (
            f"origin's push URL does not name the repo its fetch URL names ({repo}); "
            "refusing to push to a repo the checks did not read"
        )
        raise PromoteError(msg)
    return repo


def _fetch_main_and_tags() -> None:
    _git_read("fetch", "--tags", "--force", "origin", f"+refs/heads/main:{_MAIN_REF}")


def _require_on_main(release_tag: str, commit: str) -> None:
    try:
        on_main = git.is_ancestor(commit, _MAIN_REF)
    except subprocess.CalledProcessError as exc:
        msg = f"cannot check whether {release_tag} is on origin/main: {error_text(exc)}"
        raise PromoteError(msg) from exc
    if not on_main:
        msg = f"{release_tag} ({commit[:_SHORT]}) is not on origin/main"
        raise PromoteError(msg)


def _read_release_draft(repo: str, release_tag: str, *, known_published: bool) -> str:
    def read() -> str:
        return github.read_output(
            "release", "view", release_tag, "--repo", repo, "--json", "isDraft", "--jq", ".isDraft"
        )

    if known_published:
        return retry.retry_known_resource(read, resource=f"GitHub Release {release_tag}")
    return read()


def _require_published_release(repo: str, release_tag: str, *, known_published: bool) -> None:
    """Refuse unless *repo* has a published (non-draft) GitHub Release of *release_tag*.

    With *known_published* (``vrg-release``'s promote stage, which runs just
    after the CD release job published it) the read runs under
    :func:`retry.retry_known_resource`, as ``release/confirm.py`` reads the
    same Release (#3137), so a transient 404 does not fail the stage. A
    version a human typed is read once, so a missing Release fails fast.
    """
    try:
        is_draft = _read_release_draft(repo, release_tag, known_published=known_published)
    except subprocess.CalledProcessError as exc:
        if retry.is_not_found(exc):
            msg = f"{release_tag} has no published GitHub Release on {repo}: {error_text(exc)}"
        else:
            msg = f"cannot check the GitHub Release of {release_tag} on {repo}: {error_text(exc)}"
        raise PromoteError(msg) from exc
    if is_draft.strip() != "false":
        msg = f"{release_tag} has no published GitHub Release on {repo} (draft: {is_draft})"
        raise PromoteError(msg)


def _tags_for(version: str) -> tuple[str, str]:
    m = _VERSION_RE.match(version)
    if not m:
        msg = f"'{version}' is not valid semver (expected X.Y.Z or vX.Y.Z)"
        raise PromoteError(msg)
    bare = m.group(1)
    major, minor, _ = bare.split(".")
    return f"v{major}.{minor}", f"v{bare}"


def plan_promotion(version: str, *, release_just_published: bool = False) -> Promotion:
    """Resolve and check a move of the rolling tag to release *version*.

    Refuses an agent identity before any git call. Raises
    :class:`PromoteError` when the release tag is absent on origin, is not on
    origin's ``main``, or has no published GitHub Release.
    *release_just_published* retries a 404 on the Release read (see
    :func:`_require_published_release`).
    """
    if identity_mode.is_agent() or identity_mode.any_agent_signal():
        msg = "moving a rolling tag is a human maintainer action; agents cannot promote"
        raise PromoteError(msg)

    rolling_tag, release_tag = _tags_for(version)
    repo = _origin_repo()
    _fetch_main_and_tags()

    _, new_commit = _remote_tag(release_tag)
    if not new_commit:
        msg = f"{release_tag} does not exist on origin ({repo})"
        raise PromoteError(msg)
    old_ref, old_commit = _remote_tag(rolling_tag)

    _require_on_main(release_tag, new_commit)
    _require_published_release(repo, release_tag, known_published=release_just_published)
    return Promotion(repo, rolling_tag, release_tag, old_ref, old_commit, new_commit)


def describe(p: Promotion) -> str:
    """The origin repo and the tag move, as printed before a move."""
    old = p.old_commit[:_SHORT] or "(new)"
    move = f"{p.rolling_tag} {old} -> {p.release_tag} {p.new_commit[:_SHORT]}"
    return f"origin: {p.origin_repo}\n{move}"


def _changed_message(confirmed: Promotion, fresh: Promotion) -> str:
    lines = [
        f"the move of {confirmed.rolling_tag} changed since it was confirmed:",
        f"confirmed: {describe(confirmed)}",
        f"now:       {describe(fresh)}",
    ]
    if confirmed.old_ref != fresh.old_ref:
        lines.append(
            f"{confirmed.rolling_tag} on origin was {confirmed.old_ref or '(absent)'}, "
            f"now {fresh.old_ref or '(absent)'}"
        )
    return "\n".join(lines)


def _require_local_release(confirmed: Promotion) -> None:
    """Refuse unless the local release tag peels to the confirmed commit."""
    ref = f"refs/tags/{confirmed.release_tag}"
    local = _git_read("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if local != confirmed.new_commit:
        msg = (
            f"local {ref} ({local[:_SHORT]}) is not the confirmed commit "
            f"({confirmed.new_commit[:_SHORT]}); refusing to push it"
        )
        raise PromoteError(msg)


def promote_confirmed(confirmed: Promotion, *, release_just_published: bool = False) -> None:
    """Move the rolling tag exactly as *confirmed*, or refuse.

    Re-resolves the plan and refuses if anything changed since it was
    confirmed, then checks that the local release tag is the confirmed
    commit. The push sends that tag to the rolling tag's ref, leasing on the
    confirmed raw value (empty for "must not exist"), so origin applies it as
    a compare-and-swap. The local rolling tag is updated only after origin
    accepted the move.
    """
    fresh = plan_promotion(confirmed.release_tag, release_just_published=release_just_published)
    if fresh != confirmed:
        raise PromoteError(_changed_message(confirmed, fresh))
    _require_local_release(confirmed)

    rolling, release = confirmed.rolling_tag, confirmed.release_tag
    print(f"Pushing {rolling} -> {release} to origin")
    try:
        git.read_output(
            "push",
            "origin",
            f"refs/tags/{release}:refs/tags/{rolling}",
            f"--force-with-lease=refs/tags/{rolling}:{confirmed.old_ref}",
        )
    except subprocess.CalledProcessError as exc:
        expected = confirmed.old_ref or "absent"
        msg = (
            f"origin did not accept the move of {rolling} "
            f"(the push required origin's {rolling} to be {expected}): "
            f"{error_text(exc, output=False)}"
        )
        raise PromoteError(msg) from exc
    print(f"Promoted: {rolling} -> {release}")
    try:
        git.read_output("tag", "-f", rolling, f"refs/tags/{release}")
    except subprocess.CalledProcessError as exc:
        msg = (
            f"{rolling} moved to {release} on origin, but updating the local {rolling} "
            f"tag failed ({error_text(exc, output=False)}); run "
            "`vrg-git fetch --tags --force origin` to refresh it"
        )
        raise PromoteError(msg) from exc


def promote(version: str, *, dry_run: bool = False) -> None:
    """Move the vX.Y rolling tag to vX.Y.Z, without prompting.

    The ``vrg-release`` promote stage calls this right after the release was
    published, so the Release read retries a 404.
    """
    p = plan_promotion(version, release_just_published=True)
    if p.old_commit == p.new_commit:
        print(f"{p.rolling_tag} already points at {p.release_tag} — already promoted.")
        return
    if dry_run:
        print(describe(p))
        return
    promote_confirmed(p, release_just_published=True)
