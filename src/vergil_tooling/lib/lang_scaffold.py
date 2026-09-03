"""Born-green language-skeleton scaffolding (epic vergil-project/.github#342, T2).

Adds a per-language skeleton phase to ``vrg-github-repo-init``: render the
language's skeleton templates on the host, stamp the missing ones (never
clobbering an existing file), then — for a language that resolves locks (cpp) —
run its lock command and one full ``vrg-validate`` inside the dev container so a
new repo is *born green*.

The container is a hard, fail-fast precondition for a lock-resolving language:
if no runtime is available the scaffold refuses **before writing anything**, so a
cpp repo is born green or not born at all — never half-created (spec §3.1).
"""

from __future__ import annotations

import re
import subprocess
from importlib import resources
from typing import TYPE_CHECKING

from vergil_tooling.lib import container
from vergil_tooling.lib.languages import language_lock_command

if TYPE_CHECKING:
    from collections.abc import Iterator
    from importlib.resources.abc import Traversable
    from pathlib import Path

    from vergil_tooling.lib.repo_init import RepoInitContext


class ScaffoldError(RuntimeError):
    """Raised when the language-skeleton scaffold cannot complete.

    Covers both the fail-fast container precondition (no runtime) and a
    non-zero lock-resolve or born-green ``vrg-validate`` — every case where the
    repo cannot be brought to a green skeleton.
    """


def sanitize_project_name(repo_name: str) -> str:
    """Derive a C/C++-safe project/namespace identifier from a repo name.

    ``mq-protocol-gateway`` → ``mq_protocol_gateway``; ``Foo.Bar`` → ``foo_bar``.
    Lower-cases, collapses every run of non-alphanumeric characters to a single
    underscore, and trims leading/trailing underscores.
    """
    return re.sub(r"[^a-z0-9]+", "_", repo_name.lower()).strip("_")


def _skeleton_base(lang: str) -> Traversable:
    """Return the packaged skeleton directory Traversable for *lang*."""
    return resources.files("vergil_tooling.data").joinpath("skeletons", lang)


def _iter_template_files(base: Traversable, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(relative_path, content)`` for every file under *base*, recursively.

    Directory names are joined with ``/`` so nested ``src/``/``tests/`` templates
    keep their repo-relative path. Entries are visited in name order for
    deterministic output.
    """
    for entry in sorted(base.iterdir(), key=lambda e: e.name):
        rel = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from _iter_template_files(entry, prefix=f"{rel}/")
        else:
            yield rel, entry.read_text(encoding="utf-8")


def render_skeleton(lang: str, project: str) -> dict[str, str]:
    """Render a language's skeleton templates, keyed by repo-relative path.

    Substitutes ``{project}``/``{name}``/``{namespace}`` (all the sanitized
    *project* name) in both file paths and contents, and drops the ``.tmpl``
    suffix. Returns an empty dict for a falsy language or one with no packaged
    skeleton, so a language without a skeleton is a clean no-op.
    """
    if not lang:
        return {}
    base = _skeleton_base(lang)
    if not base.is_dir():
        return {}
    replacements = {"{project}": project, "{name}": project, "{namespace}": project}
    rendered: dict[str, str] = {}
    for rel_path, raw in _iter_template_files(base):
        out_path = rel_path
        content = raw
        for token, value in replacements.items():
            out_path = out_path.replace(token, value)
            content = content.replace(token, value)
        if out_path.endswith(".tmpl"):
            out_path = out_path[: -len(".tmpl")]
        rendered[out_path] = content
    return rendered


def _write_skeleton(root: Path, files: dict[str, str], *, force: bool = False) -> None:
    """Write rendered skeleton *files* under *root*, greenfield and idempotent.

    Stamps only files that do not already exist — an existing file is never
    clobbered — unless *force* is set, the narrow "regenerate, overwriting"
    hatch (safe only on an un-customized repo). Parent directories are created
    as needed.
    """
    for rel_path, content in files.items():
        dest = root / rel_path
        if dest.exists() and not force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def container_runtime_available() -> bool:
    """Return whether a container runtime (docker/nerdctl) is available.

    Reuses :func:`container.detect_runtime`, which raises ``SystemExit`` when no
    runtime is on ``PATH``; that is mapped to ``False`` so the scaffold can
    fail-fast with its own clear message rather than exiting mid-init.
    """
    try:
        container.detect_runtime()
    except SystemExit:
        return False
    return True


def _run_in_container(root: Path, cmd: list[str]) -> int:
    """Run *cmd* inside the dev container via ``vrg-container-run``; return exit code.

    ``vrg-container-run`` mounts the current working directory, so the command
    runs against *root* (the repo being scaffolded).
    """
    result = subprocess.run(  # noqa: S603
        ["vrg-container-run", "--", *cmd],  # noqa: S607
        cwd=root,
        check=False,
    )
    return result.returncode


def scaffold_language(ctx: RepoInitContext) -> None:
    """Render + resolve + verify the language skeleton for a fresh repo.

    1. Render the skeleton and look up the language's lock command. If the
       language has neither a lock command nor a skeleton, there is nothing to
       do — return without touching the container.
    2. For a lock-resolving language, refuse **before writing anything** when no
       container runtime is available (the born-green-or-not-born invariant).
    3. Stamp the missing skeleton files.
    4. Run the lock command, then one full ``vrg-validate``, in the container;
       a non-zero from either raises :class:`ScaffoldError`.
    """
    lang = ctx.primary_language
    lock_cmd = language_lock_command(lang)
    project = sanitize_project_name(ctx.name)
    files = render_skeleton(lang, project)

    if lock_cmd is None and not files:
        return

    if lock_cmd is not None and not container_runtime_available():
        msg = (
            f"{lang} init requires a container runtime to resolve locks and verify "
            "born-green, but none (docker/nerdctl) is available. Nothing was "
            "written — a lock-resolving repo is born green or not born at all. "
            "Remedy: run repo-init where a container runtime is present."
        )
        raise ScaffoldError(msg)

    if ctx.work_dir is None:  # pragma: no cover - work_dir is always set by the wizard
        msg = "work_dir not set"
        raise ScaffoldError(msg)
    root = ctx.work_dir

    _write_skeleton(root, files)

    if lock_cmd is not None:
        if _run_in_container(root, lock_cmd) != 0:
            msg = f"lock resolve failed in the container: {' '.join(lock_cmd)}"
            raise ScaffoldError(msg)
        if _run_in_container(root, ["vrg-validate"]) != 0:
            msg = "born-green verify failed in the container: vrg-validate"
            raise ScaffoldError(msg)
