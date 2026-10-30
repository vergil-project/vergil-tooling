"""Rolling-tag management: move vX.Y to an existing release vX.Y.Z.

Moving a rolling tag is a human maintainer action. :func:`plan_promotion`
refuses any agent identity, fail-closed, before it runs a single git command,
so every caller (the ``vrg-promote`` CLI and ``vrg-release``'s promote stage)
refuses alike.

The target of a move is checked: the release tag must exist on origin, its
commit must be on ``origin/main`` (the primary guard, which limits the target
to merged work), and it must have a published GitHub Release (an additional
layer). The push is a compare-and-swap (``--force-with-lease`` on the rolling
tag's raw value on origin), so the tag that is overwritten is exactly the one
that was planned, or the push is refused.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

from vergil_tooling.lib import github, identity_mode, retry
from vergil_tooling.lib.process_errors import error_text

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_ORIGIN_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
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


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command without raising; the caller judges the exit code."""
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )


def _git_read(*args: str) -> str:
    """Stripped stdout of a git command; :class:`PromoteError` if it fails."""
    result = _git(*args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        msg = f"git {' '.join(args)} failed (exit {result.returncode})"
        raise PromoteError(f"{msg}: {detail}" if detail else msg)
    return result.stdout.strip()


def _git_checked(*args: str) -> None:
    """Run a mutating git command, echoing its stderr to ours if it fails."""
    try:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


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


def _origin_repo() -> str:
    """origin's ``owner/repo``, from ``git remote get-url origin``."""
    url = _git_read("remote", "get-url", "origin")
    m = _ORIGIN_RE.match(url)
    if not m:
        msg = f"origin ({url}) is not a GitHub repository URL"
        raise PromoteError(msg)
    return m.group("repo")


def _require_on_main(release_tag: str, commit: str) -> None:
    result = _git("merge-base", "--is-ancestor", commit, "origin/main")
    if result.returncode == 1:
        msg = f"{release_tag} ({commit[:_SHORT]}) is not on origin/main"
        raise PromoteError(msg)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        msg = f"cannot check whether {release_tag} is on origin/main: {detail}"
        raise PromoteError(msg)


def _require_published_release(repo: str, release_tag: str) -> None:
    """Refuse unless *repo* has a published (non-draft) GitHub Release of *release_tag*.

    The read runs under :func:`retry.retry_known_resource`, as
    ``release/confirm.py`` reads the same Release (#3137): in ``vrg-release``'s
    promote stage the Release was just published, so a 404 may only mean "not
    readable yet".
    """
    try:
        is_draft = retry.retry_known_resource(
            lambda: github.read_output(
                "release",
                "view",
                release_tag,
                "--repo",
                repo,
                "--json",
                "isDraft",
                "--jq",
                ".isDraft",
            ),
            resource=f"GitHub Release {release_tag}",
        )
    except subprocess.CalledProcessError as exc:
        msg = f"{release_tag} has no published GitHub Release on {repo}: {error_text(exc)}"
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


def plan_promotion(version: str) -> Promotion:
    """Resolve and check a move of the rolling tag to release *version*.

    Refuses an agent identity before any git call. Raises
    :class:`PromoteError` when the release tag is absent on origin, is not on
    ``origin/main``, or has no published GitHub Release.
    """
    if identity_mode.is_agent() or identity_mode.any_agent_signal():
        msg = "moving a rolling tag is a human maintainer action; agents cannot promote"
        raise PromoteError(msg)

    rolling_tag, release_tag = _tags_for(version)
    repo = _origin_repo()
    _git_read("fetch", "--tags", "--force", "origin", "main")

    _, new_commit = _remote_tag(release_tag)
    if not new_commit:
        msg = f"{release_tag} does not exist on origin ({repo})"
        raise PromoteError(msg)
    old_ref, old_commit = _remote_tag(rolling_tag)

    _require_on_main(release_tag, new_commit)
    _require_published_release(repo, release_tag)
    return Promotion(repo, rolling_tag, release_tag, old_ref, old_commit, new_commit)


def describe(p: Promotion) -> str:
    """The origin repo and the tag move, as printed before a move."""
    old = p.old_commit[:_SHORT] or "(new)"
    move = f"{p.rolling_tag} {old} -> {p.release_tag} {p.new_commit[:_SHORT]}"
    return f"origin: {p.origin_repo}\n{move}"


def already_promoted_message(p: Promotion) -> str:
    return f"{p.rolling_tag} already points at {p.release_tag} — already promoted."


def promote_confirmed(confirmed: Promotion) -> None:
    """Move the rolling tag exactly as *confirmed*, or refuse.

    Re-resolves the plan and refuses if anything changed since it was
    confirmed. The push leases on the confirmed raw value of the rolling tag
    (empty for "must not exist"), so origin applies it as a compare-and-swap.
    """
    fresh = plan_promotion(confirmed.release_tag)
    if fresh != confirmed:
        msg = (
            f"the move of {confirmed.rolling_tag} changed since it was confirmed:\n"
            f"confirmed: {describe(confirmed)}\nnow:       {describe(fresh)}"
        )
        raise PromoteError(msg)

    rolling, release = confirmed.rolling_tag, confirmed.release_tag
    print(f"Force-updating {rolling} -> {release}")
    _git_checked("tag", "-f", rolling, release)
    print(f"Pushing {rolling} to origin")
    _git_checked(
        "push",
        "origin",
        rolling,
        f"--force-with-lease=refs/tags/{rolling}:{confirmed.old_ref}",
    )
    print(f"Promoted: {rolling} -> {release}")


def promote(version: str, *, dry_run: bool = False) -> None:
    """Move the vX.Y rolling tag to vX.Y.Z, without prompting."""
    p = plan_promotion(version)
    if p.old_commit == p.new_commit:
        print(already_promoted_message(p))
        return
    if dry_run:
        print(describe(p))
        return
    promote_confirmed(p)
