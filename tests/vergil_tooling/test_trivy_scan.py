"""Tests for vergil_tooling.lib.trivy_scan (issue #4445)."""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import trivy_scan
from vergil_tooling.lib.trivy_scan import (
    CACHE_ENV,
    RAW_REPORT,
    SARIF_REPORT,
    ScanOutcome,
    TrivyError,
    cache_dir,
    cache_lock,
    run_scan,
    scan_commands,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# -- cache_dir --------------------------------------------------------------------


def test_cache_dir_from_env(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    assert cache_dir(tmp_path / "repo", {CACHE_ENV: str(cache)}) == cache.resolve()


def test_cache_dir_defaults_under_xdg_cache_home(tmp_path: Path) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}
    assert cache_dir(tmp_path / "repo", env) == (tmp_path / "xdg" / "vergil" / "trivy").resolve()


@pytest.mark.parametrize("xdg", [None, "", "relative/cache"])
def test_cache_dir_defaults_under_home_cache(tmp_path: Path, xdg: str | None) -> None:
    env = {} if xdg is None else {"XDG_CACHE_HOME": xdg}
    home = tmp_path / "home"
    expected = (home / ".cache" / "vergil" / "trivy").resolve()
    assert cache_dir(tmp_path / "repo", env, home=home) == expected


@pytest.mark.parametrize("value", ["", "  "])
def test_cache_dir_refuses_an_empty_env(tmp_path: Path, value: str) -> None:
    with pytest.raises(TrivyError, match=rf"^{CACHE_ENV} is set but empty$"):
        cache_dir(tmp_path, {CACHE_ENV: value})


def test_cache_dir_refuses_a_relative_env(tmp_path: Path) -> None:
    with pytest.raises(TrivyError, match=r"must be an absolute path, not 'cache'"):
        cache_dir(tmp_path, {CACHE_ENV: "cache"})


@pytest.mark.parametrize("rel", ["", ".cache/trivy"])
def test_cache_dir_refuses_the_working_tree(tmp_path: Path, rel: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(TrivyError, match=r"is inside the working tree"):
        cache_dir(repo, {CACHE_ENV: str(repo / rel)})


def test_cache_dir_refuses_a_home_inside_the_working_tree(tmp_path: Path) -> None:
    # A HOME under the repo would put the default cache in the working tree.
    with pytest.raises(TrivyError, match=r"is inside the working tree"):
        cache_dir(tmp_path, {}, home=tmp_path / "home")


# -- scan_commands ----------------------------------------------------------------


def test_scan_commands_mirror_scan_sh(tmp_path: Path) -> None:
    raw = str(tmp_path / RAW_REPORT)
    assert scan_commands(tmp_path, Path("/cache")) == [
        [
            "trivy",
            "fs",
            "--scanners",
            "vuln",
            "--severity",
            "CRITICAL,HIGH",
            "--exit-code",
            "0",
            "--format",
            "json",
            "--output",
            raw,
            "--skip-files",
            "**/pip/_vendor/bom.cdx.json",
            "--cache-dir",
            "/cache",
            ".",
        ],
        ["trivy", "convert", "--severity", "CRITICAL,HIGH", "--format", "table", "--exit-code", "0", raw],
        [
            "trivy",
            "convert",
            "--severity",
            "CRITICAL,HIGH",
            "--format",
            "sarif",
            "--exit-code",
            "1",
            "--output",
            str(tmp_path / SARIF_REPORT),
            raw,
        ],
    ]


# -- run_scan ---------------------------------------------------------------------


class _FakeTrivy:
    """Plays scan.sh's three commands: one exit status per command per attempt.

    *attempts* is a list of (statuses, writes_report) pairs, one per attempt.
    The SARIF report is written by the third command when *writes_report*.
    """

    def __init__(self, workdir: Path, attempts: list[tuple[list[int], bool]]) -> None:
        self.workdir = workdir
        self.attempts = attempts
        self.calls: list[tuple[list[str], Path]] = []
        self._attempt = -1
        self._step = 0

    def __call__(self, cmd: Sequence[str], cwd: Path) -> int:
        self.calls.append((list(cmd), cwd))
        if cmd[1] == "fs":
            self._attempt += 1
            self._step = 0
        statuses, writes_report = self.attempts[self._attempt]
        rc = statuses[self._step]
        if self._step == 2 and writes_report:
            (self.workdir / SARIF_REPORT).write_text("{}")
        self._step += 1
        return rc


def _scan(
    tmp_path: Path, attempts: list[tuple[list[int], bool]]
) -> tuple[ScanOutcome, _FakeTrivy, list[float], list[str]]:
    workdir = tmp_path / "reports"
    workdir.mkdir(exist_ok=True)
    fake = _FakeTrivy(workdir, attempts)
    sleeps: list[float] = []
    logs: list[str] = []
    outcome = run_scan(
        tmp_path / "target",
        workdir,
        tmp_path / "cache",
        run=fake,
        log=logs.append,
        sleep=sleeps.append,
    )
    return outcome, fake, sleeps, logs


def test_run_scan_passes_with_a_report_and_exit_0(tmp_path: Path) -> None:
    outcome, fake, sleeps, logs = _scan(tmp_path, [([0, 0, 0], True)])
    assert outcome == ScanOutcome("passed", 0)
    assert [cmd for cmd, _ in fake.calls] == scan_commands(tmp_path / "reports", tmp_path / "cache")
    # Every command runs from the scan target, as the gate's run from the checkout.
    assert {cwd for _, cwd in fake.calls} == {tmp_path / "target"}
    assert sleeps == []
    assert logs == []


def test_run_scan_findings_keep_the_exit_status_and_are_not_retried(tmp_path: Path) -> None:
    outcome, fake, sleeps, _ = _scan(tmp_path, [([0, 0, 1], True)])
    assert outcome == ScanOutcome("findings", 1)
    assert len(fake.calls) == 3
    assert sleeps == []


def test_run_scan_retries_a_missing_report_with_backoff_then_errors(tmp_path: Path) -> None:
    # The first command fails (a failed database download): nothing after it runs.
    outcome, fake, sleeps, logs = _scan(tmp_path, [([1], False)] * 3)
    assert outcome == ScanOutcome("error", 1)
    assert [cmd[1] for cmd, _ in fake.calls] == ["fs", "fs", "fs"]
    assert sleeps == [15, 30]
    assert logs == [
        "WARNING: trivy: the scan produced no report (exit 1, attempt 1/3)",
        "trivy: retrying the scan in 15s",
        "WARNING: trivy: the scan produced no report (exit 1, attempt 2/3)",
        "trivy: retrying the scan in 30s",
        "WARNING: trivy: the scan produced no report (exit 1, attempt 3/3)",
    ]


def test_run_scan_recovers_on_a_later_attempt(tmp_path: Path) -> None:
    outcome, _, sleeps, _ = _scan(tmp_path, [([1], False), ([0, 0, 0], True)])
    assert outcome == ScanOutcome("passed", 0)
    assert sleeps == [15]


def test_run_scan_exit_0_without_a_report_is_an_error(tmp_path: Path) -> None:
    # A scan that never wrote a report never passes, whatever it exited.
    outcome, _, _, _ = _scan(tmp_path, [([0, 0, 0], False)] * 3)
    assert outcome == ScanOutcome("error", 1)


def test_run_scan_ignores_a_stale_report(tmp_path: Path) -> None:
    workdir = tmp_path / "reports"
    workdir.mkdir()
    (workdir / SARIF_REPORT).write_text("{}")
    (workdir / RAW_REPORT).write_text("{}")
    outcome, _, _, _ = _scan(tmp_path, [([1], False)] * 3)
    assert outcome == ScanOutcome("error", 1)
    assert not (workdir / RAW_REPORT).exists()


# -- cache_lock -------------------------------------------------------------------


def test_cache_lock_creates_the_cache_and_locks_it(tmp_path: Path) -> None:
    cache = tmp_path / "a" / "cache"
    logs: list[str] = []
    with cache_lock(cache, logs.append), (cache / "vrg-validate.lock").open() as other:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert logs == []


def test_cache_lock_waits_for_another_holder(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    logs: list[str] = []
    entered = threading.Event()
    with (cache / "vrg-validate.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)

        def take() -> None:
            with cache_lock(cache, logs.append):
                entered.set()

        thread = threading.Thread(target=take)
        thread.start()
        assert not entered.wait(0.2)
        fcntl.flock(holder, fcntl.LOCK_UN)
        thread.join(5)
    assert entered.is_set()
    assert logs == [f"trivy: waiting for another scan to release the database cache {cache}"]


def test_constants_match_the_gate() -> None:
    assert (trivy_scan.SCANNERS, trivy_scan.SEVERITY, trivy_scan.EXIT_CODE) == (
        "vuln",
        "CRITICAL,HIGH",
        "1",
    )
    assert (trivy_scan.SCAN_ATTEMPTS, trivy_scan.RETRY_DELAY_SECS) == (3, 15)
