#!/usr/bin/env python3
"""Build the site docs with a strict mkdocs build.

scripts/bin/validate-custom runs this, through `uv run --isolated --group
site-check`, as part of vrg-validate's `custom` stage, so `mkdocs` and
mkdocs-material come from the non-default site-check dependency group.

It builds the site docs with `mkdocs build --strict`, so a page that is deleted
or renamed while still linked (or listed in the nav) fails validation locally
rather than only in the docs deploy (vergil-tooling#4115).

The build runs on a temporary copy of docs/site, staged as CI's docs build
stages it (the changelog and release notes copied in, the release versions
patched into the nav), so it writes nothing into the working tree. Staging
here is offline: the CI-evidence download line CI adds to each release page is
an external link, which a strict build does not check, so it is left out
rather than asking GitHub for it.

A missing `mkdocs` is a failure, never a skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib.docs import patch_nav, stage_docs

ROOT = Path(__file__).resolve().parents[2]
SITE_DIR = ROOT / "docs" / "site"
# Paths below docs/site that are build output or staging products of an
# earlier local docs build (all gitignored); the copy leaves them out so the
# check sees only tracked sources plus what it stages itself.
_GENERATED = {
    SITE_DIR: {"site", "mkdocs.staged.yml"},
    SITE_DIR / "docs": {"changelog.md", "releases"},
}


def _ignore(directory: str, names: list[str]) -> set[str]:
    return _GENERATED.get(Path(directory), set()) & set(names)


def check_site_docs() -> int:
    mkdocs = shutil.which("mkdocs")
    if mkdocs is None:
        print(
            "ERROR: mkdocs not found on PATH; it comes from the site-check "
            "dependency group (mkdocs-material), so run this through vrg-validate",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory(prefix="validate-custom-docs-") as tmp:
        site = Path(tmp) / "site"
        shutil.copytree(SITE_DIR, site, ignore=_ignore)
        docs_dir = site / "docs"
        stage_docs(
            docs_dir=docs_dir,
            releases_dir=ROOT / "releases",
            changelog=ROOT / "CHANGELOG.md",
        )
        config = site / "mkdocs.yml"
        patch_nav(config, docs_dir / "releases")
        cmd = [mkdocs, "build", "--strict", "-f", str(config), "-d", str(Path(tmp) / "out")]
        print("→ mkdocs build --strict (docs/site, staged)", flush=True)
        # mkdocs-material 9.7+ prints a MkDocs 2.0 banner unless this is set;
        # CI's docs jobs and vrg-container-docs set it too.
        env = {**os.environ, "NO_MKDOCS_2_WARNING": "1"}
        return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode  # noqa: S603


def main() -> int:
    rc = check_site_docs()
    if rc != 0:
        print(f"ERROR: strict mkdocs build of docs/site failed (exit {rc})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
