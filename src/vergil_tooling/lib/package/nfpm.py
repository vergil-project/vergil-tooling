"""nFPM config rendering, overlay merge and invocation (spec §5.2, §6.5).

The tooling owns a package's identity (name, version, release, arch, ...);
the optional ``packaging/nfpm.overlay.yaml`` only contributes files, scripts
and relationships. ``vrg-validate`` checks the overlay's top-level keys
(``config._check_package_overlay``); :func:`load_overlay` checks the value
shapes this module relies on, and nFPM's own config check runs at build time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from vergil_tooling.lib.config import PACKAGE_OVERLAY_PATH
from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from vergil_tooling.lib.package.build import BuildContext, BuildResult

FORMATS = ("deb", "rpm")
# Relationship keys an overlay may set at top level or per format in ``overrides``.
_RELATION_KEYS = ("provides", "conflicts", "replaces", "recommends", "suggests")
# Keys a per-format ``overrides.<fmt>`` mapping may set; merged into the
# rendered config for that format (never passed through to nFPM's overrides).
_OVERRIDE_KEYS = frozenset({"depends", "contents", "scripts", *_RELATION_KEYS})
# A shell comment starts at a ``#`` that begins a word.
_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$")
# ``systemctl`` as a command word, bare or by path (``/usr/bin/systemctl``).
_RAW_SYSTEMCTL = re.compile(r"(?<![\w-])systemctl(?![\w-])")


def _fail(detail: str) -> PackageError:
    return PackageError(f"{PACKAGE_OVERLAY_PATH}: {detail}")


def _check_str_list(value: Any, where: str) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(f"{where} must be a list of strings (got {value!r})")


def _check_contents(value: Any, where: str) -> None:
    if not isinstance(value, list):
        raise _fail(f"{where} must be a list of mappings (got {value!r})")
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("dst"), str):
            raise _fail(f"{where} entries must be mappings with a string 'dst' (got {entry!r})")


def _check_scripts(value: Any, where: str) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise _fail(f"{where} must map script names to repo-relative paths (got {value!r})")


def _check_section(data: dict[str, Any], where: str) -> None:
    """Check the shared keys of the top level or one ``overrides.<fmt>`` mapping."""
    if "contents" in data:
        _check_contents(data["contents"], f"{where}contents")
    if "scripts" in data:
        _check_scripts(data["scripts"], f"{where}scripts")
    for key in ("depends", *_RELATION_KEYS):
        if key in data:
            _check_str_list(data[key], f"{where}{key}")


def load_overlay(repo_root: Path) -> dict[str, Any]:
    """Return the repo's nFPM overlay (``{}`` when absent), checking its value shapes."""
    path = repo_root / PACKAGE_OVERLAY_PATH
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _fail(f"not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _fail("must be a YAML mapping")
    if "depends" in data:
        raise _fail("top-level 'depends' is not allowed; use overrides.<deb|rpm>.depends")
    _check_section(data, "")
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict):
        raise _fail(f"overrides must be a mapping of deb/rpm to settings (got {overrides!r})")
    for fmt, over in overrides.items():
        if fmt not in FORMATS:
            raise _fail(f"overrides.{fmt}: unknown format (allowed: {', '.join(FORMATS)})")
        if not isinstance(over, dict):
            raise _fail(f"overrides.{fmt} must be a mapping (got {over!r})")
        unknown = sorted(set(over) - _OVERRIDE_KEYS)
        if unknown:
            raise _fail(
                f"overrides.{fmt} may not set {', '.join(unknown)} "
                f"(allowed: {', '.join(sorted(_OVERRIDE_KEYS))})"
            )
        _check_section(over, f"overrides.{fmt}.")
    return data


def _repo_contents(repo_root: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay ``contents`` with relative file sources resolved against the repo root."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        e = dict(entry)
        if "src" in e and e.get("type") != "symlink" and not str(e["src"]).startswith("/"):
            e["src"] = str(repo_root / e["src"])
        out.append(e)
    return out


def render(
    ctx: BuildContext, result: BuildResult, fmt: str, overlay: dict[str, Any]
) -> dict[str, Any]:
    """Render the nFPM config for one format: tooling identity, builder output, then overlay."""
    # Late import: build imports this module, so a module-level import would be circular.
    from vergil_tooling.lib.package.build import package_release

    over: dict[str, Any] = overlay.get("overrides", {}).get(fmt, {})
    contents = [
        *result.contents,
        *_repo_contents(ctx.repo_root, overlay.get("contents", [])),
        *_repo_contents(ctx.repo_root, over.get("contents", [])),
    ]
    cfg: dict[str, Any] = {
        "name": ctx.name,
        "arch": "all" if ctx.pkg.noarch else ctx.cell.arch,
        "platform": "linux",
        "version": ctx.version,
        "version_schema": "none",
        "release": package_release(ctx.cell, fmt),
        "maintainer": f"{ctx.pkg.vendor} packages <packages@{ctx.pkg.vendor}.invalid>",
        "description": ctx.pkg.summary,
        "vendor": ctx.pkg.vendor,
        "contents": contents,
        "depends": [*result.depends.get(fmt, []), *over.get("depends", [])],
    }
    scripts = {**overlay.get("scripts", {}), **over.get("scripts", {})}
    if scripts:
        cfg["scripts"] = {k: str(ctx.repo_root / v) for k, v in scripts.items()}
    for key in _RELATION_KEYS:
        values = [*overlay.get(key, []), *over.get(key, [])]
        if values:
            cfg[key] = values
    return cfg


def _all_scripts(overlay: dict[str, Any]) -> list[str]:
    rels = list(overlay.get("scripts", {}).values())
    for over in overlay.get("overrides", {}).values():
        rels.extend(over.get("scripts", {}).values())
    return list(dict.fromkeys(rels))


def check_maintainer_scripts(repo_root: Path, overlay: dict[str, Any]) -> None:
    """Fail on a raw ``systemctl`` in any overlay maintainer script (spec §6.5).

    Install tests run without systemd as PID 1, so scripts must use the
    helpers that no-op without it: ``deb-systemd-helper``/``deb-systemd-invoke``
    (Ubuntu) or ``/usr/lib/systemd/systemd-update-helper`` (RHEL).
    """
    for rel in _all_scripts(overlay):
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            msg = f"{PACKAGE_OVERLAY_PATH}: maintainer script {rel} not found"
            raise PackageError(msg) from exc
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RAW_SYSTEMCTL.search(_COMMENT.sub("", line)):
                msg = (
                    f"{rel}:{lineno}: raw 'systemctl' in a maintainer script; use "
                    "deb-systemd-helper/deb-systemd-invoke (Ubuntu) or "
                    "/usr/lib/systemd/systemd-update-helper (RHEL) (spec §6.5)"
                )
                raise PackageError(msg)


def package(config: dict[str, Any], fmt: str, out_dir: Path) -> Path:
    """Run nFPM on ``config`` for ``fmt``; move the one artifact into ``out_dir`` and return it."""
    if shutil.which("nfpm") is None:
        msg = "nfpm not found on PATH (CI installs it via actions/shared/setup/nfpm)"
        raise PackageError(msg)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vrg-nfpm-") as tmp:
        cfg_path = Path(tmp) / f"nfpm-{fmt}.yaml"
        cfg_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        build_dir = Path(tmp) / "out"
        build_dir.mkdir()
        cmd = ["nfpm", "package", "--config", str(cfg_path), "--packager", fmt]
        cmd += ["--target", str(build_dir)]
        proc = subprocess.run(  # noqa: S603 - fixed nfpm argv, no shell
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            msg = f"nfpm package --packager {fmt} failed (exit {proc.returncode}):\n{detail}"
            raise PackageError(msg)
        produced = sorted(build_dir.glob(f"*.{fmt}"))
        if len(produced) != 1:
            msg = f"nfpm produced {len(produced)} .{fmt} files, expected exactly 1"
            raise PackageError(msg)
        dest = out_dir / produced[0].name
        shutil.move(produced[0], dest)
        return dest
