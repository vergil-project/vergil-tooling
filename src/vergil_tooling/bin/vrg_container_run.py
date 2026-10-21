"""Run an arbitrary command inside a dev container.

Auto-detects the project language to select a default container image.
Falls back to dev-base:latest when no language is detected and
DOCKER_DEV_IMAGE is not set.  The command to run is taken from CLI
arguments after ``--``.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import conan_token, git, platform_env
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import (
    DEFAULT_VALIDATION_COMMAND,
    TEST_CONTEXT_ENV,
    TEST_WORKERS_ENV,
    container_env_prefixes,
    primary_ci_version,
    validation_container_command,
)
from vergil_tooling.lib.container import (
    assert_runtime_available,
    build_container_args,
    default_image,
    detect_runtime,
    resolve_language,
)
from vergil_tooling.lib.container_cache import ensure_cached_image

if TYPE_CHECKING:
    from pathlib import Path

_VALID_PREFIXES = {"dev", "prod"}

# When the in-container command is the validation entry point, rewrite it using
# the target repo's [validation].container-command override (issue #1433). This
# lets the fleet-wide `vrg-container-run -- vrg-validate` keep working in repos
# that need a different activation (e.g. the self-repo's "uv run vrg-validate"),
# regardless of which repo the invoking agent's session started in.
_VALIDATION_ENTRY = DEFAULT_VALIDATION_COMMAND

_USAGE = """\
usage: vrg-container-run [--prefix <dev|prod>] [--] <command> [args...]

Run a command inside the project's dev container.

The project language is auto-detected to select the right container image;
falls back to dev-base:latest when detection fails.

options:
  -h, --help              show this help message and exit
  --prefix <dev|prod>     image prefix (default: prod)

environment variables:
  DOCKER_DEV_IMAGE        override the auto-detected container image
  DOCKER_NETWORK          join a Docker network (e.g. for integration tests)
  VRG_DOCKER_INSTALL_TAG   override the vergil-tooling version tag from vergil.toml
  PYTHONPYCACHEPREFIX     container bytecode cache root (default: /tmp/pycache,
                          keeping __pycache__ off the bind-mounted repo)
  CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER
                          cpp repos only: the ConanCenter audit token. When
                          unset, it is read from ~/.config/vergil/conan.env,
                          then from conan_audit_token_path in
                          ~/.config/vergil/identities.toml, and forwarded
  VRG_TEST_WORKERS        override the test worker count ("auto" or N) on this
                          machine; forwarded into the container
  VRG_TEST_CONTEXT        override the validation context that selects a
                          [test.<context>] worker count (default: ci on GitHub
                          Actions, else the vrg-whoami --platform token)

examples:
  vrg-container-run -- uv run vrg-validate
  vrg-container-run --prefix dev -- vrg-validate
  vrg-container-run -- uv run pytest tests/
  DOCKER_DEV_IMAGE=custom:img vrg-container-run -- make build
"""


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # Split on -- separator
    if "--" in args:
        idx = args.index("--")
        pre_separator = args[:idx]
        command = args[idx + 1 :]
    else:
        pre_separator = args
        command = args

    if {"-h", "--help"} & set(pre_separator):
        print(_USAGE, end="")
        return 0

    prefix = "prod"
    if "--prefix" in pre_separator:
        pi = pre_separator.index("--prefix")
        if pi + 1 >= len(pre_separator):
            print("error: --prefix requires a value (dev or prod)", file=sys.stderr)
            return 1
        prefix = pre_separator[pi + 1]
        if prefix not in _VALID_PREFIXES:
            allowed = ", ".join(sorted(_VALID_PREFIXES))
            print(f"error: invalid prefix '{prefix}' (allowed: {allowed})", file=sys.stderr)
            return 1

    if not command:
        print("error: no command specified", file=sys.stderr)
        return 1

    runtime = detect_runtime()
    repo_root = git.repo_root()
    lang = resolve_language(repo_root)
    command = _apply_validation_override(command, repo_root)

    # Resolve before any image work, so a broken token file fails fast.
    try:
        conan_status, token_names = _inject_conan_audit_token(lang)
    except conan_token.ConanTokenError as exc:
        print(f"error: Conan audit token: {exc}", file=sys.stderr)
        return 1

    env_image = os.environ.get("DOCKER_DEV_IMAGE")
    if env_image:
        image = env_image
        image_source = "env"
    else:
        # The repo's declared [ci].versions is authoritative for the container
        # version — never a hardcoded tooling-side default (issue #2468). Fall
        # back to the built-in default only when the repo declares none, and say
        # so loudly: a silent fallback is exactly how a default bump once flipped
        # every repo's local build to a different interpreter.
        declared_version = primary_ci_version(repo_root)
        if declared_version is None and lang:
            print(
                f"WARNING: vergil.toml declares no [ci].versions; using the "
                f"built-in default container for '{lang}'. Declare [ci].versions "
                f"to pin the container version and keep local builds aligned with CI.",
                file=sys.stderr,
            )
        base = default_image(lang, fallback=True, prefix=prefix, version=declared_version)
        image = ensure_cached_image(repo_root, lang, base, runtime=runtime)
        image_source = "cached" if image != base else "default"

    print(f"Language: {lang or '<none>'}")
    if image_source == "cached":
        print(f"Image:    {image} (cached)")
    else:
        print(f"Image:    {image}")
    print(f"Command:  {' '.join(command)}")
    if conan_status:
        print(f"Conan:    {conan_status}")
    network = os.environ.get("DOCKER_NETWORK", "")
    if network:
        print(f"Network:  {network}")
    print("---")

    assert_runtime_available(runtime)

    env_prefixes = container_env_prefixes(repo_root)
    pull_policy = "never" if image_source == "cached" else "always"
    container_args = build_container_args(
        repo_root,
        image,
        command,
        runtime=runtime,
        pull_policy=pull_policy,
        env_prefixes=env_prefixes,
        env_names=token_names,
        extra_env=_test_tuning_env(),
    )
    if runtime == "nerdctl":
        os.execvp("nerdctl", container_args)  # noqa: S606, S607
    else:
        os.execvp("docker", container_args)  # noqa: S606, S607
    return 0  # pragma: no cover


def _inject_conan_audit_token(lang: str) -> tuple[str, list[str]]:
    """Resolve the ConanCenter audit token for a cpp repo and stage it for the run.

    Returns a printable status line (``""`` for non-cpp repos, which never
    consume the token and so never read its files) and the env names to forward.
    The token is resolved from its canonical file rather than trusted to the
    ambient environment, which a zsh or non-interactive shell never populated
    (#2902); see ``lib/conan_token`` for the precedence. The value is placed in
    this process's environment, which ``execvp`` hands to the container runtime,
    and forwarded by name only, so it never appears in the runtime's argv.
    Raises :class:`conan_token.ConanTokenError` for a broken token source.
    """
    if lang != "cpp":
        return "", []
    resolved = conan_token.resolve_conan_audit_token(os.environ)
    if resolved is None:
        return "no audit token found (vrg-validate will skip `conan audit scan`)", []
    os.environ[conan_token.CONAN_AUDIT_TOKEN_ENV] = resolved.value
    return (
        f"audit token from {resolved.source}",
        [conan_token.CONAN_AUDIT_TOKEN_ENV],
    )


def _test_context() -> str:
    """Resolve the validation context the test worker count is tuned for.

    An explicit ``VRG_TEST_CONTEXT`` passes through untouched (vrg-validate
    validates it). Otherwise a GitHub Actions runner is ``ci`` and anything else
    is the ``vrg-whoami --platform`` token. This runs outside the container,
    where the platform is observable; inside it the signals are not.
    """
    explicit = os.environ.get(TEST_CONTEXT_ENV, "").strip()
    if explicit:
        return explicit
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "ci"
    return platform_env.current_platform().value


def _test_tuning_env() -> dict[str, str]:
    """Env that tells in-container vrg-validate how to size the test run (#3322)."""
    env = {TEST_CONTEXT_ENV: _test_context()}
    workers = os.environ.get(TEST_WORKERS_ENV, "").strip()
    if workers:
        env[TEST_WORKERS_ENV] = workers
    return env


def _apply_validation_override(command: list[str], repo_root: Path) -> list[str]:
    """Rewrite a ``vrg-validate`` invocation per the repo's override.

    Only the validation entry point is rewritten; any other command passes
    through unchanged. Trailing arguments (e.g. ``--check common``) are
    preserved. The default override is ``vrg-validate`` itself, so repos
    without a ``[validation]`` section are left untouched.
    """
    if not command or command[0] != _VALIDATION_ENTRY:
        return command
    override = validation_container_command(repo_root)
    return shlex.split(override) + command[1:]


if __name__ == "__main__":
    sys.exit(main())
