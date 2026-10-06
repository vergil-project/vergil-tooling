"""The ``python`` builder (spec §6.1).

Builds a Python product as a venv on the pinned ``vergil-python`` runtime, from
``uv.lock`` only. It runs as root in a clean container of the build cell's OS:

1. Trust the **vergil** package repository (the runtime always comes from the
   vergil org, whatever the product's vendor — D7) and install
   ``vergil-python<runtime>`` the way consumers do: apt in the shared Ubuntu
   24.04 cell or a ``native`` Ubuntu cell, dnf in a ``native`` RHEL cell. The
   installed version (the PBS build) becomes the package's runtime dependency
   floor.
2. Create the venv at its **final** path, ``/opt/<vendor>/<name>/venv`` (venvs
   are not relocatable), from ``/opt/vergil/python/<runtime>/bin/python<X.Y>``.
3. ``uv sync --frozen --no-dev --no-editable --compile-bytecode`` into it.
4. Copy the product tree into the staging root and add ``/usr/bin`` shims for
   ``[project.scripts]`` (or the ``[package.python].commands`` subset).

The glibc floor guard, overlay and nFPM packaging run afterwards in
:func:`~vergil_tooling.lib.package.build.run_build`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.package import PackageError, matrix, orgs, repo_setup
from vergil_tooling.lib.package import targets as tg
from vergil_tooling.lib.package.build import (
    BuildContext,
    BuildResult,
    contents_from_tree,
    register,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.config import PackageConfig

# Where the venv is built. Always /opt for real builds (the venv must sit at its
# final install path); tests point it at a temporary directory.
_OPT = "/opt"

# The runtime package always comes from the vergil org (spec D7).
_RUNTIME_VENDOR = "vergil"

# The apt suite of the shared build image (every non-native cell builds there).
_SHARED_SUITE = next(t.suite for t in tg.all_targets() if t.image == matrix.SHARED_IMAGE)


def _run(
    *argv: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv``, capturing stdout; stderr streams to the build log. Raise on failure."""
    return subprocess.run(  # noqa: S603 - fixed tool argv built by this module
        list(argv), check=True, stdout=subprocess.PIPE, text=True, env=env, cwd=cwd
    )


def runtime_dir(vendor: str, runtime: str) -> str:
    """The install prefix of the ``vergil-python<runtime>`` package (spec §6.3)."""
    return f"/opt/{vendor}/python/{runtime}"


def runtime_depends(runtime: str, pbs_version: str) -> dict[str, list[str]]:
    """The per-format dependency on the runtime the venv was built against."""
    name = f"vergil-python{runtime}"
    return {"deb": [f"{name} (>= {pbs_version})"], "rpm": [f"{name} >= {pbs_version}"]}


def shim_commands(repo_root: Path, pkg: PackageConfig) -> list[str]:
    """The commands to shim into ``/usr/bin``: all of ``[project.scripts]`` or the subset."""
    pyproject = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"pyproject.toml not found at {repo_root}"
        raise PackageError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{pyproject} is not valid TOML: {exc}"
        raise PackageError(msg) from exc
    project: Any = data.get("project", {})
    scripts: Any = project.get("scripts", {}) if isinstance(project, dict) else None
    if not isinstance(scripts, dict) or not all(isinstance(v, str) for v in scripts.values()):
        msg = f"{pyproject}: [project.scripts] must be a table of strings"
        raise PackageError(msg)
    if pkg.python is None or pkg.python.commands is None:
        return sorted(scripts)
    for cmd in pkg.python.commands:
        if cmd not in scripts:
            msg = f"[package.python].commands: {cmd!r} is not in [project.scripts]"
            raise PackageError(msg)
    return list(pkg.python.commands)


def _install_runtime(fmt: str, rt_pkg: str) -> str:
    """Install ``rt_pkg`` with the cell's package manager; return its upstream version."""
    if fmt == "deb":
        apt_env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        # Refresh against the permanent source the keyring package just installed.
        _run("apt-get", "update", env=apt_env)
        _run("apt-get", "install", "-y", rt_pkg, env=apt_env)
        raw = _run("dpkg-query", "-W", "-f=${Version}", rt_pkg).stdout.strip()
        # Drop the Debian revision (``-1``); the upstream version is the PBS build.
        version = raw.rsplit("-", 1)[0] if "-" in raw else raw
    else:
        _run("dnf", "install", "-y", rt_pkg)
        raw = version = _run("rpm", "-q", "--qf", "%{VERSION}", rt_pkg).stdout.strip()
    if not version:
        msg = f"could not read the installed {rt_pkg} version (got {raw!r})"
        raise PackageError(msg)
    return version


@register("python")
def build_python(ctx: BuildContext) -> BuildResult:
    """Build the product venv on the pinned runtime and stage it with its shims."""
    py = ctx.pkg.python
    if py is None:
        msg = 'builder = "python": [package.python] is required'
        raise PackageError(msg)
    if not (ctx.repo_root / "uv.lock").is_file():
        msg = f'uv.lock is required for builder = "python" (lock-only builds): {ctx.repo_root}'
        raise PackageError(msg)
    # Validate the shim selection before any slow work.
    commands = shim_commands(ctx.repo_root, ctx.pkg)

    if ctx.cell.native:
        target = tg.REGISTRY[ctx.cell.targets[0]]
        fmt, suite = target.fmt, target.suite
    else:
        fmt, suite = "deb", _SHARED_SUITE

    runtime = py.runtime
    interpreter = f"{runtime_dir(_RUNTIME_VENDOR, runtime)}/bin/python{runtime.rsplit('.', 1)[0]}"
    product = Path(_OPT) / ctx.pkg.vendor / ctx.name
    venv = product / "venv"
    if product.exists():
        # Never build over (or delete) an installed copy of the product.
        msg = f"{product} already exists; build in a clean container"
        raise PackageError(msg)

    try:
        repo_setup.bootstrap(
            repo_setup.local_run, orgs.for_vendor(_RUNTIME_VENDOR), fmt, suite, sudo=False
        )
        pbs_version = _install_runtime(fmt, f"vergil-python{runtime}")
        _run("uv", "venv", "--python", interpreter, str(venv))
        sync_env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(venv),
            "UV_PYTHON": interpreter,
            "UV_PYTHON_DOWNLOADS": "never",
        }
        _run(
            "uv",
            "sync",
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--compile-bytecode",
            env=sync_env,
            cwd=ctx.repo_root,
        )
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(exc.cmd)
        stderr = (exc.stderr or "").strip()
        msg = f"{cmd} failed (exit {exc.returncode})" + (f": {stderr}" if stderr else "")
        raise PackageError(msg) from exc

    for cmd in commands:
        if not (venv / "bin" / cmd).exists():
            msg = f"command {cmd!r} is not in the built venv ({venv}/bin)"
            raise PackageError(msg)

    shutil.copytree(product, ctx.staging_root / "opt" / ctx.pkg.vendor / ctx.name, symlinks=True)
    contents = contents_from_tree(ctx.staging_root, own_below=f"/opt/{ctx.pkg.vendor}")
    final_bin = f"/opt/{ctx.pkg.vendor}/{ctx.name}/venv/bin"
    contents += [
        {"src": f"{final_bin}/{c}", "dst": f"/usr/bin/{c}", "type": "symlink"} for c in commands
    ]
    return BuildResult(contents=contents, depends=runtime_depends(runtime, pbs_version))
