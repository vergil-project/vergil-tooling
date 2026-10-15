"""Tests for vergil_tooling.bin.vrg_host (the ``vrg-host`` CLI)."""

from __future__ import annotations

from typing import Any

import pytest

from vergil_tooling.bin import vrg_host
from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib.host_deps import AuxCheck, HostDependency, HostPin, ManifestError


def _dep(**kw: Any) -> HostDependency:
    base: dict[str, Any] = {
        "name": "gh",
        "why": "",
        "method": "brew-formula",
        "upgrade": "auto",
        "latest": "probe",
        "probes": {},
        "checks": (),
        "pin": None,
    }
    base.update(kw)
    return HostDependency(**base)


def _pin(constraint: str) -> HostPin:
    return HostPin(
        constraint=constraint,
        inducing_release="2.0",
        deterministic=True,
        reason="breaks us",
        state="active",
        tracking_issue=None,
        pinned_date="2026-10-01",
    )


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    deps: tuple[HostDependency, ...],
    installed: dict[str, Any],
    latest: dict[str, Any],
) -> None:
    """Stub the manifest and handlers; a value that is an exception is raised."""

    def _lookup(table: dict[str, Any], dep: HostDependency) -> Any:
        value = table[dep.name]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", lambda: deps)
    monkeypatch.setattr(vrg_host.host_methods, "installed_version", lambda d: _lookup(installed, d))
    monkeypatch.setattr(vrg_host.host_methods, "latest_version", lambda d: _lookup(latest, d))


def test_status_exit_code_reflects_worst(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, (_dep(),), {"gh": None}, {"gh": "2.0"})
    assert vrg_host.main(["status"]) == 2  # missing -> ERROR


def test_bare_invocation_defaults_to_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, (_dep(),), {"gh": "2.0"}, {"gh": "2.0"})
    assert vrg_host.main([]) == 0
    out = capsys.readouterr().out
    assert "[OK   ] gh" in out
    assert "overall: OK (1 dependency)" in out


def test_parse_args_default_cmd() -> None:
    assert vrg_host.parse_args([]).cmd == "status"
    assert vrg_host.parse_args(["status"]).cmd == "status"


def test_status_warn_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = (_dep(name="gh"), _dep(name="uv", method="uv-tool"))
    _stub(monkeypatch, deps, {"gh": "2.0", "uv": "0.4"}, {"gh": "2.0", "uv": None})
    assert vrg_host.main(["status"]) == 1  # latest-unknown -> WARN


def test_unmanaged_latest_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    dep = _dep(name="macos", method="custom", latest="unmanaged")
    _stub(monkeypatch, (dep,), {"macos": "15.0"}, {"macos": hm.UNMANAGED})
    assert vrg_host.main(["status"]) == 0


def test_probe_error_is_reported_per_dep_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (
        _dep(name="gh"),
        _dep(name="uv", method="uv-tool"),
        _dep(name="weird", method="custom"),
        _dep(name="ok"),
    )
    _stub(
        monkeypatch,
        deps,
        {
            "gh": hm.ProbeError("gh: unexpected 'brew list --versions' output 'junk'"),
            "uv": "0.5",
            "weird": "1.0",
            "ok": "1.0",
        },
        {
            "gh": "2.0",
            "uv": hm.ProbeError("uv: garbled"),
            "weird": ManifestError("dependency 'weird': custom method requires probe"),
            "ok": "1.0",
        },
    )
    assert vrg_host.main(["status"]) == 2
    out = capsys.readouterr().out
    assert "[ERROR] gh" in out
    assert "error: ProbeError: gh: unexpected 'brew list --versions' output 'junk'" in out
    assert "[ERROR] uv" in out and "error: ProbeError: uv: garbled" in out
    assert "[ERROR] weird" in out and "error: ManifestError:" in out
    assert "[OK   ] ok" in out  # the healthy dep is still reported
    assert out.count("probe-error") == 3


def test_unsupported_pin_constraint_is_error_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (
        _dep(name="gh", pin=_pin("<=1.x")),
        _dep(name="uv", method="uv-tool", pin=_pin("0.5.x")),
        _dep(name="ok"),
    )
    _stub(
        monkeypatch,
        deps,
        {"gh": "1.0", "uv": "0.5.2", "ok": "1.0"},
        {"gh": "2.0", "uv": "0.6.0", "ok": "1.0"},
    )
    assert vrg_host.main(["status"]) == 2
    out = capsys.readouterr().out
    assert "[ERROR] gh" in out and "probe-error" in out
    assert "error: ValueError: unsupported pin constraint '<=1.x'" in out
    assert "[OK   ] uv" in out and "pinned (tracked debt)" in out
    assert "[OK   ] ok" in out


def test_check_probe_refused_is_probe_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _dep(checks=(AuxCheck("root", ["sudo", "true"], None),))
    _stub(monkeypatch, (dep,), {"gh": "2.0"}, {"gh": "2.0"})
    assert vrg_host.main(["status"]) == 2
    out = capsys.readouterr().out
    assert "probe-error" in out and "ValueError: refusing to escalate privilege" in out


def test_aux_checks_run_and_classify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _dep(
        checks=(
            AuxCheck("good", ["probe-good"], None),
            AuxCheck("bad", ["probe-bad"], ["fix"]),
        )
    )
    _stub(monkeypatch, (dep,), {"gh": "2.0"}, {"gh": "2.0"})
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> str | None:
        calls.append(cmd)
        return "" if cmd == ["probe-good"] else None

    monkeypatch.setattr(vrg_host.host_methods, "_run", fake_run)
    assert vrg_host.main(["status"]) == 2
    assert calls == [["probe-good"], ["probe-bad"]]
    out = capsys.readouterr().out
    assert "check good: satisfied" in out
    assert "check bad: UNSATISFIED" in out


def test_run_check_returns_name_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: "out")
    assert vrg_host._run_check(AuxCheck("c", ["x"], None)) == ("c", True)
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: None)
    assert vrg_host._run_check(AuxCheck("c", ["x"], None)) == ("c", False)


def test_invalid_manifest_exits_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> tuple[HostDependency, ...]:
        raise ManifestError("dependency 'gh': unknown method 'apt'")

    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", boom)
    assert vrg_host.main(["status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-host: invalid host-dependency manifest:" in captured.err
    assert "unknown method 'apt'" in captured.err


def test_status_against_packaged_manifest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real manifest, stubbed shell-out seam: every probe fails -> all missing.
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: None)
    assert vrg_host.main([]) == 2
    out = capsys.readouterr().out
    assert "[ERROR] uv" in out and "[ERROR] gh" in out


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_host.main(["--help"])
    assert exc.value.code == 0
    assert "status" in capsys.readouterr().out
