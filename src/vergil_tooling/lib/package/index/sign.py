"""Sign the repository metadata with the org signing subkey (spec §7.2, §7.4).

The key and its passphrase come only from the environment (the ``index-signing``
GitHub environment's ``PACKAGE_SIGNING_KEY`` / ``PACKAGE_SIGNING_PASSPHRASE``).
Neither ever appears on a command line: each is written to a ``0600`` temporary
file for ``gpg`` (``--pinentry-mode loopback --passphrase-file``) and deleted in a
``finally``, whatever the outcome.

- ``clearsign``: ``Release`` → ``InRelease``.
- ``detach``: ``Release`` → ``Release.gpg``, ``repomd.xml`` → ``repomd.xml.asc``
  (ASCII-armored detached signatures).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index.collect import _call

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vergil_tooling.lib.package.repo_setup import Run

KEY_ENV = "PACKAGE_SIGNING_KEY"
PASS_ENV = "PACKAGE_SIGNING_PASSPHRASE"  # noqa: S105 - an env var name, not a secret


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        msg = f"{name} is not set"
        raise PackageError(msg)
    return value


@contextlib.contextmanager
def _secret_file(content: str) -> Iterator[Path]:
    """A ``0600`` temporary file holding ``content``, deleted on exit."""
    fd, name = tempfile.mkstemp(prefix="vrg-sign-")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _gpg(run: Run, what: str, pass_env: str, *argv: str) -> None:
    with _secret_file(_env(pass_env)) as pw:
        _call(
            run,
            what,
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase-file",
            str(pw),
            *argv,
        )


def import_key(run: Run, key_env: str = KEY_ENV, pass_env: str = PASS_ENV) -> None:
    """Import the secret signing key held in ``$key_env`` into the gpg keyring."""
    key = _env(key_env)
    _env(pass_env)
    with _secret_file(key) as keyfile:
        _gpg(run, f"importing the signing key from {key_env}", pass_env, "--import", str(keyfile))


def _user(local_user: str | None) -> tuple[str, ...]:
    return ("--local-user", local_user) if local_user else ()


def clearsign(
    run: Run, src: Path, dst: Path, *, pass_env: str = PASS_ENV, local_user: str | None = None
) -> None:
    """Write an inline (clearsigned) signature of ``src`` to ``dst``."""
    _gpg(
        run,
        f"clearsigning {src}",
        pass_env,
        *_user(local_user),
        "--digest-algo",
        "SHA256",
        "--output",
        str(dst),
        "--clearsign",
        str(src),
    )


def detach(
    run: Run, src: Path, dst: Path, *, pass_env: str = PASS_ENV, local_user: str | None = None
) -> None:
    """Write an ASCII-armored detached signature of ``src`` to ``dst``."""
    _gpg(
        run,
        f"signing {src}",
        pass_env,
        *_user(local_user),
        "--digest-algo",
        "SHA256",
        "--output",
        str(dst),
        "--detach-sign",
        "--armor",
        str(src),
    )
