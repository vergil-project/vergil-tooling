"""Per-branch container image caching with vergil-tooling pre-installed."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import vrg_install_tag
from vergil_tooling.lib.container import (
    _GHCR,
    container_platform,
    default_image,
    detect_runtime,
    repo_resolution,
    workspace_mount_args,
)
from vergil_tooling.lib.languages import CheckKind, language_commands
from vergil_tooling.lib.versions.extensions import container_env

if TYPE_CHECKING:
    from collections.abc import Mapping
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
    "rust": ["Cargo.lock", "vergil.toml"],
    "go": ["go.sum", "vergil.toml"],
    "java": ["pom.xml", "vergil.toml"],
    # cpp names both conanfile spellings; cache_files() filters to what exists.
    "cpp": ["conanfile.txt", "conanfile.py", "conan.lock", "CMakeLists.txt", "vergil.toml"],
    "typescript": ["package-lock.json", "vergil.toml"],
}
_DEFAULT_CACHE_FILES = ["vergil.toml"]

# Files each language's warmup reads. The warmup IS the install-stage command
# list, so it fails outright on a repo whose manifests do not exist yet — the
# bootstrap case, where the container is the natural place to author them
# (issue #2871). Each entry is a list of groups; a group is satisfied when ANY
# of its names is present, and every group must be satisfied for the warmup to
# run. The group form — a group satisfied by any of several names — is retained
# for languages that accept alternative manifest spellings.
#
# cpp's warmup requires conan.lock, because it runs
# `conan install --lockfile=conan.lock` (#3021), exactly as python's warmup
# requires uv.lock. Born-green scaffolding (epic #342) does NOT eliminate a
# half-bootstrapped window — it *creates* one: `scaffold_language` renders the
# cpp skeleton (conanfile.txt, CMakeLists.txt) and then runs `conan lock create`
# to produce the lock. Between those two steps the lock does not yet exist, so a
# warmup that assumed it would fail (`Lockfile doesn't exist`, issue #3049). The
# conan.lock group makes warmup skip during that transient no-lock window, so the
# scaffold's `conan lock create` runs in an unwarmed-but-usable container; the
# later vrg-validate runs with the lock present and warms normally.
_WARMUP_REQUIRES: dict[str, list[list[str]]] = {
    "python": [["pyproject.toml"], ["uv.lock"]],
    "rust": [["Cargo.toml"]],
    "go": [["go.mod"]],
    "java": [["pom.xml"], ["mvnw"]],
    "cpp": [["conanfile.txt", "conanfile.py"], ["CMakeLists.txt"], ["conan.lock"]],
    "typescript": [["package.json"], ["package-lock.json"]],
}


def missing_warmup_files(lang: str, repo_root: Path) -> list[str]:
    """Return unsatisfied warmup prerequisites for *lang*, as display strings.

    Empty when the repo is bootstrapped for *lang* (or when the language
    declares no prerequisites). Each returned entry renders one unsatisfied
    group, e.g. ``"conanfile.txt or conanfile.py"``.
    """
    return [
        " or ".join(group)
        for group in _WARMUP_REQUIRES.get(lang, [])
        if not any((repo_root / name).is_file() for name in group)
    ]


def _warmup_command(lang: str, repo_root: Path) -> str:
    """Return the warmup shell command, or ``""`` when it cannot run.

    Skipping yields an image that is unwarmed but usable, rather than aborting
    the image build and taking ``vrg-container-run`` down with it. The caller
    reports the skip; see :func:`missing_warmup_files`.
    """
    cmds = language_commands(lang, CheckKind.INSTALL)
    if not cmds or missing_warmup_files(lang, repo_root):
        return ""
    return " && ".join(" ".join(cmd) for cmd in cmds)


def apt_install_command(packages: list[str], platform_label: str) -> str:
    """Return a shell snippet installing *packages* fail-closed, or ``""``.

    Installs one package at a time so a missing candidate names the offending
    package; on failure it prints a message with the package and *platform_label*
    and exits non-zero (no silent skip). Debian ``apt`` names, from the base
    image's existing sources, ``--no-install-recommends``. This is the single
    speller shared by the local cache build and the CI setup step (epic
    vergil-project/.github#272).
    """
    if not packages:
        return ""
    parts = ["apt-get update"]
    for pkg in packages:
        q = shlex.quote(pkg)
        parts.append(
            f"{{ apt-get install -y --no-install-recommends {q} "
            f'|| {{ echo "system package {q} is not installable on '
            f'{platform_label} (apt: no installation candidate)" >&2; exit 1; }} }}'
        )
    return " && ".join(parts)


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
    """Return paths of cache-sensitive files that exist in *repo_root*.

    The language defaults (lockfiles + ``vergil.toml``) are joined with the
    repo-relative files named in ``[container].build-cache-files`` — the inputs
    the repo's ``build-command`` reads — so a bump to a declared lockfile changes
    the image cache hash and forces a rebuild (epic vergil-project/.github#291).
    Declared files that duplicate a default are folded in once; only existing
    files are returned.
    """
    from vergil_tooling.lib.config import container_build_cache_files

    names = list(_CACHE_FILES.get(lang, _DEFAULT_CACHE_FILES))
    for name in container_build_cache_files(repo_root):
        if name not in names:
            names.append(name)
    return [repo_root / n for n in names if (repo_root / n).is_file()]


def compute_cache_hash(
    files: list[Path],
    *,
    base_digest: str = "",
    salt: str = "",
    env: Mapping[str, str] | None = None,
) -> str:
    """SHA-256 over sorted file contents, base image digest, salt, and build env.

    Folding ``base_digest`` (the content id of the base image the cache is built
    from) into the key means a republished base tag yields a different hash, so the
    stale cache is rebuilt instead of reused. ``env`` is the environment the setup
    step runs with (:func:`_compose_setup`); folding it in means a change of
    resolved version or interpreter source rebuilds the cache (#3421). An empty
    ``env`` adds nothing, so a language without a container env keeps its key.
    Returns the first 8 hex chars.
    """
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
    if base_digest:
        h.update(base_digest.encode())
    if salt:
        h.update(salt.encode())
    if env:
        h.update(b"\0env\0")
        h.update("\0".join(f"{k}={v}" for k, v in sorted(env.items())).encode())
    return h.hexdigest()[:8]


def _sanitize_branch(branch: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", branch)


def cache_image_tag(base_image: str, branch: str, cache_hash: str) -> str:
    """Construct the cached image tag."""
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    sanitized = _sanitize_branch(branch)
    return f"{base_repo}:{base_tag}--{sanitized}--{cache_hash}"


# A cached image's reference, as cache_image_tag builds it:
# ``<base_repo>:<base_tag>--<sanitized_branch>--<cache_hash>``. Base images are
# only ever ours (default_image), the base tag never contains ``--``, and the hash
# is compute_cache_hash's 8 hex chars. The branch is the greedy middle, so a
# sanitized name that itself contains ``--`` still parses whole.
_CACHE_REF_RE = re.compile(
    rf"^{re.escape(_GHCR)}/[^:]+:(?P<base_tag>.+?)--(?P<branch>.+)--(?P<hash>[0-9a-f]{{8}})$"
)


def parse_cache_image_ref(ref: str) -> tuple[str, str] | None:
    """Return ``(base_tag, sanitized_branch)`` for a cached image *ref*, else None.

    The inverse of :func:`cache_image_tag`, as far as it goes: the branch comes
    back sanitized (``feature/1-x`` and ``feature-1-x`` both read
    ``feature-1-x``), and the tag carries no repository at all, so the caller
    must match the branch against every repository that could have built it.
    Anything not shaped exactly like a cached image (a base image, a foreign
    image, ``<none>``) returns None so it is never treated as ours.
    """
    match = _CACHE_REF_RE.match(ref)
    if match is None:
        return None
    return match["base_tag"], match["branch"]


def sanitize_branch(branch: str) -> str:
    """Return *branch* as it appears in a cached image tag."""
    return _sanitize_branch(branch)


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


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _env_export(build_env: Mapping[str, str]) -> str:
    """Return ``export K=V …`` for *build_env* (values shell-quoted), or ``""``."""
    for name in build_env:
        if not _ENV_NAME_RE.fullmatch(name):
            msg = f"{name!r} is not a valid environment variable name"
            raise ValueError(msg)
    if not build_env:
        return ""
    return "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in build_env.items())


def _compose_setup(repo_root: Path, lang: str, *, build_env: Mapping[str, str]) -> str:
    """Return the shell setup string baked into the cached image.

    The composed step is, in order,
    ``<export env> && <apt fragment> && <uv tool install …> && <build-command> &&
    <warmup>``:

    * **env** — *build_env*, the resolver's container env
      (:func:`vergil_tooling.lib.versions.extensions.container_env`), exported
      first so every later fragment runs with it, exactly as the run path passes
      it to the run container. For Python this is ``UV_PYTHON`` plus
      ``UV_PYTHON_PREFERENCE=only-system``, so the warmup uses the image's
      interpreter and a repo's ``python-preference`` cannot make uv download its
      own (spec §6.5, #3421). Exported in the shell, not set on the container, so
      it is not baked into the committed image's config. Empty ⇒ no fragment.
    * **apt** — repo-declared ``[container].system-packages``, installed first so
      the rest of the step runs against the provisioned base (epic
      vergil-project/.github#272).
    * **uv install** — the pinned vergil-tooling install. Skipped for the self-repo
      (vergil-tooling itself), which uses its local dev version; the build-command
      then runs after apt and before warmup, the same relative slot.
    * **build-command** — the repo's declared ``[container].build-command`` (epic
      vergil-project/.github#291), run after the install so its environment matches
      CI. Absent (``None``) ⇒ no fragment, and the result is byte-identical to the
      pre-existing apt/install/warmup composition. A non-zero build-command aborts
      the ``&&`` chain, failing the build fail-closed via the setup-step path.
    * **warmup** — the language dependency warmup.

    Pure and unit-testable: it reads config and derives the string without
    spawning a container.
    """
    from vergil_tooling.lib.config import (
        container_build_command,
        container_system_packages,
    )

    warmup = _warmup_command(lang, repo_root)
    build = container_build_command(repo_root)

    # The post-install tail: build-command then warmup, each included only when
    # present. Keeping the tail separate reproduces the old install/warmup joining
    # exactly, so an absent build-command yields a byte-identical setup string.
    tail = " && ".join(part for part in (build, warmup) if part)

    if _is_self_repo(repo_root):
        setup = tail or "true"
    else:
        tag = vrg_install_tag(repo_root)
        uv_install = f"uv tool install --quiet 'vergil-tooling @ git+{_VRG_GIT_URL}@{tag}'"
        setup = f"{uv_install} && {tail}" if tail else uv_install

    apt = apt_install_command(container_system_packages(repo_root), container_platform())
    if apt:
        setup = f"{apt} && {setup}"
    export = _env_export(build_env)
    if export:
        setup = f"{export} && {setup}"
    return setup


def _build_cached_image(
    repo_root: Path,
    lang: str,
    base_image: str,
    target_tag: str,
    *,
    runtime: str = "",
    quiet_warmup: bool = False,
    build_env: Mapping[str, str],
) -> str:
    """Build a cached image with vergil-tooling installed.

    ``build_env`` is exported for the whole setup step (see :func:`_compose_setup`).

    ``quiet_warmup`` captures the warmup subprocess's output instead of
    streaming it to the inherited terminal, surfacing it only on failure. The
    finalize pipeline sets it: provisioning runs under a live progress display
    that owns stdout, and a raw-streamed multi-minute build (e.g. cpp compiling
    GoogleTest from source) reads as output "outside the script" over the
    progress tree (#2906). The interactive ``vrg-container-run`` path leaves it
    False so a cold rebuild still streams live and never looks hung.
    """
    rt = runtime or detect_runtime()
    self_repo = _is_self_repo(repo_root)
    warmup = _warmup_command(lang, repo_root)
    warmup_missing = missing_warmup_files(lang, repo_root)

    from vergil_tooling.lib.config import (
        container_build_command,
        container_system_packages,
    )

    apt = apt_install_command(container_system_packages(repo_root), container_platform())
    build = container_build_command(repo_root)
    setup = _compose_setup(repo_root, lang, build_env=build_env)

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
        print(f"  Install: vergil-tooling@{vrg_install_tag(repo_root)}")
    if build_env:
        print(f"  Env:     {' '.join(f'{k}={v}' for k, v in build_env.items())}")
    if build:
        print(f"  Build:   {build}")
    if warmup:
        print(f"  Warmup:  {warmup}")
    elif warmup_missing:
        # Say so out loud: a silently unwarmed image would look identical to a
        # warmed one right up until a dependency was unexpectedly absent.
        print(
            f"  Warmup:  skipped — no {', '.join(warmup_missing)} "
            f"({lang} repo not bootstrapped yet)"
        )
    if apt:
        print(f"  Packages: {' '.join(container_system_packages(repo_root))}")

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
            capture_output=quiet_warmup,
            text=True if quiet_warmup else None,
        )
        if run_result.returncode != 0:
            msg = "Cache build failed"
            if quiet_warmup:
                # The output was captured (not streamed), so fold it into the
                # error — otherwise a provisioning failure surfaces with no
                # diagnostic at all under the finalize progress display (#2906).
                captured = "".join(
                    part for part in (run_result.stdout, run_result.stderr) if part
                ).strip()
                if captured:
                    msg = f"{msg}\n{captured}"
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


def _image_is_launchable(image: str, *, runtime: str = "") -> bool:
    """Return True if *image* can instantiate a container right now.

    ``docker images`` can list a cached image whose underlying snapshot layers
    have been orphaned in the container store: the image record survives, but a
    parent snapshot is gone, so the next ``docker run`` fails at container
    creation ("parent snapshot … does not exist: not found", exit 125).
    ``image inspect`` does not detect this — only preparing a container rootfs
    does. Probe by *creating* (not starting) a throwaway container from the
    image and removing it: creation exercises snapshot preparation without
    executing anything in the image (issue #3016). ``--pull=never`` keeps the
    probe purely local — it must never mask a corrupt cache by fetching.
    """
    rt = runtime or detect_runtime()
    create = subprocess.run(  # noqa: S603
        [rt, "create", f"--platform={container_platform()}", "--pull=never", image, "true"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    container_id = create.stdout.strip()
    if container_id:
        subprocess.run(  # noqa: S603
            [rt, "rm", "-f", container_id],  # noqa: S607
            capture_output=True,
        )
    return create.returncode == 0


def ensure_cached_image(
    repo_root: Path,
    lang: str,
    base_image: str,
    *,
    runtime: str = "",
    quiet_warmup: bool = False,
    build_env: Mapping[str, str],
) -> str:
    """Return a cached image tag, building one if needed.

    Returns *base_image* unchanged if no cache-sensitive files are found.
    ``quiet_warmup`` is forwarded to :func:`_build_cached_image` (see there).

    ``build_env`` is the resolver's container env (``container_env(resolution)``,
    the same env the run path passes). It is required, so no caller can build a
    cache without deciding it: it is exported for the setup step and folded into
    the cache key, so a change of resolved version or source rebuilds (#3421).
    """
    rt = runtime or detect_runtime()
    files = cache_sensitive_files(repo_root, lang)
    if not files:
        return base_image

    from vergil_tooling.lib import git as _git

    branch = _git.current_branch()
    base_digest, _verified = resolve_base_digest(base_image, runtime=rt)
    current_hash = compute_cache_hash(
        files, base_digest=base_digest, salt=repo_root.name, env=build_env
    )
    existing = find_cached_image(base_image, branch, runtime=rt)

    if existing is not None:
        existing_tag, existing_hash = existing
        if existing_hash == current_hash and _image_is_launchable(existing_tag, runtime=rt):
            return existing_tag
        # Either the cache key moved (stale hash) or the listed image can no
        # longer launch — its snapshot layers were orphaned in the container
        # store (issue #3016). A metadata match is not proof the image will run,
        # so in both cases drop the old tag and rebuild rather than hand back an
        # image that fails at container creation. Force removal and surface a
        # non-zero result instead of swallowing it: the rebuild still proceeds
        # (commit reassigns the tag), but a genuine removal error should be seen.
        rmi = subprocess.run(  # noqa: S603
            [rt, "rmi", "-f", existing_tag],  # noqa: S607
            capture_output=True,
            text=True,
        )
        if rmi.returncode != 0:
            print(
                f"warning: could not remove stale cached image '{existing_tag}': "
                f"{rmi.stderr.strip()}; rebuilding anyway",
                file=sys.stderr,
            )

    target_tag = cache_image_tag(base_image, branch, current_hash)
    return _build_cached_image(
        repo_root,
        lang,
        base_image,
        target_tag,
        runtime=rt,
        quiet_warmup=quiet_warmup,
        build_env=build_env,
    )


def provision_dev_image(
    repo_root: Path,
    lang: str,
    *,
    prefix: str = "prod",
    runtime: str = "",
    quiet_warmup: bool = False,
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

    ``quiet_warmup`` is forwarded to the cache build so the warmup output is
    captured (surfaced on failure) rather than streamed. ``vrg-finalize-pr``
    sets it because it provisions under a live progress display (#2906).
    """
    env_image = os.environ.get("DOCKER_DEV_IMAGE")
    if env_image:
        return env_image, "env"
    # The version resolver picks the container version from the published
    # window, so provisioning warms the same image vrg-container-run selects.
    resolution = repo_resolution(repo_root, lang)
    base = default_image(resolution, fallback=True, prefix=prefix)
    image = ensure_cached_image(
        repo_root,
        lang,
        base,
        runtime=runtime,
        quiet_warmup=quiet_warmup,
        build_env=container_env(resolution),
    )
    return image, ("cached" if image != base else "default")
