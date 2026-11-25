"""Resolve a vergil-tooling ref once and render its locked ``uv tool install``.

Every vergil-tooling install installs one commit (epic
vergil-project/.github#1350, spec §4.1 and §4.2):

- a **line** pin ``vX.Y`` follows its rolling tag: it resolves to the commit
  the tag ``vX.Y`` peels to and the one ``vX.Y.Z`` tag on that commit, never
  simply the newest ``vX.Y.Z``, so a line demoted with ``vrg-promote`` is
  followed by every caller (epic vergil-project/.github#1440, spec §4.6). An
  **exact** pin ``vX.Y.Z`` resolves to its SHA. Each takes one
  ``git ls-remote``. Everything after the resolution uses the SHA: the code
  (``git+…@<sha>``), the ``uv.lock`` the constraints come from, and the log;
- a released install is constrained by that commit's own ``uv.lock``, read
  over git (a shallow fetch of the commit and ``uv export``), never through a
  raw-content URL;
- a **dev** ref installs from git, unconstrained;
- every install runs ``uv tool install --no-config --python 3.15
  --managed-python`` with ``UV_PYTHON``, ``UV_PYTHON_PREFERENCE`` and
  ``UV_NO_MANAGED_PYTHON`` unset, so no uv config file and no interpreter
  variable can change or block it.

The install is rendered as shell lines (:func:`install_lines`,
:func:`check_lines`) for the callers that run it somewhere else: the
container cache build and ``vrg-vm``. The lines run under ``bash`` and need
no ``set -e``: each step that can fail names itself and exits non-zero.

:func:`install_ref` runs the same released install in Python, here, for
a host: ``vrg-tooling-install`` (vergil-tooling#4827), which takes a line
``vX.Y`` through the same resolver as well as an exact ``vX.Y.Z``, and any
other git ref of vergil-tooling (a branch, a tag or a full commit SHA,
vergil-tooling#4841), resolved to one commit and locked to that commit's
``uv.lock`` too. :func:`resolve` keeps its release-or-dev contract for the
rendered installs. The Mac's
install stage (``vergil.toml``'s ``[publish].install``) runs the
installed ``vrg-tooling-install`` (vergil-tooling#4830), so the
installed vergil-tooling installs the just-released one; that reverses spec
§4.5's "nothing runs vergil-tooling to install vergil-tooling" for the Mac.
If the installed copy is broken, the documented install by hand is the
workaround while it is fixed. The container and VM paths keep the
rendered lines for now; vergil-project/.github#1435 looks at moving them to
:func:`install_ref`.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

GIT_URL = "https://github.com/vergil-project/vergil-tooling"

_LINE = re.compile(r"v(\d+)\.(\d+)")
_EXACT = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_PACKAGE = "vergil-tooling"
_PYTHON = "3.15"
_UV_VARS = ("UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_NO_MANAGED_PYTHON")
_SHA = re.compile(r"[0-9a-f]{40}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HOME_PREFIX = "$HOME/"
# A constraints line that names the project itself, an editable, a local path
# or a git source: none belongs in a third-party constraints file.
_NOT_THIRD_PARTY = r"^(-e[[:space:]]|vergil-tooling([[:space:]=@;<>!~]|$)|\.|/|git\+)"
# The same test as _NOT_THIRD_PARTY (a POSIX ERE for grep), for Python.
_NOT_THIRD_PARTY_RE = re.compile(r"^(-e\s|vergil-tooling([\s=@;<>!~]|$)|\.|/|git\+)")
_MESSAGE_VARS = {
    "<sha>": "${vt_sha}",
    "<found>": "${vt_found:-nothing}",
    "<expected>": "${vt_expected}",
}
_EXPORT = (
    "uv export --no-config --frozen --no-dev --no-emit-project --no-hashes "
    "--format requirements-txt"
)


def classify_ref(ref: str) -> Literal["line", "exact", "dev"]:
    """Classify a tooling ref: a release line, an exact release, or a dev ref."""
    if _EXACT.fullmatch(ref):
        return "exact"
    if _LINE.fullmatch(ref):
        return "line"
    return "dev"


class ToolingInstallError(RuntimeError):
    """A vergil-tooling ref could not be resolved or its install rendered."""


@dataclass(frozen=True)
class ResolvedRef:
    """A vergil-tooling ref and what it resolved to.

    ``tag`` and ``sha`` are both set for a resolved release. An exact pin from
    :func:`unresolved` has its ``tag`` and no ``sha``: the rendered install
    resolves it, once, at run time. A dev ref has neither.
    """

    ref: str
    kind: Literal["line", "exact", "dev"]
    tag: str | None
    sha: str | None


def git_ls_remote(url: str, pattern: str) -> str:
    """``git ls-remote --tags <url> <pattern>``'s output; raises on any failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "ls-remote", "--tags", url, pattern),  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"git ls-remote {url} {pattern} could not run: {exc}"
        raise ToolingInstallError(msg) from exc
    if result.returncode != 0:
        msg = (
            f"git ls-remote {url} {pattern} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
        raise ToolingInstallError(msg)
    return result.stdout


def _tag_shas(listing: str) -> dict[str, str]:
    """Map each tag in an ``ls-remote`` *listing* to its commit SHA.

    An annotated tag lists twice: the tag object, then ``<tag>^{}``, the
    commit it points at. The commit wins; a lightweight tag lists once, as
    its commit.
    """
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/tags/"):  # noqa: PLR2004
            continue
        sha, name = fields[0], fields[1].removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            direct[name] = sha
    return {name: peeled.get(name, sha) for name, sha in direct.items()}


def resolve(ref: str, *, ls_remote: Callable[[str, str], str]) -> ResolvedRef:
    """Resolve *ref* to one tag and commit SHA with a single *ls_remote* call.

    A line ``vX.Y`` follows its rolling tag: the one listing of
    ``refs/tags/vX.Y*`` holds the tag ``vX.Y`` itself, peeled to its commit,
    and the line's ``vX.Y.Z`` tags, and the release the line names is the one
    ``vX.Y.Z`` tag on that commit. It is never simply the newest ``vX.Y.Z``,
    so a line demoted with ``vrg-promote`` is followed. An exact ``vX.Y.Z``
    resolves to itself. Both carry the tag's commit SHA. A dev ref returns
    with no tag and no SHA, and makes no call. *ls_remote* is
    :func:`git_ls_remote`'s shape, ``(url, pattern) -> listing``.

    A failed call, a missing tag, or a line tag whose commit carries no
    ``vX.Y.Z`` tag (or more than one) raises :class:`ToolingInstallError`
    naming the URL and the ref, never falling back to another or an
    already-installed release.
    """
    kind = classify_ref(ref)
    if kind == "dev":
        return ResolvedRef(ref=ref, kind="dev", tag=None, sha=None)
    tag, sha = _resolve_release(ref, kind, ls_remote=ls_remote)
    return ResolvedRef(ref=ref, kind=kind, tag=tag, sha=sha)


def _resolve_release(
    ref: str, kind: Literal["line", "exact"], *, ls_remote: Callable[[str, str], str]
) -> tuple[str, str]:
    """:func:`resolve` of a release *ref*: its exact ``vX.Y.Z`` tag and commit SHA."""
    try:
        listing = ls_remote(GIT_URL, f"refs/tags/{ref}*")
    except (ToolingInstallError, OSError, subprocess.SubprocessError) as exc:
        msg = f"could not list the tags of {GIT_URL} to resolve {ref}: {exc}"
        raise ToolingInstallError(msg) from exc
    tags = _tag_shas(listing)
    if ref not in tags:
        detail = f": the line tag {ref} does not exist" if kind == "line" else ""
        msg = f"no release tag for {ref} at {GIT_URL}{detail}"
        raise ToolingInstallError(msg)
    tag = ref if kind == "exact" else _release_on_line(ref, tags)
    sha = tags[tag]
    if not _SHA.fullmatch(sha):
        msg = f"tag {tag} at {GIT_URL} lists {sha!r}, not a commit SHA"
        raise ToolingInstallError(msg)
    return tag, sha


def _release_on_line(line: str, tags: Mapping[str, str]) -> str:
    """The one ``vX.Y.Z`` tag in *tags* on the commit the *line* tag peels to."""
    commit = tags[line]
    on_line = re.compile(re.escape(line) + r"\.\d+")
    releases = sorted(n for n, sha in tags.items() if sha == commit and on_line.fullmatch(n))
    if len(releases) != 1:
        found = f"several ({', '.join(releases)})" if releases else "none"
        msg = (
            f"the line tag {line} at {GIT_URL} names commit {commit}, which carries "
            f"{found} of the {line}.Z release tags, not exactly one"
        )
        raise ToolingInstallError(msg)
    return releases[0]


def unresolved(ref: str) -> ResolvedRef:
    """An exact pin with no SHA, built with no network call.

    :func:`install_lines` resolves its SHA in shell, once, where the install
    runs. So an exact pin's cache key can use the tag alone, offline. A line or
    dev ref raises :class:`ToolingInstallError`: a line must be resolved
    first, and a dev ref has no tag.
    """
    if classify_ref(ref) != "exact":
        msg = f"{ref!r} is not an exact release (vX.Y.Z); only an exact pin installs unresolved"
        raise ToolingInstallError(msg)
    return ResolvedRef(ref=ref, kind="exact", tag=ref, sha=None)


def _say(msg: str) -> str:
    """A shell line printing *msg* (a constant, shell-quoted) to stderr."""
    return f"printf '%s\\n' {shlex.quote(msg)} >&2"


def _fail(msg: str) -> str:
    """A shell fragment, for after ``||``, printing *msg* to stderr and exiting 1.

    *msg* is escaped for double quotes, then each ``<sha>``, ``<found>`` and
    ``<expected>`` in it becomes a reference to that ``vt_*`` variable, so no
    ref or path in *msg* is ever expanded or run.
    """
    escaped = re.sub(r'([\\"$`])', r"\\\1", f"vergil-tooling: {msg}")
    for token, var in _MESSAGE_VARS.items():
        escaped = escaped.replace(token, var)
    return f"{{ printf '%s\\n' \"{escaped}\" >&2; exit 1; }}"


def _shell_path(path: str) -> str:
    """*path* as a shell word: absolute, or under ``$HOME/`` (expanded at run time)."""
    if path.startswith(_HOME_PREFIX):
        return f'"$HOME"/{shlex.quote(path.removeprefix(_HOME_PREFIX))}'
    if path.startswith("/"):
        return shlex.quote(path)
    msg = f"{path!r} is neither an absolute path nor under $HOME/"
    raise ToolingInstallError(msg)


def _environment_lines(
    python_dir_env: str | None, tool_dir: str | None, bin_dir: str | None
) -> list[str]:
    lines = [f"unset {' '.join(_UV_VARS)}"]
    if python_dir_env is not None:
        if not _ENV_NAME.fullmatch(python_dir_env):
            msg = f"{python_dir_env!r} is not a shell variable name"
            raise ToolingInstallError(msg)
        lines += [
            f'if [ -n "${{{python_dir_env}:-}}" ]; then',
            f'  export UV_PYTHON_INSTALL_DIR="${python_dir_env}"',
            "  export UV_PYTHON_DOWNLOADS=never",
            "else",
            f"  {_say(f'vergil-tooling: no {python_dir_env}; uv may download {_PYTHON}')}",
            "fi",
        ]
    if tool_dir is not None:
        lines.append(f"export UV_TOOL_DIR={_shell_path(tool_dir)}")
    if bin_dir is not None:
        lines.append(f"export UV_TOOL_BIN_DIR={_shell_path(bin_dir)}")
    return lines


def _resolve_in_shell_lines(tag: str) -> list[str]:
    """Shell lines setting ``vt_sha`` to *tag*'s commit, with one ``git ls-remote``."""
    name = f"refs/tags/{tag}"
    peel = '$2 == t "^{}" {p = $1} $2 == t {d = $1} END {print (p != "" ? p : d)}'
    return [
        f'vt_refs="$(git ls-remote --tags {GIT_URL} {shlex.quote(name + "*")})" '
        f"|| {_fail(f'listing the tags of {GIT_URL} for {tag} failed')}",
        f'vt_sha="$(printf \'%s\\n\' "$vt_refs" | awk -v t={shlex.quote(name)} '
        f'{shlex.quote(peel)})"',
        f'[[ "$vt_sha" =~ ^[0-9a-f]{{40}}$ ]] || {_fail(f"no release tag {tag} at {GIT_URL}")}',
    ]


def _released_lines(tag: str) -> list[str]:
    """Shell lines exporting commit ``$vt_sha``'s lock and installing it constrained."""
    spec = f"{_PACKAGE} @ git+{GIT_URL}@"
    return [
        f'vt_tmp="$(mktemp -d)" || {_fail("mktemp failed")}',
        "trap 'rm -rf \"$vt_tmp\"' EXIT",
        '{ git init -q "$vt_tmp/src" && '
        f'git -C "$vt_tmp/src" fetch -q --depth 1 {GIT_URL} "$vt_sha" && '
        'git -C "$vt_tmp/src" checkout -q FETCH_HEAD; } '
        f"|| {_fail(f'fetching commit <sha> ({tag}) from {GIT_URL} failed')}",
        f'(cd "$vt_tmp/src" && {_EXPORT} -o "$vt_tmp/constraints.txt" >/dev/null) '
        f"|| {_fail(f'exporting the uv.lock of commit <sha> ({tag}) failed')}",
        f'[ -s "$vt_tmp/constraints.txt" ] '
        f"|| {_fail(f'the uv.lock export of commit <sha> ({tag}) is empty')}",
        f'if grep -Eq {shlex.quote(_NOT_THIRD_PARTY)} "$vt_tmp/constraints.txt"; then '
        f"{_fail(f'the uv.lock export of commit <sha> ({tag}) names a non-registry package')}"
        "; fi",
        f"uv tool install --force --no-config --python {_PYTHON} --managed-python "
        f'-c "$vt_tmp/constraints.txt" "{spec}$vt_sha" '
        f"|| {_fail(f'uv tool install of {tag} (commit <sha>) failed')}",
        'rm -rf "$vt_tmp"',
        "trap - EXIT",
        f"printf '%s\\n' \"vergil-tooling: installed {tag} (commit $vt_sha)\" >&2",
    ]


def install_lines(
    resolved: ResolvedRef,
    *,
    python_dir_env: str | None,
    tool_dir: str | None,
    bin_dir: str | None,
) -> list[str]:
    """The shell lines installing *resolved* with uv.

    They clear the three ``UV_*`` interpreter variables, export the commit's
    ``uv.lock`` as constraints, run ``uv tool install --force`` of the commit
    and log its tag and SHA. A dev ref installs unconstrained, with
    ``--reinstall`` so a moved branch is fetched again.

    *python_dir_env* names a variable (e.g. ``"VERGIL_CORE_PYTHON_DIR"``):
    when it is set at run time the install uses the managed Python under it and
    never downloads one; when it is not, the lines log that uv may download
    3.15. *tool_dir* and *bin_dir* set ``UV_TOOL_DIR`` and ``UV_TOOL_BIN_DIR``;
    each is absolute or starts with ``$HOME/``. ``None`` keeps uv's default.

    An exact pin from :func:`unresolved` resolves its SHA in shell first, with
    one ``git ls-remote``, and fails if the tag is missing.

    The lines set ``vt_*`` shell variables, and a released install sets an
    ``EXIT`` trap that removes its temporary directory, then clears it once
    the install succeeds; a caller's own ``EXIT`` trap does not survive them.
    """
    lines = _environment_lines(python_dir_env, tool_dir, bin_dir)
    if resolved.kind == "dev":
        spec = shlex.quote(f"{_PACKAGE} @ git+{GIT_URL}@{resolved.ref}")
        return [
            *lines,
            f"uv tool install --force --reinstall --no-config --python {_PYTHON} "
            f"--managed-python {spec} "
            f"|| {_fail(f'uv tool install of dev ref {resolved.ref} failed')}",
            _say(f"vergil-tooling: installed dev ref {resolved.ref} (unconstrained)"),
        ]
    if resolved.tag is None:
        msg = f"released ref {resolved.ref!r} has no tag"
        raise ToolingInstallError(msg)
    if resolved.sha is None:
        lines += _resolve_in_shell_lines(resolved.tag)
    elif _SHA.fullmatch(resolved.sha):
        lines.append(f"vt_sha={resolved.sha}")
    else:
        msg = f"{resolved.sha!r} is not a commit SHA"
        raise ToolingInstallError(msg)
    return [*lines, *_released_lines(resolved.tag)]


def check_lines(bin_dir: str) -> list[str]:
    """Shell lines checking ``vrg-whoami`` resolves to *bin_dir* and runs.

    A stale ``/usr/bin/vrg-whoami`` shim (an old package install) earlier on
    ``PATH`` fails the check, as does a ``vrg-whoami`` that does not run.
    *bin_dir* is absolute or starts with ``$HOME/``.
    """
    expected = f"{_shell_path(bin_dir)}/vrg-whoami"
    return [
        f"vt_expected={expected}",
        'vt_found="$(command -v vrg-whoami || true)"',
        '[ "$vt_found" = "$vt_expected" ] '
        f"|| {_fail('vrg-whoami resolves to <found>, not <expected>')}",
        f"vrg-whoami --mode >/dev/null || {_fail('<expected> --mode failed')}",
    ]


@dataclass(frozen=True)
class Installed:
    """What :func:`install_ref` installed: the ref it reports and its commit.

    ``ref`` is the exact ``vX.Y.Z`` for a release (a line reports the release
    its tag names) and the ref as given for any other ref.
    """

    ref: str
    sha: str


@dataclass(frozen=True)
class _Target:
    """What :func:`install_ref` fetches, and how its steps and errors name it."""

    ref: str
    label: str
    fetch: str
    expected_sha: str | None
    source: str


# What git's ref-name rules (``git check-ref-format``) refuse, with ``:``
# refusing a URL or a refspec too.
_REF_BAD_CHARS = frozenset(" ~^:?*[\\")
_REF_BAD_SEQUENCES = ("..", "@{", "//")
_REF_NAMESPACES = ("refs/heads/", "refs/tags/")
_DEL = 0x7F
_SPACE = 0x20


def check_ref_name(ref: str) -> None:
    """Raise unless *ref* is a plain git ref name, never an option, URL or refspec.

    It refuses anything starting with ``-`` (so it can never be read as an
    option), and anything git's ref-name rules refuse: a ``:`` (which a URL
    or a refspec needs), whitespace or a control character, any of
    ``~^?*[\\``, ``..``, ``@{``, ``//``, a leading ``+`` or ``/``, a trailing
    ``/`` or ``.``, a ``.lock`` suffix, a path component starting with ``.``,
    or ``@`` alone. A full name under ``refs/`` must be a branch
    (``refs/heads/``) or a tag (``refs/tags/``). It checks the form only:
    whether the ref exists is for the resolution to say.
    """
    if not ref:
        msg = "the ref is empty"
        raise ToolingInstallError(msg)
    if ref.startswith("-"):
        msg = f"{ref!r} starts with '-'; a ref is never an option"
        raise ToolingInstallError(msg)
    bad = (
        any(c in _REF_BAD_CHARS or ord(c) <= _SPACE or ord(c) == _DEL for c in ref)
        or any(s in ref for s in _REF_BAD_SEQUENCES)
        or ref.startswith(("+", "/"))
        or ref.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in ref.split("/"))
        or ref == "@"
    )
    if bad:
        msg = f"{ref!r} is not a git ref name (a branch, tag or commit SHA of {GIT_URL})"
        raise ToolingInstallError(msg)
    if ref.startswith("refs/") and not ref.startswith(_REF_NAMESPACES):
        msg = f"{ref!r} is neither a branch (refs/heads/) nor a tag (refs/tags/)"
        raise ToolingInstallError(msg)


def git_ls_refs(url: str, patterns: tuple[str, ...]) -> str:
    """``git ls-remote <url> <patterns…>``'s output; raises on any failure."""
    shown = " ".join(patterns)
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "ls-remote", url, *patterns),  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"git ls-remote {url} {shown} could not run: {exc}"
        raise ToolingInstallError(msg) from exc
    if result.returncode != 0:
        msg = (
            f"git ls-remote {url} {shown} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
        raise ToolingInstallError(msg)
    return result.stdout


def _ref_shas(listing: str) -> dict[str, str]:
    """Map each ref in an ``ls-remote`` *listing* to its commit, peeling annotated tags."""
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/"):  # noqa: PLR2004
            continue
        sha, name = fields
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            direct[name] = sha
    return {name: peeled.get(name, sha) for name, sha in direct.items()}


def _resolve_any(ref: str, *, ls_refs: Callable[[str, tuple[str, ...]], str]) -> _Target:
    """The commit a non-release *ref* names: a full SHA as itself, else its branch or tag.

    A 40-character SHA names its commit and makes no call. Any other ref is
    looked up with one *ls_refs* call among the branches and tags of
    :data:`GIT_URL` (never its pull-request or other refs): it must name
    exactly one, else the resolution fails, naming the URL and the ref.
    """
    if _SHA.fullmatch(ref):
        return _Target(ref, f"commit {ref}", ref, ref, "ref names")
    names = (ref,) if ref.startswith(_REF_NAMESPACES) else tuple(p + ref for p in _REF_NAMESPACES)
    try:
        listing = ls_refs(GIT_URL, names)
    except (ToolingInstallError, OSError, subprocess.SubprocessError) as exc:
        msg = f"could not list the refs of {GIT_URL} to resolve {ref}: {exc}"
        raise ToolingInstallError(msg) from exc
    refs = _ref_shas(listing)
    found = [name for name in names if name in refs]
    if not found:
        msg = f"no branch or tag {ref} at {GIT_URL} (a commit is named by its full 40-character SHA)"
        raise ToolingInstallError(msg)
    if len(found) > 1:
        msg = f"{ref} is both a branch and a tag at {GIT_URL}; name one: {' or '.join(found)}"
        raise ToolingInstallError(msg)
    name = found[0]
    sha = refs[name]
    if not _SHA.fullmatch(sha):
        msg = f"{name} at {GIT_URL} lists {sha!r}, not a commit SHA"
        raise ToolingInstallError(msg)
    kind = "branch" if name.startswith("refs/heads/") else "tag"
    label = f"{kind} {name.removeprefix('refs/heads/').removeprefix('refs/tags/')}"
    return _Target(ref, label, sha, sha, f"{label} names")


def _target(
    ref: str,
    *,
    ls_remote: Callable[[str, str], str] | None,
    ls_refs: Callable[[str, tuple[str, ...]], str] | None,
) -> _Target:
    """What :func:`install_ref` fetches for *ref*, resolved (but not fetched) here."""
    kind = classify_ref(ref)
    if kind == "dev":
        check_ref_name(ref)
        return _resolve_any(ref, ls_refs=ls_refs or git_ls_refs)
    if kind == "line":
        tag, sha = _resolve_release(ref, kind, ls_remote=ls_remote or git_ls_remote)
        return _Target(tag, f"tag {tag}", f"refs/tags/{tag}", sha, "line's tag names")
    return _Target(ref, f"tag {ref}", f"refs/tags/{ref}", None, "")


def install_ref(
    ref: str,
    *,
    environ: Mapping[str, str] | None = None,
    ls_remote: Callable[[str, str], str] | None = None,
    ls_refs: Callable[[str, tuple[str, ...]], str] | None = None,
) -> Installed:
    """Install the commit *ref* names with uv here, locked to that commit's ``uv.lock``.

    *ref* is a git ref of :data:`GIT_URL`, and only of it
    (vergil-tooling#4841):

    - a line ``vX.Y`` is first resolved with :func:`resolve`'s release
      resolution (one *ls_remote* call, default :func:`git_ls_remote`) to the
      exact ``vX.Y.Z`` its rolling tag names, and that exact tag is what is
      fetched, installed and reported. The fetched commit must be the one the
      line resolved to;
    - an exact ``vX.Y.Z`` is fetched as that tag;
    - any other ref is first checked by :func:`check_ref_name`, then
      resolved: a full commit SHA as itself, a branch (``develop``) or any
      other tag with one *ls_refs* call (default :func:`git_ls_refs`) to the
      commit it names now. That commit is fetched by its SHA, and the fetched
      commit must be it. It is reported as *ref*, as given.

    :func:`resolve` and its other callers are unchanged: the container cache
    and ``vrg-vm`` still install a non-release ref from git, unconstrained,
    while this host install locks every ref to its commit's ``uv.lock``.

    In a temporary directory it shallow-fetches the commit over git, reads it,
    exports its ``uv.lock`` as constraints and runs ``uv tool install --force
    --no-config --python 3.15 --managed-python`` of that commit under them.
    The code and the lock therefore come from one commit. ``UV_PYTHON``,
    ``UV_PYTHON_PREFERENCE`` and ``UV_NO_MANAGED_PYTHON`` are removed from
    the environment each command sees (*environ*, or ``os.environ``), so no
    interpreter variable can change or block the install. The temporary
    directory is removed however the install ends.

    Every failure raises :class:`ToolingInstallError` naming the step that
    failed and its error output: a *ref* that is not a ref name, one that
    does not resolve, a failed git or uv command, a fetch that names no
    commit (or not the resolved one), an empty constraints export, or one
    that names a non-registry package. ``uv tool install``'s own output goes
    straight to this process's standard output and error.

    The running ``vrg-tooling-install`` is itself an installed vergil-tooling,
    which the install replaces: nothing is imported after it.
    """
    target = _target(ref, ls_remote=ls_remote, ls_refs=ls_refs)
    name, label = target.ref, target.label
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if key not in _UV_VARS}
    with tempfile.TemporaryDirectory(prefix="vrg-tooling-install-") as tmp:
        src = Path(tmp) / "src"
        constraints = Path(tmp) / "constraints.txt"
        _run_step(("git", "init", "-q", str(src)), env=env, what="creating a git repository")
        _run_step(
            ("git", "-C", str(src), "fetch", "-q", "--depth", "1", GIT_URL, target.fetch),
            env=env,
            what=f"fetching {label} from {GIT_URL}",
        )
        _run_step(
            ("git", "-C", str(src), "checkout", "-q", "FETCH_HEAD"),
            env=env,
            what=f"checking out {label}",
        )
        sha = _run_step(
            ("git", "-C", str(src), "rev-parse", "HEAD"),
            env=env,
            what=f"reading the commit of {label}",
        ).strip()
        if not _SHA.fullmatch(sha):
            msg = f"{label} from {GIT_URL} checked out as {sha!r}, not a commit SHA"
            raise ToolingInstallError(msg)
        if target.expected_sha is not None and sha != target.expected_sha:
            msg = (
                f"{label} from {GIT_URL} checked out as commit {sha}, not the commit "
                f"{target.expected_sha} the {target.source}"
            )
            raise ToolingInstallError(msg)
        _run_step(
            (*shlex.split(_EXPORT), "-o", str(constraints)),
            env=env,
            cwd=src,
            what=f"exporting the uv.lock of {name} (commit {sha})",
        )
        _check_constraints(constraints, tag=name, sha=sha)
        spec = f"{_PACKAGE} @ git+{GIT_URL}@{sha}"
        argv = (
            *("uv", "tool", "install", "--force", "--no-config"),
            *("--python", _PYTHON, "--managed-python", "-c", str(constraints), spec),
        )
        sys.stdout.flush()
        try:
            result = subprocess.run(argv, env=env, check=False)  # noqa: S603
        except OSError as exc:
            msg = f"uv tool install of {name} (commit {sha}) could not run: {exc}"
            raise ToolingInstallError(msg) from exc
        if result.returncode != 0:
            msg = f"uv tool install of {name} (commit {sha}) failed (exit {result.returncode})"
            raise ToolingInstallError(msg)
    return Installed(ref=name, sha=sha)


def _run_step(
    argv: tuple[str, ...], *, env: Mapping[str, str], what: str, cwd: Path | None = None
) -> str:
    """Run one install step, capturing its output; its stdout, or raise naming *what*."""
    try:
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, env=env, cwd=cwd, check=False
        )
    except OSError as exc:
        msg = f"{what} failed: {argv[0]} could not run: {exc}"
        raise ToolingInstallError(msg) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no error output"
        msg = f"{what} failed (exit {result.returncode}): {detail}"
        raise ToolingInstallError(msg)
    return result.stdout


def _check_constraints(path: Path, *, tag: str, sha: str) -> None:
    """Raise unless *path* is a non-empty export naming registry packages only."""
    try:
        text = path.read_text()
    except OSError as exc:
        msg = f"the uv.lock export of {tag} (commit {sha}) could not be read: {exc}"
        raise ToolingInstallError(msg) from exc
    if not text.strip():
        msg = f"the uv.lock export of {tag} (commit {sha}) is empty"
        raise ToolingInstallError(msg)
    bad = [line for line in text.splitlines() if _NOT_THIRD_PARTY_RE.match(line)]
    if bad:
        msg = f"the uv.lock export of {tag} (commit {sha}) names a non-registry package: {bad[0]!r}"
        raise ToolingInstallError(msg)
