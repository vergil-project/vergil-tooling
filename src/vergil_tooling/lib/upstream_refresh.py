"""Mechanics behind ``vrg-upstream-refresh`` (epic vergil-project/.github#1168, spec §6.2).

The repo supplies the logic in its ``[upstream-refresh].hook``; this module
supplies the mechanics, in two halves split by the key boundary (spec §6):

- **prepare** takes a fresh clone of ``develop``, reads ``[upstream-refresh]``
  from it, and runs the hook there with a scrubbed environment (no GitHub
  token and no App credential). It refuses untracked files ("edit tracked
  files only") and writes the hook's tracked changes as a binary patch
  (:data:`PATCH_FILE`) against the clone's ``develop`` commit, with that
  commit's SHA, the changed paths and the hook's notes (:data:`META_FILE`).
  It holds no App credentials, and refuses to run when the App's key is in
  its environment.
- **publish** (:func:`publish`) reads that output as untrusted data
  (:func:`read_prepared`). It never runs the hook and never runs git in the
  tree prepare worked in. It takes its own fresh clone of ``develop``, reads
  the fence rules from that tip, checks that the recorded commit is still on
  ``develop``, checks that commit out and applies the patch there
  (:func:`staged_clone`), so the PR's commit has the hook's own base as its
  parent. It then checks the applied tree against the repo's **path fence**
  (:func:`fence_violations`), and only then hands the clone to the
  mechanized-PR building block (:func:`vergil_tooling.lib.mechanized_pr.open_sync_pr`),
  which opens the PR and waits until it merges.

The fence is computed only from what publish sees in its own clone: the
touched paths and changed lines of the applied tree, the rules from the
``develop`` tip, and ``vergil.toml`` before and after. The touched set must
equal the paths prepare listed, which are otherwise used only for the PR body.
``.github/**`` is always refused. A refused patch pushes nothing.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from vergil_tooling.lib import develop_clone, git, github, mechanized_pr, retry
from vergil_tooling.lib.config import CONFIG_FILE, parse_upstream_refresh_toml, read_config
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from vergil_tooling.lib.config import PathRule, UpstreamRefreshConfig

REPORT_ENV = "VRG_UPSTREAM_REPORT"
COMMIT_SUBJECT = "chore(upstream): refresh upstream inputs"
PATCH_FILE = "changes.patch"
META_FILE = "meta.json"

#: The App's private key variable (spec §6.1). Prepare refuses to run while it
#: is set, so a wiring mistake cannot put the key beside the hook (spec §6).
KEY_ENV = "VRG_AUTOMATION_PRIVATE_KEY"

_BODY_INTRO = (
    "Automated upstream refresh. GitHub's auto-merge merges this PR once CI passes, and "
    "the upstream-refresh job watches it until it merges."
)

#: Variables the hook never sees: GitHub tokens, the App's credentials, the
#: agent App-mode credentials, Actions' own tokens, and git's environment
#: config (where an ``http.extraHeader`` would carry a token).
_SCRUBBED_VARS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "VRG_APP_ID",
        "VRG_PRIVATE_KEY_PATH",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)
_SCRUBBED_PREFIXES = ("VRG_AUTOMATION_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_META_KEYS = frozenset({"repo", "base_sha", "paths", "notes"})

#: The format of a ``[[package]]`` entry's ``upstream-version``: ``<cpython>+<pbs_tag>``.
_UPSTREAM_VERSION_RE = re.compile(r"(?P<cpython>\d+\.\d+\.\d+)\+(?P<pbs>\d{8})")
#: The keys of an existing ``[[package]]`` entry a refresh may change.
_FREE_PACKAGE_KEYS = frozenset({"upstream-version", "summary"})
_SERIES_TOKEN_RE = re.compile(r"\d+\.\d+")
#: Symlinks and gitlinks (submodules) are never valid refresh output.
_LINK_MODES = frozenset({"120000", "160000"})
#: The longest slice of an offending line a violation quotes.
_QUOTE_LIMIT = 120
#: Context lines for :func:`diff_changed_lines`: more than any data file holds,
#: so each file's diff is one hunk over the whole file, and small enough that
#: git's doubling of it stays inside a 32-bit int.
_WHOLE_FILE_CONTEXT = 999_999_999
_HUNK_HEADER_RE = re.compile(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
#: The line-rule group that names the block a versioned line belongs to.
_ANCHOR_GROUP = "anchor"

#: The mechanized-PR job: its branches are ``automation/upstream-refresh/<run-id>``.
JOB = "upstream-refresh"
#: Publish's wait-until-merged deadline under a token source, counted from the
#: tool's own start: the clone and the patch come out of it, and the wait gets
#: what is left. Closing an earlier run's PR, the push and opening the PR come
#: after that is worked out and are not counted (spec §6.1, "The wait"). The
#: publish job's ``timeout-minutes`` is 140.
PUBLISH_DEADLINE_SECS = 7200
#: The compare statuses that put the recorded base commit on ``develop``.
_ON_DEVELOP = frozenset({"ahead", "identical"})
#: Variables that would override the bot identity publish sets in its clone.
_GIT_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)

#: The clock publish measures its deadline with; tests replace it.
_monotonic = time.monotonic


class UpstreamRefreshError(Exception):
    """A refusal or failure of the upstream refresh; the message says why."""


class FenceRefusedError(UpstreamRefreshError):
    """The applied patch is refused; :attr:`problems` names each path or table and its rule."""

    def __init__(self, repo: str, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        listed = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            f"the refresh patch for {repo} is refused by the path fence; nothing was pushed:\n"
            f"{listed}"
        )


@dataclass(frozen=True)
class Prepared:
    """What prepare recorded: the repo, the ``develop`` commit, the changed paths and notes."""

    repo: str
    base_sha: str
    paths: list[str]
    notes: list[str]


# -- git helpers ---------------------------------------------------------------


def _git_bytes(tree: Path, *args: str) -> bytes:
    """Run a local git command in *tree* and return its raw stdout.

    Raw, never stripped: a patch, a NUL list or a changed line ends exactly
    where git ends it, so trailing whitespace is never lost. Only local
    commands go through here, so no token is ever added.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "-C", str(tree), *args),  # noqa: S607
            check=True,
            capture_output=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        msg = f"git {args[0]} failed in {tree} (exit {exc.returncode}): {stderr}"
        raise UpstreamRefreshError(msg) from exc
    return result.stdout


def _nul_paths(raw: bytes, what: str) -> list[str]:
    try:
        return [item.decode() for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        msg = f"{what} has a path that is not UTF-8: {exc.object[exc.start : exc.end]!r}"
        raise UpstreamRefreshError(msg) from exc


# -- hook ----------------------------------------------------------------------


def hook_env(report: Path) -> dict[str, str]:
    """The hook's environment: the caller's, scrubbed of every token and credential."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _SCRUBBED_VARS and not name.startswith(_SCRUBBED_PREFIXES)
    }
    env[REPORT_ENV] = str(report)
    return env


def run_hook(tree: Path, hook: str, report: Path) -> list[str]:
    """Run *hook* with ``bash -c`` from *tree*; return the report's notes.

    The hook runs with :func:`hook_env`: no ``GH_TOKEN``, ``GITHUB_TOKEN``,
    ``VRG_AUTOMATION_*``, ``VRG_APP_ID`` or ``VRG_PRIVATE_KEY_PATH``. The
    report path is passed in ``$VRG_UPSTREAM_REPORT``. The notes are its
    non-empty lines (stripped); a hook that writes no report has none.
    """
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", hook],  # noqa: S607
        cwd=tree,
        env=hook_env(report),
        check=False,
    )
    if result.returncode != 0:
        msg = f"[upstream-refresh].hook failed (exit {result.returncode}): {hook}"
        raise UpstreamRefreshError(msg)
    if not report.exists():
        return []
    return [line.strip() for line in report.read_text().splitlines() if line.strip()]


def changed_paths(tree: Path, base_sha: str) -> list[str]:
    """The tracked paths changed in *tree* since *base_sha*, sorted; untracked files are refused.

    The hook contract is "edit tracked files only": a file it leaves untracked
    is never published silently. Ignored files are not reported.
    """
    untracked = _nul_paths(
        _git_bytes(tree, "ls-files", "-z", "--others", "--exclude-standard"), "the hook's output"
    )
    if untracked:
        msg = f"hook left untracked files: {', '.join(sorted(untracked))}"
        raise UpstreamRefreshError(msg)
    # Against the base commit, so staged, unstaged and even committed edits all
    # count; --no-renames lists both sides of a rename.
    raw = _git_bytes(tree, "diff", "--name-only", "--no-renames", "-z", base_sha)
    return sorted(set(_nul_paths(raw, "the hook's output")))


# -- prepare -------------------------------------------------------------------


def prepare(repo: str, out_dir: Path, *, token: str | None = None) -> Prepared:
    """Run the hook in a fresh clone of *repo*'s ``develop``; write the patch and meta.

    ``[upstream-refresh]`` is read from the clone, never from the caller's
    checkout. *token*, when given, authenticates the clone only; the hook never
    sees it. With no change, the meta has ``paths: []`` and no patch is
    written. Refuses to run while :data:`KEY_ENV` is set, and refuses an
    *out_dir* that already holds a patch or meta.
    """
    if KEY_ENV in os.environ:
        msg = (
            f"${KEY_ENV} is set; prepare runs the hook and must never hold the App's key "
            "(spec §6, the key boundary)"
        )
        raise UpstreamRefreshError(msg)
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = [name for name in (PATCH_FILE, META_FILE) if (out_dir / name).exists()]
    if stale:
        msg = f"{out_dir} already holds {', '.join(stale)}; prepare writes into an empty directory"
        raise UpstreamRefreshError(msg)
    with (
        develop_clone.clone_develop(repo, token=token) as clone,
        tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-") as tmp,
    ):
        cfg = read_config(clone)
        if cfg.upstream_refresh is None:
            msg = f"{CONFIG_FILE} on {repo}'s develop has no [upstream-refresh] section"
            raise UpstreamRefreshError(msg)
        # Read before the hook runs: the hook may move HEAD in its clone.
        base_sha = _git_bytes(clone, "rev-parse", "HEAD").decode().strip()
        report = Path(tmp) / "report.txt"
        print(f"Running [upstream-refresh].hook in {clone} ({REPORT_ENV}={report}).")
        notes = run_hook(clone, cfg.upstream_refresh.hook, report)
        paths = changed_paths(clone, base_sha)
        if paths:
            patch = _git_bytes(
                clone,
                "diff",
                "--binary",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                base_sha,
            )
            (out_dir / PATCH_FILE).write_bytes(patch)
    prepared = Prepared(repo=repo, base_sha=base_sha, paths=paths, notes=notes)
    meta = {"repo": repo, "base_sha": base_sha, "paths": paths, "notes": notes}
    (out_dir / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")
    return prepared


def _bad_path_reason(path: str) -> str | None:
    """Why *path* is not a plain repo-relative path, or None when it is."""
    if not path:
        return "is empty"
    if "\0" in path or "\n" in path or "\r" in path:
        return "contains a NUL or line break"
    if path.startswith("/"):
        return "is absolute"
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        return "has an empty, . or .. segment"
    return None


def read_prepared(in_dir: Path) -> tuple[Prepared, bytes | None]:
    """Read and validate prepare's output in *in_dir*; refuse anything else.

    The meta must be a JSON object with exactly ``repo`` (``OWNER/NAME``),
    ``base_sha`` (a full lower-case hex SHA), ``paths`` (unique repo-relative
    paths, no ``..``) and ``notes`` (single-line strings). The patch must be
    present exactly when ``paths`` is non-empty.
    """
    meta_path = in_dir / META_FILE
    try:
        raw = json.loads(meta_path.read_text())
    except FileNotFoundError as exc:
        msg = f"{meta_path} not found; run prepare first"
        raise UpstreamRefreshError(msg) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{meta_path} is not readable JSON: {exc}"
        raise UpstreamRefreshError(msg) from exc
    prepared = _validate_meta(raw, meta_path)
    patch_path = in_dir / PATCH_FILE
    patch = patch_path.read_bytes() if patch_path.exists() else None
    if prepared.paths and patch is None:
        msg = f"{meta_path} lists changed paths but {patch_path} is missing"
        raise UpstreamRefreshError(msg)
    if not prepared.paths and patch is not None:
        msg = f"{meta_path} lists no changed paths but {patch_path} exists"
        raise UpstreamRefreshError(msg)
    return prepared, patch


def _validate_meta(raw: object, source: Path) -> Prepared:
    def bad(why: str) -> UpstreamRefreshError:
        return UpstreamRefreshError(f"{source}: {why}")

    if not isinstance(raw, dict):
        raise bad("must be a JSON object")
    keys = set(raw)
    if keys != _META_KEYS:
        missing = sorted(_META_KEYS - keys)
        extra = sorted(keys - _META_KEYS)
        raise bad(f"wrong keys (missing: {missing or 'none'}; unexpected: {extra or 'none'})")
    repo, base_sha, paths, notes = raw["repo"], raw["base_sha"], raw["paths"], raw["notes"]
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise bad(f"repo must be OWNER/NAME (got {repo!r})")
    if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
        raise bad(f"base_sha must be a full lower-case hex SHA (got {base_sha!r})")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise bad("paths must be a list of strings")
    for path in paths:
        reason = _bad_path_reason(path)
        if reason is not None:
            raise bad(f"path {path!r} {reason}")
    if len(set(paths)) != len(paths):
        raise bad("paths has duplicates")
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise bad("notes must be a list of strings")
    if any("\n" in n or "\r" in n for n in notes):
        raise bad("a note contains a line break")
    return Prepared(repo=repo, base_sha=base_sha, paths=list(paths), notes=list(notes))


# -- PR body -------------------------------------------------------------------


def render_body(notes: list[str], paths: list[str]) -> str:
    """The refresh PR body: intro, changed files, and the hook's notes (if any)."""
    lines = [_BODY_INTRO, "", "## Changed files", ""]
    lines.extend(f"- `{path}`" for path in paths)
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


# -- publish ---------------------------------------------------------------------


@dataclass(frozen=True)
class Staged:
    """Publish's clone with the patch applied, and what the fence found.

    *clone* is detached at the recorded base commit, the patch applied to its
    index and working tree. *touched* is the touched set computed from the
    clone itself. *problems* is every refusal, empty when the patch may be
    published: a mismatch with prepare's listed paths, then each
    :func:`fence_violations` message.
    """

    clone: Path
    touched: list[str]
    problems: list[str]


def read_rules_file(path: Path) -> UpstreamRefreshConfig:
    """The ``[upstream-refresh]`` rules in the ``vergil.toml`` at *path* (``--rules-from``)."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read the rules file {path}: {exc}"
        raise UpstreamRefreshError(msg) from exc
    rules = parse_upstream_refresh_toml(text, str(path))
    if rules is None:
        msg = f"the rules file {path} has no [upstream-refresh] section"
        raise UpstreamRefreshError(msg)
    return rules


def _tip_rules(repo: str, clone: Path) -> UpstreamRefreshConfig:
    """The fence rules from the ``develop`` tip in *clone*, never from prepare's output."""
    try:
        cfg = read_config(clone)
    except FileNotFoundError as exc:
        msg = f"{repo}'s develop has no {CONFIG_FILE}, so it has no path-fence rules"
        raise UpstreamRefreshError(msg) from exc
    if cfg.upstream_refresh is None:
        msg = f"{CONFIG_FILE} on {repo}'s develop has no [upstream-refresh] section"
        raise UpstreamRefreshError(msg)
    return cfg.upstream_refresh


def check_base_on_develop(repo: str, base_sha: str, tip_sha: str) -> None:
    """Refuse a recorded base commit that is not ``develop``'s tip or one of its ancestors.

    Asks GitHub's compare API (``base...tip``): ``identical`` or ``ahead``
    means the tip descends from the base. Any other status (``diverged``,
    ``behind``) or an unknown commit is refused, naming the commit.
    """
    if base_sha == tip_sha:
        return
    endpoint = f"repos/{repo}/compare/{base_sha}...{tip_sha}?per_page=1"
    try:
        data = github.read_json("api", endpoint)
    except subprocess.CalledProcessError as exc:
        if retry.is_not_found(exc):
            msg = f"the recorded base commit {base_sha} is not a commit of {repo}; nothing pushed"
        else:
            msg = (
                f"could not check that the recorded base commit {base_sha} is on {repo}'s "
                f"develop: {error_text(exc)}"
            )
        raise UpstreamRefreshError(msg) from exc
    status = data.get("status") if isinstance(data, dict) else None
    if status not in _ON_DEVELOP:
        msg = (
            f"the recorded base commit {base_sha} is no longer on {repo}'s develop "
            f"(GitHub's compare with the tip {tip_sha} says {status!r}); nothing pushed"
        )
        raise UpstreamRefreshError(msg)


def touched_paths(tree: Path) -> list[str]:
    """Every path *tree*'s index or working tree changes against ``HEAD``, sorted.

    From ``git status --porcelain=v1 -z --untracked-files=all --no-renames``:
    added, modified, deleted, mode-changed and untracked files, each side of
    a rename as its own path.
    """
    raw = _git_bytes(
        tree, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"
    )
    return sorted({entry[3:] for entry in _nul_paths(raw, "the applied tree")})


def _blob_text(tree: Path, spec: str, path: str) -> str | None:
    """The text of *path* at *spec* (a commit, or ``""`` for the index); None when absent."""
    if spec:
        # ls-tree matches its paths literally; it takes no pathspec magic.
        listed = _git_bytes(tree, "ls-tree", "-z", spec, "--", path)
    else:
        listed = _git_bytes(tree, "ls-files", "-z", "--", f":(literal){path}")
    if not listed:
        return None
    return _git_bytes(tree, "show", f"{spec}:{path}").decode(errors="surrogateescape")


def _apply(clone: Path, patch: bytes, base_sha: str) -> None:
    with tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-patch-") as tmp:
        patch_file = Path(tmp) / PATCH_FILE
        patch_file.write_bytes(patch)
        try:
            # --index, so a new file is in the index and has a diff for the fence.
            _git_bytes(clone, "apply", "--index", str(patch_file))
        except UpstreamRefreshError as exc:
            msg = f"the refresh patch does not apply cleanly on {base_sha}: {exc}"
            raise UpstreamRefreshError(msg) from exc


@contextlib.contextmanager
def staged_clone(
    repo: str,
    prepared: Prepared,
    patch: bytes | None,
    *,
    rules: UpstreamRefreshConfig | None = None,
) -> Generator[Staged]:
    """Yield publish's own clone of ``develop`` with *patch* applied on the recorded base.

    The steps, in order: clone ``develop``; read the fence rules from that
    tip before anything else (or use *rules*, ``--dry-run --rules-from``);
    check that ``prepared.base_sha`` is still on ``develop``; fetch it and
    check it out detached; ``git apply --index`` the patch; compute the
    touched set from the clone; and run the fence on that set. With no
    listed paths, the clean ``develop`` clone is yielded as it is.
    """
    with develop_clone.clone_develop(repo) as clone:
        fence_rules = rules if rules is not None else _tip_rules(repo, clone)
        if not prepared.paths:
            yield Staged(clone=clone, touched=[], problems=[])
            return
        if patch is None:
            msg = "prepare listed changed paths but gave no patch"
            raise UpstreamRefreshError(msg)
        base_sha = prepared.base_sha
        tip_sha = _git_bytes(clone, "rev-parse", "HEAD").decode().strip()
        check_base_on_develop(repo, base_sha, tip_sha)
        if base_sha != tip_sha:
            try:
                git.read_output(
                    "-C", str(clone), "fetch", "--quiet", "--depth", "1", "origin", base_sha
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                msg = f"could not fetch the recorded base commit {base_sha}: {error_text(exc)}"
                raise UpstreamRefreshError(msg) from exc
        _git_bytes(clone, "checkout", "--quiet", "--detach", base_sha)
        _apply(clone, patch, base_sha)
        touched = touched_paths(clone)
        problems = _listing_mismatch(touched, prepared.paths)
        problems.extend(
            fence_violations(
                fence_rules,
                touched,
                diff_changed_lines(clone, base_sha, touched),
                _blob_text(clone, base_sha, CONFIG_FILE) if CONFIG_FILE in touched else None,
                _blob_text(clone, "", CONFIG_FILE) if CONFIG_FILE in touched else None,
            )
        )
        yield Staged(clone=clone, touched=touched, problems=problems)


def _listing_mismatch(touched: Sequence[str], listed: Sequence[str]) -> list[str]:
    """Refusals for a touched set that is not exactly prepare's listed paths."""
    extra = sorted(set(touched) - set(listed))
    missing = sorted(set(listed) - set(touched))
    problems: list[str] = []
    if extra:
        problems.append(
            "the applied patch touches paths prepare did not list (refused, the touched set "
            f"must equal the listed paths): {', '.join(extra)}"
        )
    if missing:
        problems.append(
            "prepare listed paths the applied patch does not touch (refused, the touched set "
            f"must equal the listed paths): {', '.join(missing)}"
        )
    return problems


def bot_identity(slug: str) -> tuple[str, str]:
    """The commit name and email of the App *slug*'s bot user.

    ``<slug>[bot]`` and ``<id>+<slug>[bot]@users.noreply.github.com``, with
    ``<id>`` from ``GET /users/<slug>[bot]``.
    """
    login = f"{slug}[bot]"
    try:
        payload = github.read_json("api", f"users/{quote(login, safe='')}")
    except subprocess.CalledProcessError as exc:
        msg = f"could not look up the bot user {login!r}: {error_text(exc)}"
        raise UpstreamRefreshError(msg) from exc
    user_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        msg = f"the bot user lookup for {login!r} returned no numeric id"
        raise UpstreamRefreshError(msg)
    return login, f"{user_id}+{login}@users.noreply.github.com"


def _set_identity(clone: Path, bot: tuple[str, str]) -> None:
    """Make *bot* the author and committer of every commit made in *clone*.

    The ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` variables would override the
    clone's config, so they are removed from this process's environment.
    """
    name, email = bot
    for var in _GIT_IDENTITY_ENV:
        os.environ.pop(var, None)
    _git_bytes(clone, "config", "user.name", name)
    _git_bytes(clone, "config", "user.email", email)


def publish(
    repo: str,
    in_dir: Path,
    *,
    deadline_secs: int | None,
    bot: tuple[str, str] | None,
    started: float | None = None,
) -> mechanized_pr.SyncResult:
    """Publish prepare's output in *in_dir* as one PR on *repo*'s ``develop``, and wait.

    The output is read as untrusted data (:func:`read_prepared`); its repo must
    be *repo*. The patch is applied in publish's own clone on the recorded
    base and checked against the fence (:func:`staged_clone`); any refusal
    raises :class:`FenceRefusedError` before anything is pushed. With *bot*
    (``(name, email)``), the clone commits as that user; with ``None`` (a
    human run) it commits as the caller's git identity.

    *deadline_secs* is counted from *started* (the tool's start on
    :data:`_monotonic`; default now): what is left of it once the clone and
    the patch are done is the wait's deadline. ``None`` (a run with no token
    source) keeps the building block's own rule, ``$VRG_MECHANIZED_PR_DEADLINE``.
    A run with nothing to change still closes an earlier run's open PR.
    """
    start = _monotonic() if started is None else started
    prepared, patch = read_prepared(in_dir)
    if prepared.repo.lower() != repo.lower():
        msg = f"the prepared output is for {prepared.repo}, not {repo}; nothing pushed"
        raise UpstreamRefreshError(msg)
    with staged_clone(repo, prepared, patch) as staged:
        if staged.problems:
            raise FenceRefusedError(repo, staged.problems)
        if bot is not None:
            _set_identity(staged.clone, bot)
        remaining: int | None = None
        if deadline_secs is not None:
            remaining = deadline_secs - int(_monotonic() - start)
            if remaining <= 0:
                msg = (
                    f"the {deadline_secs}s publish deadline passed before the PR could be "
                    "opened; nothing pushed"
                )
                raise UpstreamRefreshError(msg)
        return mechanized_pr.open_sync_pr(
            repo,
            staged.clone,
            job=JOB,
            title=COMMIT_SUBJECT,
            body=render_body(prepared.notes, prepared.paths),
            deadline_secs=remaining,
        )


# -- the path fence: inputs ------------------------------------------------------


def diff_changed_lines(tree: Path, base_sha: str, paths: Sequence[str]) -> dict[str, list[str]]:
    """Each path's whole-file ``git diff <base_sha>`` in *tree*, for :func:`fence_violations`.

    Every unchanged line comes as context, so a changed file's diff is one
    hunk over the whole file: the no-downgrade rule and the hash rule need
    each versioned or hashed line's place in the file (and so its block),
    not only the changed lines.
    Each value is the path's whole diff section (its header, then its hunk),
    split on ``\\n`` only, so a ``\\r`` stays inside its line. A new file must
    be in the index (``git apply --index``), or it has no diff and the fence
    refuses it. No textconv or external diff driver runs.
    """
    result: dict[str, list[str]] = {}
    for path in paths:
        raw = _git_bytes(
            tree,
            "diff",
            f"--unified={_WHOLE_FILE_CONTEXT}",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            base_sha,
            "--",
            f":(literal){path}",
        )
        text = raw.decode(errors="surrogateescape")
        result[path] = [line for line in text.split("\n") if line]
    return result


def glob_match(path: str, glob: str) -> bool:
    """True when *path* matches *glob*: ``*`` within one segment, ``**`` across any number."""
    return _match_segments(path.split("/"), glob.split("/"))


def _match_segments(parts: list[str], pats: list[str]) -> bool:
    if not pats:
        return not parts
    head, rest = pats[0], pats[1:]
    if head == "**":
        return any(_match_segments(parts[i:], rest) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match_segments(parts[1:], rest)


@dataclass
class _FileDiff:
    """One path's ``git diff -U0`` section, parsed."""

    added: bool = False
    deleted: bool = False
    binary: bool = False
    mode_changed: bool = False
    link: bool = False
    hunks: list[tuple[list[str], list[str]]] = field(default_factory=list)
    #: The file before and after the patch, in order, as far as the hunks show it.
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    #: True when the diff is one hunk starting at the first line on both sides,
    #: so ``before`` and ``after`` are the whole file (:func:`diff_changed_lines`).
    whole_file: bool = False


def _parse_file_diff(lines: Sequence[str]) -> _FileDiff:
    """Parse one path's diff; a type change (file to symlink) gives two sections, both read."""
    diff = _FileDiff()
    in_header = True
    starts: list[bool] = []
    for line in lines:
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
            diff.hunks.append(([], []))
            header = _HUNK_HEADER_RE.match(line)
            starts.append(
                header is not None and header["old"] in {"0", "1"} and header["new"] in {"0", "1"}
            )
        elif not in_header:
            _parse_hunk_line(diff, line)
        else:
            _parse_header_line(diff, line)
    diff.whole_file = starts == [True]
    return diff


def _parse_hunk_line(diff: _FileDiff, line: str) -> None:
    removed, added = diff.hunks[-1]
    if line.startswith("-"):
        removed.append(line[1:])
        diff.before.append(line[1:])
    elif line.startswith("+"):
        added.append(line[1:])
        diff.after.append(line[1:])
    elif line.startswith(" "):
        diff.before.append(line[1:])
        diff.after.append(line[1:])


def _parse_header_line(diff: _FileDiff, line: str) -> None:
    words = line.split()
    if line.startswith("new file mode"):
        diff.added = True
    elif line.startswith("deleted file mode"):
        diff.deleted = True
    elif line.startswith(("old mode", "new mode")):
        diff.mode_changed = True
    elif line.startswith(("Binary files ", "GIT binary patch")):
        diff.binary = True
    if line.startswith(("new file mode", "deleted file mode", "old mode", "new mode")):
        diff.link = diff.link or words[-1] in _LINK_MODES
    elif line.startswith("index ") and len(words) == 3:  # noqa: PLR2004 - "index a..b <mode>"
        diff.link = diff.link or words[2] in _LINK_MODES


# -- the path fence ----------------------------------------------------------------


def fence_violations(
    rules: UpstreamRefreshConfig,
    paths: Sequence[str],
    changed_lines: Mapping[str, Sequence[str]],
    vergil_toml_before: str | None,
    vergil_toml_after: str | None,
) -> list[str]:
    """Check a refresh against the path fence; one message per violation, empty when allowed.

    *paths* is always the touched set the caller computed from the applied
    tree, never one read from prepare's output. *changed_lines* maps each of
    them to its whole-file ``git diff <base_sha>`` lines (:func:`diff_changed_lines`).
    The rules (spec §6.2):

    - a path must be repo-relative and must match a ``rules.paths`` glob; with
      no ``paths``, every path is refused;
    - ``.github/**`` is always refused, whatever the allowlist says;
    - a symlink or submodule is always refused;
    - under a rule with ``lines``, every removed and added line must match the
      regex in full, an added, deleted, binary or mode-changed file is
      refused, and a ``(?P<version>…)`` group may not go down, compared
      per line identity across the whole file, an existing
      ``(?P<anchor>…)`` block may not be removed (:func:`_downgrades`), and a
      ``(?P<hash>…)`` line may change only when a version of its block
      changes too (:func:`_hash_violations`);
    - ``vergil.toml`` follows its own rules (:func:`_vergil_toml_violations`).
    """
    violations: list[str] = []
    for path in sorted(set(paths)):
        violations.extend(_path_violations(rules, path, changed_lines.get(path)))
        if path == CONFIG_FILE:
            violations.extend(_vergil_toml_violations(vergil_toml_before, vergil_toml_after))
    return violations


def _first_rule(rules: UpstreamRefreshConfig, path: str) -> PathRule | None:
    return next((rule for rule in rules.paths if glob_match(path, rule.glob)), None)


def _path_violations(
    rules: UpstreamRefreshConfig, path: str, lines: Sequence[str] | None
) -> list[str]:
    reason = _bad_path_reason(path)
    if reason is not None:
        return [f"{path!r}: refused, the path {reason}"]
    if path.split("/")[0].lower() == ".github":
        return [
            f"{path}: refused, .github/** is never changed by a refresh "
            "(whatever [upstream-refresh].paths allows)"
        ]
    rule = _first_rule(rules, path)
    if rule is None:
        if not rules.paths:
            return [
                f"{path}: refused, [upstream-refresh].paths is empty or absent, "
                "so every path is refused"
            ]
        return [f"{path}: refused, not covered by any [upstream-refresh].paths entry"]
    if not lines:
        return [f"{path}: refused, it has no diff against the base commit"]
    diff = _parse_file_diff(lines)
    if diff.link:
        return [f"{path}: refused, a symlink or submodule is never refresh output"]
    if rule.lines is None:
        return []
    return _line_rule_violations(path, rule.glob, rule.lines, diff)


def _line_rule_violations(path: str, glob: str, regex: str, diff: _FileDiff) -> list[str]:
    where = f"{path}: refused by the line rule of paths entry {glob!r}"
    kinds = [
        name
        for name, flag in (
            ("an added file", diff.added),
            ("a deleted file", diff.deleted),
            ("a binary file", diff.binary),
            ("a mode change", diff.mode_changed),
        )
        if flag
    ]
    if kinds:
        return [f"{where}: {' and '.join(kinds)} is not allowed under a line rule"]
    pattern = re.compile(regex)
    groups = _version_groups(pattern)
    violations: list[str] = []
    for removed, added in diff.hunks:
        for sign, side in (("-", removed), ("+", added)):
            violations.extend(
                f"{where}: line {sign}{_quote(line)} does not match {regex!r} in full"
                for line in side
                if not pattern.fullmatch(line)
            )
    if violations:
        # The file is refused already; a line outside the rule is not versioned,
        # so pairing would only repeat that refusal as an unpaired line.
        return violations
    hashes = _hash_groups(pattern)
    if not groups and not hashes and _ANCHOR_GROUP not in pattern.groupindex:
        # With no version group, no hash group and no anchor, nothing is versioned
        # or hashed and no block can be removed, so there is nothing to compare.
        return []
    if not diff.whole_file:
        if hashes and groups:
            needs = "the no-downgrade rule and the hash rule cannot place their lines"
        elif hashes:
            needs = "the hash rule cannot place its lines"
        else:
            needs = "the no-downgrade rule cannot place its versioned lines"
        return [f"{where}: the diff does not show the whole file, so {needs}"]
    violations = _downgrades(path, pattern, groups, diff.before, diff.after)
    if hashes:
        violations.extend(_hash_violations(path, pattern, groups, hashes, diff.before, diff.after))
    return violations


def _quote(line: str) -> str:
    return repr(line if len(line) <= _QUOTE_LIMIT else line[:_QUOTE_LIMIT] + "…")


def _version_groups(pattern: re.Pattern[str]) -> list[str]:
    """The regex's version groups, in order: ``version``, or ``version_<suffix>``.

    Python's ``re`` refuses two groups of one name, so a rule that marks two
    keys' versions (``cpython`` and ``pbs_tag``) names the second
    ``version_<suffix>``.
    """
    names = [
        name for name in pattern.groupindex if name == "version" or name.startswith("version_")
    ]
    return sorted(names, key=lambda name: pattern.groupindex[name])


def _hash_groups(pattern: re.Pattern[str]) -> list[str]:
    """The regex's hash groups, in order: ``hash``, or ``hash_<suffix>``.

    As for :func:`_version_groups`, a regex that marks hashes in two
    alternatives (``x86_64`` and ``aarch64``) names the second ``hash_<suffix>``.
    """
    names = [name for name in pattern.groupindex if name == "hash" or name.startswith("hash_")]
    return sorted(names, key=lambda name: pattern.groupindex[name])


#: A versioned line's identity: the anchor value in force (None before any
#: anchor line, or with no anchor group) and the text before the version group.
_Identity = tuple[str | None, str]


def _versioned(
    pattern: re.Pattern[str], groups: Sequence[str], lines: Sequence[str]
) -> tuple[dict[_Identity, list[str]], set[str]]:
    """The versions in one whole file, by line identity in file order, and its anchor values.

    *lines* is the whole file. A line that does not match the rule in full
    (an unchanged line the rule does not cover) is neither versioned nor an
    anchor. A line whose ``(?P<anchor>…)`` group took part starts a new block:
    its anchor value names every versioned line after it, up to the next
    anchor line.
    """
    marked, anchors = _marked(pattern, groups, lines)
    return {ident: [value for value, _ in found] for ident, found in marked.items()}, anchors


def _marked(
    pattern: re.Pattern[str], groups: Sequence[str], lines: Sequence[str]
) -> tuple[dict[_Identity, list[tuple[str, str]]], set[str]]:
    """The lines one of *groups* marks, as ``(value, line)`` by identity in file order.

    The block walk behind :func:`_versioned`, shared by the hash rule
    (:func:`_hash_violations`): the identity is the anchor value in force and
    the text before the first of *groups* that took part.
    """
    has_anchor = _ANCHOR_GROUP in pattern.groupindex
    anchor: str | None = None
    anchors: set[str] = set()
    by_identity: dict[_Identity, list[tuple[str, str]]] = {}
    for line in lines:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        if has_anchor and match.start(_ANCHOR_GROUP) >= 0:
            anchor = match.group(_ANCHOR_GROUP)
            anchors.add(anchor)
        group = next((name for name in groups if match.start(name) >= 0), None)
        if group is None:
            continue
        identity = (anchor, line[: match.start(group)])
        by_identity.setdefault(identity, []).append((match.group(group), line))
    return by_identity, anchors


def _describe(identity: _Identity) -> str:
    anchor, key = identity
    where = "" if anchor is None else f" (anchor {anchor!r})"
    return f"{key.strip()!r}{where}"


def _version_tuple(text: str) -> tuple[int, ...] | None:
    """*text* as integers split on ``.``, trailing zeros dropped; None if not numeric."""
    parts = text.split(".")
    if not all(part.isdigit() and part.isascii() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def _downgrades(
    path: str,
    pattern: re.Pattern[str],
    groups: Sequence[str],
    before: Sequence[str],
    after: Sequence[str],
) -> list[str]:
    """The no-downgrade rule over one whole file; one violation per breach.

    Each versioned line is identified by the anchor value in force and the
    text before its version group (:func:`_versioned`), wherever it stands in
    the file, so moving a line or editing a block's lines in place changes
    nothing about which versions are compared. Per identity:

    - the same versions before and after: nothing to compare;
    - present on one side only: allowed as a new or a dropped entry only
      when the whole block goes with it. Under a rule with no anchor group,
      that is any key (a new or a dropped tool pin). Under a rule with one,
      the identity's anchor value must itself be on that side only (a new
      ``[[series]]`` block, or a removed one, refused below); a versioned
      line that appears or disappears inside a block that stays, or before
      the first anchor, is refused, since moving a pin above its block's
      anchor line would otherwise give it a new identity;
    - one version on each side: it may not go down or be non-numeric;
    - more than one version on either side, and a change: refused, since the
      pairing would be a guess. A rule over a file with repeated keys
      (one ``cpython`` per ``[[series]]`` block) names its blocks with a
      ``(?P<anchor>…)`` group, such as the block's ``series`` value.

    An existing anchored block is never removed: an anchor value in *before*
    and not in *after* is refused. Dropping a series is a human change
    (spec §6.2).
    """
    old_versions, old_anchors = _versioned(pattern, groups, before)
    new_versions, new_anchors = _versioned(pattern, groups, after)
    anchored = _ANCHOR_GROUP in pattern.groupindex
    violations = [
        f"{path}: refused, the block with anchor {anchor!r} is removed; a refresh never "
        "removes an existing block (dropping a series is a human change)"
        for anchor in sorted(old_anchors - new_anchors)
    ]
    identities = sorted(
        old_versions.keys() | new_versions.keys(),
        key=lambda ident: (ident[0] is not None, ident[0] or "", ident[1]),
    )
    for identity in identities:
        olds, news = old_versions.get(identity, []), new_versions.get(identity, [])
        if olds == news:
            continue
        if not olds or not news:
            anchor = identity[0]
            whole_block = not anchored or (
                anchor is not None and (anchor in old_anchors) != (anchor in new_anchors)
            )
            if not whole_block:
                side = "added" if news else "removed"
                violations.append(
                    f"{path}: refused, {_describe(identity)} has a versioned line {side} "
                    "in a block that stays, so the no-downgrade rule cannot pair it"
                )
            continue
        if len(olds) > 1 or len(news) > 1:
            violations.append(
                f"{path}: refused, {_describe(identity)} has more than one versioned line "
                f"({', '.join(olds)} to {', '.join(news)}), so the no-downgrade rule cannot "
                "pair them"
            )
            continue
        violations.extend(_compare_versions(path, identity, olds[0], news[0]))
    return violations


def _hash_violations(  # noqa: PLR0913 - the rule's whole input, as _downgrades takes it
    path: str,
    pattern: re.Pattern[str],
    groups: Sequence[str],
    hashes: Sequence[str],
    before: Sequence[str],
    after: Sequence[str],
) -> list[str]:
    """The hash rule over one whole file: a hash changes only with its block's version.

    A hash line is a line a ``(?P<hash>…)`` or ``(?P<hash_<suffix>>…)`` group
    marks. Its identity is its block's anchor value and the text before the
    group, as for a versioned line (:func:`_marked`), and its block is the one
    that anchor value names (under a rule with no anchor group, the whole file).
    Per identity:

    - on both sides, with the same hashes: nothing to check;
    - on both sides, with a hash changed: allowed only when a version of the
      same block also changes (some versioned identity with that anchor value
      has different versions before and after); otherwise refused, naming
      the line;
    - after only: a hash pinned for the first time (a new block, or a new key
      in an existing block), allowed;
    - before only: allowed when its whole block goes, or under a rule with no
      anchor group (a dropped pin). Under a rule with one, a hash line that
      disappears from a block that stays is refused: moving it above its
      block's anchor line would otherwise give it a new identity and let it
      pass as a first pin.

    A rule with hash groups and no version group has no version to change
    with, so any change to an existing hash is refused.
    """
    old_hashes, old_anchors = _marked(pattern, hashes, before)
    new_hashes, new_anchors = _marked(pattern, hashes, after)
    old_versions, _ = _versioned(pattern, groups, before)
    new_versions, _ = _versioned(pattern, groups, after)
    bumped_blocks = {
        identity[0]
        for identity in old_versions.keys() | new_versions.keys()
        if old_versions.get(identity, []) != new_versions.get(identity, [])
    }
    anchored = _ANCHOR_GROUP in pattern.groupindex
    identities = sorted(
        old_hashes.keys() | new_hashes.keys(),
        key=lambda ident: (ident[0] is not None, ident[0] or "", ident[1]),
    )
    violations: list[str] = []
    for identity in identities:
        olds, news = old_hashes.get(identity, []), new_hashes.get(identity, [])
        old_values, new_values = [v for v, _ in olds], [v for v, _ in news]
        if old_values == new_values or not olds:
            continue
        anchor = identity[0]
        if not news:
            whole_block = not anchored or (
                anchor is not None and (anchor in old_anchors) != (anchor in new_anchors)
            )
            if not whole_block:
                violations.append(
                    f"{path}: refused, hash line {_quote(olds[0][1])}{_block(anchor)} is removed "
                    "from a block that stays, so the hash rule cannot pair it"
                )
            continue
        if anchor in bumped_blocks:
            continue
        line = next((ln for v, ln in news if v not in old_values), news[0][1])
        violations.append(
            f"{path}: refused, hash line {_quote(line)}{_block(anchor)} changes while no version "
            "in its block changes (a hash changes only with its block's version)"
        )
    return violations


def _block(anchor: str | None) -> str:
    return "" if anchor is None else f" (anchor {anchor!r})"


def _compare_versions(path: str, identity: _Identity, old: str, new: str) -> list[str]:
    """A violation if the paired version *new* is below *old*, or either is not numeric."""
    old_t, new_t = _version_tuple(old), _version_tuple(new)
    if old_t is None or new_t is None:
        return [
            f"{path}: refused, {_describe(identity)} has a version that is not numeric "
            f"({old!r} to {new!r}), so the no-downgrade rule cannot compare it"
        ]
    if new_t < old_t:
        return [
            f"{path}: refused, {_describe(identity)} lowers its version from {old} to {new} "
            "(a refresh never lowers a pinned version)"
        ]
    return []


# -- the path fence: vergil.toml ----------------------------------------------------


def _vergil_toml_violations(before: str | None, after: str | None) -> list[str]:
    """``vergil.toml``'s own rules (spec §6.2); one message per violation.

    Outside ``[[package]]`` nothing may change. An existing entry (matched by
    ``name``) may change only ``upstream-version`` and ``summary``; an
    existing entry is never removed (dropping a series is a human change);
    a new entry must be a series copy of an existing one.
    Every changed or new ``upstream-version`` must be ``<cpython>+<pbs_tag>``
    and must not go down.
    """
    if before is None or after is None:
        return [f"{CONFIG_FILE}: refused, a refresh may edit it but never add or delete it"]
    docs: list[dict[str, Any]] = []
    for label, text in (("before", before), ("after", after)):
        try:
            docs.append(tomllib.loads(text))
        except tomllib.TOMLDecodeError as exc:
            return [f"{CONFIG_FILE}: refused, it does not parse {label} the patch ({exc})"]
    old_doc, new_doc = docs
    violations = [
        f"{CONFIG_FILE}: refused, [{key}] changed; outside [[package]] nothing may change"
        for key in sorted(set(old_doc) | set(new_doc))
        if key != "package" and old_doc.get(key) != new_doc.get(key)
    ]
    if old_doc.get("package") != new_doc.get("package"):
        violations.extend(_package_violations(old_doc.get("package"), new_doc.get("package")))
    return violations


def _package_entries(value: object) -> dict[str, dict[str, Any]] | str:
    """The ``[[package]]`` entries by name, or why they cannot be matched."""
    if value is None:
        return {}
    entries = [value] if isinstance(value, dict) else value
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return "[[package]] must be a table or an array of tables"
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or name in by_name:
            return (
                "every [[package]] entry must have a unique string name for the fence to match it"
            )
        by_name[name] = entry
    return by_name


def _package_violations(old_value: object, new_value: object) -> list[str]:
    old = _package_entries(old_value)
    if isinstance(old, str):
        return [f"{CONFIG_FILE}: refused, {old}"]
    new = _package_entries(new_value)
    if isinstance(new, str):
        return [f"{CONFIG_FILE}: refused, {new}"]
    violations = [
        f"{CONFIG_FILE}: refused, [[package]] {name!r} is removed; a refresh never removes "
        "an existing entry (dropping a series is a human change)"
        for name in sorted(old.keys() - new.keys())
    ]
    for name, entry in new.items():
        violations.extend(_upstream_version_violations(name, old.get(name), entry))
        if name in old:
            violations.extend(
                f"{CONFIG_FILE}: refused, [[package]] {name!r}: {key} changed; only "
                "upstream-version and summary may change"
                for key in sorted(set(old[name]) | set(entry))
                if key not in _FREE_PACKAGE_KEYS and old[name].get(key) != entry.get(key)
            )
        else:
            violations.extend(_series_copy_violations(name, entry, old))
    return violations


def _upstream_key(match: re.Match[str]) -> tuple[tuple[int, ...], int]:
    """An ``upstream-version`` as ``(<cpython> numbers, <pbs_tag>)``, compared in that order."""
    return tuple(int(part) for part in match["cpython"].split(".")), int(match["pbs"])


def _upstream_version_violations(
    name: str, old: dict[str, Any] | None, new: dict[str, Any]
) -> list[str]:
    old_value = old.get("upstream-version") if old is not None else None
    new_value = new.get("upstream-version")
    where = f"{CONFIG_FILE}: refused, [[package]] {name!r}: upstream-version"
    if new_value is None and old_value is not None:
        return [f"{where} is removed; a refresh never removes a pinned version"]
    if new_value is None or new_value == old_value:
        return []
    new_match = _UPSTREAM_VERSION_RE.fullmatch(new_value) if isinstance(new_value, str) else None
    if new_match is None:
        return [f"{where} {new_value!r} does not match {_UPSTREAM_VERSION_RE.pattern}"]
    old_match = _UPSTREAM_VERSION_RE.fullmatch(old_value) if isinstance(old_value, str) else None
    if old_match is None:
        return []
    if _upstream_key(new_match) < _upstream_key(old_match):
        return [
            f"{where} lowers its version from {old_value} to {new_value} "
            "(a refresh never lowers a pinned version)"
        ]
    return []


def _series_swaps(old_name: str, new_name: str) -> list[tuple[str, str]]:
    """Each ``(old, new)`` series token whose swap turns *old_name* into *new_name*."""
    swaps: list[tuple[str, str]] = []
    for match in _SERIES_TOKEN_RE.finditer(old_name):
        prefix, suffix = old_name[: match.start()], old_name[match.end() :]
        if len(new_name) <= len(prefix) + len(suffix):
            continue
        if not (new_name.startswith(prefix) and new_name.endswith(suffix)):
            continue
        token = new_name[len(prefix) : len(new_name) - len(suffix)]
        if token != match.group() and _SERIES_TOKEN_RE.fullmatch(token):
            swaps.append((match.group(), token))
    return swaps


def _swap(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_swap(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _swap(item, old, new) for key, item in value.items()}
    return value


def _copy_differences(
    source: dict[str, Any], entry: dict[str, Any], swap: tuple[str, str]
) -> list[str]:
    """The keys where *entry* is not *source* with the series token swapped."""
    skip = _FREE_PACKAGE_KEYS | {"name"}
    return [
        key
        for key in sorted((set(source) | set(entry)) - skip)
        if key not in source or key not in entry or _swap(source[key], *swap) != entry[key]
    ]


def _series_copy_violations(
    name: str, entry: dict[str, Any], existing: dict[str, dict[str, Any]]
) -> list[str]:
    """Refuse a new entry unless it is a series copy of one of *existing* (spec §6.2)."""
    best: tuple[str, tuple[str, str], list[str]] | None = None
    for source_name, source in existing.items():
        for swap in _series_swaps(source_name, name):
            differences = _copy_differences(source, entry, swap)
            if not differences:
                return []
            if best is None or len(differences) < len(best[2]):
                best = (source_name, swap, differences)
    where = f"{CONFIG_FILE}: refused, new [[package]] {name!r}"
    if best is None:
        return [
            f"{where} is not a series copy: its name differs from every existing entry's "
            r"by more than one \d+\.\d+ series token"
        ]
    source_name, (old, new), differences = best
    return [
        f"{where}: {key} is not {source_name!r}'s with {old} replaced by {new} "
        "(a new entry must be a series copy)"
        for key in differences
    ]
