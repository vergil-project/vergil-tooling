"""Per-branch Docker image caching with standard-tooling pre-installed."""

from __future__ import annotations

import hashlib
import re
import subprocess
from typing import TYPE_CHECKING

from standard_tooling.lib.config import st_install_tag

if TYPE_CHECKING:
    from pathlib import Path

_SELF_PROJECT_NAME = "standard-tooling"

_ST_GIT_URL = "https://github.com/wphillipmoore/standard-tooling"

_CACHE_FILES: dict[str, list[str]] = {
    "python": ["uv.lock", "standard-tooling.toml"],
    "ruby": ["Gemfile.lock", "standard-tooling.toml"],
    "rust": ["Cargo.lock", "standard-tooling.toml"],
    "go": ["go.sum", "standard-tooling.toml"],
    "java": ["pom.xml", "standard-tooling.toml"],
}
_DEFAULT_CACHE_FILES = ["standard-tooling.toml"]

_WARMUP_COMMANDS: dict[str, str] = {
    "python": "uv sync --group dev",
    "ruby": "bundle install --jobs 4",
    "rust": "cargo fetch && cargo build --lib",
    "go": "go mod download && go build ./...",
    "java": "./mvnw dependency:resolve",
}


def _is_self_repo(repo_root: Path) -> bool:
    """Return True when *repo_root* is the standard-tooling project itself."""
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


def compute_cache_hash(files: list[Path], *, salt: str = "") -> str:
    """SHA-256 over sorted file contents plus optional salt, first 8 hex chars."""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
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


def find_cached_image(base_image: str, branch: str) -> tuple[str, str] | None:
    """Find an existing cached image for *base_image* and *branch*.

    Returns ``(full_tag, hash_suffix)`` or ``None``.
    """
    sanitized = _sanitize_branch(branch)
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    pattern = f"{base_repo}:{base_tag}--{sanitized}--"

    result = subprocess.run(  # noqa: S603
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
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
) -> str:
    """Build a cached image with standard-tooling installed."""
    self_repo = _is_self_repo(repo_root)
    warmup = _WARMUP_COMMANDS.get(lang)

    if self_repo:
        setup = warmup or "true"
    else:
        tag = st_install_tag(repo_root)
        uv_install = f"uv tool install --quiet 'standard-tooling @ git+{_ST_GIT_URL}@{tag}'"
        setup = f"{uv_install} && {warmup}" if warmup else uv_install

    print(f"Building cached image: {target_tag}")
    print(f"  Base:    {base_image}")
    if self_repo:
        print("  Install: skipped (self-repo uses local dev version)")
    else:
        print(f"  Install: standard-tooling@{tag}")
    if warmup:
        print(f"  Warmup:  {warmup}")

    cid_result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "create",
            "-v",
            f"{repo_root}:/workspace",
            "-w",
            "/workspace",
            base_image,
            "bash",
            "-c",
            setup,
        ],
        capture_output=True,
        text=True,
    )
    if cid_result.returncode != 0:
        msg = f"Failed to create container: {cid_result.stderr.strip()}"
        raise RuntimeError(msg)

    container_id = cid_result.stdout.strip()

    try:
        run_result = subprocess.run(  # noqa: S603
            ["docker", "start", "-a", container_id],  # noqa: S607
        )
        if run_result.returncode != 0:
            msg = "Cache build failed"
            raise RuntimeError(msg)

        subprocess.run(  # noqa: S603
            ["docker", "commit", container_id, target_tag],  # noqa: S607
            capture_output=True,
            check=True,
        )
    finally:
        subprocess.run(  # noqa: S603
            ["docker", "rm", container_id],  # noqa: S607
            capture_output=True,
        )

    print(f"Cached image ready: {target_tag}")
    return target_tag


def ensure_cached_image(
    repo_root: Path,
    lang: str,
    base_image: str,
) -> str:
    """Return a cached image tag, building one if needed.

    Returns *base_image* unchanged if no cache-sensitive files are found.
    """
    files = cache_sensitive_files(repo_root, lang)
    if not files:
        return base_image

    from standard_tooling.lib import git as _git

    branch = _git.current_branch()
    current_hash = compute_cache_hash(files, salt=repo_root.name)
    existing = find_cached_image(base_image, branch)

    if existing is not None:
        existing_tag, existing_hash = existing
        if existing_hash == current_hash:
            return existing_tag
        # Stale cache — remove it.
        subprocess.run(  # noqa: S603
            ["docker", "rmi", existing_tag],  # noqa: S607
            capture_output=True,
        )

    target_tag = cache_image_tag(base_image, branch, current_hash)
    return _build_cached_image(repo_root, lang, base_image, target_tag)


def clean_branch_images(branch: str) -> int:
    """Remove all cached images for *branch*. Returns count removed."""
    sanitized = _sanitize_branch(branch)
    pattern = f"--{sanitized}--"

    result = subprocess.run(  # noqa: S603
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return 0

    removed = 0
    for line in result.stdout.splitlines():
        if pattern in line:
            subprocess.run(  # noqa: S603
                ["docker", "rmi", line],  # noqa: S607
                capture_output=True,
            )
            removed += 1
    return removed
