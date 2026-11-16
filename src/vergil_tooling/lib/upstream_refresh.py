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
- **publish** (Task 3b) reads that output as untrusted data
  (:func:`read_prepared`), applies the patch in its own clone, and checks it
  against the repo's **path fence** (:func:`fence_violations`) before it
  pushes anything.

The fence is computed only from what publish sees in its own clone: the
touched paths and changed lines of the applied tree, the rules from the
``develop`` tip, and ``vergil.toml`` before and after. ``.github/**`` is
always refused. Nothing here pushes, opens a PR or merges.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import develop_clone
from vergil_tooling.lib.config import CONFIG_FILE, read_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vergil_tooling.lib.config import PathRule, UpstreamRefreshConfig

REPORT_ENV = "VRG_UPSTREAM_REPORT"
COMMIT_SUBJECT = "chore(upstream): refresh upstream inputs"
PATCH_FILE = "changes.patch"
META_FILE = "meta.json"

#: The App's private key variable (spec §6.1). Prepare refuses to run while it
#: is set, so a wiring mistake cannot put the key beside the hook (spec §6).
KEY_ENV = "VRG_AUTOMATION_PRIVATE_KEY"

_BODY_INTRO = (
    "Automated upstream refresh. The upstream-refresh job merges this PR itself once CI passes."
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


class UpstreamRefreshError(Exception):
    """A refusal or failure of the upstream refresh; the message says why."""


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


# -- the path fence: inputs ------------------------------------------------------


def diff_changed_lines(tree: Path, base_sha: str, paths: Sequence[str]) -> dict[str, list[str]]:
    """Each path's ``git diff -U0 <base_sha>`` in *tree*, as :func:`fence_violations` reads it.

    Each value is the path's whole diff section (its header, then its hunks),
    split on ``\\n`` only, so a ``\\r`` stays inside its line. A new file must
    be in the index (``git apply --index``), or it has no diff and the fence
    refuses it. No textconv or external diff driver runs.
    """
    result: dict[str, list[str]] = {}
    for path in paths:
        raw = _git_bytes(
            tree,
            "diff",
            "-U0",
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


def _parse_file_diff(lines: Sequence[str]) -> _FileDiff:
    """Parse one path's diff; a type change (file to symlink) gives two sections, both read."""
    diff = _FileDiff()
    in_header = True
    for line in lines:
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
            diff.hunks.append(([], []))
        elif not in_header:
            removed, added = diff.hunks[-1]
            if line.startswith("-"):
                removed.append(line[1:])
            elif line.startswith("+"):
                added.append(line[1:])
        else:
            _parse_header_line(diff, line)
    return diff


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
    them to its ``git diff -U0 <base_sha>`` lines (:func:`diff_changed_lines`).
    The rules (spec §6.2):

    - a path must be repo-relative and must match a ``rules.paths`` glob; with
      no ``paths``, every path is refused;
    - ``.github/**`` is always refused, whatever the allowlist says;
    - a symlink or submodule is always refused;
    - under a rule with ``lines``, every removed and added line must match the
      regex in full, an added, deleted, binary or mode-changed file is
      refused, and a ``(?P<version>…)`` group may not go down, nor may a
      versioned line be moved or left unpaired beside other edits
      (:func:`_downgrades`);
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
    # With no version group, nothing is versioned, so the no-downgrade check finds nothing.
    return _downgrades(path, pattern, groups, diff.hunks)


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


def _versioned(
    pattern: re.Pattern[str], groups: Sequence[str], lines: Sequence[str]
) -> dict[str, list[str]]:
    """The *lines* where a version group took part, by the text before that group."""
    by_key: dict[str, list[str]] = {}
    for line in lines:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        group = next((name for name in groups if match.start(name) >= 0), None)
        if group is None:
            continue
        by_key.setdefault(line[: match.start(group)], []).append(match.group(group))
    return by_key


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
    hunks: Sequence[tuple[list[str], list[str]]],
) -> list[str]:
    """The no-downgrade rule over one file's hunks; one violation per breach.

    Within a hunk, removed and added versioned lines are paired by the text
    before the version group (the key), in order, and a paired version may
    not go down or be non-numeric. A versioned line left unpaired is a new or
    a dropped series only when its hunk holds nothing but added lines, or
    nothing but removed lines, and no hunk of the file leaves a line of the
    same key unpaired on the other side. Anything else is refused: a pinned
    line moved within the file would otherwise escape the comparison, since
    ``git diff -U0`` can split its removal and its addition into two hunks.
    """
    violations: list[str] = []
    removed_keys: set[str] = set()  # keys with an unpaired removed line in a removal-only hunk
    added_keys: set[str] = set()  # keys with an unpaired added line in an addition-only hunk
    for removed, added in hunks:
        old_by_key = _versioned(pattern, groups, removed)
        new_by_key = _versioned(pattern, groups, added)
        for key in sorted(old_by_key.keys() | new_by_key.keys()):
            olds, news = old_by_key.get(key, []), new_by_key.get(key, [])
            for old, new in zip(olds, news, strict=False):
                violations.extend(_compare_versions(path, key, old, new))
            if len(olds) == len(news):
                continue
            if removed and added:
                violations.append(
                    f"{path}: refused, {key.strip()!r} has a versioned line with no "
                    "counterpart in a hunk that both removes and adds lines, so the "
                    "no-downgrade rule cannot pair it"
                )
            elif removed:
                removed_keys.add(key)
            else:
                added_keys.add(key)
    violations.extend(
        f"{path}: refused, {key.strip()!r} has a versioned line removed in one place and "
        "added in another, so the no-downgrade rule cannot pair it"
        for key in sorted(removed_keys & added_keys)
    )
    return violations


def _compare_versions(path: str, key: str, old: str, new: str) -> list[str]:
    """A violation if the paired version *new* is below *old*, or either is not numeric."""
    old_t, new_t = _version_tuple(old), _version_tuple(new)
    if old_t is None or new_t is None:
        return [
            f"{path}: refused, {key.strip()!r} has a version that is not numeric "
            f"({old!r} to {new!r}), so the no-downgrade rule cannot compare it"
        ]
    if new_t < old_t:
        return [
            f"{path}: refused, {key.strip()!r} lowers its version from {old} to {new} "
            "(a refresh never lowers a pinned version)"
        ]
    return []


# -- the path fence: vergil.toml ----------------------------------------------------


def _vergil_toml_violations(before: str | None, after: str | None) -> list[str]:
    """``vergil.toml``'s own rules (spec §6.2); one message per violation.

    Outside ``[[package]]`` nothing may change. An existing entry (matched by
    ``name``) may change only ``upstream-version`` and ``summary``; a removed
    entry is allowed; a new entry must be a series copy of an existing one.
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
    violations: list[str] = []
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
    if new_value is None or new_value == old_value:
        return []
    where = f"{CONFIG_FILE}: refused, [[package]] {name!r}: upstream-version"
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
