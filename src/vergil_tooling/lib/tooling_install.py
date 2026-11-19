"""Resolve a vergil-tooling ref once and render its locked ``uv tool install``.

Every vergil-tooling install installs one commit (epic
vergil-project/.github#1350, spec §4.1 and §4.2):

- a **line** pin ``vX.Y`` resolves to the newest ``vX.Y.Z`` tag and its commit
  SHA, and an **exact** pin ``vX.Y.Z`` to its SHA, with one ``git ls-remote``.
  Everything after the resolution uses the SHA: the code
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

The Mac's consumer refresh (``vergil.toml``'s ``[publish].consumer-refresh``)
is the one install that does not use this module: it is plain shell, so
nothing runs vergil-tooling to install vergil-tooling (spec §4.5).
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

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

    A line ``vX.Y`` resolves to its newest ``vX.Y.Z`` tag, an exact ``vX.Y.Z``
    to itself; both carry the tag's commit SHA. A dev ref returns with no tag
    and no SHA, and makes no call. *ls_remote* is :func:`git_ls_remote`'s
    shape, ``(url, pattern) -> listing``. A failed call, or no matching tag,
    raises :class:`ToolingInstallError` naming the URL and the ref, never an
    older or already-installed release.
    """
    kind = classify_ref(ref)
    if kind == "dev":
        return ResolvedRef(ref=ref, kind="dev", tag=None, sha=None)
    try:
        listing = ls_remote(GIT_URL, f"refs/tags/{ref}*")
    except (ToolingInstallError, OSError, subprocess.SubprocessError) as exc:
        msg = f"could not list the tags of {GIT_URL} to resolve {ref}: {exc}"
        raise ToolingInstallError(msg) from exc
    tags = _tag_shas(listing)
    if kind == "exact":
        tag = ref if ref in tags else None
    else:
        on_line = re.compile(re.escape(ref) + r"\.(\d+)")
        patches = {int(m.group(1)): name for name in tags if (m := on_line.fullmatch(name))}
        tag = patches[max(patches)] if patches else None
    if tag is None:
        msg = f"no release tag for {ref} at {GIT_URL}"
        raise ToolingInstallError(msg)
    sha = tags[tag]
    if not _SHA.fullmatch(sha):
        msg = f"tag {tag} at {GIT_URL} lists {sha!r}, not a commit SHA"
        raise ToolingInstallError(msg)
    return ResolvedRef(ref=ref, kind=kind, tag=tag, sha=sha)


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
