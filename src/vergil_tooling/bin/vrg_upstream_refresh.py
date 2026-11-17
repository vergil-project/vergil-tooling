"""Refresh a repo's upstream inputs with its ``[upstream-refresh].hook``, by a self-merging PR.

Language-neutral (epic vergil-project/.github#472, spec §5.6): the repo's hook
refreshes its upstream inputs by editing tracked files. Since epic
vergil-project/.github#1168 the command runs on the mechanized-PR building
block, in two halves split by the key boundary (that epic's spec §6): the
automation App's private key never shares a runner with the hook.

- ``prepare --out DIR`` runs the hook in a fresh clone of ``develop`` with no
  credentials and writes a patch and its notes to *DIR*. It refuses to run
  when the App's credentials are in its environment.
- ``publish --from DIR`` reads that output as data. Under GitHub Actions it
  needs the App's credentials (``$VRG_AUTOMATION_CLIENT_ID``,
  ``$VRG_AUTOMATION_PRIVATE_KEY``) and ``$VRG_AUTOMATION_APP``: it mints
  repo-scoped tokens in-process, renews them while it waits and revokes them
  on exit, including on SIGINT or SIGTERM. It applies the patch on the
  recorded ``develop`` commit in its own clone, checks it against the repo's
  path fence (``[upstream-refresh].paths``, read from the ``develop`` tip),
  commits as the App's bot user, opens the PR, and waits until it merges.
- With no mode, both halves run in one process. That is for a human's local
  run under the human's own credentials, and is refused under Actions.
- ``--dry-run`` runs prepare, applies the patch in a second clone as publish
  does, runs the fence, and prints the PR body and every violation. It pushes
  nothing; it is the one mode the agent identities may run.

The command never touches the caller's checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import app_token, automation_identity, github, identity_mode
from vergil_tooling.lib.automation_identity import AutomationIdentityError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.develop_clone import DevelopCloneError
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.mechanized_pr import MechanizedPRError, PRNotMergedError
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.upstream_refresh import (
    PUBLISH_DEADLINE_SECS,
    UpstreamRefreshError,
    bot_identity,
    prepare,
    publish,
    read_prepared,
    read_rules_file,
    render_body,
    staged_clone,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import FrameType

    from vergil_tooling.lib.mechanized_pr import SyncResult

_PROG = "vrg-upstream-refresh"

#: The agent App mode's settings; publish refuses to mix its minting path with them.
_APP_MODE_ENV = ("VRG_APP_ID", "VRG_PRIVATE_KEY_PATH")
_AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})
_SIGNALS = (signal.SIGINT, signal.SIGTERM)

_EXIT_CODES = """\
exit codes:
  0  merged, or nothing to change (a dry run: no fence violation)
  1  any other failure or refusal, with the reason: the hook, the clone, the
     path fence, a refused identity, a failed mint or revocation, a usage error
  2  the PR was opened but did not merge; the PR and the reason are named
  130, 143  interrupted by SIGINT or SIGTERM (publish revokes its token first)
"""


class _RefusedError(Exception):
    """A refusal this command decides itself; the message says why."""


@cli_main
def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    # Before anything else, so no child process ever starts with the key.
    try:
        creds = app_token.read_credentials_from_env()
    except app_token.AppTokenError as exc:
        return _fail(str(exc))
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        return 1  # a usage error; argparse has printed why
    _line_buffer_stdout()
    try:
        return _run(args, creds, started)
    except PRNotMergedError as exc:
        print(f"{_PROG}: the refresh PR {exc.pr_url} did not merge: {exc.reason}", file=sys.stderr)
        return 2
    except (
        _RefusedError,
        UpstreamRefreshError,
        ConfigError,
        DevelopCloneError,
        MechanizedPRError,
        app_token.AppTokenError,
        AutomationIdentityError,
        github.TokenSourceConflictError,
    ) as exc:
        return _fail(str(exc))
    except subprocess.CalledProcessError as exc:
        return _fail(f"command failed: {error_text(exc)}")


def _line_buffer_stdout() -> None:
    """Write each stdout line as it is printed, not when the buffer fills (#4596).

    Under Actions stdout is a pipe, so Python block-buffers it: the PR
    poller's lines would reach the log only when the step ends, stamped with
    its end time, below token events that are flushed as they happen. Line
    buffering keeps the log in the real order with the real times. A stdout
    that is not a text stream (a caller's own replacement) is left as it is.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)


def _fail(message: str) -> int:
    print(f"{_PROG}: {message}", file=sys.stderr)
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Run the repo's [upstream-refresh].hook in a fresh clone of develop, check the "
            "patch against the repo's path fence, and open a PR that merges itself once CI "
            "passes. Never touches the checkout. With no mode, prepare and publish run in one "
            "process (a human's local run; refused under GitHub Actions)."
        ),
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    repo_help = "Target repository (default: $GITHUB_REPOSITORY, then the origin remote)."
    parser.add_argument("--repo", metavar="OWNER/NAME", help=repo_help)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the hook, apply its patch in a second clone and check the path fence; print "
            "the PR body and every violation. Pushes nothing; exits 1 on any violation."
        ),
    )
    parser.add_argument(
        "--rules-from",
        metavar="PATH",
        help=(
            "With --dry-run only: read the fence rules from this vergil.toml instead of the "
            "develop tip, so a branch can test its new [upstream-refresh].paths."
        ),
    )
    modes = parser.add_subparsers(dest="mode", metavar="{prepare,publish}")
    prep = modes.add_parser(
        "prepare",
        help="Run the hook with no credentials; write the patch and notes to --out.",
        description=(
            "Run the hook in a fresh clone of develop with no credentials, and write the "
            "patch, the develop commit, the changed paths and the notes to --out. Refused "
            "when the App's credentials are in the environment."
        ),
    )
    prep.add_argument("--out", required=True, metavar="DIR", help="An empty output directory.")
    prep.add_argument("--repo", metavar="OWNER/NAME", default=argparse.SUPPRESS, help=repo_help)
    pub = modes.add_parser(
        "publish",
        help="Apply prepare's output, check the fence, open the PR and wait until it merges.",
        description=(
            "Read prepare's output from --from as data, apply the patch on the recorded "
            "develop commit in a fresh clone, check the path fence, and open the PR and wait "
            "until it merges. Under GitHub Actions it needs $VRG_AUTOMATION_CLIENT_ID, "
            "$VRG_AUTOMATION_PRIVATE_KEY and $VRG_AUTOMATION_APP."
        ),
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pub.add_argument(
        "--from", dest="from_dir", required=True, metavar="DIR", help="prepare's output directory."
    )
    pub.add_argument("--repo", metavar="OWNER/NAME", default=argparse.SUPPRESS, help=repo_help)
    args = parser.parse_args(argv)
    if args.mode is not None and args.dry_run:
        parser.error(f"--dry-run runs the combined mode; it does not combine with {args.mode}")
    if args.rules_from is not None and not args.dry_run:
        parser.error("--rules-from is accepted only with --dry-run")
    return args


def _run(args: argparse.Namespace, creds: app_token.AppCredentials | None, started: float) -> int:
    if args.dry_run:
        return _dry_run(args, creds)
    if args.mode == "prepare":
        return _prepare(args, creds)
    if args.mode == "publish":
        return _publish(args, creds, started)
    return _combined(args, creds)


def _resolve_repo(arg: str | None) -> str:
    return arg or os.environ.get("GITHUB_REPOSITORY") or github.current_repo()


def _in_actions() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS"))


def _refuse_creds(creds: app_token.AppCredentials | None, what: str) -> None:
    if creds is not None:
        msg = (
            f"{what} runs the hook, so it must never hold the App's credentials "
            f"(${app_token.CLIENT_ID_ENV}, ${app_token.PRIVATE_KEY_ENV}): the key never shares "
            "a runner with the hook (the key boundary)"
        )
        raise _RefusedError(msg)


def _require_mode(allowed: frozenset[IdentityMode], what: str) -> IdentityMode:
    """The resolved identity mode; refuse the agent modes and any mode not in *allowed*."""
    mode = identity_mode.current_mode()
    if mode in _AGENT_MODES:
        msg = (
            f"{_PROG} opens PRs and is not run by agents (identity mode {mode.value!r}); "
            "only --dry-run is open to them"
        )
        raise _RefusedError(msg)
    if mode not in allowed:
        names = " or ".join(sorted(m.value for m in allowed))
        msg = f"{what} runs only as {names}, and the identity mode is {mode.value!r}"
        raise _RefusedError(msg)
    return mode


def _prepare(args: argparse.Namespace, creds: app_token.AppCredentials | None) -> int:
    _refuse_creds(creds, "prepare")
    _require_mode(frozenset({IdentityMode.HUMAN, IdentityMode.AUTOMATION}), "prepare")
    repo = _resolve_repo(args.repo)
    out = Path(args.out)
    prepared = prepare(repo, out)
    if prepared.paths:
        print(
            f"{repo}: prepared {len(prepared.paths)} changed path(s) on develop "
            f"{prepared.base_sha} in {out}"
        )
    else:
        print(f"{repo}: nothing to refresh on develop {prepared.base_sha}; wrote {out}")
    return 0


def _combined(args: argparse.Namespace, creds: app_token.AppCredentials | None) -> int:
    if _in_actions():
        msg = (
            "the combined mode (prepare and publish in one process) is for a human's local "
            "run and is refused under GitHub Actions; run prepare and publish as separate jobs"
        )
        raise _RefusedError(msg)
    _refuse_creds(creds, "the combined mode")
    _require_mode(frozenset({IdentityMode.HUMAN}), "the combined mode")
    repo = _resolve_repo(args.repo)
    with tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-out-") as out:
        prepare(repo, Path(out))
        result = publish(repo, Path(out), deadline_secs=None, bot=None)
    _report(repo, result)
    return 0


def _publish(
    args: argparse.Namespace, creds: app_token.AppCredentials | None, started: float
) -> int:
    in_dir = Path(args.from_dir)
    if creds is None:
        if _in_actions():
            msg = (
                "publish under GitHub Actions needs the automation App's credentials "
                f"(${app_token.CLIENT_ID_ENV} and ${app_token.PRIVATE_KEY_ENV}) and "
                f"${automation_identity.AUTOMATION_ENV}; none were given, so nothing was done"
            )
            raise _RefusedError(msg)
        _require_mode(frozenset({IdentityMode.HUMAN}), "publish without the App's credentials")
        repo = _resolve_repo(args.repo)
        result = publish(repo, in_dir, deadline_secs=None, bot=None)
        _report(repo, result)
        return 0
    configured = [name for name in _APP_MODE_ENV if name in os.environ]
    if configured:
        msg = (
            f"publish holds the automation App's credentials, but the agent App mode is "
            f"configured ({', '.join(configured)} set); the two minting paths never mix"
        )
        raise _RefusedError(msg)
    slug = automation_identity.env_slug()
    if slug is None:
        msg = (
            f"publish was given the App's credentials but ${automation_identity.AUTOMATION_ENV} "
            "is not set, so the token cannot be checked against the automation App"
        )
        raise _RefusedError(msg)
    repo = _resolve_repo(args.repo)
    source = app_token.AppTokenSource(
        creds, repo, app_token.UPSTREAM_REFRESH_PERMISSIONS, expected_slug=slug
    )
    # The log handler is outermost, so the revocation on exit is still shown.
    with _token_events_to_stdout(), _token_source_lifecycle(source):
        # Resolving the identity verifies the source's first token, so it mints here.
        _require_mode(frozenset({IdentityMode.AUTOMATION}), "publish with the App's credentials")
        bot = bot_identity(slug)
        result = publish(
            repo, in_dir, deadline_secs=PUBLISH_DEADLINE_SECS, bot=bot, started=started
        )
    _report(repo, result)
    return 0


@contextlib.contextmanager
def _token_source_lifecycle(source: app_token.AppTokenSource) -> Generator[None]:
    """Install the signal handlers, then register *source*; revoke its token on every exit.

    The handlers are in place before anything can mint a token. SIGINT or
    SIGTERM closes the source (revoking the current token) and exits 130 or
    143. A revocation that fails on the way out fails the run.
    """

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        code = 128 + signum
        name = signal.Signals(signum).name
        print(f"{_PROG}: {name} received; revoking the token and exiting {code}", file=sys.stderr)
        try:
            source.close()
        except app_token.AppTokenError as exc:
            print(f"{_PROG}: {exc}", file=sys.stderr)
        raise SystemExit(code)

    previous = {sig: signal.signal(sig, on_signal) for sig in _SIGNALS}
    try:
        with source, github.token_source(source):
            yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class _StdoutHandler(logging.Handler):
    """Print each record to the current ``sys.stdout``, looked up at emit time."""

    def emit(self, record: logging.LogRecord) -> None:
        print(self.format(record), flush=True)


@contextlib.contextmanager
def _token_events_to_stdout() -> Generator[None]:
    """Show the token source's log (each mint, renewal and revocation, with its time)."""
    logger = logging.getLogger(app_token.__name__)
    handler = _StdoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _report(repo: str, result: SyncResult) -> None:
    if result.status == "merged":
        print(f"{repo}: the refresh PR {result.pr_url} merged")
    else:
        print(f"{repo}: in sync; nothing to refresh on develop")


def _dry_run(args: argparse.Namespace, creds: app_token.AppCredentials | None) -> int:
    _refuse_creds(creds, "--dry-run")
    rules = read_rules_file(Path(args.rules_from)) if args.rules_from is not None else None
    repo = _resolve_repo(args.repo)
    with tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-out-") as out:
        prepare(repo, Path(out))
        prepared, patch = read_prepared(Path(out))
        with staged_clone(repo, prepared, patch, rules=rules) as staged:
            problems = staged.problems
    source = f"--rules-from {args.rules_from}" if rules is not None else "the develop tip"
    if not prepared.paths:
        print("Dry run: nothing to refresh on develop.")
        return 0
    print(f"Dry run: would open the refresh PR on {repo} (develop):\n")
    print(render_body(prepared.notes, prepared.paths))
    if problems:
        print(f"Path fence (rules from {source}): {len(problems)} violation(s); nothing pushed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Path fence (rules from {source}): no violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
