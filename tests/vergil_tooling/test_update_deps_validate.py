from __future__ import annotations

import subprocess

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.failure import RAW_TAIL_HEADING
from vergil_tooling.lib.update_deps.validate import run_validation

_MOD = "vergil_tooling.lib.update_deps.validate"


def test_run_validation_invokes_container_run(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: seen.append(list(cmd)) or 0)
    run_validation()
    assert seen == [["vrg-container-run", "--", "vrg-validate"]]


def test_run_validation_raises_on_failure(monkeypatch) -> None:
    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, output="boom-out", stderr="boom-err")

    monkeypatch.setattr(_MOD + ".progress.run", _boom)
    with pytest.raises(UpdateDepsError, match="Validation failed") as info:
        run_validation()
    # Both streams: pytest failures land on stdout, tool noise on stderr.
    assert info.value.detail == "boom-out\nboom-err"
    # No stage markers to isolate: the section says it is the raw tail (#3283).
    assert info.value.detail_heading == RAW_TAIL_HEADING
    assert info.value.command == "vrg-container-run -- vrg-validate"


def test_run_validation_carries_the_failing_stage(monkeypatch) -> None:
    stdout = "→ lint  starting...\n✓ lint  0.1s\n→ test  starting...\nFAILED t\n✗ test  1.0s — x"

    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, output=stdout, stderr="uv noise")

    monkeypatch.setattr(_MOD + ".progress.run", _boom)
    with pytest.raises(UpdateDepsError) as info:
        run_validation()
    assert info.value.detail == "→ test  starting...\nFAILED t\n✗ test  1.0s — x"
    assert info.value.detail_heading == "Output of the failing vrg-validate stage (test):"
