from __future__ import annotations

import subprocess

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
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
    with pytest.raises(UpdateDepsError, match="Validation failed"):
        run_validation()
