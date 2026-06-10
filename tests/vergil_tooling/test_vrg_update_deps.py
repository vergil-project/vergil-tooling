from __future__ import annotations

from pathlib import Path

from vergil_tooling.bin import vrg_update_deps

_MOD = "vergil_tooling.bin.vrg_update_deps"


def test_main_refuses_agent_identity(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: False)
    rc = vrg_update_deps.main([])
    assert rc == 1
    assert "human" in capsys.readouterr().err.lower()


def test_main_runs_pipeline_for_human(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: True)
    monkeypatch.setattr(_MOD + ".git.repo_root", lambda: Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(
        _MOD + ".progress.run_pipeline",
        lambda state, stages, **kw: seen.update(command=kw["command"], root=kw["repo_root"]) or 0,
    )
    rc = vrg_update_deps.main([])
    assert rc == 0
    assert seen["command"] == "vrg-update-deps"
    assert seen["root"] == Path("/tmp/r")  # noqa: S108


def test_only_flag_threads_to_state(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: True)
    monkeypatch.setattr(_MOD + ".git.repo_root", lambda: Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(
        _MOD + ".progress.run_pipeline",
        lambda state, stages, **kw: seen.update(only=state.only, skip=state.skip) or 0,
    )
    rc = vrg_update_deps.main(["--only", "python,vergil"])
    assert rc == 0
    assert seen["only"] == ["python", "vergil"]
    assert seen["skip"] is None


def test_vergil_flag_implies_only_vergil(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: True)
    monkeypatch.setattr(_MOD + ".git.repo_root", lambda: Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(
        _MOD + ".progress.run_pipeline",
        lambda state, stages, **kw: seen.update(
            only=state.only, skip=state.skip, bump=state.vergil_bump
        )
        or 0,
    )
    rc = vrg_update_deps.main(["--vergil", "2.2"])
    assert rc == 0
    assert seen["only"] == ["vergil"]
    assert seen["skip"] is None
    assert seen["bump"] == "2.2"
