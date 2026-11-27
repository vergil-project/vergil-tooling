"""Tests for the vrg-upstream-refresh CLI (epic vergil-project/.github#472, T6; #1168, Task 3b).

The CLI's modes: ``prepare --out``, ``publish --from``, the combined default
(a human's local run) and ``--dry-run``. The halves themselves are tested in
test_upstream_refresh; here ``publish`` is mostly a stub, and the token source
a stand-in, so each test sees what the CLI decides and in what order.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_upstream_refresh as cli
from vergil_tooling.lib import (
    app_token,
    develop_clone,
    git,
    github,
    identity_mode,
    upstream_refresh,
)
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.mechanized_pr import PRNotMergedError, SyncResult
from vergil_tooling.lib.upstream_refresh import PUBLISH_DEADLINE_SECS, FenceRefusedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""

_HOOK_TOML = _TOML + '\n[upstream-refresh]\nhook = "echo new > data.txt"\npaths = ["data.txt"]\n'
_SLUG = "acme-automation"
_TOKEN = "ghs_not_a_real_token_0123456789"
_MERGED = SyncResult(status="merged", pr_url="https://github.com/o/r/pull/9", closed=[])
_IN_SYNC = SyncResult(status="in_sync", pr_url=None, closed=[])
_HELD = SyncResult(
    status="held",
    pr_url="https://github.com/o/r/pull/9",
    closed=[],
    detail="held for release 2.1.400",
)

_ENV = (
    "GITHUB_ACTIONS",
    "GITHUB_REPOSITORY",
    "VRG_IDENTITY_MODE",
    "VRG_APP_ID",
    "VRG_PRIVATE_KEY_PATH",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    app_token.CLIENT_ID_ENV,
    app_token.PRIVATE_KEY_ENV,
    "VRG_AUTOMATION_APP",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare origin on ``develop`` that ``clone_develop`` clones; the CLI runs from a checkout."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(github, "repo_installation_token", lambda _repo: None)
    monkeypatch.setattr(github, "get_installation_token", lambda *_a, **_k: None)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(bare))
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: bare.as_uri())
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _git(work, "checkout", "-q", "-b", "develop")
    monkeypatch.chdir(work)
    return bare


def _seed(origin: Path, toml: str) -> Path:
    work = origin.parent / "work"
    (work / "vergil.toml").write_text(toml)
    (work / "data.txt").write_text("old\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "develop")
    return work


def _never(what: str) -> Callable[..., object]:
    def fail(*_a: object, **_k: object) -> object:
        pytest.fail(f"{what} must not run")

    return fail


def _as_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.HUMAN)


def _with_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(app_token.CLIENT_ID_ENV, "Iv1.client")
    monkeypatch.setenv(app_token.PRIVATE_KEY_ENV, "-----BEGIN PRIVATE KEY-----\nx\n")


class _FakeSource:
    """Stands in for ``AppTokenSource``: mints on first use, records close."""

    instances: list[_FakeSource] = []  # noqa: RUF012 - per-test, reset by the fixture

    def __init__(
        self,
        creds: app_token.AppCredentials,
        repo: str,
        permissions: object,
        *,
        expected_slug: str,
    ) -> None:
        self.creds = creds
        self.repo = repo
        self.permissions = permissions
        self.expected_slug = expected_slug
        self.minted = 0
        self.closed = False
        _FakeSource.instances.append(self)

    def __enter__(self) -> _FakeSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def token(self) -> str:
        assert not self.closed, "token() after close()"
        if not self.minted:
            self.minted = 1
            logging.getLogger(app_token.__name__).info("T0: minted the automation token")
        return _TOKEN

    def close(self) -> None:
        if not self.closed and self.minted:
            logging.getLogger(app_token.__name__).info("T1: revoked the automation token on exit")
        self.closed = True


@pytest.fixture
def automation(monkeypatch: pytest.MonkeyPatch) -> list[_FakeSource]:
    """An Actions publish run: credentials, ``VRG_AUTOMATION_APP``, a stand-in source.

    GraphQL ``viewer`` answers ``<slug>[bot]`` only for the source's own token,
    so the mode resolves to automation only through the registered source.
    """
    _FakeSource.instances = []
    _with_creds(monkeypatch)
    monkeypatch.setenv("VRG_AUTOMATION_APP", _SLUG)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cli.app_token, "AppTokenSource", _FakeSource)

    def graphql(_query: str, **_v: object) -> dict[str, object]:
        login = f"{_SLUG}[bot]" if github.registered_token() == _TOKEN else "someone"
        return {"viewer": {"login": login}}

    monkeypatch.setattr(github, "graphql", graphql)
    monkeypatch.setattr(cli, "bot_identity", lambda slug: (f"{slug}[bot]", f"1+{slug}[bot]@x"))
    return _FakeSource.instances


# -- usage and exit codes -------------------------------------------------------


def test_cli_base_flag_removed_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--base", "develop"]) == 1
    assert "vrg-upstream-refresh: error:" in capsys.readouterr().err
    assert cli.main(["--base=develop"]) == 1


def test_help_states_the_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "exit codes:" in out
    assert "2  the PR was opened but did not merge" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["prepare", "--out", "d", "--dry-run"],
        ["--dry-run", "prepare", "--out", "d"],
        ["--dry-run", "publish", "--from", "d"],
        ["publish", "--from", "d", "--rules-from", "v.toml"],
        ["--rules-from", "v.toml"],
        ["prepare"],
        ["publish"],
    ],
)
def test_usage_errors_exit_1(argv: list[str]) -> None:
    assert cli.main(argv) == 1


def _publish_raising(exc: BaseException) -> Callable[..., SyncResult]:
    def publish(*_a: object, **_k: object) -> SyncResult:
        raise exc

    return publish


@pytest.mark.parametrize(
    ("outcome", "code", "stream", "text"),
    [
        (_MERGED, 0, "out", "o/r: the refresh PR https://github.com/o/r/pull/9 merged"),
        (_IN_SYNC, 0, "out", "o/r: in sync; nothing to refresh on develop"),
        (
            _HELD,
            0,
            "out",
            "o/r: the refresh PR https://github.com/o/r/pull/9 is held for release 2.1.400; "
            "the unlock sweep arms it",
        ),
        (
            PRNotMergedError("o/r", "https://github.com/o/r/pull/9", "Checks failed"),
            2,
            "err",
            "the refresh PR https://github.com/o/r/pull/9 did not merge: Checks failed",
        ),
        (
            FenceRefusedError("o/r", ["data.txt: refused, not covered"]),
            1,
            "err",
            "data.txt: refused, not covered",
        ),
    ],
    ids=["merged", "in-sync", "held", "not-merged", "fence"],
)
def test_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: SyncResult | BaseException,
    code: int,
    stream: str,
    text: str,
) -> None:
    _as_human(monkeypatch)
    if isinstance(outcome, BaseException):
        monkeypatch.setattr(cli, "publish", _publish_raising(outcome))
    else:
        monkeypatch.setattr(cli, "publish", lambda *_a, **_k: outcome)
    assert cli.main(["publish", "--from", "d", "--repo", "o/r"]) == code
    captured = capsys.readouterr()
    assert text in (captured.out if stream == "out" else captured.err)


def test_incomplete_credentials_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(app_token.CLIENT_ID_ENV, "Iv1.client")
    assert cli.main(["publish", "--from", "d"]) == 1
    assert "credentials are incomplete" in capsys.readouterr().err
    assert app_token.CLIENT_ID_ENV not in os.environ


# -- prepare ----------------------------------------------------------------------


def test_prepare_refuses_when_key_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _with_creds(monkeypatch)
    _as_human(monkeypatch)
    monkeypatch.setattr(cli, "prepare", _never("prepare"))
    assert cli.main(["prepare", "--out", str(tmp_path / "out"), "--repo", "o/r"]) == 1
    assert "prepare runs the hook, so it must never hold the App's credentials" in (
        capsys.readouterr().err
    )
    assert app_token.PRIVATE_KEY_ENV not in os.environ


def test_prepare_writes_its_output(
    origin: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(origin, _HOOK_TOML)
    out = tmp_path / "out"
    assert cli.main(["prepare", "--out", str(out), "--repo", "o/r"]) == 0
    assert "o/r: prepared 1 changed path(s) on develop" in capsys.readouterr().out
    assert (out / upstream_refresh.META_FILE).exists()
    assert (out / upstream_refresh.PATCH_FILE).exists()


def test_prepare_runs_under_actions_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    _as_human(monkeypatch)
    seen: list[str] = []

    def prepare(repo: str, _out: Path) -> upstream_refresh.Prepared:
        seen.append(repo)
        return upstream_refresh.Prepared(repo=repo, base_sha="a" * 40, paths=[], notes=[])

    monkeypatch.setattr(cli, "prepare", prepare)
    assert cli.main(["prepare", "--out", str(tmp_path)]) == 0
    assert seen == ["o/r"]


# -- under Actions ----------------------------------------------------------------


def test_actions_default_mode_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(cli, "prepare", _never("prepare"))
    monkeypatch.setattr(identity_mode, "current_mode", _never("identity resolution"))
    assert cli.main(["--repo", "o/r"]) == 1
    assert "refused under GitHub Actions" in capsys.readouterr().err


def test_actions_publish_without_creds_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("VRG_AUTOMATION_APP", _SLUG)
    monkeypatch.setattr(cli, "publish", _never("publish"))
    monkeypatch.setattr(identity_mode, "current_mode", _never("identity resolution"))
    monkeypatch.setattr(github, "read_json", _never("a GitHub call"))
    assert cli.main(["publish", "--from", "d", "--repo", "o/r"]) == 1
    assert "publish under GitHub Actions needs the automation App's credentials" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize("var", ["VRG_APP_ID", "VRG_PRIVATE_KEY_PATH"])
def test_publish_refuses_app_mode_configured(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    var: str,
) -> None:
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(cli, "publish", _never("publish"))
    assert cli.main(["publish", "--from", "d"]) == 1
    assert f"the agent App mode is configured ({var} set)" in capsys.readouterr().err
    assert automation == []


def test_publish_creds_without_automation_app_refused(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("VRG_AUTOMATION_APP")
    assert cli.main(["publish", "--from", "d"]) == 1
    assert "$VRG_AUTOMATION_APP is not set" in capsys.readouterr().err
    assert automation == []


def test_publish_mints_before_identity_resolution(
    automation: list[_FakeSource], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    def publish(repo: str, in_dir: Path, **kwargs: object) -> SyncResult:
        assert github.registered_token() == _TOKEN
        calls.append({"repo": repo, "in_dir": in_dir, **kwargs})
        return _MERGED

    monkeypatch.setattr(cli, "publish", publish)
    assert cli.main(["publish", "--from", str(tmp_path)]) == 0
    (source,) = automation
    assert source.minted == 1
    assert source.closed
    assert source.repo == "o/r"
    assert source.expected_slug == _SLUG
    assert source.permissions == app_token.UPSTREAM_REFRESH_PERMISSIONS
    assert source.creds.client_id == "Iv1.client"
    assert app_token.PRIVATE_KEY_ENV not in os.environ
    assert not github.has_token_source()
    (call,) = calls
    assert call["deadline_secs"] == PUBLISH_DEADLINE_SECS
    assert call["bot"] == (f"{_SLUG}[bot]", f"1+{_SLUG}[bot]@x")
    assert isinstance(call["started"], float)


def test_publish_with_creds_refuses_a_token_of_another_app(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(github, "graphql", lambda *_a, **_k: {"viewer": {"login": "other[bot]"}})
    monkeypatch.setattr(cli, "publish", _never("publish"))
    assert cli.main(["publish", "--from", "d"]) == 1
    assert "belongs to 'other[bot]'" in capsys.readouterr().err
    assert automation[0].closed


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
@pytest.mark.parametrize("when", ["identity", "publish"])
def test_sigterm_revokes_and_exits_nonzero(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    sig: signal.Signals,
    when: str,
) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    def graphql(_query: str, **_v: object) -> dict[str, object]:
        github.registered_token()  # the first mint
        if when == "identity":
            os.kill(os.getpid(), sig)
        return {"viewer": {"login": f"{_SLUG}[bot]"}}

    def publish(*_a: object, **_k: object) -> SyncResult:
        os.kill(os.getpid(), sig)
        pytest.fail("the signal must stop the run")

    monkeypatch.setattr(github, "graphql", graphql)
    monkeypatch.setattr(cli, "publish", publish if when == "publish" else _never("publish"))
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["publish", "--from", "d"])
    assert excinfo.value.code == 128 + sig
    (source,) = automation
    assert source.minted == 1
    assert source.closed
    assert not github.has_token_source()
    assert {s: signal.getsignal(s) for s in before} == before


def test_signal_with_failed_revocation_still_exits(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def close(self: _FakeSource) -> None:
        if self.closed:  # idempotent, as AppTokenSource.close is
            return
        self.closed = True
        msg = "revoking the automation token for o/r on exit failed (DELETE /installation/token)"
        raise app_token.AppTokenError(msg)

    def publish(*_a: object, **_k: object) -> SyncResult:
        os.kill(os.getpid(), signal.SIGTERM)
        pytest.fail("the signal must stop the run")

    monkeypatch.setattr(_FakeSource, "close", close)
    monkeypatch.setattr(cli, "publish", publish)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["publish", "--from", "d"])
    assert excinfo.value.code == 143
    err = capsys.readouterr().err
    assert "SIGTERM received; revoking the token and exiting 143" in err
    assert "revoking the automation token for o/r on exit failed" in err


def test_log_lines(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def publish(*_a: object, **_k: object) -> SyncResult:
        logging.getLogger(app_token.__name__).info("T2: renewed the automation token")
        logging.getLogger(app_token.__name__).info("T3: revoked the replaced automation token")
        return _MERGED

    monkeypatch.setattr(cli, "publish", publish)
    assert cli.main(["publish", "--from", "d"]) == 0
    captured = capsys.readouterr()
    lines = [
        "T0: minted the automation token",
        "T2: renewed the automation token",
        "T3: revoked the replaced automation token",
        "T1: revoked the automation token on exit",
        "o/r: the refresh PR https://github.com/o/r/pull/9 merged",
    ]
    positions = [captured.out.index(line) for line in lines]
    assert positions == sorted(positions)
    assert _TOKEN not in captured.out + captured.err
    assert not logging.getLogger(app_token.__name__).handlers


def test_poll_lines_reach_a_piped_stdout_as_they_are_printed(
    automation: list[_FakeSource], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #4596: under Actions stdout is a pipe, which Python block-buffers. A
    # poll line must reach it when printed, not when the step ends.
    raw = io.BytesIO()
    piped = io.TextIOWrapper(raw, encoding="utf-8")
    assert not piped.line_buffering
    monkeypatch.setattr("sys.stdout", piped)
    seen: list[bytes] = []

    def publish(*_a: object, **_k: object) -> SyncResult:
        print("Pending checks: ci (12s)")
        seen.append(raw.getvalue())
        return _MERGED

    monkeypatch.setattr(cli, "publish", publish)
    assert cli.main(["publish", "--from", "d"]) == 0
    (written,) = seen
    assert b"Pending checks: ci (12s)\n" in written
    assert piped.line_buffering


def test_a_stdout_that_is_not_a_text_stream_is_left_alone(
    automation: list[_FakeSource], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr(cli, "publish", lambda *_a, **_k: _MERGED)
    assert cli.main(["publish", "--from", "d"]) == 0
    assert "the refresh PR https://github.com/o/r/pull/9 merged" in out.getvalue()


# -- identity ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["user", "audit"])
@pytest.mark.parametrize(
    "argv",
    [["prepare", "--out", "d", "--repo", "o/r"], ["publish", "--from", "d"], ["--repo", "o/r"]],
    ids=["prepare", "publish", "combined"],
)
def test_agent_modes_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    argv: list[str],
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", mode)
    for name in ("prepare", "publish"):
        monkeypatch.setattr(cli, name, _never(name))
    assert cli.main(argv) == 1
    assert "vrg-upstream-refresh opens PRs and is not run by agents" in capsys.readouterr().err


def test_agent_publish_with_creds_refused(
    automation: list[_FakeSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    monkeypatch.setattr(cli, "publish", _never("publish"))
    assert cli.main(["publish", "--from", "d"]) == 1
    assert "identity signals contradict" in capsys.readouterr().err
    assert automation[0].closed


def test_human_publish_without_creds_runs_without_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _as_human(monkeypatch)
    seen: list[dict[str, object]] = []

    def publish(repo: str, in_dir: Path, **kwargs: object) -> SyncResult:
        seen.append({"repo": repo, "in_dir": in_dir, "source": github.has_token_source(), **kwargs})
        return _IN_SYNC

    monkeypatch.setattr(cli, "publish", publish)
    assert cli.main(["publish", "--from", str(tmp_path), "--repo", "o/r"]) == 0
    assert seen == [
        {"repo": "o/r", "in_dir": tmp_path, "source": False, "deadline_secs": None, "bot": None}
    ]


def test_automation_publish_without_creds_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.AUTOMATION)
    monkeypatch.setattr(cli, "publish", _never("publish"))
    assert cli.main(["publish", "--from", "d", "--repo", "o/r"]) == 1
    assert "publish without the App's credentials runs only as human" in capsys.readouterr().err


def test_human_default_runs_prepare_then_publish_without_source(
    origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(origin, _HOOK_TOML)
    _as_human(monkeypatch)
    seen: list[dict[str, object]] = []

    def publish(repo: str, in_dir: Path, **kwargs: object) -> SyncResult:
        prepared, _patch = upstream_refresh.read_prepared(in_dir)
        seen.append(
            {"repo": repo, "paths": prepared.paths, "source": github.has_token_source(), **kwargs}
        )
        return _MERGED

    monkeypatch.setattr(cli, "publish", publish)
    assert cli.main(["--repo", "o/r"]) == 0
    assert seen == [
        {"repo": "o/r", "paths": ["data.txt"], "source": False, "deadline_secs": None, "bot": None}
    ]


def test_combined_refuses_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_creds(monkeypatch)
    _as_human(monkeypatch)
    monkeypatch.setattr(cli, "prepare", _never("prepare"))
    assert cli.main(["--repo", "o/r"]) == 1
    assert "must never hold the App's credentials" in capsys.readouterr().err


# -- dry run ----------------------------------------------------------------------


def test_cli_requires_config(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "has no [upstream-refresh] section" in capsys.readouterr().err


def test_cli_invalid_config(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = ""\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "must be a non-empty string" in capsys.readouterr().err


def test_cli_dry_run_prints_body_and_leaves_checkout(
    origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = _seed(origin, _HOOK_TOML)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: would open the refresh PR on o/r (develop)" in out
    assert "## Changed files\n\n- `data.txt`" in out
    assert "Path fence (rules from the develop tip): no violations." in out
    # Nothing is pushed, and the caller's checkout is untouched.
    assert _git(work, "ls-remote", "origin").count("refs/heads/") == 1
    assert _git(work, "status", "--porcelain", "--untracked-files=all") == ""
    assert (work / "data.txt").read_text() == "old\n"


def test_cli_dry_run_nothing(
    origin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = "true"\n')
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert cli.main(["--dry-run"]) == 0
    assert "Dry run: nothing to refresh on develop" in capsys.readouterr().out


def test_cli_dry_run_resolves_repo_from_origin(
    origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(origin, _HOOK_TOML)
    monkeypatch.setattr(cli.github, "current_repo", lambda: "o/from-origin")
    seen: list[str] = []
    real = upstream_refresh.prepare

    def spy(repo: str, out_dir: Path, **kwargs: object) -> upstream_refresh.Prepared:
        seen.append(repo)
        return real(repo, out_dir)

    monkeypatch.setattr(cli, "prepare", spy)
    assert cli.main(["--dry-run"]) == 0
    assert seen == ["o/from-origin"]


def test_dry_run_reports_fence_violations(
    origin: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = _seed(origin, _HOOK_TOML)
    rules = tmp_path / "branch-vergil.toml"
    rules.write_text('[upstream-refresh]\nhook = "x"\npaths = ["vergil.toml"]\n')
    assert cli.main(["--repo", "o/r", "--dry-run", "--rules-from", str(rules)]) == 1
    out = capsys.readouterr().out
    assert f"Path fence (rules from --rules-from {rules}): 1 violation(s); nothing pushed:" in out
    assert "  - data.txt: refused, not covered by any [upstream-refresh].paths entry" in out
    assert _git(work, "ls-remote", "origin").count("refs/heads/") == 1
    # --rules-from without --dry-run is a usage error.
    assert cli.main(["--repo", "o/r", "--rules-from", str(rules)]) == 1


def test_dry_run_reports_violations_from_the_develop_tip(
    origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = "echo new > data.txt"\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "data.txt: refused, [upstream-refresh].paths is empty or absent" in (
        capsys.readouterr().out
    )


def test_agent_dry_run_allowed(
    origin: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed(origin, _HOOK_TOML)
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    for name in ("create_pr", "run", "graphql", "set_token_source", "token_source"):
        monkeypatch.setattr(github, name, _never(f"github.{name}"))
    monkeypatch.setattr(git, "run", _never("git.run (a push)"))
    monkeypatch.setattr(cli.app_token, "AppTokenSource", _never("a token source"))
    monkeypatch.setattr(identity_mode, "current_mode", _never("identity resolution"))
    rules = tmp_path / "vergil.toml"
    rules.write_text('[upstream-refresh]\nhook = "x"\npaths = ["data.txt"]\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 0
    assert cli.main(["--repo", "o/r", "--dry-run", "--rules-from", str(rules)]) == 0
    assert not github.has_token_source()


def test_dry_run_refuses_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_creds(monkeypatch)
    monkeypatch.setattr(cli, "prepare", _never("prepare"))
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "--dry-run runs the hook" in capsys.readouterr().err


def test_cli_hook_failure_exits_1(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = "exit 7"\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "[upstream-refresh].hook failed (exit 7)" in capsys.readouterr().err


def test_cli_clone_failure_exits_1(
    origin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = origin.parent / "missing.git"
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: missing.as_uri())
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "Could not clone the 'develop' branch of o/r" in capsys.readouterr().err


def test_cli_command_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: object, **_k: object) -> object:
        raise subprocess.CalledProcessError(1, ["gh", "repo", "view"], stderr="HTTP 502")

    monkeypatch.setattr(cli, "prepare", boom)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "command failed" in err
    assert "HTTP 502" in err
