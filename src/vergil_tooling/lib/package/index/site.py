"""Build the signed package-repository site end to end (spec §7.2, §7.3).

``build_site`` is a pure function of ``packages.toml``, the org public key and
the configured products' release assets:

1. load ``packages.toml``;
2. collect the packaged stable releases;
3. verify their provenance and rpm signatures (rpm key in a private rpm DB);
4. apply retention;
5. wipe the output directory;
6. write apt metadata for the registry's Ubuntu suites;
7. write dnf metadata for the registry's EL releases;
8. sign: ``Release`` → ``InRelease`` + ``Release.gpg``, ``repomd.xml`` →
   ``repomd.xml.asc``;
9. publish the public key at ``keys/<vendor>.asc``;
10. write a minimal ``index.html`` with setup instructions and the fingerprint;
11. enforce the size guard.
"""

from __future__ import annotations

import html
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError, repo_setup
from vergil_tooling.lib.package.index import apt, collect, retention, rpm, sign
from vergil_tooling.lib.package.index import config as index_config
from vergil_tooling.lib.package.index.collect import _call
from vergil_tooling.lib.package.targets import REGISTRY

if TYPE_CHECKING:
    from vergil_tooling.lib.package.repo_setup import Run

SIZE_WARN = 750_000_000
SIZE_FAIL = 900_000_000


def size_guard(site: Path, warn: int = SIZE_WARN, fail: int = SIZE_FAIL) -> int:
    """Total bytes under ``site``: warn above ``warn``, raise above ``fail``."""
    total = sum(p.lstat().st_size for p in site.rglob("*") if p.is_file())
    if total > fail:
        msg = f"package site is {total} bytes, over the {fail}-byte limit"
        raise PackageError(msg)
    if total > warn:
        print(
            f"WARNING: package site is {total} bytes, over the {warn}-byte warning "
            f"threshold (hard limit {fail} bytes)",
            file=sys.stderr,
        )
    else:
        print(f"package site is {total} bytes")
    return total


def suites(fmt: str) -> list[str]:
    """The registry's apt suites (``deb``) or EL releases (``rpm``), in registry order."""
    return list(dict.fromkeys(t.suite for t in REGISTRY.values() if t.fmt == fmt))


def default_base_url() -> str:
    """The GitHub Pages URL of ``$GITHUB_REPOSITORY`` (``https://<owner>.github.io/<repo>``)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        msg = "the repository base URL is unknown: pass --base-url or set GITHUB_REPOSITORY"
        raise PackageError(msg)
    return f"https://{owner.lower()}.github.io/{name}"


def fingerprint(keyfile: Path, run: Run) -> str:
    """The primary-key fingerprint of the public key in ``keyfile``."""
    what = f"reading the key in {keyfile}"
    argv = ("gpg", "--batch", "--with-colons", "--show-keys", str(keyfile))
    seen_pub = False
    for line in str(_call(run, what, *argv).stdout).splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            seen_pub = True
        elif fields[0] == "fpr" and seen_pub:
            return fields[9]
    msg = f"{keyfile} holds no public key"
    raise PackageError(msg)


def _wipe(out: Path, *keep: Path) -> None:
    """Empty ``out``, refusing when it contains the cwd or an input."""
    target = out.resolve()
    for p in (Path.cwd(), *keep):
        if p.resolve().is_relative_to(target):
            msg = f"refusing to wipe {out}: it contains {p}"
            raise PackageError(msg)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)


def index_html(vendor: str, base_url: str, fpr: str, deb_suites: list[str]) -> str:
    """A minimal landing page: the key fingerprint and apt/dnf setup instructions."""
    v, url, f = (html.escape(s) for s in (vendor, base_url, fpr))
    keyring = f"/usr/share/keyrings/{v}-archive-keyring.asc"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{v} packages</title>
</head>
<body>
<h1>{v} package repository</h1>
<p>Signing key fingerprint: <code>{f}</code>
(<a href="keys/{v}.asc">keys/{v}.asc</a>)</p>
<h2>Ubuntu ({html.escape(", ".join(deb_suites))})</h2>
<pre>curl -fsSL {url}/keys/{v}.asc | sudo tee {keyring} &gt;/dev/null
echo "deb [signed-by={keyring}] {url}/deb $(. /etc/os-release; echo $VERSION_CODENAME) main" \\
  | sudo tee /etc/apt/sources.list.d/{v}.list
sudo apt-get update</pre>
<h2>RHEL</h2>
<pre>sudo rpm --import {url}/keys/{v}.asc
sudo tee /etc/yum.repos.d/{v}.repo &lt;&lt;'EOF'
[{v}]
name={v} packages
baseurl={url}/rpm/el$releasever/$basearch
gpgcheck=1
repo_gpgcheck=1
gpgkey={url}/keys/{v}.asc
enabled=1
EOF</pre>
</body>
</html>
"""


def build_site(
    config: Path,
    keys_dir: Path,
    out: Path,
    workdir: Path,
    run: Run = repo_setup.local_run,
    *,
    base_url: str,
) -> None:
    """Build, sign and size-check the package-repository site in ``out``."""
    cfg = index_config.load(config)
    keyfile = keys_dir / f"{cfg.vendor}.asc"
    if not keyfile.is_file():
        msg = f"the {cfg.vendor} public key is missing: {keyfile}"
        raise PackageError(msg)
    fpr = fingerprint(keyfile, run)

    workdir.mkdir(parents=True, exist_ok=True)
    artifacts = collect.collect(cfg, workdir, run)
    rpmdb = workdir / "rpmdb"
    shutil.rmtree(rpmdb, ignore_errors=True)
    collect.verify(artifacts, keyfile, run, rpmdb=rpmdb)
    kept = retention.select(artifacts, cfg.keep, cfg.lines)
    print(f"indexing {len(kept)} of {len(artifacts)} collected package(s)")

    _wipe(out, config, keys_dir, workdir)
    deb_suites = suites("deb")
    releases = apt.write(
        out, kept, deb_suites, lambda p: apt.deb_control(p, run), vendor=cfg.vendor
    )
    repomds = rpm.write(out, kept, base_url, suites("rpm"), run)

    sign.import_key(run, sign.KEY_ENV, sign.PASS_ENV)
    for release in releases:
        sign.clearsign(run, release, release.with_name("InRelease"), local_user=fpr)
        sign.detach(run, release, release.with_name("Release.gpg"), local_user=fpr)
    for repomd in repomds:
        sign.detach(run, repomd, repomd.with_name("repomd.xml.asc"), local_user=fpr)

    (out / "keys").mkdir()
    shutil.copyfile(keyfile, out / "keys" / keyfile.name)
    (out / "index.html").write_text(
        index_html(cfg.vendor, base_url.rstrip("/"), fpr, deb_suites), encoding="utf-8"
    )
    size_guard(out)
