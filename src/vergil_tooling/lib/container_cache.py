"""Per-branch container image caching with vergil-tooling pre-installed."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import primary_ci_version, vrg_install_tag
from vergil_tooling.lib.container import (
    container_platform,
    default_image,
    detect_runtime,
    workspace_mount_args,
)
from vergil_tooling.lib.languages import CheckKind, language_commands

if TYPE_CHECKING:
    from pathlib import Path

_SELF_PROJECT_NAME = "vergil-tooling"

_VRG_GIT_URL = "https://github.com/vergil-project/vergil-tooling"

_PULL_TIMEOUT_SECONDS = 120

# Opt-in escape hatch: when set to a truthy value, a failed base-image pull
# during the staleness check degrades to using the local base instead of
# failing hard. Off by default — see resolve_base_digest.
_ALLOW_STALE_BASE_ENV = "VRG_ALLOW_STALE_BASE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _allow_stale_base() -> bool:
    """Return True when the operator has opted in to a possibly-stale base."""
    return os.environ.get(_ALLOW_STALE_BASE_ENV, "").strip().lower() in _TRUTHY


_CACHE_FILES: dict[str, list[str]] = {
    "python": ["uv.lock", "vergil.toml"],
    "ruby": ["Gemfile.lock", "vergil.toml"],
    "rust": ["Cargo.lock", "vergil.toml"],
    "go": ["go.sum", "vergil.toml"],
    "java": ["pom.xml", "vergil.toml"],
}
_DEFAULT_CACHE_FILES = ["vergil.toml"]


def _warmup_command(lang: str) -> str:
    cmds = language_commands(lang, CheckKind.INSTALL)
    return " && ".join(" ".join(cmd) for cmd in cmds) if cmds else ""


def _inspect_image_id(image: str, *, runtime: str) -> str | None:
    """Return the local content id (``.Id``) of *image*, or None if absent."""
    result = subprocess.run(  # noqa: S603
        [runtime, "image", "inspect", image, "--format", "{{.Id}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _summarize_pull_error(stderr: str) -> str:
    """Return the most informative line from a failed pull's stderr.

    Container runtimes print the actionable cause — ``denied``, ``unauthorized``,
    ``manifest unknown``, or a network error — on the last non-empty stderr line.
    Surfacing it distinguishes an auth failure from a genuine offline host instead
    of guessing.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


def resolve_base_digest(
    base_image: str, *, runtime: str = "", allow_stale: bool | None = None
) -> tuple[str, bool]:
    """Resolve *base_image*'s content digest, refreshing it from the registry.

    Pulls the base (so a moved tag is both detected and available to build from),
    then inspects the local image id. The pull *is* the staleness check: if it
    fails, we genuinely cannot tell whether the local base is current.

    By default a failed pull is a hard error — running against a possibly-stale
    local base silently is the worst available outcome (a host can validate
    against an old image that predates a tool addition and never know). Set
    ``VRG_ALLOW_STALE_BASE`` (or pass ``allow_stale=True``) to opt in to the
    degraded "use the local base anyway" path, which warns and continues.

    Returns ``(digest, verified)`` where ``verified`` is False only on the
    opted-in stale path. Raises ``RuntimeError`` when no digest can be resolved,
    or when the pull failed and the stale-base opt-in is not set.
    """
    rt = runtime or detect_runtime()
    if allow_stale is None:
        allow_stale = _allow_stale_base()

    pull_ok = True
    pull_error = ""
    try:
        pull = subprocess.run(  # noqa: S603
            [rt, "pull", base_image],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_PULL_TIMEOUT_SECONDS,
        )
        pull_ok = pull.returncode == 0
        if not pull_ok:
            pull_error = _summarize_pull_error(pull.stderr)
    except subprocess.TimeoutExpired:
        pull_ok = False
        pull_error = f"timed out after {_PULL_TIMEOUT_SECONDS}s"

    digest = _inspect_image_id(base_image, runtime=rt)
    if digest is None:
        outcome = "succeeded" if pull_ok else f"failed ({pull_error})"
        msg = (
            f"Could not resolve base image '{base_image}': pull "
            f"{outcome} and no local copy is present."
        )
        raise RuntimeError(msg)

    if not pull_ok:
        if not allow_stale:
            msg = (
                f"Could not verify base image freshness for '{base_image}': "
                f"base pull failed ({pull_error}). Refusing to run against a "
                f"possibly-stale local cache. Set {_ALLOW_STALE_BASE_ENV}=1 to "
                "accept the local base anyway."
            )
            raise RuntimeError(msg)
        print(
            f"warning: could not verify base image freshness for '{base_image}': "
            f"base pull failed ({pull_error}); {_ALLOW_STALE_BASE_ENV} set, "
            "using local image",
            file=sys.stderr,
        )
    return digest, pull_ok


def _is_self_repo(repo_root: Path) -> bool:
    """Return True when *repo_root* is the vergil-tooling project itself."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib

        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        name: object = data.get("project", {}).get("name")
        return name == _SELF_PROJECT_NAME
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return False


def cache_sensitive_files(repo_root: Path, lang: str) -> list[Path]:
    """Return paths of cache-sensitive files that exist in *repo_root*."""
    names = _CACHE_FILES.get(lang, _DEFAULT_CACHE_FILES)
    return [repo_root / n for n in names if (repo_root / n).is_file()]


def compute_cache_hash(files: list[Path], *, base_digest: str = "", salt: str = "") -> str:
    """SHA-256 over sorted file contents, base image digest, and optional salt.

    Folding ``base_digest`` (the content id of the base image the cache is built
    from) into the key means a republished base tag yields a different hash, so the
    stale cache is rebuilt instead of reused. Returns the first 8 hex chars.
    """
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
    if base_digest:
        h.update(base_digest.encode())
    if salt:
        h.update(salt.encode())
    return h.hexdigest()[:8]


def _sanitize_branch(branch: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", branch)


def cache_image_tag(base_image: str, branch: str, cache_hash: str) -> str:
    """Construct the cached image tag."""
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    sanitized = _sanitize_branch(branch)
    return f"{base_repo}:{base_tag}--{sanitized}--{cache_hash}"


def find_cached_image(base_image: str, branch: str, *, runtime: str = "") -> tuple[str, str] | None:
    """Find an existing cached image for *base_image* and *branch*.

    Returns ``(full_tag, hash_suffix)`` or ``None``.
    """
    rt = runtime or detect_runtime()
    sanitized = _sanitize_branch(branch)
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    pattern = f"{base_repo}:{base_tag}--{sanitized}--"

    result = subprocess.run(  # noqa: S603
        [rt, "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return None

    for line in result.stdout.splitlines():
        if line.startswith(pattern):
            tag_hash = line[len(pattern) :]
            return (line, tag_hash)
    return None


def _build_cached_image(
    repo_root: Path,
    lang: str,
    base_image: str,
    target_tag: str,
    *,
    runtime: str = "",
) -> str:
    """Build a cached image with vergil-tooling installed."""
    rt = runtime or detect_runtime()
    self_repo = _is_self_repo(repo_root)
    warmup = _warmup_command(lang)

    if self_repo:
        setup = warmup or "true"
    else:
        tag = vrg_install_tag(repo_root)
        uv_install = f"uv tool install --quiet 'vergil-tooling @ git+{_VRG_GIT_URL}@{tag}'"
        setup = f"{uv_install} && {warmup}" if warmup else uv_install

    # Attribute the build clearly as an environment-provisioning step. A rebuild
    # can be triggered lazily by an operational command (the first vrg-container-run
    # whose cache key no longer matches), and without this framing a build failure
    # reads as a failure of whatever operation happened to trigger it — e.g. a
    # cold build during vrg-finalize-pr's validation looking like "validation
    # failed after a clean merge" (issue #2462). This banner keeps a provisioning
    # failure attributable to provisioning, not to the caller's operation.
    print("── Provisioning dev image (environment build — not part of your command) ──")
    print(f"Building cached image: {target_tag}")
    print(f"  Base:    {base_image}")
    if self_repo:
        print("  Install: skipped (self-repo uses local dev version)")
    else:
        print(f"  Install: vergil-tooling@{tag}")
    if warmup:
        print(f"  Warmup:  {warmup}")

    create_args = [
        rt,
        "create",
        f"--platform={container_platform()}",
        # Use the freshly-pulled base (resolve_base_digest pulled it). Only pull
        # here if it is somehow absent locally; never --pull=always, which would
        # fail an offline build that has a usable local copy.
        "--pull=missing",
        # Shared with the run path (container.py): the workspace bind-mount, the
        # working dir, and the Python-gated `.venv` mask. Masking the host `.venv`
        # here too keeps the cache-build (cold-rebuild) `setup` step from
        # corrupting the bind-mounted host venv — the mount site #2486 missed (#2495).
        *workspace_mount_args(repo_root),
        base_image,
        "bash",
        "-c",
        setup,
    ]
    cid_result = subprocess.run(  # noqa: S603
        create_args,
        capture_output=True,
        text=True,
    )
    if cid_result.returncode != 0:
        msg = f"Failed to create container: {cid_result.stderr.strip()}"
        raise RuntimeError(msg)

    container_id = cid_result.stdout.strip()

    try:
        run_result = subprocess.run(  # noqa: S603
            [rt, "start", "-a", container_id],  # noqa: S607
        )
        if run_result.returncode != 0:
            msg = "Cache build failed"
            raise RuntimeError(msg)

        subprocess.run(  # noqa: S603
            [rt, "commit", container_id, target_tag],  # noqa: S607
            capture_output=True,
            check=True,
        )
    finally:
        subprocess.run(  # noqa: S603
            [rt, "rm", "-v", container_id],  # noqa: S607
            capture_output=True,
        )

    print(f"Cached image ready: {target_tag}")
    return target_tag


def ensure_cached_image(
    repo_root: Path,
    lang: str,
    base_image: str,
    *,
    runtime: str = "",
) -> str:
    """Return a cached image tag, building one if needed.

    Returns *base_image* unchanged if no cache-sensitive files are found.
    """
    rt = runtime or detect_runtime()
    files = cache_sensitive_files(repo_root, lang)
    if not files:
        return base_image

    from vergil_tooling.lib import git as _git

    branch = _git.current_branch()
    base_digest, _verified = resolve_base_digest(base_image, runtime=rt)
    current_hash = compute_cache_hash(files, base_digest=base_digest, salt=repo_root.name)
    existing = find_cached_image(base_image, branch, runtime=rt)

    if existing is not None:
        existing_tag, existing_hash = existing
        if existing_hash == current_hash:
            return existing_tag
        # Stale cache — remove it.
        subprocess.run(  # noqa: S603
            [rt, "rmi", existing_tag],  # noqa: S607
            capture_output=True,
        )

    target_tag = cache_image_tag(base_image, branch, current_hash)
    return _build_cached_image(repo_root, lang, base_image, target_tag, runtime=rt)


def provision_dev_image(
    repo_root: Path,
    lang: str,
    *,
    prefix: str = "prod",
    runtime: str = "",
) -> tuple[str, str]:
    """Resolve the dev image for *repo_root*, building/warming it if needed.

    This is the explicit provisioning seam: it names, as a single operation, the
    image resolution that ``vrg-container-run`` performs lazily on every call.
    ``vrg-finalize-pr`` calls it up front — right after develop advances — so the
    target-branch image is warm before validation (or the next PR's work) uses
    it, instead of triggering a cold rebuild mid-operation (issue #2462).

    Returns ``(image, source)`` where *source* is ``"env"`` (a ``DOCKER_DEV_IMAGE``
    override), ``"cached"`` (a per-branch cached image), or ``"default"`` (the base
    image, when the repo declares no cache-sensitive files).

    Kept in step with ``vrg_container_run.main``'s inline resolution: both honour
    ``DOCKER_DEV_IMAGE`` first, then fall back to ``default_image`` +
    ``ensure_cached_image``. Change the two together.
    """
    env_image = os.environ.get("DOCKER_DEV_IMAGE")
    if env_image:
        return env_image, "env"
    # The repo's declared [ci].versions picks the container version, not a
    # hardcoded default (issue #2468), so provisioning warms the same image
    # vrg-container-run selects.
    base = default_image(lang, fallback=True, prefix=prefix, version=primary_ci_version(repo_root))
    image = ensure_cached_image(repo_root, lang, base, runtime=runtime)
    return image, ("cached" if image != base else "default")


def clean_branch_images(branch: str, *, runtime: str = "") -> int:
    """Remove all cached images for *branch*. Returns count removed."""
    rt = runtime or detect_runtime()
    sanitized = _sanitize_branch(branch)
    pattern = f"--{sanitized}--"

    result = subprocess.run(  # noqa: S603
        [rt, "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return 0

    removed = 0
    for line in result.stdout.splitlines():
        if pattern in line:
            subprocess.run(  # noqa: S603
                [rt, "rmi", line],  # noqa: S607
                capture_output=True,
            )
            removed += 1
    return removed
