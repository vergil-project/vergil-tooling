"""Tests for vergil_tooling.bin.vrg_host (the ``vrg-host`` CLI)."""

from __future__ import annotations

from typing import Any

import pytest

from vergil_tooling.bin import vrg_host
from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib import host_pins
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


_PIN_ARGS = [
    "pin",
    "gh",
    "--constraint",
    "2.40.x",
    "--inducing-release",
    "2.41.0",
    "--reason",
    "2.41.0 breaks vrg-gh auth",
]


def test_pin_records_in_pins_file_and_status_shows_debt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [*_PIN_ARGS, "--tracking-issue", "https://github.com/o/r/issues/3", "--deterministic"]
    assert vrg_host.main(args) == 0
    out = capsys.readouterr().out
    assert "pinned gh at 2.40.x (inducing release 2.41.0)" in out
    pin = host_pins.load_pins()["gh"]
    assert pin.deterministic is True
    assert pin.tracking_issue == "https://github.com/o/r/issues/3"
    assert pin.state == "active"

    monkeypatch.setattr(vrg_host.host_methods, "installed_version", lambda d: "2.40.1")
    monkeypatch.setattr(vrg_host.host_methods, "latest_version", lambda d: "2.42.0")
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: None)
    assert vrg_host.main(["status"]) == 1  # gh pinned but re-evaluation due -> WARN
    out = capsys.readouterr().out
    assert "pinned (tracked debt)" in out
    assert "pin: 2.40.x, holding back from 2.42.0 (inducing release 2.41.0)" in out
    assert "!! RE-EVALUATION DUE" in out


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        (["--reason", "  "], "reason"),
        (["--constraint", "<=2.40"], "unsupported pin constraint '<=2.40'"),
    ],
)
def test_pin_refuses_invalid_input(
    capsys: pytest.CaptureFixture[str], extra: list[str], match: str
) -> None:
    assert vrg_host.main([*_PIN_ARGS, *extra]) == 1
    assert match in capsys.readouterr().err
    assert host_pins.load_pins() == {}


def test_pin_refuses_undeclared_dependency(capsys: pytest.CaptureFixture[str]) -> None:
    args = list(_PIN_ARGS)
    args[1] = "nope"
    assert vrg_host.main(args) == 1
    assert "vrg-host pin: refused: 'nope' is not a dependency declared" in capsys.readouterr().err


def test_pin_requires_reason_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_host.main(_PIN_ARGS[:-2])
    assert exc.value.code == 2
    assert "--reason" in capsys.readouterr().err


def test_unpin_frees_pin(capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_host.main(_PIN_ARGS) == 0
    assert vrg_host.main(["unpin", "gh"]) == 0
    assert "freed pin on gh (kept as history" in capsys.readouterr().out
    assert host_pins.load_pins()["gh"].state == "freed"


def test_unpin_refuses_unpinned(capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_host.main(["unpin", "gh"]) == 1
    assert "vrg-host unpin: refused: 'gh' has no pin" in capsys.readouterr().err


def test_status_invalid_pins_file_exits_error(capsys: pytest.CaptureFixture[str]) -> None:
    path = host_pins.default_pins_path()
    path.parent.mkdir(parents=True)
    path.write_text("[pin.gh]\nconstraint = 1\n")
    assert vrg_host.main(["status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-host: invalid host pins file:" in captured.err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_host.main(["--help"])
    assert exc.value.code == 0
    assert "status" in capsys.readouterr().out


# -- auxiliary-check remediation (spec §7.4; absorbs vergil-project/.github#166) --


def test_gcloud_entry_has_numpy_check() -> None:
    from vergil_tooling.lib import host_deps

    gcloud = {d.name: d for d in host_deps.load_manifest()}["gcloud"]
    assert gcloud.method == "custom"
    assert set(gcloud.probes) == {"installed_version", "latest_version", "upgrade"}
    # Installed and latest come from the same (core component) version scheme.
    assert "value(core)" in " ".join(gcloud.probes["installed_version"])
    assert "--filter=id=core" in gcloud.probes["latest_version"]
    (check,) = gcloud.checks
    assert check.name == "numpy-in-interpreter"
    assert "basic.python_location" in check.probe[-1]
    assert "import numpy" in check.probe[-1]
    assert check.remediation is not None
    assert "-m pip install" in check.remediation[-1] and "numpy" in check.remediation[-1]
    assert not any(part in {"sudo", "doas", "su", "pkexec"} for part in check.remediation)


class _Host:
    """A stateful fake of the ``_run`` seam: probes pass once their fix has run."""

    def __init__(self, satisfied: set[str], fixes: dict[str, str | None]) -> None:
        self.satisfied = set(satisfied)
        self.fixes = fixes  # remediation cmd -> probe it satisfies (None = fix fails)
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str]) -> str | None:
        self.calls.append(cmd)
        (word,) = cmd
        if word in self.fixes:
            probe = self.fixes[word]
            if probe is None:
                return None
            self.satisfied.add(probe)
            return "fixed"
        return "" if word in self.satisfied else None


def _checked_dep(name: str = "gc") -> HostDependency:
    return _dep(
        name=name,
        method="custom",
        probes={"installed_version": ["installed"], "latest_version": ["installed"]},
        checks=(
            AuxCheck("good", ["probe-good"], ["fix-good"]),
            AuxCheck("bad", ["probe-bad"], ["fix-bad"]),
            AuxCheck("manual", ["probe-manual"], None),
        ),
    )


def test_remediate_checks_runs_only_unsatisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Host({"probe-good"}, {"fix-good": "probe-good", "fix-bad": "probe-bad"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    assert vrg_host.remediate_checks(_checked_dep()) == ["bad"]
    assert ["fix-good"] not in host.calls
    assert host.calls == [
        ["probe-good"],
        ["probe-bad"],
        ["fix-bad"],
        ["probe-bad"],
        ["probe-manual"],
    ]


def test_remediate_checks_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Host({"probe-good", "probe-bad", "probe-manual"}, {})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    assert vrg_host.remediate_checks(_checked_dep()) == []


def test_remediate_checks_failed_command_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Host({"probe-good"}, {"fix-bad": None})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    with pytest.raises(vrg_host.RemediationFailedError, match="remediation for check 'bad' failed"):
        vrg_host.remediate_checks(_checked_dep())


def test_remediate_checks_still_unsatisfied_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix "succeeds" but satisfies some other probe: never reported as fixed.
    host = _Host({"probe-good"}, {"fix-bad": "elsewhere"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    with pytest.raises(vrg_host.RemediationFailedError, match="still unsatisfied"):
        vrg_host.remediate_checks(_checked_dep())


def test_remediate_checks_refuses_privilege_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def unsatisfied(cmd: list[str], **_kw: Any) -> Any:
        raise subprocess.CalledProcessError(1, cmd)

    # Real _run (so the privilege guard is exercised), with a failing probe.
    monkeypatch.setattr(hm.subprocess, "run", unsatisfied)
    dep = _dep(checks=(AuxCheck("root", ["probe"], ["sudo", "fix"]),))
    with pytest.raises(ValueError, match="refusing to escalate privilege"):
        vrg_host.remediate_checks(dep)


def test_status_fix_remediates_then_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _dep(
        name="gc",
        method="custom",
        probes={"installed_version": ["installed"], "latest_version": ["installed"]},
        checks=(AuxCheck("numpy", ["probe-numpy"], ["fix-numpy"]),),
    )
    plain = _dep(name="gh")
    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", lambda: (dep, plain))
    host = _Host(set(), {"fix-numpy": "probe-numpy"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    monkeypatch.setattr(
        vrg_host.host_methods,
        "installed_version",
        lambda d: "1.0" if d.name == "gc" else "2.0",
    )
    monkeypatch.setattr(vrg_host.host_methods, "latest_version", lambda d: "2.0")
    assert vrg_host.main(["status", "--fix"]) == 1  # gc behind 1.0 -> 2.0: WARN only
    out = capsys.readouterr().out
    assert "fixed: gc check numpy" in out
    assert "check numpy: satisfied" in out


def test_status_fix_skips_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _checked_dep()
    _stub(monkeypatch, (dep,), {"gc": None}, {"gc": "1.0"})
    host = _Host(set(), {"fix-good": "probe-good", "fix-bad": "probe-bad"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    assert vrg_host.main(["status", "--fix"]) == 2
    assert not any(cmd[0].startswith("fix-") for cmd in host.calls)
    captured = capsys.readouterr()
    assert "fixed:" not in captured.out
    assert "missing" in captured.out
    assert captured.err == ""


def test_status_fix_failure_reported_and_forces_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _dep(name="gc", checks=(AuxCheck("numpy", ["probe-numpy"], ["fix-numpy"]),))
    _stub(monkeypatch, (dep,), {"gc": "1.0"}, {"gc": "1.0"})
    # The remediation command fails, yet the check happens to pass on the
    # status re-probe: the failed fix must still not be reported as OK.
    calls = {"probe-numpy": 0}

    def flaky(cmd: list[str]) -> str | None:
        if cmd == ["fix-numpy"]:
            return None
        calls["probe-numpy"] += 1
        return "" if calls["probe-numpy"] > 1 else None

    monkeypatch.setattr(vrg_host.host_methods, "_run", flaky)
    assert vrg_host.main(["status", "--fix"]) == 2
    captured = capsys.readouterr()
    assert "vrg-host: fix gc: RemediationFailedError:" in captured.err
    assert "[OK   ] gc" in captured.out


def test_status_fix_probe_error_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dep = _dep(name="gc", checks=(AuxCheck("numpy", ["probe-numpy"], ["fix-numpy"]),))
    _stub(monkeypatch, (dep,), {"gc": hm.ProbeError("garbled")}, {"gc": "1.0"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: None)
    assert vrg_host.main(["status", "--fix"]) == 2
    captured = capsys.readouterr()
    assert "vrg-host: fix gc: ProbeError: garbled" in captured.err


def test_status_without_fix_never_remediates(monkeypatch: pytest.MonkeyPatch) -> None:
    dep = _checked_dep()
    _stub(monkeypatch, (dep,), {"gc": "1.0"}, {"gc": "1.0"})
    host = _Host(set(), {"fix-good": "probe-good", "fix-bad": "probe-bad"})
    monkeypatch.setattr(vrg_host.host_methods, "_run", host.run)
    assert vrg_host.main(["status"]) == 2
    assert not any(cmd[0].startswith("fix-") for cmd in host.calls)
