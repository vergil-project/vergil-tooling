"""Preview or build MkDocs documentation inside a dev container.

Supports ``serve`` and ``build`` subcommands.  For Python repos, wraps
commands with ``uv sync --group docs && uv run ...`` so that
mkdocstrings plugins resolve correctly.  A Python repo with no ``docs``
dependency group runs plain ``uv run mkdocs``, which falls through to the
image's mkdocs-material, as CI's docs jobs do (vergil-tooling#3563).

Before mkdocs runs, the docs sources are staged on the host exactly as CI's
``actions/shared/docs/stage`` does (vergil-tooling#3439): ``vrg-docs-stage``
copies the changelog and release notes into the docs dir, and
``vrg-docs-patch-nav`` patches the release versions into the nav. The nav
patch goes to a sibling copy of the config (``mkdocs.staged.yml``), never the
tracked ``mkdocs.yml``, and mkdocs is pointed at that copy. Every generated
path must be gitignored, so staging never dirties the working tree.
"""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from pathlib import Path

from vergil_tooling.bin import vrg_docs_patch_nav, vrg_docs_stage
from vergil_tooling.lib import git
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import container_env_prefixes
from vergil_tooling.lib.container import build_container_args, detect_runtime
from vergil_tooling.lib.docs import collect_releases
from vergil_tooling.lib.output import emit_error
from vergil_tooling.lib.process import exec_replace
from vergil_tooling.lib.versions.extensions.python import source_env

_VALID_PREFIXES = {"dev", "prod"}

# The nav-patched copy of the mkdocs config, written next to the original so
# every path the config holds (docs_dir, mkdocstrings paths, INHERIT) still
# resolves the same way. Gitignored by the managed base gitignore.
_STAGED_CONFIG = "mkdocs.staged.yml"

# mkdocs-material 9.7.x prints a MkDocs 2.0 warning banner on every mkdocs
# run unless NO_MKDOCS_2_WARNING is set
# (https://squidfunk.github.io/mkdocs-material/blog/2026/02/18/mkdocs-2.0/).
# CI and CD already set it (vergil-project/vergil-actions#947); setting it
# here, for both build and serve, keeps local builds matching CI.
_DOCS_ENV = {"NO_MKDOCS_2_WARNING": "1"}


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    config = os.environ.get("MKDOCS_CONFIG", "docs/site/mkdocs.yml")
    port = os.environ.get("DOCS_PORT", "8000")

    if {"-h", "--help"} & set(args):
        _usage(port)
        return 0

    prefix = "prod"
    if "--prefix" in args:
        pi = args.index("--prefix")
        if pi + 1 >= len(args):
            print(
                "error: --prefix requires a value (dev or prod)",
                file=sys.stderr,
            )
            return 1
        prefix = args[pi + 1]
        if prefix not in _VALID_PREFIXES:
            allowed = ", ".join(sorted(_VALID_PREFIXES))
            print(
                f"error: invalid prefix '{prefix}' (allowed: {allowed})",
                file=sys.stderr,
            )
            return 1
        del args[pi : pi + 2]

    if not args:
        _usage(port)
        return 1

    command = args[0]
    extra_args = args[1:]

    if command not in ("serve", "build"):
        print(f"ERROR: unknown command: {command}", file=sys.stderr)
        _usage(port)
        return 1

    repo_root = git.repo_root()
    staged_config = _stage_sources(repo_root, config)
    if staged_config is None:
        return 1

    if command == "serve":
        mkdocs_cmd = f"mkdocs serve -f {staged_config} -a 0.0.0.0:8000"
    else:
        mkdocs_cmd = f"mkdocs build -f {staged_config}"

    if extra_args:
        mkdocs_cmd += " " + " ".join(extra_args)

    runtime = detect_runtime()
    image = _docs_image(prefix)

    container_cmd = mkdocs_cmd
    docs_env = dict(_DOCS_ENV)
    note = None
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        has_docs_group = _has_docs_group(pyproject)
        if has_docs_group is None:
            return 1
        if has_docs_group:
            container_cmd = f"uv sync --group docs && uv run {mkdocs_cmd}"
        else:
            # CI's docs jobs sync no group: `uv run mkdocs` finds no mkdocs in
            # the project venv and falls through to the image's
            # mkdocs-material. A repo whose docs need nothing more (no
            # mkdocstrings) declares no docs group, so do the same here
            # (vergil-tooling#3563).
            container_cmd = f"uv run {mkdocs_cmd}"
            note = "no [dependency-groups].docs in pyproject.toml; using the image's mkdocs"
        # Pin uv to the docs image's own Python, as the CI docs jobs do
        # (vergil-actions#1002), so a repo's python-preference cannot make uv
        # fetch a managed interpreter. The docs build resolves no version, so
        # only the source is set. No fallback: if the image's Python misses
        # requires-python, uv fails loudly (vergil-tooling#3414).
        docs_env.update(source_env())

    env_prefixes = container_env_prefixes(repo_root)
    container_args = build_container_args(
        repo_root,
        image,
        ["bash", "-c", container_cmd],
        runtime=runtime,
        env_prefixes=env_prefixes,
        extra_env=docs_env,
    )

    if command == "serve":
        idx = container_args.index(image)
        container_args[idx:idx] = ["-p", f"{port}:8000"]

    print(f"Image:   {image}")
    print(f"Config:  {staged_config} (staged from {config})")
    print(f"Command: {container_cmd}")
    if note is not None:
        print(f"Note:    {note}")
    if command == "serve":
        print(f"URL:     http://localhost:{port}")
    print("---")

    if runtime == "nerdctl":
        exec_replace("nerdctl", container_args)
    else:
        exec_replace("docker", container_args)
    return 0  # pragma: no cover


def _usage(port: str) -> None:
    print("Usage: vrg-container-docs [--prefix <dev|prod>] <serve|build> [mkdocs args...]")
    print()
    print("Stages the changelog and release notes and patches the nav, as CI does,")
    print("before mkdocs runs; needs gh auth when the repo has release notes.")
    print()
    print("Commands:")
    print(f"  serve   Start a live-reloading preview server (port {port})")
    print("  build   Build the static documentation site")
    print()
    print("Options:")
    print("  --prefix <dev|prod>  image prefix (default: prod)")
    print()
    print("Environment variables:")
    print("  DOCKER_DOCS_IMAGE  Container image (override; ignores --prefix)")
    print("  MKDOCS_CONFIG      Path to mkdocs.yml (default: docs/site/mkdocs.yml)")
    print("  DOCS_PORT          Host port for serve (default: 8000)")


def _has_docs_group(pyproject: Path) -> bool | None:
    """Return whether *pyproject* declares a ``docs`` dependency group.

    Returns ``None`` after reporting the error when the file cannot be parsed,
    so the command fails here rather than with an opaque ``uv`` error inside
    the container.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        emit_error(f"cannot parse pyproject.toml: {exc}")
        return None
    return "docs" in data.get("dependency-groups", {})


def _stage_sources(repo_root: Path, config: str) -> str | None:
    """Stage the docs sources as CI does; return the staged config path.

    Mirrors CI's ``actions/shared/docs/stage``: ``vrg-docs-stage`` into
    ``<config dir>/docs``, then ``vrg-docs-patch-nav`` -- but on a sibling copy
    of the config, so the tracked ``mkdocs.yml`` is never rewritten. Refuses
    before writing anything when a generated path is not gitignored (a tracked
    staged copy, or a repo whose gitignore predates these patterns). A staging
    failure, including missing ``gh`` auth for release-evidence links, fails
    the command; staging is never skipped. Returns ``None`` after reporting the
    error, else the staged config path in the same form as *config*.
    """
    source_config = repo_root / config
    if not source_config.is_file():
        emit_error(f"mkdocs config not found: {config} (set MKDOCS_CONFIG)")
        return None
    staged_config = source_config.with_name(_STAGED_CONFIG)
    docs_dir = source_config.parent / "docs"
    releases_dir = repo_root / "releases"
    changelog = repo_root / "CHANGELOG.md"

    generated = [staged_config, docs_dir / "releases" / "index.md"]
    if changelog.is_file():
        generated.append(docs_dir / "changelog.md")
    generated += [docs_dir / "releases" / e.filename for e in collect_releases(releases_dir)]
    unignored = [p for p in generated if not git.is_ignored(p)]
    if unignored:
        listing = "\n".join(f"  {p.relative_to(repo_root)}" for p in unignored)
        emit_error(
            "docs staging would write paths that are not gitignored:\n"
            f"{listing}\n"
            "Stop tracking any of these that are committed "
            "(vrg-git rm --cached <path>), then run vrg-gitignore-sync so the "
            "managed .gitignore covers the staged docs paths."
        )
        return None

    stage_rc = vrg_docs_stage.main(
        [
            "--docs-dir",
            str(docs_dir),
            "--releases-dir",
            str(releases_dir),
            "--changelog",
            str(changelog),
        ]
    )
    if stage_rc != 0:
        return None
    shutil.copyfile(source_config, staged_config)
    nav_rc = vrg_docs_patch_nav.main(
        ["--mkdocs-yml", str(staged_config), "--releases-dir", str(docs_dir / "releases")]
    )
    if nav_rc != 0:
        return None
    return str(Path(config).with_name(_STAGED_CONFIG))


def _docs_image(prefix: str) -> str:
    return os.environ.get(
        "DOCKER_DOCS_IMAGE",
        f"ghcr.io/vergil-project/{prefix}-base:latest",
    )


if __name__ == "__main__":
    sys.exit(main())
