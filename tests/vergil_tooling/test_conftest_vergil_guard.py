"""Tests for the run-log write guard in ``conftest.py`` (#5409).

The autouse guard wraps ``progress.RunLog.__init__`` and fails a test whose run
log would land in the repository's own ``.vergil/``. It guards the write site
rather than snapshotting the directory, because other processes (vrg-finalize-pr,
vrg-validate) write there legitimately while the suite runs. Each test here that
needs a patched ``_REPO_VERGIL`` patches it with ``monkeypatch``, so the real
directory is untouched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import tests.vergil_tooling.conftest as conftest
from vergil_tooling.lib import progress


def test_resolves_to_repo_vergil_for_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    assert conftest._resolves_to_repo_vergil(conftest._REPO_VERGIL.parent)
    monkeypatch.chdir(conftest._REPO_VERGIL.parent)
    assert conftest._resolves_to_repo_vergil(Path())


def test_resolves_to_repo_vergil_is_false_under_tmp_path(tmp_path: Path) -> None:
    assert not conftest._resolves_to_repo_vergil(tmp_path)


def test_run_log_under_repo_vergil_fails_the_test(
    request: pytest.FixtureRequest, _run_log_write_guard: list[str]
) -> None:
    repo_root = conftest._REPO_VERGIL.parent
    with pytest.raises(AssertionError, match=re.escape(request.node.nodeid)):
        progress.RunLog("x", repo_root)
    # Clear the offence the guard recorded, so this test's own teardown passes.
    assert len(_run_log_write_guard) == 1
    _run_log_write_guard.clear()
    # The guard refuses before writing: no log appeared in the real directory.
    assert list(conftest._REPO_VERGIL.glob("x-*.log")) == []


def test_run_log_under_tmp_path_is_written(tmp_path: Path) -> None:
    log = progress.RunLog("x", tmp_path)
    try:
        assert log.path.parent == tmp_path / ".vergil"
    finally:
        log.close()
    assert list((tmp_path / ".vergil").glob("x-*.log")) == [log.path]


def test_file_from_another_process_does_not_fail_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vergil = tmp_path / ".vergil"
    vergil.mkdir()
    monkeypatch.setattr(conftest, "_REPO_VERGIL", vergil)
    # A separate process writes into the repository's .vergil/ during the test.
    script = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('other\\n')"
    target = vergil / "vrg-finalize-pr-1.log"
    subprocess.run([sys.executable, "-c", script, str(target)], check=True)  # noqa: S603
    assert target.read_text() == "other\n"
    # This test's own run log lives elsewhere, so the guard does not fire.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    log = progress.RunLog("x", elsewhere)
    log.close()
    assert log.path.exists()
