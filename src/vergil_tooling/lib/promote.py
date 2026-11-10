"""Rolling-tag management: move vX.Y to an existing release vX.Y.Z.

Moving a rolling tag is a human maintainer action. :func:`plan_promotion`
refuses any agent identity before it runs a single git command (any agent
signal refuses, whatever else is set), so every caller (the ``vrg-promote``
CLI and ``vrg-release``'s promote stage) refuses alike. The refusal stops an
agent from running it by mistake; it is not a security boundary, since the
identity signals it reads come from the caller's environment. The rolling-tag
ruleset on GitHub, which only the repository Admin role bypasses, is what
enforces who can move the tag.

The target of a move is checked: the release tag must exist on origin, its
commit must be merged work (the primary guard), and it must have a
published GitHub Release (an additional layer). Every check, and the push,
act on the one repo origin's fetch and push URLs both name.

Merged work is a commit on origin's ``main``, or that release's freeze
commit (#4140). vergil-actions' CD never tags a commit on ``main``: its
freeze-internal-refs action runs ``vrg-freeze-refs``, which rewrites the
repo's internal action refs to the release tag, and tags the commit it adds
on top of ``main``. A release tag ``vX.Y.Z`` on a commit off ``main`` passes
only when that commit has exactly one parent, the parent is on origin's
``main``, its subject is exactly ``chore(release): freeze internal refs to
vX.Y.Z``, and every file it changes is a regular file whose new content is
exactly what :func:`freeze_refs.freeze_references` makes of the parent's,
for origin's repo and that tag; no file is added, deleted, renamed or
re-moded. A refusal names the rule that failed.

The push sends the release tag object that was checked (resolved once
locally and verified against the commit origin reported) straight to the
rolling tag's ref, by object id, as a
compare-and-swap (``--force-with-lease`` on the rolling tag's raw value on
origin). The tag that is overwritten is exactly the one that was planned, or
the push is refused.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from vergil_tooling.lib import freeze_refs, git, github, identity_mode, retry
from vergil_tooling.lib.process_errors import error_text

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_MAIN_REF = "refs/remotes/origin/main"
_SHORT = 12
# The subject vergil-actions' freeze-internal-refs action commits its freeze with.
_FREEZE_SUBJECT = "chore(release): freeze internal refs to {tag}"
# The git modes of a regular file (plain and executable).
_FILE_MODES = frozenset({"100644", "100755"})


class PromoteError(ValueError):
    """A promotion was refused, or its plan could not be resolved.

    One raised for a failed command carries that command's ``cmd``,
    ``stdout`` and ``stderr``, as :class:`subprocess.CalledProcessError`
    does, so ``vrg-release``'s tracking issue records the command and its
    output. A refusal that is not a command failure has none of them.
    """

    cmd: str | list[str]
    stdout: str | None
    stderr: str | None


def _process_error(message: str, exc: subprocess.CalledProcessError) -> PromoteError:
    """A :class:`PromoteError` for *message* carrying *exc*'s command and output."""
    err = PromoteError(message)
    err.cmd = exc.cmd
    err.stdout = exc.stdout
    err.stderr = exc.stderr
    return err


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
        raise _process_error(error_text(exc, output=False), exc) from exc


def _remote_tag(tag: str) -> tuple[str, str]:
    """``(raw, peeled)`` for origin's *tag*, or ``("", "")`` if it is absent.

    *raw* is the ``refs/tags/<tag>`` line (an annotated tag's object id), and
    *peeled* the commit it resolves to (the ``^{}`` line, or *raw* itself for a
    lightweight tag). ``ls-remote`` returns the ``^{}`` line only for a pattern
    that matches it, so both refs are asked for by name. A failed
    ``ls-remote`` raises rather than reading as an absent tag.
    """
    raw = peeled = ""
    ref_lines = _git_read("ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    for line in ref_lines.splitlines():
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
    """``owner/repo`` of a github.com remote *url* (:func:`github.github_remote_repo`).

    The refusal never echoes *url*: a remote URL can carry credentials.
    """
    repo = github.github_remote_repo(url)
    if repo is None:
        msg = (
            f"origin's {which} URL is not a GitHub repository URL "
            "(expected https://github.com/<owner>/<repo> or git@github.com:<owner>/<repo>)"
        )
        raise PromoteError(msg)
    return repo


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


def _on_main(what: str, commit: str) -> bool:
    """Whether *commit* (named *what* in a failure) is on origin's ``main``."""
    try:
        return git.is_ancestor(commit, _MAIN_REF)
    except subprocess.CalledProcessError as exc:
        detail = error_text(exc, output=False)
        msg = f"cannot check whether {what} is on origin/main: {detail}"
        raise _process_error(msg, exc) from exc


def _commit_parents_and_subject(commit: str) -> tuple[list[str], str]:
    """*commit*'s parents and its subject: the message's first line, as stored.

    The raw commit object is read rather than ``%s``, which folds a
    multi-line first paragraph into one line.
    """
    headers, _, message = _git_read("cat-file", "commit", commit).partition("\n\n")
    parents = [line.split(" ", 1)[1] for line in headers.split("\n") if line.startswith("parent ")]
    return parents, message.split("\n", 1)[0]


def _blob_text(sha: str, path: str) -> str | None:
    """The blob *sha* of *path* as UTF-8 text, or ``None`` when it is not UTF-8."""
    try:
        data = git.blob_bytes(sha)
    except subprocess.CalledProcessError as exc:
        msg = f"cannot read {path} ({sha[:_SHORT]}): {error_text(exc, output=False)}"
        raise _process_error(msg, exc) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _change_failure(repo: str, release_tag: str, meta: str, path: str) -> str | None:
    """Why the change *meta* to *path* is not a freeze of its refs, or ``None`` if it is.

    *meta* is a ``git diff-tree --raw`` entry,
    ``:<old mode> <new mode> <old blob> <new blob> <status>``.
    """
    old_mode, new_mode, old_sha, new_sha, status = meta.removeprefix(":").split(" ")
    verbs = {"A": "adds", "D": "deletes", "T": "changes the type of"}
    if status != "M":
        return f"it {verbs.get(status, f'changes ({status})')} {path}"
    if old_mode != new_mode:
        return f"it changes the mode of {path}"
    if new_mode not in _FILE_MODES:
        return f"{path} is not a regular file"
    old, new = _blob_text(old_sha, path), _blob_text(new_sha, path)
    if old is None or new is None:
        return f"{path} is not UTF-8 text"
    if freeze_refs.freeze_references(old, repo, release_tag) != new:
        return f"the freeze commit changes more than internal refs in {path}"
    return None


def _freeze_commit_failure(repo: str, release_tag: str, commit: str) -> str | None:
    """Why *commit* is not *release_tag*'s freeze commit, or ``None`` if it is.

    A freeze commit is what vergil-actions' CD tags: one commit on top of
    origin's ``main``, made by ``vrg-freeze-refs`` for the same tag. It has
    exactly one parent, on origin's ``main``; its subject is exactly
    ``chore(release): freeze internal refs to <tag>``; and every file it
    changes is a regular file whose new content is exactly
    :func:`freeze_refs.freeze_references` of its parent's content, for
    *repo* and the tag. Nothing is added, deleted, renamed or re-moded.
    """
    parents, subject = _commit_parents_and_subject(commit)
    if len(parents) != 1:
        return f"it has {len(parents)} parents (a freeze commit has exactly one)"
    expected = _FREEZE_SUBJECT.format(tag=release_tag)
    if subject != expected:
        return f"its subject is not '{expected}'"
    parent = parents[0]
    if not _on_main(f"{release_tag}'s parent ({parent[:_SHORT]})", parent):
        return f"its parent ({parent[:_SHORT]}) is not on origin/main"
    raw = _git_read("diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", parent, commit)
    fields = raw.removesuffix("\0").split("\0") if raw else []
    if not fields:
        return "it changes no file"
    for meta, path in zip(fields[::2], fields[1::2], strict=True):
        failure = _change_failure(repo, release_tag, meta, path)
        if failure:
            return failure
    return None


def _require_merged_work(repo: str, release_tag: str, commit: str) -> None:
    """Refuse unless *commit* is on origin's ``main`` or is *release_tag*'s freeze commit.

    See :func:`_freeze_commit_failure`. A refusal names the freeze-commit
    rule that failed.
    """
    if _on_main(release_tag, commit):
        return
    failure = _freeze_commit_failure(repo, release_tag, commit)
    if failure:
        msg = (
            f"{release_tag} ({commit[:_SHORT]}) is not on origin/main, "
            f"and is not its freeze commit: {failure}"
        )
        raise PromoteError(msg)


def _read_release_draft(repo: str, release_tag: str, *, known_published: bool) -> str:
    def read() -> str:
        return github.read_output(
            "release",
            "view",
            release_tag,
            "--repo",
            f"github.com/{repo}",
            "--json",
            "isDraft",
            "--jq",
            ".isDraft",
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

    The read names ``github.com`` explicitly, the host origin was checked
    against, so ``GH_HOST`` cannot point it at another host's repo. A missing
    GitHub token propagates unwrapped, so the command's entry point gives its
    guided message.
    """
    try:
        is_draft = _read_release_draft(repo, release_tag, known_published=known_published)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        if retry.is_not_found(exc):
            msg = (
                f"{release_tag} has no published GitHub Release on {repo}, or {repo} is not "
                f"readable with this GitHub credential: {error_text(exc, output=False)}"
            )
        else:
            msg = (
                f"cannot check the GitHub Release of {release_tag} on {repo}: "
                f"{error_text(exc, output=False)}"
            )
        raise _process_error(msg, exc) from exc
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
    :class:`PromoteError` when the release tag is absent on origin, is
    neither on origin's ``main`` nor that release's freeze commit, or has no
    published GitHub Release.
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

    _require_merged_work(repo, release_tag, new_commit)
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


def _local_release_object(confirmed: Promotion) -> str:
    """The local release tag's object id, refusing unless it peels to the confirmed commit.

    The id is resolved once, and the push and the local tag update use it
    rather than the tag's name, so what is pushed is the object checked here.
    """
    ref = f"refs/tags/{confirmed.release_tag}"
    obj = _git_read("rev-parse", "--verify", "--end-of-options", ref)
    local = _git_read("rev-parse", "--verify", "--end-of-options", f"{obj}^{{commit}}")
    if local != confirmed.new_commit:
        msg = (
            f"local {ref} ({local[:_SHORT]}) is not the confirmed commit "
            f"({confirmed.new_commit[:_SHORT]}); refusing to push it"
        )
        raise PromoteError(msg)
    return obj


def _require_push_landed(
    confirmed: Promotion, obj: str, exc: subprocess.CalledProcessError
) -> None:
    """Accept a failed push only if origin's rolling tag now holds *obj*; else raise.

    ``git.read_output`` retries a push after a dropped connection, so a first
    attempt can land and the retry then fail its lease against the new value.
    Origin is re-read so that case reads as the move it was, not a refusal.
    """
    rolling = confirmed.rolling_tag
    try:
        raw, _ = _remote_tag(rolling)
    except PromoteError as read_exc:
        msg = (
            f"the push of {rolling} failed ({error_text(exc, output=False)}), and origin's "
            f"{rolling} could not be read back to see whether it moved ({read_exc}); "
            f"check origin's {rolling} before running again"
        )
        raise _process_error(msg, exc) from exc
    if raw == obj:
        return
    expected = confirmed.old_ref or "absent"
    msg = (
        f"origin did not accept the move of {rolling} "
        f"(the push required origin's {rolling} to be {expected}): "
        f"{error_text(exc, output=False)}"
    )
    raise _process_error(msg, exc) from exc


def promote_confirmed(confirmed: Promotion, *, release_just_published: bool = False) -> None:
    """Move the rolling tag exactly as *confirmed*, or refuse.

    Re-resolves the plan and refuses if anything changed since it was
    confirmed, then resolves the local release tag once and checks that it
    is the confirmed commit. The push sends that object to the rolling tag's
    ref, leasing on the confirmed raw value (empty for "must not exist"), so
    origin applies it as a compare-and-swap. The local rolling tag is updated
    only after origin accepted the move.
    """
    fresh = plan_promotion(confirmed.release_tag, release_just_published=release_just_published)
    if fresh != confirmed:
        raise PromoteError(_changed_message(confirmed, fresh))
    obj = _local_release_object(confirmed)

    rolling, release = confirmed.rolling_tag, confirmed.release_tag
    print(f"Pushing {rolling} -> {release} to origin")
    try:
        git.read_output(
            "push",
            "origin",
            f"{obj}:refs/tags/{rolling}",
            f"--force-with-lease=refs/tags/{rolling}:{confirmed.old_ref}",
        )
    except subprocess.CalledProcessError as exc:
        _require_push_landed(confirmed, obj, exc)
    print(f"Promoted: {rolling} -> {release}")
    try:
        git.read_output("tag", "-f", rolling, obj)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"{rolling} moved to {release} on origin, but updating the local {rolling} "
            f"tag failed ({error_text(exc, output=False)}); run "
            "`vrg-git fetch --tags --force origin` to refresh it"
        )
        raise _process_error(msg, exc) from exc


def promote(version: str) -> None:
    """Move the vX.Y rolling tag to vX.Y.Z, without prompting.

    The ``vrg-release`` promote stage calls this right after the release was
    published, so the Release read retries a 404. The ``vrg-promote`` CLI
    does not call it: it runs :func:`plan_promotion` and
    :func:`promote_confirmed` itself, with its own dry run and confirmation.
    """
    p = plan_promotion(version, release_just_published=True)
    if p.old_commit == p.new_commit:
        print(f"{p.rolling_tag} already points at {p.release_tag} — already promoted.")
        return
    promote_confirmed(p, release_just_published=True)
