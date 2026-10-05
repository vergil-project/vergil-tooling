"""Tests for vergil_tooling.lib.package.index.sign (spec §7.2, §7.4)."""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import sign
from vergil_tooling.lib.package.repo_setup import local_run

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Recorder:
    """A ``Run`` that records argv plus the content and mode of each secret file."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.secrets: list[tuple[str, int]] = []
        self.paths: list[Path] = []
        self.fail = fail

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        for flag in ("--passphrase-file", "--import"):
            if flag in argv:
                p = Path(argv[argv.index(flag) + 1])
                self.paths.append(p)
                self.secrets.append((p.read_text(), stat.S_IMODE(p.stat().st_mode)))
        if self.fail:
            raise subprocess.CalledProcessError(2, argv, "", "bad passphrase")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_SIGNING_KEY", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nk\n")
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")


def test_import_key_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PACKAGE_SIGNING_KEY", raising=False)
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")
    run = MagicMock()
    with pytest.raises(PackageError, match=r"PACKAGE_SIGNING_KEY is not set"):
        sign.import_key(run, "PACKAGE_SIGNING_KEY", "PACKAGE_SIGNING_PASSPHRASE")
    run.assert_not_called()


def test_import_key_requires_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_SIGNING_KEY", "k")
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "")
    run = MagicMock()
    with pytest.raises(PackageError, match=r"PACKAGE_SIGNING_PASSPHRASE is not set"):
        sign.import_key(run)
    run.assert_not_called()


@pytest.mark.usefixtures("env")
def test_import_key_pipes_key_and_passphrase_through_0600_files_then_deletes_them() -> None:
    run = _Recorder()
    sign.import_key(run, "PACKAGE_SIGNING_KEY", "PACKAGE_SIGNING_PASSPHRASE")
    (argv,) = run.calls
    assert argv[:6] == [
        "gpg",
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--passphrase-file",
    ]
    assert argv[7] == "--import"
    assert sorted(run.secrets) == sorted(
        [("pw", 0o600), ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nk\n", 0o600)]
    )
    assert "pw" not in argv and not any("PRIVATE KEY" in a for a in argv)
    assert not any(p.exists() for p in run.paths)


@pytest.mark.usefixtures("env")
def test_import_key_failure_is_fatal_and_still_deletes_secrets() -> None:
    run = _Recorder(fail=True)
    with pytest.raises(
        PackageError,
        match=r"importing the signing key from PACKAGE_SIGNING_KEY failed: bad passphrase",
    ):
        sign.import_key(run)
    assert len(run.paths) == 2
    assert not any(p.exists() for p in run.paths)


def test_clearsign_and_detach_use_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    sign.clearsign(run, tmp_path / "Release", tmp_path / "InRelease")
    sign.detach(run, tmp_path / "repomd.xml", tmp_path / "repomd.xml.asc")
    flat = [" ".join(c.args) for c in run.call_args_list]
    assert all("--pinentry-mode loopback" in c and "--passphrase-file" in c for c in flat)
    assert f"--output {tmp_path / 'InRelease'} --clearsign {tmp_path / 'Release'}" in flat[0]
    assert (
        f"--output {tmp_path / 'repomd.xml.asc'} --detach-sign --armor {tmp_path / 'repomd.xml'}"
        in flat[1]
    )
    assert all("--digest-algo SHA256" in c for c in flat)
    assert not any("--local-user" in c for c in flat)


def test_local_user_selects_the_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")
    run = _Recorder()
    sign.clearsign(run, tmp_path / "R", tmp_path / "I", local_user="ABCD")
    sign.detach(run, tmp_path / "R", tmp_path / "R.gpg", local_user="ABCD")
    assert all("--local-user ABCD" in " ".join(c) for c in run.calls)
    assert run.secrets == [("pw", 0o600), ("pw", 0o600)]
    assert not any(p.exists() for p in run.paths)


def test_signing_without_passphrase_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PACKAGE_SIGNING_PASSPHRASE", raising=False)
    with pytest.raises(PackageError, match=r"PACKAGE_SIGNING_PASSPHRASE is not set"):
        sign.detach(MagicMock(), tmp_path / "R", tmp_path / "R.gpg")


def test_signing_failure_is_fatal_and_deletes_the_passphrase_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")
    run = _Recorder(fail=True)
    with pytest.raises(PackageError, match=r"clearsigning .*Release failed: bad passphrase"):
        sign.clearsign(run, tmp_path / "Release", tmp_path / "InRelease")
    with pytest.raises(PackageError, match=r"signing .*repomd\.xml failed: bad passphrase"):
        sign.detach(run, tmp_path / "repomd.xml", tmp_path / "repomd.xml.asc")
    assert not any(p.exists() for p in run.paths)


# --- real gpg ------------------------------------------------------------------


@pytest.fixture
def gnupghome(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short path: gpg-agent's socket path must fit in sun_path (~108 bytes).
    home = Path(tempfile.mkdtemp(prefix="g", dir="/tmp"))  # noqa: S108
    home.chmod(0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    try:
        yield home
    finally:
        subprocess.run(["gpgconf", "--kill", "all"], check=False, capture_output=True)
        shutil.rmtree(home, ignore_errors=True)


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")
def test_real_gpg_round_trip(
    tmp_path: Path, gnupghome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact flags work with gpg: import a protected key, sign, verify."""
    gen = gnupghome / "gen"
    gen.mkdir(mode=0o700)
    base = ["gpg", "--homedir", str(gen), "--batch", "--pinentry-mode", "loopback"]
    uid = "Test <t@example.invalid>"
    gen_key = [*base, "--passphrase", "pw", "--quick-gen-key", uid, "ed25519", "sign", "never"]
    subprocess.run(gen_key, check=True, capture_output=True)
    secret = subprocess.run(
        [*base, "--passphrase", "pw", "--armor", "--export-secret-keys"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["gpgconf", "--homedir", str(gen), "--kill", "all"], check=False)
    monkeypatch.setenv("PACKAGE_SIGNING_KEY", secret)
    monkeypatch.setenv("PACKAGE_SIGNING_PASSPHRASE", "pw")

    sign.import_key(local_run)
    release = tmp_path / "Release"
    release.write_text("Suite: noble\n")
    sign.clearsign(local_run, release, tmp_path / "InRelease")
    sign.detach(local_run, release, tmp_path / "Release.gpg")

    assert "BEGIN PGP SIGNED MESSAGE" in (tmp_path / "InRelease").read_text()
    assert "BEGIN PGP SIGNATURE" in (tmp_path / "Release.gpg").read_text()
    subprocess.run(["gpg", "--batch", "--verify", str(tmp_path / "InRelease")], check=True)
    subprocess.run(
        ["gpg", "--batch", "--verify", str(tmp_path / "Release.gpg"), str(release)], check=True
    )
