"""Per-branch container image caching with vergil-tooling pre-installed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import tooling_install
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
from vergil_tooling.lib.self_repo import is_self_repo
from vergil_tooling.lib.tooling_release import ToolingResolveError, resolve_tooling_tag
from vergil_tooling.lib.versions.extensions import container_env
from vergil_tooling.lib.vm_packages import classify_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from vergil_tooling.lib.tooling_install import ResolvedRef

_PULL_TIMEOUT_SECONDS = 120

# A transient pull failure is retried: _PULL_ATTEMPTS pulls in all, waiting
# _PULL_RETRY_DELAYS_SECONDS[i] after attempt i + 1 fails, so an outage that
# fails fast still fails within seconds (#4210). A pull that hangs is bounded
# by _PULL_ATTEMPTS times _PULL_TIMEOUT_SECONDS.
_PULL_ATTEMPTS = 3
_PULL_RETRY_DELAYS_SECONDS = (2.0, 5.0)

# Error text a failed pull prints when the registry gave a definite answer.
# Checked before the transient patterns, so a definite answer wrapped in a
# transient-looking prefix (containerd's "failed to fetch anonymous token:
# unexpected status from GET request to ...: 401 Unauthorized") still fails at
# once. Where each comes from:
# - "manifest unknown", "denied", "unauthorized": the OCI distribution spec's
#   error codes (MANIFEST_UNKNOWN, DENIED, UNAUTHORIZED), as distribution's
#   registry/api/errcode/register.go defines them; "unauthorized" is also
#   Go's status text for a 401 ("401 Unauthorized").
# - "not found": containerd's errdefs.ErrNotFound, which its resolver
#   (core/remotes/docker/resolver.go) wraps for a missing reference, and
#   moby's "manifest for %s not found" / "repository %s not found"
#   (daemon/internal/distribution/errors.go).
# - "authentication required": distribution's UNAUTHORIZED message.
# - "authorization failed": containerd's ErrInvalidAuthorization
#   (core/remotes/docker/resolver.go); "pull access denied" (moby's and
#   containerd's wording for a missing or private repository) is matched by
#   "denied".
_DEFINITE_PULL_ERROR_PATTERNS = (
    "manifest unknown",
    "not found",
    "denied",
    "unauthorized",
    "authentication required",
    "authorization failed",
)

# Error text a failed pull prints when it never got an answer from the
# registry, or got a server-side failure. Docker and nerdctl both reach the
# registry through Go's net/http (and docker's containerd image store, like
# nerdctl, through containerd's resolver), so the text is Go's:
# - "timeout awaiting response headers", "tls handshake timeout",
#   "request canceled while waiting for connection": net/http's transport
#   errors (src/net/http/transport.go). The first is the cause in #4209's
#   "failed to fetch anonymous token: ... net/http: timeout awaiting response
#   headers": a failed token fetch is retried by its cause, never by the
#   "failed to fetch ... token" prefix alone (containerd's authorizer.go wraps
#   every token error, a 401 included, in that prefix).
# - "i/o timeout": Go's net and internal/poll deadline error text.
# - "context deadline exceeded": Go's context.DeadlineExceeded.
# - "no such host": Go's net DNS errNoSuchHost (src/net/net.go).
# - "server misbehaving": Go's DNS errServerMisbehaving, a SERVFAIL reply
#   (src/net/dnsclient_unix.go).
# - "temporary failure in name resolution": glibc's EAI_AGAIN text
#   (gai_strerror), printed when the runtime resolves through cgo.
# - "connection refused", "connection reset by peer", "network is
#   unreachable", "no route to host": the Linux errno texts Go's syscall
#   package prints for ECONNREFUSED, ECONNRESET, ENETUNREACH, EHOSTUNREACH.
# - "unexpected eof": Go's io.ErrUnexpectedEOF, a connection dropped mid-body.
# - A 5xx status, as containerd's ErrUnexpectedStatus prints it ("unexpected
#   status from %s request to %s: %s", core/remotes/errors/errors.go) or as
#   distribution's client does ("received unexpected HTTP status: %s",
#   internal/client/errors.go); and "service unavailable", distribution's
#   UNAVAILABLE error message and Go's 503 status text.
# Anything matching neither list is NOT transient: an unrecognised failure
# fails at once, as every failure did before retries existed, rather than
# being retried on a guess.
_TRANSIENT_PULL_ERROR_PATTERNS = (
    "timeout awaiting response headers",
    "tls handshake timeout",
    "request canceled while waiting for connection",
    "i/o timeout",
    "context deadline exceeded",
    "no such host",
    "server misbehaving",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset by peer",
    "network is unreachable",
    "no route to host",
    "unexpected eof",
    "service unavailable",
)
_TRANSIENT_PULL_STATUS = re.compile(
    r"(?:unexpected status from \S+ request to \S+|received unexpected http status): 5\d\d\b"
)

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
#
# rust's warmup requires Cargo.lock as its own group, because it runs
# `cargo fetch --locked` (epic vergil-project/.github#1165, spec §6.1). A repo
# with no lockfile skips the warmup, so `cargo generate-lockfile` can still run
# in the container. In the Cargo.toml group it would not be required at all.
_WARMUP_REQUIRES: dict[str, list[list[str]]] = {
    "python": [["pyproject.toml"], ["uv.lock"]],
    "rust": [["Cargo.toml"], ["Cargo.lock"]],
    "go": [["go.mod"]],
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


# The package.json fields that declare dependencies npm would install.
_NPM_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def _package_json_declares_no_dependencies(path: Path) -> bool:
    """Return True when *path* is a JSON object that declares no npm dependency.

    Each of :data:`_NPM_DEPENDENCY_FIELDS` must be absent or an empty object,
    as in the minimal ``package.json`` ``vrg-github-repo-init`` writes
    (``name``, ``version``, ``private``; vergil-tooling#4552). A file that is
    not such an object (JSON that does not parse, a field of another type)
    returns False: the repo then counts as bootstrapped, and its install
    stage reports the error, so a broken manifest never passes as unstarted.
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return False
    if not isinstance(manifest, dict):
        return False
    return all(manifest.get(field, {}) == {} for field in _NPM_DEPENDENCY_FIELDS)


def unbootstrapped_manifests(lang: str, repo_root: Path) -> list[str]:
    """Return *lang*'s missing manifests when the repo is not bootstrapped, else ``[]``.

    The same table as the image warmup (:data:`_WARMUP_REQUIRES`), read
    strictly: a repo is *unbootstrapped* only when **every** group is
    unsatisfied, as in a freshly initialised repo with no crate or project yet.
    vrg-validate skips its language stages, loudly, for such a repo (issue
    #4384). A partial bootstrap (``pyproject.toml`` with no ``uv.lock``, say)
    is not this state: the warmup may skip it, but validation runs and fails
    on it, so a broken project never passes as an unstarted one.

    TypeScript has one more unbootstrapped state: a ``package.json`` that
    declares no dependencies, with no ``package-lock.json``. That is exactly
    what ``vrg-github-repo-init`` writes (vergil-tooling#4624), and only the
    lockfile is returned as missing. A ``package.json`` that declares
    dependencies but has no lockfile is a partial bootstrap, and fails.
    """
    groups = _WARMUP_REQUIRES.get(lang, [])
    missing = missing_warmup_files(lang, repo_root)
    if groups and len(missing) == len(groups):
        return missing
    if (
        lang == "typescript"
        and missing == ["package-lock.json"]
        and _package_json_declares_no_dependencies(repo_root / "package.json")
    ):
        return missing
    return []


# File suffixes of each language's own sources. A repo with none of its
# manifests but with sources like these is not unstarted: it has lost its
# manifest, or never got one, so vrg-validate fails it rather than skip its
# language stages (issue #4384).
_SOURCE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "rust": (".rs",),
    "go": (".go",),
    "cpp": (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"),
    "typescript": (".ts", ".tsx", ".mts", ".cts"),
}


def language_sources(lang: str, paths: Iterable[str]) -> list[str]:
    """Return the paths in *paths* that are *lang* source files, sorted.

    Always empty for a language with no source suffixes declared.
    """
    suffixes = _SOURCE_SUFFIXES.get(lang)
    if not suffixes:
        return []
    return sorted(path for path in paths if path.endswith(suffixes))


# The line the warmup shell prints when it skips itself at run time, as the
# rust warmup does on a stale Cargo.lock. It is a fixed prefix so the host can
# find it in captured output (``quiet_warmup``) and print the notice itself:
# a skip is never silent.
WARMUP_SKIPPED_MARKER = "VRG-WARMUP-SKIPPED:"
# cargo's own error when ``--locked`` refuses a lockfile that needs updating.
# Its wording has changed: older cargo says "the lock file <path> needs to be
# updated but --locked was passed to prevent this", cargo 1.99 says "cannot
# update the lock file <path> because --locked was passed to prevent this".
# This is the part both share.
_CARGO_STALE_LOCK_ERROR = "--locked was passed to prevent this"
_RUST_STALE_LOCK_NOTICE = (
    "warmup skipped: Cargo.lock does not match Cargo.toml; update it in the "
    "container (a cargo command without --locked, such as 'cargo fetch') and commit it"
)


def _warmup_line(lang: str, repo_root: Path) -> str:
    """Return *lang*'s install commands as one shell line, or ``""`` when it cannot run."""
    cmds = language_commands(lang, CheckKind.INSTALL)
    if not cmds or missing_warmup_files(lang, repo_root):
        return ""
    return " && ".join(" ".join(cmd) for cmd in cmds)


def _rust_warmup(line: str) -> str:
    """Wrap rust's locked fetch so a stale ``Cargo.lock`` skips the warmup, loudly.

    The fetch runs with ``--locked``, so it never writes ``Cargo.lock``. Its
    output is streamed and kept; when it fails with cargo's stale-lockfile
    error, the warmup prints the :data:`WARMUP_SKIPPED_MARKER` line and the
    image build goes on, unwarmed. Any other failure (a registry outage, a git
    dependency that cannot be fetched) exits with cargo's status, failing the
    build with cargo's output above it. One brace group, so it composes into
    the setup's ``&&`` chain. ``pipefail`` keeps cargo's status through
    ``tee``; the setup runs under ``bash -c``.
    """
    notice = shlex.quote(f"{WARMUP_SKIPPED_MARKER} {_RUST_STALE_LOCK_NOTICE}")
    stale = shlex.quote(_CARGO_STALE_LOCK_ERROR)
    return (
        "{ vrg_log=$(mktemp) || exit 1; "
        f'( set -o pipefail; {line} 2>&1 | tee "$vrg_log" ); vrg_rc=$?; '
        'if [ "$vrg_rc" -ne 0 ]; then '
        f'if grep -qF -e {stale} "$vrg_log"; then echo {notice}; '
        'else exit "$vrg_rc"; fi; '
        'fi; rm -f "$vrg_log"; }'
    )


def _warmup_command(lang: str, repo_root: Path) -> str:
    """Return the warmup shell command, or ``""`` when it cannot run.

    Skipping yields an image that is unwarmed but usable, rather than aborting
    the image build and taking ``vrg-container-run`` down with it. The caller
    reports the skip; see :func:`missing_warmup_files`. Rust's warmup also
    skips itself at run time on a stale ``Cargo.lock`` (:func:`_rust_warmup`).
    """
    line = _warmup_line(lang, repo_root)
    if line and lang == "rust":
        return _rust_warmup(line)
    return line


def _report_warmup_skip(captured: str) -> None:
    """Print the notice of each warmup skip found in *captured* output.

    A quiet build captures the warmup's output, so the shell's own skip line
    is never seen; this prints it on the host instead.
    """
    for raw in captured.splitlines():
        line = raw.strip()
        if line.startswith(WARMUP_SKIPPED_MARKER):
            print(f"NOTICE: {line.removeprefix(WARMUP_SKIPPED_MARKER).strip()}")


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


def _is_transient_pull_error(stderr: str) -> bool:
    """Return True when a failed pull's *stderr* names a transient cause.

    A definite registry answer (see ``_DEFINITE_PULL_ERROR_PATTERNS``) is never
    transient, even inside a transient-looking message. Text matching no known
    transient pattern is not transient either, so an unrecognised failure
    fails at once rather than being retried.
    """
    text = stderr.lower()
    if any(pattern in text for pattern in _DEFINITE_PULL_ERROR_PATTERNS):
        return False
    if any(pattern in text for pattern in _TRANSIENT_PULL_ERROR_PATTERNS):
        return True
    return _TRANSIENT_PULL_STATUS.search(text) is not None


def _pull_with_retry(
    base_image: str, *, runtime: str, sleep: Callable[[float], None]
) -> tuple[bool, str]:
    """Pull *base_image*, retrying transient failures; return ``(ok, error)``.

    Makes up to ``_PULL_ATTEMPTS`` pulls, sleeping the next of
    ``_PULL_RETRY_DELAYS_SECONDS`` before each retry. A timed-out pull is
    transient; a failed one is retried only when ``_is_transient_pull_error``
    says so. On failure, *error* names the number of attempts made and each
    attempt's summarized error.
    """
    errors: list[str] = []
    attempt = 0
    while True:
        attempt += 1
        try:
            pull = subprocess.run(  # noqa: S603
                [runtime, "pull", base_image],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=_PULL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"timed out after {_PULL_TIMEOUT_SECONDS}s")
            transient = True
        else:
            if pull.returncode == 0:
                return True, ""
            errors.append(_summarize_pull_error(pull.stderr))
            transient = _is_transient_pull_error(pull.stderr)
        if not transient or attempt == _PULL_ATTEMPTS:
            break
        delay = _PULL_RETRY_DELAYS_SECONDS[attempt - 1]
        print(
            f"warning: base pull of '{base_image}' failed on attempt "
            f"{attempt}/{_PULL_ATTEMPTS} ({errors[-1]}); retrying in {delay:g}s",
            file=sys.stderr,
        )
        sleep(delay)
    if len(errors) == 1:
        return False, f"1 attempt: {errors[0]}"
    detail = "; ".join(f"attempt {i}: {error}" for i, error in enumerate(errors, 1))
    return False, f"{len(errors)} attempts: {detail}"


def resolve_base_digest(
    base_image: str,
    *,
    runtime: str = "",
    allow_stale: bool | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[str, bool]:
    """Resolve *base_image*'s content digest, refreshing it from the registry.

    Pulls the base (so a moved tag is both detected and available to build from),
    then inspects the local image id. The pull *is* the staleness check: if it
    fails, we genuinely cannot tell whether the local base is current. A
    transient pull failure (a timeout, a network error, a registry 5xx) is
    retried first; see ``_pull_with_retry``. *sleep* (default ``time.sleep``)
    is the wait between attempts, injectable for tests.

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

    pull_ok, pull_error = _pull_with_retry(base_image, runtime=rt, sleep=sleep or time.sleep)

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
    tooling: str = "",
    tooling_sha: str = "",
) -> str:
    """SHA-256 over sorted file contents, base image digest, salt, build env and tooling.

    Folding ``base_digest`` (the content id of the base image the cache is built
    from) into the key means a republished base tag yields a different hash, so the
    stale cache is rebuilt instead of reused. ``env`` is the environment the setup
    step runs with (:func:`_compose_setup`); folding it in means a change of
    resolved version or interpreter source rebuilds the cache (#3421). An empty
    ``env`` adds nothing, so a language without a container env keeps its key.
    ``tooling`` is the vergil-tooling release the cache installs, the pin as
    :func:`~vergil_tooling.lib.tooling_release.resolve_tooling_tag` resolves it
    (an exact tag ``vX.Y.Z``, or a dev ref): a new release on a pinned line
    changes the hash, so the cache is rebuilt with it (#4550). Empty for the
    self-repo, which installs none. ``tooling_sha`` is the commit a line pin
    resolved to, a separate input so the key names the commit the build
    installs; empty for an exact pin, whose key is its immutable tag alone, and
    for a dev ref. Returns the first 8 hex chars.
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
    if tooling:
        h.update(b"\0tooling\0")
        h.update(tooling.encode())
    if tooling_sha:
        h.update(b"\0tooling-sha\0")
        h.update(tooling_sha.encode())
    return h.hexdigest()[:8]


def _sanitize_branch(branch: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", branch)


def sanitize_repo(name: str) -> str:
    """Return the repository *name* as it appears in a cached image tag.

    Characters a tag cannot hold become ``-``, runs of ``-`` collapse to one and
    leading or trailing ``-`` are dropped, so the name never contains ``--``,
    the tag's separator: that is what keeps :func:`parse_cache_image_ref`
    unambiguous (#4162). Raises ``ValueError`` when nothing is left.
    """
    sanitized = re.sub(r"-{2,}", "-", re.sub(r"[^a-zA-Z0-9._-]", "-", name)).strip("-")
    if not sanitized:
        msg = f"repository name {name!r} leaves nothing to name a cached image by"
        raise ValueError(msg)
    return sanitized


def cache_repo_name(repo_root: Path) -> str:
    """Return the repository a cached image built from *repo_root* belongs to.

    The directory name of the repository's main worktree, the clone itself, as
    :func:`vergil_tooling.lib.git.main_worktree_root` reads it. A secondary
    worktree (``.worktrees/issue-<N>-<slug>``) belongs to its clone, so the name
    is the same from every worktree of one repository (#4162). Raises
    ``subprocess.CalledProcessError`` when *repo_root* is not in a git
    repository.
    """
    from vergil_tooling.lib import git as _git

    common_dir = _git.read_output(
        "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    return Path(common_dir).resolve().parent.name


# The longest tag a container runtime accepts (the distribution spec's limit).
_MAX_TAG_LENGTH = 128


def cache_image_tag(base_image: str, branch: str, cache_hash: str, *, repo: str) -> str:
    """Construct the cached image reference for *repo* on *branch*.

    ``<base_repo>:<base_tag>--<sanitized_branch>--<repo>.<cache_hash>`` (#4162):
    the repository is in the tag, so two repositories on one base image and
    branch each have their own cached image. Raises ``ValueError`` when the tag
    is longer than a runtime accepts, rather than failing later at commit.
    """
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    tag = f"{base_tag}--{_sanitize_branch(branch)}--{sanitize_repo(repo)}.{cache_hash}"
    if len(tag) > _MAX_TAG_LENGTH:
        msg = (
            f"cached image tag '{tag}' is {len(tag)} characters, over the "
            f"{_MAX_TAG_LENGTH} a container runtime accepts; use a shorter branch name"
        )
        raise ValueError(msg)
    return f"{base_repo}:{tag}"


# A cached image's reference, as cache_image_tag builds it:
# ``<base_repo>:<base_tag>--<sanitized_branch>--<repo>.<cache_hash>``. Base images
# are only ever ours (default_image), the base tag never contains ``--``, the
# repository never contains ``--`` (sanitize_repo) and the hash is
# compute_cache_hash's 8 hex chars. The branch is the greedy middle, so a
# sanitized name that itself contains ``--`` still parses whole.
_CACHE_REF_RE = re.compile(
    rf"^(?P<base_repo>{re.escape(_GHCR)}/[^:]+):(?P<base_tag>.+?)--(?P<branch>.+)"
    r"--(?P<repo>[a-zA-Z0-9._]+(?:-[a-zA-Z0-9._]+)*)\.(?P<hash>[0-9a-f]{8})$"
)

# The format before #4162, with no repository:
# ``<base_repo>:<base_tag>--<sanitized_branch>--<cache_hash>``. The hash follows
# ``--`` here and ``.`` in the current format, so no reference matches both.
_LEGACY_CACHE_REF_RE = re.compile(
    rf"^(?P<base_repo>{re.escape(_GHCR)}/[^:]+):(?P<base_tag>.+?)--(?P<branch>.+)"
    r"--(?P<hash>[0-9a-f]{8})$"
)


@dataclass(frozen=True)
class CacheImageRef:
    """A parsed cached image reference.

    *branch* and *repo* are as the tag holds them (sanitized). *repo* is None
    for an image in the format before #4162, which named no repository: no
    current lookup returns such an image, and the prune removes it.
    """

    base_repo: str
    base_tag: str
    branch: str
    repo: str | None
    cache_hash: str

    @property
    def legacy(self) -> bool:
        """True for an image in the format before #4162."""
        return self.repo is None


def parse_cache_image_ref(ref: str) -> CacheImageRef | None:
    """Return the parts of a cached image *ref*, else None.

    The inverse of :func:`cache_image_tag`, as far as it goes: the branch and
    repository come back sanitized (``feature/1-x`` and ``feature-1-x`` both read
    ``feature-1-x``). A reference in the format before #4162 parses with
    ``repo=None``. Anything not shaped exactly like a cached image (a base image,
    a foreign image, ``<none>``) returns None so it is never treated as ours.
    """
    match = _CACHE_REF_RE.match(ref)
    if match is not None:
        repo: str | None = match["repo"]
    else:
        match = _LEGACY_CACHE_REF_RE.match(ref)
        if match is None:
            return None
        repo = None
    return CacheImageRef(
        base_repo=match["base_repo"],
        base_tag=match["base_tag"],
        branch=match["branch"],
        repo=repo,
        cache_hash=match["hash"],
    )


def sanitize_branch(branch: str) -> str:
    """Return *branch* as it appears in a cached image tag."""
    return _sanitize_branch(branch)


def find_cached_image(
    base_image: str, branch: str, *, repo: str, runtime: str = ""
) -> tuple[str, str] | None:
    """Find *repo*'s existing cached image for *base_image* and *branch*.

    Returns ``(full_tag, hash_suffix)`` or ``None``. Only an image whose tag
    names *repo* matches: another repository's image on the same base and
    branch, or an image in the format before #4162, is never returned, so the
    cache build that removes a stale match only ever removes *repo*'s own.
    """
    rt = runtime or detect_runtime()
    base_tag = base_image.split(":")[-1] if ":" in base_image else "latest"
    base_repo = base_image.split(":")[0]
    pattern = re.compile(
        rf"^{re.escape(base_repo)}:{re.escape(base_tag)}--{re.escape(_sanitize_branch(branch))}"
        rf"--{re.escape(sanitize_repo(repo))}\.(?P<hash>[0-9a-f]{{8}})$"
    )

    result = subprocess.run(  # noqa: S603
        [rt, "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return None

    for line in result.stdout.splitlines():
        match = pattern.match(line.strip())
        if match is not None:
            return (match.group(0), match["hash"])
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


# Where the cache build installs vergil-tooling: uv's tool environments under a
# fixed directory, the ``vrg-*`` entry points in a directory on every image's
# ``PATH`` (spec §4.3, following ``core-tools.dockerfile``). The image publishes
# its managed core Python's directory in ``VERGIL_CORE_PYTHON_DIR`` (spec §4.4).
_TOOL_DIR = "/opt/vergil/uv/tools"
_TOOL_BIN_DIR = "/usr/local/bin"
_CORE_PYTHON_DIR_ENV = "VERGIL_CORE_PYTHON_DIR"


def vergil_tooling_setup_command(resolved: ResolvedRef) -> str:
    """Return the cache-build fragment installing vergil-tooling as *resolved*.

    Every ref is ``uv tool install`` of one commit on a uv-managed Python 3.15
    (:func:`~vergil_tooling.lib.tooling_install.install_lines`): a released ref
    constrained by that commit's ``uv.lock``, a dev ref unconstrained. A line
    pin arrives resolved, with its SHA; an exact pin arrives with its tag
    alone (:func:`~vergil_tooling.lib.tooling_install.unresolved`) and the
    fragment resolves its SHA with one ``git ls-remote`` inside the build.

    The install uses the image's managed Python under ``$VERGIL_CORE_PYTHON_DIR``
    with downloads off, so a wrong path fails rather than downloads; on an image
    that does not set it, uv may download 3.15 and the fragment logs that. The
    entry points land in ``/usr/local/bin``, and a check then confirms
    ``vrg-whoami`` resolves there and runs.

    :func:`_compose_setup` joins fragments with ``&&`` and sets no ``-e``, so
    the fragment runs as its own ``bash -euo pipefail -c``: any failure fails
    the build, and the ``UV_*`` variables it clears stay cleared only here.
    """
    lines = [
        *tooling_install.install_lines(
            resolved,
            python_dir_env=_CORE_PYTHON_DIR_ENV,
            tool_dir=_TOOL_DIR,
            bin_dir=_TOOL_BIN_DIR,
        ),
        *tooling_install.check_lines(_TOOL_BIN_DIR),
    ]
    return f"bash -euo pipefail -c {shlex.quote(chr(10).join(lines))}"


def tooling_label(resolved: ResolvedRef) -> str:
    """The name a cache records for *resolved*: its exact tag, or a dev ref itself."""
    return resolved.tag or resolved.ref


def _compose_setup(
    repo_root: Path,
    lang: str,
    *,
    build_env: Mapping[str, str],
    tooling: ResolvedRef | None,
) -> str:
    """Return the shell setup string baked into the cached image.

    *tooling* is the vergil-tooling release to install: the pin as
    :func:`current_cache_key` resolved it, the same release and commit the
    cache key names. Ignored for the self-repo; any other repo must name one.

    The composed step is, in order,
    ``<export env> && <apt fragment> && <vergil-tooling install> && <build-command> &&
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
    * **install** — the vergil-tooling install of *tooling*
      (:func:`vergil_tooling_setup_command`). Skipped for the self-repo
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

    if is_self_repo(repo_root):
        setup = tail or "true"
    else:
        if tooling is None:
            msg = "no vergil-tooling release to install in the cached image"
            raise ValueError(msg)
        install = vergil_tooling_setup_command(tooling)
        setup = f"{install} && {tail}" if tail else install

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
    tooling: ResolvedRef | None,
) -> str:
    """Build a cached image with vergil-tooling installed.

    ``build_env`` is exported for the whole setup step (see :func:`_compose_setup`).
    ``tooling`` is the vergil-tooling release installed (see there); its exact
    tag (:func:`tooling_label`) is recorded as the committed image's message,
    where :func:`cached_tooling` reads it back.

    ``quiet_warmup`` captures the warmup subprocess's output instead of
    streaming it to the inherited terminal, surfacing it only on failure. The
    finalize pipeline sets it: provisioning runs under a live progress display
    that owns stdout, and a raw-streamed multi-minute build (e.g. cpp compiling
    GoogleTest from source) reads as output "outside the script" over the
    progress tree (#2906). The interactive ``vrg-container-run`` path leaves it
    False so a cold rebuild still streams live and never looks hung.
    """
    rt = runtime or detect_runtime()
    self_repo = is_self_repo(repo_root)
    # The banner shows the plain install line, not rust's stale-lock wrapper.
    warmup = _warmup_line(lang, repo_root)
    warmup_missing = missing_warmup_files(lang, repo_root)

    from vergil_tooling.lib.config import (
        container_build_command,
        container_system_packages,
    )

    apt = apt_install_command(container_system_packages(repo_root), container_platform())
    build = container_build_command(repo_root)
    setup = _compose_setup(repo_root, lang, build_env=build_env, tooling=tooling)
    label = tooling_label(tooling) if tooling is not None else ""

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
    # A consumer repo's missing release was refused by _compose_setup above.
    if self_repo or tooling is None:
        print("  Install: skipped (self-repo uses local dev version)")
    else:
        detail = tooling_detail(vrg_install_tag(repo_root), tooling)
        print(f"  Install: vergil-tooling@{label}{detail}")
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
        if quiet_warmup:
            _report_warmup_skip(run_result.stdout or "")

        commit = [rt, "commit"]
        if not self_repo:
            commit += ["--message", _tooling_message(label)]
        subprocess.run(  # noqa: S603
            [*commit, container_id, target_tag],
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


# The committed image's message, recording the vergil-tooling release it holds.
_TOOLING_MESSAGE_PREFIX = "vergil-tooling "


def _tooling_message(label: str) -> str:
    return f"{_TOOLING_MESSAGE_PREFIX}{label}"


def tooling_detail(pin: str, resolved: ResolvedRef) -> str:
    """`` (pin v2.1, commit <sha>)``: what *resolved* adds to its label, or ``""``.

    The pin when it differs from the label (a line pin), and the commit when it
    is known here (a line pin; an exact pin's is resolved inside the build).
    """
    parts = [] if pin == tooling_label(resolved) else [f"pin {pin}"]
    if resolved.sha:
        parts.append(f"commit {resolved.sha}")
    return f" ({', '.join(parts)})" if parts else ""


def cached_tooling(image: str, *, runtime: str = "") -> str | None:
    """Return the vergil-tooling release *image* was built with, or None.

    Read from the image's commit message, which :func:`_build_cached_image`
    writes. None for an image with no such message: one built for the
    self-repo, or by a vergil-tooling release before #4550, or one that cannot
    be inspected.
    """
    rt = runtime or detect_runtime()
    result = subprocess.run(  # noqa: S603
        [rt, "image", "inspect", image, "--format", "{{.Comment}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    comment = result.stdout.strip()
    if result.returncode != 0 or not comment.startswith(_TOOLING_MESSAGE_PREFIX):
        return None
    return comment.removeprefix(_TOOLING_MESSAGE_PREFIX).strip() or None


@dataclass(frozen=True)
class CacheKey:
    """The key a branch's cached image must have now, and the tooling it names.

    *pin* is the ``[dependencies].vergil`` pin (or ``VRG_DOCKER_INSTALL_TAG``)
    and *tooling* the release it resolves to now, always the exact tag
    ``vX.Y.Z`` for a released pin (or a dev ref itself); both are ``""`` for
    the self-repo, which installs no vergil-tooling. *resolved* is the full
    resolution the build installs: for a line pin it carries the commit SHA,
    which the hash folds in beside the tag. ``None`` for the self-repo.
    """

    cache_hash: str
    pin: str
    tooling: str
    resolved: ResolvedRef | None = None


def _resolve_tooling(
    repo_root: Path, existing_tag: str | None, runtime: str
) -> tuple[str, ResolvedRef | None]:
    """Return ``(pin, resolved)`` for the cache of *repo_root*; see :class:`CacheKey`.

    A line pin is resolved once, with ``git ls-remote``, to its newest tag and
    commit. An exact pin is resolved with no network call
    (:func:`~vergil_tooling.lib.tooling_install.unresolved`): its key is the
    tag, and the build resolves its SHA. ``("", None)`` for the self-repo.

    A failure to resolve a line pin is never papered over with the release
    the existing cache holds: it raises :class:`ToolingResolveError`, naming
    the cached release when it is an exact tag as the one to pin. An exact
    pin's key is its tag alone and a line pin's also holds the commit, so an
    image built under the line pin is not reused: the exact pin's cache needs
    one build with the network, and is then found offline.
    """
    if is_self_repo(repo_root):
        return "", None
    pin = vrg_install_tag(repo_root)
    try:
        return pin, resolve_tooling_tag(pin)
    except ToolingResolveError as exc:
        held = cached_tooling(existing_tag, runtime=runtime) if existing_tag else None
        # Only an exact tag can be pinned; a dev ref the image was built with cannot.
        reusable = held if held and classify_ref(held) == "exact" else None
        remedy = (
            " To work without the network, pin an exact release (vX.Y.Z) with "
            "VRG_DOCKER_INSTALL_TAG: its cache needs one build with the network, "
            "then is found offline."
        )
        reuse = (
            f" This branch's cached image holds {reusable}, built under {pin}, so an "
            f"exact pin does not reuse it.{remedy} To stay on that release, set "
            f"VRG_DOCKER_INSTALL_TAG={reusable} once the network is back."
            if reusable
            else remedy
        )
        msg = (
            f"cannot resolve vergil-tooling {pin} to a release: {exc}. Refusing to use a "
            f"cached image that may hold an older release than {pin} now installs.{reuse}"
        )
        raise ToolingResolveError(msg) from exc


def current_cache_key(
    repo_root: Path,
    base_image: str,
    files: list[Path],
    *,
    build_env: Mapping[str, str],
    existing_tag: str | None,
    runtime: str = "",
) -> CacheKey:
    """Return the key *repo_root*'s cached image must have now.

    The one computation :func:`ensure_cached_image` and ``vrg-container-cache
    status`` share: the base image's digest (:func:`resolve_base_digest`, which
    pulls it), the vergil-tooling release the pin resolves to (one
    ``git ls-remote`` for a line pin, which adds its commit SHA; none for an
    exact pin or a dev ref; #4550), the cache-sensitive *files*,
    the repository salt and *build_env*. *existing_tag* is the branch's cached
    image, if any, named in the error when the release cannot be resolved.
    Raises ``RuntimeError`` (:class:`ToolingResolveError` for the release)
    when either lookup fails.
    """
    rt = runtime or detect_runtime()
    base_digest, _verified = resolve_base_digest(base_image, runtime=rt)
    pin, resolved = _resolve_tooling(repo_root, existing_tag, rt)
    tooling = tooling_label(resolved) if resolved is not None else ""
    # Only a line pin's resolution carries a SHA: an exact pin's key is its tag.
    line_sha = resolved.sha if resolved is not None and resolved.kind == "line" else None
    cache_hash = compute_cache_hash(
        files,
        base_digest=base_digest,
        salt=repo_root.name,
        env=build_env,
        tooling=tooling,
        tooling_sha=line_sha or "",
    )
    return CacheKey(cache_hash=cache_hash, pin=pin, tooling=tooling, resolved=resolved)


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

    The key (:func:`current_cache_key`) also names the vergil-tooling release
    the pin resolves to now, and the build installs exactly that release, so
    a new release on a pinned line rebuilds the cache (#4550).

    Before a build it runs the low-disk auto-prune
    (:func:`~vergil_tooling.lib.cache_space.ensure_build_space`), which raises
    :class:`~vergil_tooling.lib.cache_space.CacheSpaceError` when the build
    cannot have the room it needs.
    """
    rt = runtime or detect_runtime()
    files = cache_sensitive_files(repo_root, lang)
    if not files:
        return base_image

    from vergil_tooling.lib import git as _git

    branch = _git.current_branch()
    repo = cache_repo_name(repo_root)
    existing = find_cached_image(base_image, branch, repo=repo, runtime=rt)
    key = current_cache_key(
        repo_root,
        base_image,
        files,
        build_env=build_env,
        existing_tag=existing[0] if existing else None,
        runtime=rt,
    )
    current_hash = key.cache_hash

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
        # find_cached_image only returns this repository's own image, so this
        # never removes another repository's cache (#4162).
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

    # Make room first: below the free-space threshold, prune finished branches'
    # images, and stop here rather than fail part way through the build (#3571).
    # Imported here because cache_space depends on this module.
    from vergil_tooling.lib.cache_space import ensure_build_space

    ensure_build_space(rt)
    target_tag = cache_image_tag(base_image, branch, current_hash, repo=repo)
    return _build_cached_image(
        repo_root,
        lang,
        base_image,
        target_tag,
        runtime=rt,
        quiet_warmup=quiet_warmup,
        build_env=build_env,
        tooling=key.resolved,
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
