"""Tests for vergil_tooling.bin.vrg_host (the ``vrg-host`` CLI)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
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
    # Only auxiliary-check probes reach _run here; satisfy them (gcloud's numpy check).
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: "")
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


# -- upgrade -------------------------------------------------------------------


def _fake_host(
    monkeypatch: pytest.MonkeyPatch,
    errors: dict[str, Exception] | None = None,
    *,
    installed: dict[str, Any] | None = None,
    latest: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> list[str]:
    """Stub the version probes and ``host_methods.upgrade``; return the upgraded names.

    Unlisted dependencies are installed at ``1.0`` with latest ``2.0``, and an
    upgrade moves the installed version to *after* (default: the latest). A
    probe value that is an exception is raised.
    """
    calls: list[str] = []
    failing = errors or {}
    have = dict(installed or {})
    newest = latest or {}
    moves = after or {}

    def _value(value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    def fake_upgrade(dep: HostDependency) -> None:
        calls.append(dep.name)
        if dep.name in failing:
            raise failing[dep.name]
        have[dep.name] = moves.get(dep.name, newest.get(dep.name, "2.0"))

    monkeypatch.setattr(
        vrg_host.host_methods, "installed_version", lambda d: _value(have.get(d.name, "1.0"))
    )
    monkeypatch.setattr(
        vrg_host.host_methods, "latest_version", lambda d: _value(newest.get(d.name, "2.0"))
    )
    monkeypatch.setattr(vrg_host.host_methods, "upgrade", fake_upgrade)
    return calls


def _stub_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    deps: tuple[HostDependency, ...],
    errors: dict[str, Exception] | None = None,
    **versions: dict[str, Any],
) -> list[str]:
    """Stub the manifest, the probes and ``host_methods.upgrade`` (see :func:`_fake_host`)."""
    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", lambda: deps)
    return _fake_host(monkeypatch, errors, **versions)


def test_upgrade_all_skips_manual_and_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (
        _dep(name="gh"),
        _dep(name="macos", method="custom", upgrade="manual"),
        _dep(name="xcode", method="custom", upgrade="manual"),
    )
    calls = _stub_upgrade(monkeypatch, deps)
    assert vrg_host.main(["upgrade", "--all"]) == 0
    out = capsys.readouterr().out
    assert calls == ["gh"]  # only auto upgraded
    assert "upgraded gh" in out
    assert "skipped (upgrade = manual, upgrade by hand): macos, xcode" in out


def test_upgrade_named_dep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(monkeypatch, (_dep(name="gh"), _dep(name="uv", method="uv-tool")))
    assert vrg_host.main(["upgrade", "uv", "uv"]) == 0
    assert calls == ["uv"]  # deduplicated, and only what was named
    out = capsys.readouterr().out
    assert "upgraded uv" in out and "skipped" not in out


def test_upgrade_named_manual_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="gh"), _dep(name="macos", method="custom", upgrade="manual"))
    calls = _stub_upgrade(monkeypatch, deps)
    assert vrg_host.main(["upgrade", "macos", "gh"]) == 1
    assert calls == ["gh"]
    out = capsys.readouterr().out
    assert "refused  macos: upgrade = manual" in out
    assert "upgraded gh" in out


def test_upgrade_unknown_dep_is_error_and_upgrades_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(monkeypatch, (_dep(name="gh"),))
    assert vrg_host.main(["upgrade", "gh", "nope"]) == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "vrg-host: unknown dependency: nope (known: gh)" in err


def test_upgrade_pinned_dep_is_held_without_force(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(monkeypatch, (_dep(name="gh", pin=_pin("2.40.x")),))
    assert vrg_host.main(["upgrade", "gh"]) == 1
    assert calls == []
    out = capsys.readouterr().out
    assert "held     gh: pinned to 2.40.x (breaks us)" in out
    assert "vrg-host unpin gh" in out and "--force" in out


def test_upgrade_all_holds_pin_without_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="gh", pin=_pin("2.x")), _dep(name="uv", method="uv-tool"))
    calls = _stub_upgrade(monkeypatch, deps)
    assert vrg_host.main(["upgrade", "--all"]) == 0
    assert calls == ["uv"]
    assert "held     gh" in capsys.readouterr().out


def test_upgrade_force_crosses_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(monkeypatch, (_dep(name="gh", pin=_pin("2.40.x")),))
    assert vrg_host.main(["upgrade", "--force", "gh"]) == 0
    assert calls == ["gh"]
    assert "upgraded gh" in capsys.readouterr().out


def test_upgrade_inactive_pin_does_not_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    freed = HostPin(
        constraint="2.x",
        inducing_release="3.0",
        deterministic=False,
        reason="was broken",
        state="freed",
        tracking_issue=None,
        pinned_date="2026-10-01",
    )
    calls = _stub_upgrade(monkeypatch, (_dep(name="gh", pin=freed),))
    assert vrg_host.main(["upgrade", "gh"]) == 0
    assert calls == ["gh"]


@pytest.mark.parametrize("force", [[], ["--force"]])
def test_upgrade_unsupported_pin_constraint_is_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], force: list[str]
) -> None:
    deps = (_dep(name="gh", pin=_pin("<=2.x")), _dep(name="uv", method="uv-tool"))
    calls = _stub_upgrade(monkeypatch, deps)
    assert vrg_host.main(["upgrade", "--all", *force]) == 2
    assert calls == ["uv"]  # the bad pin fails gh alone
    out = capsys.readouterr().out
    assert "ERROR    gh: unsupported pin constraint '<=2.x'" in out
    assert "upgraded uv" in out


def test_upgrade_privilege_refusal_is_manual_action_never_retried(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A custom upgrade command that escalates hits the real _run guard.
    dep = _dep(name="tool", method="custom", probes={"upgrade": ["sudo", "tool", "update"]})
    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", lambda: (dep, _dep(name="gh")))
    have = {"tool": "1.0", "gh": "1.0"}
    monkeypatch.setattr(vrg_host.host_methods, "installed_version", lambda d: have[d.name])
    monkeypatch.setattr(vrg_host.host_methods, "latest_version", lambda d: "2.0")
    runs: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **_kw: Any) -> Any:
        runs.append(cmd)
        have[cmd[-1]] = "2.0"
        return type("Done", (), {"stdout": ""})()

    monkeypatch.setattr(hm.subprocess, "run", fake_subprocess_run)
    assert vrg_host.main(["upgrade", "--all"]) == 2
    assert runs == [["brew", "upgrade", "gh"]]  # sudo never executed, gh still upgraded
    out = capsys.readouterr().out
    assert "MANUAL   tool: upgrade refused, needs manual action" in out
    assert "refusing to escalate privilege: 'sudo'" in out
    assert "upgraded gh" in out


def test_upgrade_failures_are_reported_per_dep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="gh"), _dep(name="weird", method="custom"), _dep(name="uv", method="uv-tool"))
    errors: dict[str, Exception] = {
        "gh": hm.UpgradeFailedError("gh: upgrade command failed: brew upgrade gh"),
        "weird": ManifestError("dependency 'weird': custom method requires probe 'upgrade'"),
    }
    calls = _stub_upgrade(monkeypatch, deps, errors)
    assert vrg_host.main(["upgrade", "--all"]) == 2
    assert calls == ["gh", "weird", "uv"]  # one failure never stops the rest
    out = capsys.readouterr().out
    assert "FAILED   gh: gh: upgrade command failed: brew upgrade gh" in out
    assert "upgrade it manually" in out
    assert "FAILED   weird: dependency 'weird': custom method requires probe 'upgrade'" in out
    assert "upgraded uv" in out


def test_upgrade_invalid_manifest_exits_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> tuple[HostDependency, ...]:
        raise ManifestError("bad")

    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", boom)
    assert vrg_host.main(["upgrade", "--all"]) == 2
    assert "vrg-host: invalid host-dependency manifest: bad" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["upgrade"], ["upgrade", "--all", "gh"]])
def test_upgrade_requires_names_xor_all(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_host.main(argv)
    assert exc.value.code == 2
    assert "name one or more dependencies, or pass --all (not both)" in capsys.readouterr().err


def test_upgrade_against_packaged_manifest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real manifest, every dependency behind, stubbed shell-out seam: every
    # upgrade command fails.
    monkeypatch.setattr(vrg_host.host_methods, "installed_version", lambda d: "1.0")
    monkeypatch.setattr(vrg_host.host_methods, "latest_version", lambda d: "2.0")
    monkeypatch.setattr(vrg_host.host_methods, "_run", lambda cmd: None)
    assert vrg_host.main(["upgrade", "--all"]) == 2
    out = capsys.readouterr().out
    assert "FAILED   uv" in out and "FAILED   gh" in out and "FAILED   gcloud" in out


def test_upgrade_honors_pin_from_pins_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pin recorded with `vrg-host pin` (host-local pins file, not the packaged
    # manifest) is a real hold for upgrade.
    assert vrg_host.main(_PIN_ARGS) == 0
    calls = _fake_host(monkeypatch)  # every dependency 1.0, latest 2.0
    capsys.readouterr()

    assert vrg_host.main(["upgrade", "gh"]) == 1
    assert calls == []
    assert (
        "held     gh: pinned to 2.40.x (2.41.0 breaks vrg-gh auth) at 1.0, holding back from 2.0"
        in capsys.readouterr().out
    )

    # Every other auto dependency in the packaged manifest, in manifest order.
    unpinned_auto = [
        "uv",
        "git",
        "bash",
        "gcloud",
        "lima",
        "opentofu",
        "git-cliff",
        "vergil-tooling",
        "claude-code",
    ]
    assert vrg_host.main(["upgrade", "--all"]) == 0
    assert calls == unpinned_auto  # gh held, the rest upgraded
    assert "held     gh" in capsys.readouterr().out

    assert vrg_host.main(["upgrade", "--force", "gh"]) == 0
    assert calls == [*unpinned_auto, "gh"]
    assert "upgraded gh: 1.0 -> 2.0" in capsys.readouterr().out

    assert vrg_host.main(["unpin", "gh"]) == 0  # a freed pin no longer holds
    assert vrg_host.main(["upgrade", "gh"]) == 0
    assert calls == [*unpinned_auto, "gh"]  # now current: skipped, not held
    out = capsys.readouterr().out
    assert "skipped  gh: current (2.0)" in out and "held" not in out


def test_upgrade_honors_pin_written_via_host_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    host_pins.write_pin(
        "uv",
        host_pins.make_pin(
            constraint="0.5.x",
            inducing_release="0.6.0",
            deterministic=False,
            reason="0.6 breaks the lockfile",
            tracking_issue=None,
        ),
    )
    calls = _fake_host(monkeypatch)
    assert vrg_host.main(["upgrade", "uv"]) == 1
    assert calls == []


def test_upgrade_invalid_pins_file_exits_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = host_pins.default_pins_path()
    path.parent.mkdir(parents=True)
    path.write_text("[pin.gh]\nconstraint = 1\n")
    calls: list[str] = []
    monkeypatch.setattr(vrg_host.host_methods, "upgrade", lambda d: calls.append(d.name))
    assert vrg_host.main(["upgrade", "--all"]) == 2
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-host: invalid host pins file:" in captured.err


# -- upgrade: probe first, report before -> after (#3301) ----------------------


def test_upgrade_skips_current_dependency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="uv", method="uv-tool"), _dep(name="git"))
    calls = _stub_upgrade(
        monkeypatch,
        deps,
        installed={"uv": "0.13.1", "git": "2.54.0"},
        latest={"uv": "0.13.1", "git": "2.56.0_1"},
    )
    assert vrg_host.main(["upgrade", "--all"]) == 0
    assert calls == ["git"]  # the current dependency's upgrade never runs
    out = capsys.readouterr().out.splitlines()
    assert out == ["skipped  uv: current (0.13.1)", "upgraded git: 2.54.0 -> 2.56.0_1"]


def test_upgrade_newer_than_latest_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_upgrade(
        monkeypatch, (_dep(name="gh"),), installed={"gh": "2.1"}, latest={"gh": "2.0"}
    )
    assert vrg_host.main(["upgrade", "gh"]) == 0
    assert calls == []


@pytest.mark.parametrize("argv", [["upgrade", "bash"], ["upgrade", "--all"]])
def test_upgrade_unchanged_while_behind_is_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    calls = _stub_upgrade(
        monkeypatch,
        (_dep(name="bash"),),
        installed={"bash": "5.3.9"},
        latest={"bash": "5.3.20"},
        after={"bash": "5.3.9"},
    )
    assert vrg_host.main(argv) == 1  # WARN even under --all: not a deliberate hold
    assert calls == ["bash"]
    out = capsys.readouterr().out
    assert "unchanged bash: still 5.3.9, latest 5.3.20" in out
    assert "upgraded" not in out


def test_upgrade_that_falls_short_of_latest_is_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_upgrade(
        monkeypatch,
        (_dep(name="git"),),
        installed={"git": "2.54.0"},
        latest={"git": "2.56.0"},
        after={"git": "2.55.0"},
    )
    assert vrg_host.main(["upgrade", "git"]) == 1
    assert "upgraded git: 2.54.0 -> 2.55.0 (still behind latest 2.56.0)" in capsys.readouterr().out


def test_upgrade_result_not_orderable_against_latest_is_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_upgrade(
        monkeypatch,
        (_dep(name="git"),),
        installed={"git": "2.54.0"},
        latest={"git": "2.56.0"},
        after={"git": "2.56.0-rc1"},
    )
    assert vrg_host.main(["upgrade", "git"]) == 1
    out = capsys.readouterr().out
    assert "upgraded git: 2.54.0 -> 2.56.0-rc1 (cannot order against latest 2.56.0:" in out


def test_upgrade_reports_reprobed_version_not_expected_latest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_upgrade(
        monkeypatch,
        (_dep(name="git"),),
        installed={"git": "2.54.0"},
        latest={"git": "2.56.0"},
        after={"git": "2.56.0_1"},  # a newer rebuild than the latest feed knew of
    )
    assert vrg_host.main(["upgrade", "git"]) == 0
    assert capsys.readouterr().out.strip() == "upgraded git: 2.54.0 -> 2.56.0_1"


def test_upgrade_multi_level_offers_compare_against_leading_edge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_upgrade(
        monkeypatch,
        (_dep(name="tool", method="custom"),),
        installed={"tool": "26.5.2"},
        latest={"tool": hm.Offers(("26.7.1", "27.0.1"))},
        after={"tool": "27.0.1"},
    )
    assert vrg_host.main(["upgrade", "tool"]) == 0
    assert "upgraded tool: 26.5.2 -> 27.0.1" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("after", "line"),
    [
        ("2.1.244", "ran      vergil-tooling: 2.1.244 -> 2.1.244 (unchanged; no latest feed)"),
        ("2.1.245", "ran      vergil-tooling: 2.1.244 -> 2.1.245 (no latest feed)"),
    ],
)
def test_upgrade_unmanaged_latest_runs_and_reports_before_after(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], after: str, line: str
) -> None:
    dep = _dep(name="vergil-tooling", method="uv-tool", latest="unmanaged")
    calls = _stub_upgrade(
        monkeypatch,
        (dep,),
        installed={"vergil-tooling": "2.1.244"},
        latest={"vergil-tooling": hm.UNMANAGED},
        after={"vergil-tooling": after},
    )
    assert vrg_host.main(["upgrade", "vergil-tooling"]) == 0  # informational
    assert calls == ["vergil-tooling"]
    assert capsys.readouterr().out.strip() == line


def test_upgrade_latest_unknown_runs_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(
        monkeypatch,
        (_dep(name="lima"),),
        installed={"lima": "1.0"},
        latest={"lima": None},
        after={"lima": "1.0"},
    )
    assert vrg_host.main(["upgrade", "--all"]) == 1  # a failed latest probe stays WARN
    assert calls == ["lima"]
    assert capsys.readouterr().out.strip() == (
        "ran      lima: 1.0 -> 1.0 (unchanged; latest unknown: its latest probe did not answer)"
    )


def test_upgrade_held_reports_installed_and_held_back_latest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="gh", pin=_pin("2.40.x")), _dep(name="vt", pin=_pin("1.x")))
    calls = _stub_upgrade(
        monkeypatch,
        deps,
        installed={"gh": "2.40.1", "vt": "1.2"},
        latest={"gh": "2.42.0", "vt": hm.UNMANAGED},
    )
    assert vrg_host.main(["upgrade", "--all"]) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "held     gh: pinned to 2.40.x (breaks us) at 2.40.1, holding back from 2.42.0;" in out
    assert "held     vt: pinned to 1.x (breaks us) at 1.2, holding back from unmanaged;" in out


def test_upgrade_force_does_not_upgrade_current_pinned_dependency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_upgrade(
        monkeypatch,
        (_dep(name="gh", pin=_pin("2.40.x")),),
        installed={"gh": "2.42.0"},
        latest={"gh": "2.42.0"},
    )
    assert vrg_host.main(["upgrade", "--force", "gh"]) == 0
    assert calls == []
    assert "skipped  gh: current (2.42.0)" in capsys.readouterr().out


def test_upgrade_missing_dependency_is_error_and_not_attempted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = (_dep(name="lima"), _dep(name="gh"))
    calls = _stub_upgrade(monkeypatch, deps, installed={"lima": None})
    assert vrg_host.main(["upgrade", "--all"]) == 2
    assert calls == ["gh"]  # one missing dependency never stops the rest
    out = capsys.readouterr().out
    assert "ERROR    lima: missing (not installed by brew-formula)" in out
    assert "upgraded gh: 1.0 -> 2.0" in out


@pytest.mark.parametrize(
    ("installed", "latest", "message"),
    [
        (hm.ProbeError("lima: unreadable"), "2.0", "ProbeError: lima: unreadable"),
        ("1.0", hm.ProbeError("lima: no json"), "ProbeError: lima: no json"),
        ("1.0", ManifestError("bad entry"), "ManifestError: bad entry"),
        ("2.0-beta", "2.0", "IncomparableVersionError: "),
    ],
)
def test_upgrade_probe_error_is_error_and_not_attempted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: Any,
    latest: Any,
    message: str,
) -> None:
    calls = _stub_upgrade(
        monkeypatch, (_dep(name="lima"),), installed={"lima": installed}, latest={"lima": latest}
    )
    assert vrg_host.main(["upgrade", "lima"]) == 2
    assert calls == []
    out = capsys.readouterr().out
    assert f"ERROR    lima: probe-error ({message}" in out and "not upgraded" in out


@pytest.mark.parametrize(
    ("after", "message"),
    [
        (
            hm.ProbeError("gh: garbled"),
            "ERROR    gh: upgrade ran but the installed version could not be re-probed "
            "(ProbeError: gh: garbled)",
        ),
        (None, "ERROR    gh: upgrade ran but gh is no longer detected as installed"),
    ],
)
@pytest.mark.parametrize("latest", ["2.0", hm.UNMANAGED])
def test_upgrade_reprobe_failure_is_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    after: Any,
    message: str,
    latest: Any,
) -> None:
    calls = _stub_upgrade(
        monkeypatch, (_dep(name="gh"),), latest={"gh": latest}, after={"gh": after}
    )
    assert vrg_host.main(["upgrade", "gh"]) == 2
    assert calls == ["gh"]
    assert capsys.readouterr().out.strip() == message


def test_upgrade_failure_with_no_latest_is_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    errors: dict[str, Exception] = {"vt": hm.UpgradeFailedError("vt: upgrade command failed")}
    _stub_upgrade(monkeypatch, (_dep(name="vt"),), errors, latest={"vt": hm.UNMANAGED})
    assert vrg_host.main(["upgrade", "vt"]) == 2
    assert "FAILED   vt: vt: upgrade command failed" in capsys.readouterr().out


# -- end to end against the live-host captures (#3261) ---------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "host"

# What each stub tool prints, per exact argv (after the tool name), for the
# fixtures captured (#3261) or recorded (#3300) on the maintainer's Mac on
# 2026-10-16. Any other argv exits 1. git, bash and claude-code have only their
# installed versions recorded, so their latest probes fail here (latest-unknown).
_HOST_TOOLS: dict[str, dict[str, str]] = {
    "brew": {
        "list --formula --versions uv": "brew-list-versions-uv.txt",
        "info --json=v2 --formula uv": "brew-info-formula-uv.json",
        "list --formula --versions gh": "brew-list-versions-gh.txt",
        "info --json=v2 --formula gh": "brew-info-formula-gh.json",
        "info --json=v2 --cask docker-desktop": "brew-info-cask-docker-desktop.json",
        "list --formula --versions git": "brew-list-versions-git.txt",
        "list --formula --versions bash": "brew-list-versions-bash.txt",
        "list --cask --versions claude-code": "brew-list-versions-cask-claude-code.txt",
    },
    "pkgutil": {
        "--pkg-info=com.apple.pkg.CLTools_Executables": "pkgutil-pkg-info-clt-version-line.txt",
    },
    "defaults": {
        "read /Applications/Docker.app/Contents/Info.plist CFBundleShortVersionString": (
            "docker-desktop-info-plist-version.txt"
        ),
    },
    "gcloud": {
        "version --format=value(core)": "gcloud-version-core.txt",
        "components list --filter=id=core --format=value(latest_version_string)": (
            "gcloud-components-list-core-latest.txt"
        ),
    },
    "uv": {"tool list": "uv-tool-list.txt", "tool list --outdated": "uv-tool-list-outdated.txt"},
    "sw_vers": {"-productVersion": "sw-vers-product-version.txt"},
    "softwareupdate": {"--list": "softwareupdate-list-minor-and-major-available.txt"},
}

# lima, opentofu and git-cliff have no captured probe output (only their rows
# in vrg-host-status-before-3261.txt), so they are left out of this harness.
_UNCAPTURED = frozenset({"lima", "opentofu", "git-cliff"})


def _install_captured_host(bin_dir: Path) -> None:
    """Install stub host tools on *bin_dir* that replay the captured fixtures."""
    bin_dir.mkdir()
    python = bin_dir / "gcloud-python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")  # imports numpy
    python.chmod(0o755)
    for tool, routes in _HOST_TOOLS.items():
        arms = [
            f"  {shlex.quote(args)}) cat {shlex.quote(str(_FIXTURES / name))} ;;"
            for args, name in routes.items()
        ]
        if tool == "gcloud":
            # gcloud's SDK-version notice goes to stderr (CONSTRUCTED wording;
            # the capture recorded only stdout). It must never reach a parser.
            arms[1] = arms[1].replace(" ;;", "; echo 'Updates are available.' >&2 ;;")
            arms.append(
                f"  {shlex.quote('info --format=value(basic.python_location)')}) "
                f"echo {shlex.quote(str(python))} ;;"
            )
        script = "\n".join(
            ["#!/usr/bin/env bash", 'case "$*" in', *arms, "  *) exit 1 ;;", "esac", ""]
        )
        path = bin_dir / tool
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _row(line: str) -> list[str]:
    return line.split()


def _rows_by_name(lines: list[str]) -> dict[str, str]:
    """Map each dependency's name to its status table row."""
    return {line.split("]", 1)[1].split()[0]: line for line in lines if line.startswith("[")}


def test_status_classifies_the_captured_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drive `vrg-host status` through the real probes and the live-host captures."""
    from vergil_tooling.lib import host_deps

    deps = tuple(d for d in host_deps.load_manifest() if d.name not in _UNCAPTURED)
    monkeypatch.setattr(vrg_host.host_deps, "load_manifest", lambda: deps)
    bin_dir = tmp_path / "bin"
    _install_captured_host(bin_dir)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))

    assert vrg_host.main(["status"]) == 1  # overall WARN
    lines = capsys.readouterr().out.splitlines()
    rows = _rows_by_name(lines)

    # Entries #3261 changed: uv via Homebrew, Docker Desktop via its Info.plist.
    assert _row(rows["uv"]) == _row("[WARN ] uv brew-formula 0.11.8 -> 0.13.0 behind (drift)")
    assert _row(rows["docker-desktop"]) == _row(
        "[WARN ] docker-desktop brew-cask 4.71.0 -> 4.94.0 behind (drift)"
    )
    # Entries #3261 left alone render exactly as the real host printed them.
    before = (_FIXTURES / "vrg-host-status-before-3261.txt").read_text(encoding="utf-8")
    real = _rows_by_name(before.splitlines())
    for name in ("gh", "gcloud", "vergil-tooling"):
        assert rows[name] == real[name], name
    # macos reports every pending level (#3269); before, only the newest
    # (27.0.1) showed and the same-major 26.7.1 was hidden.
    assert real["macos"] == (
        "[WARN ] macos            custom              26.5.2 -> 27.0.1       behind (drift)"
    )
    assert rows["macos"] == (
        "[WARN ] macos            custom              26.5.2 -> 27.0.1       "
        "behind (drift): minor 26.7.1, major 27.0.1"
    )
    # #3300: Homebrew's git and bash and the claude-code cask read from their
    # recorded installs; no latest was recorded for them.
    assert _row(rows["git"]) == _row("[WARN ] git brew-formula 2.54.0 -> unknown latest-unknown")
    assert _row(rows["bash"]) == _row("[WARN ] bash brew-formula 5.3.9 -> unknown latest-unknown")
    assert _row(rows["claude-code"]) == _row(
        "[WARN ] claude-code brew-cask 2.1.280 -> unknown latest-unknown"
    )
    # The captured Software Update listing has no CLT item: the CLT is current,
    # its 26.6.0.0.1781586589 receipt read as 26.6.0.
    assert _row(rows["xcode-clt"]) == _row("[OK   ] xcode-clt custom 26.6.0 -> 26.6.0 current")
    assert "          check numpy-in-interpreter: satisfied" in lines
    assert lines[-1] == f"overall: WARN ({len(deps)} dependencies)"


# --- vrg-host plugins (epic vergil-project/.github#760, spec §6) ---------------

_PLUGIN_SETTINGS = {
    "extraKnownMarketplaces": {
        "vergil-marketplace": {
            "source": {
                "source": "github",
                "repo": "vergil-project/vergil-claude-plugin",
                "ref": "v2.1",
            }
        }
    },
    "enabledPlugins": {"vergil@vergil-marketplace": True, "paad@paad": True},
}

_REGISTERED_V21 = json.dumps(
    [
        {
            "name": "vergil-marketplace",
            "source": "github",
            "repo": "vergil-project/vergil-claude-plugin",
            "ref": "v2.1",
        }
    ]
)

_INSTALLS = json.dumps(
    [
        {"id": "vergil@vergil-marketplace", "scope": "user", "enabled": True},
        {"id": "paad@paad", "scope": "user", "enabled": True},
    ]
)


class _LocalRun:
    """Stands in for ``claude_marketplace.local_run``; records every argv."""

    def __init__(self, fail_on: tuple[str, ...] = ()) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        cmd = argv[-1]
        if any(f in cmd for f in self.fail_on):
            raise subprocess.CalledProcessError(1, list(argv), stderr="")
        out = ""
        if "marketplace list --json" in cmd:
            out = _REGISTERED_V21
        elif "plugin list --json" in cmd:
            out = _INSTALLS
        return subprocess.CompletedProcess(list(argv), 0, stdout=out, stderr="")


@pytest.fixture
def host_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host ``~/.claude/settings.json`` declaring the marketplace at v2.1."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(_PLUGIN_SETTINGS))
    monkeypatch.setattr(vrg_host, "_host_settings_path", lambda: settings)
    return settings


@pytest.fixture
def local_run(monkeypatch: pytest.MonkeyPatch) -> _LocalRun:
    run = _LocalRun()
    monkeypatch.setattr(vrg_host.claude_marketplace, "local_run", run)
    return run


def test_parse_args_plugins() -> None:
    assert vrg_host.parse_args(["plugins"]).cmd == "plugins"


def test_plugins_refuses_agent(
    monkeypatch: pytest.MonkeyPatch,
    host_claude: Path,
    local_run: _LocalRun,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert vrg_host.main(["plugins"]) == 1
    assert local_run.calls == []
    assert "Agents cannot run vrg-host plugins" in capsys.readouterr().err


def test_plugins_refuses_mixed_signals(
    monkeypatch: pytest.MonkeyPatch, host_claude: Path, local_run: _LocalRun
) -> None:
    # A human mode value cannot talk the guard out of a provisioned agent signal.
    monkeypatch.setenv("VRG_IDENTITY_MODE", "human")
    monkeypatch.setattr(vrg_host.identity_mode, "any_agent_signal", lambda: True)
    assert vrg_host.main(["plugins"]) == 1
    assert local_run.calls == []


def test_plugins_runs_refresh(host_claude: Path, local_run: _LocalRun) -> None:
    assert vrg_host.main(["plugins"]) == 0
    # The host runs bare bash -c commands, with no PATH prefix.
    assert all(c[:2] == ("bash", "-c") for c in local_run.calls)
    assert [c[-1] for c in local_run.calls] == [
        "claude plugin marketplace add vergil-project/vergil-claude-plugin@v2.1",
        "claude plugin marketplace update",
        "claude plugin list --json",
        "claude plugin update paad@paad --scope user",
        "claude plugin update vergil@vergil-marketplace --scope user",
        "claude plugin marketplace list --json",
    ]


def test_plugins_exits_2_naming_each_failure(
    monkeypatch: pytest.MonkeyPatch, host_claude: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _LocalRun(fail_on=("plugin update paad@paad", "plugin update vergil@"))
    monkeypatch.setattr(vrg_host.claude_marketplace, "local_run", run)
    assert vrg_host.main(["plugins"]) == 2
    err = capsys.readouterr().err
    assert "paad@paad (user)" in err
    assert "vergil@vergil-marketplace (user)" in err


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_plugins_exits_2_on_unusable_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_run: _LocalRun,
    content: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = tmp_path / "settings.json"
    if content is not None:
        settings.write_text(content)
    monkeypatch.setattr(vrg_host, "_host_settings_path", lambda: settings)
    assert vrg_host.main(["plugins"]) == 2
    assert local_run.calls == []
    assert str(settings) in capsys.readouterr().err


def test_plugins_exits_2_on_malformed_listing(
    monkeypatch: pytest.MonkeyPatch, host_claude: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        out = '{"not": "a list"}' if "marketplace list --json" in argv[-1] else "[]"
        return subprocess.CompletedProcess(list(argv), 0, stdout=out, stderr="")

    monkeypatch.setattr(vrg_host.claude_marketplace, "local_run", run)
    assert vrg_host.main(["plugins"]) == 2
    assert "MarketplaceError" in capsys.readouterr().err


def test_host_settings_path_is_the_user_settings() -> None:
    assert vrg_host._host_settings_path() == Path.home() / ".claude" / "settings.json"


def test_plugins_exits_2_on_non_utf8_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_run: _LocalRun,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"\xff\xfe")
    monkeypatch.setattr(vrg_host, "_host_settings_path", lambda: settings)
    assert vrg_host.main(["plugins"]) == 2
    assert local_run.calls == []
    assert f"cannot read {settings}" in capsys.readouterr().err
