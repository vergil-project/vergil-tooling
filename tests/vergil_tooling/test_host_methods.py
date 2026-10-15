"""Tests for vergil_tooling.lib.host_methods (install-method handler registry)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib.host_deps import HostDependency, ManifestError


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


class _FakeRun:
    """Stub for ``hm._run``: maps an argv tuple to its stdout (or None = failure)."""

    def __init__(self, responses: dict[tuple[str, ...], str | None]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str | None:
        self.calls.append(cmd)
        if tuple(cmd) not in self.responses:
            raise AssertionError(f"unexpected command {cmd}")
        return self.responses[tuple(cmd)]


def _stub(
    monkeypatch: pytest.MonkeyPatch, responses: dict[tuple[str, ...], str | None]
) -> _FakeRun:
    fake = _FakeRun(responses)
    monkeypatch.setattr(hm, "_run", fake)
    return fake


# -- plan Task 2 contract ----------------------------------------------------------


def test_latest_unmanaged_returns_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {})
    assert hm.latest_version(_dep(latest="unmanaged")) is hm.UNMANAGED
    assert fake.calls == []
    assert repr(hm.UNMANAGED) == "UNMANAGED"


def test_installed_version_none_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hm, "_run", lambda cmd: None)
    assert hm.installed_version(_dep()) is None


def test_manual_upgrade_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {})
    with pytest.raises(hm.UpgradeUnsupported, match="gh"):
        hm.upgrade(_dep(upgrade="manual"))
    assert fake.calls == []
    assert hm.UpgradeUnsupported is hm.UpgradeUnsupportedError


# -- _run seam ---------------------------------------------------------------------


def test_run_returns_stripped_stdout_with_closed_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="  1.2.3\n", stderr="")

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    assert hm._run(["tool", "--version"]) == "1.2.3"
    assert seen["cmd"] == ["tool", "--version"]
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["check"] is True


def test_run_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    assert hm._run(["tool"]) is None


def test_run_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    assert hm._run(["no-such-tool"]) is None


@pytest.mark.parametrize("escalator", ["sudo", "doas", "su", "pkexec"])
def test_run_refuses_privilege_escalation(monkeypatch: pytest.MonkeyPatch, escalator: str) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not execute")

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="escalate privilege"):
        hm._run([escalator, "brew", "upgrade"])


def test_run_refuses_empty_command() -> None:
    with pytest.raises(ValueError, match="empty command"):
        hm._run([])


# -- brew ----------------------------------------------------------------------------


def test_brew_formula_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "list", "--versions", "gh"): "gh 2.40.0 2.40.1"})
    assert hm.installed_version(_dep()) == "2.40.1"


def test_brew_cask_installed_uses_cask_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "list", "--cask", "--versions", "app"): "app 4.1"})
    assert hm.installed_version(_dep(name="app", method="brew-cask")) == "4.1"


def test_brew_installed_empty_output_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "list", "--versions", "gh"): ""})
    assert hm.installed_version(_dep()) is None


@pytest.mark.parametrize("output", ["gh", "other 1.0"])
def test_brew_installed_unparseable_raises(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    _stub(monkeypatch, {("brew", "list", "--versions", "gh"): output})
    with pytest.raises(hm.ProbeError, match="brew list"):
        hm.installed_version(_dep())


def test_brew_formula_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    info = json.dumps({"formulae": [{"versions": {"stable": "2.41.0"}}], "casks": []})
    _stub(monkeypatch, {("brew", "info", "--json=v2", "gh"): info})
    assert hm.latest_version(_dep()) == "2.41.0"


def test_brew_cask_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    info = json.dumps({"formulae": [], "casks": [{"version": "4.2"}]})
    _stub(monkeypatch, {("brew", "info", "--json=v2", "--cask", "app"): info})
    assert hm.latest_version(_dep(name="app", method="brew-cask")) == "4.2"


@pytest.mark.parametrize(
    "output",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"formulae": []}),
        json.dumps({"formulae": ["x"]}),
        json.dumps({"formulae": [{"versions": "x"}]}),
        json.dumps({"formulae": [{"versions": {"stable": ""}}]}),
        json.dumps({"formulae": [{"versions": {"stable": None}}]}),
    ],
)
def test_brew_latest_unknown_when_probe_fails_or_unparseable(
    monkeypatch: pytest.MonkeyPatch, output: str | None
) -> None:
    _stub(monkeypatch, {("brew", "info", "--json=v2", "gh"): output})
    assert hm.latest_version(_dep()) is None


def test_brew_formula_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {("brew", "upgrade", "gh"): ""})
    hm.upgrade(_dep())
    assert fake.calls == [["brew", "upgrade", "gh"]]


def test_brew_cask_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {("brew", "upgrade", "--cask", "app"): ""})
    hm.upgrade(_dep(name="app", method="brew-cask"))
    assert fake.calls == [["brew", "upgrade", "--cask", "app"]]


def test_upgrade_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "upgrade", "gh"): None})
    with pytest.raises(hm.UpgradeFailedError, match="brew upgrade gh"):
        hm.upgrade(_dep())


# -- uv-tool -------------------------------------------------------------------------

_UV_LIST = "ruff v0.6.0\n- ruff\nvergil-tooling v2.1.240\n- vrg-commit\n- vrg-git\n"


def _uv(**kw: Any) -> HostDependency:
    return _dep(name="vergil-tooling", method="uv-tool", **kw)


def test_uv_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("uv", "tool", "list"): _UV_LIST})
    assert hm.installed_version(_uv()) == "2.1.240"


def test_uv_installed_absent_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("uv", "tool", "list"): "ruff v0.6.0\n- ruff\n"})
    assert hm.installed_version(_uv()) is None


def test_uv_installed_probe_failure_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("uv", "tool", "list"): None})
    assert hm.installed_version(_uv()) is None


@pytest.mark.parametrize("line", ["vergil-tooling", "vergil-tooling 2.1", "vergil-tooling v"])
def test_uv_installed_unparseable_raises(monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    _stub(monkeypatch, {("uv", "tool", "list"): line})
    with pytest.raises(hm.ProbeError, match="uv tool list"):
        hm.installed_version(_uv())


def test_uv_latest_when_outdated(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "vergil-tooling v2.1.240 [latest: 2.1.241]\n- vrg-commit\n"
    _stub(monkeypatch, {("uv", "tool", "list", "--outdated"): out})
    assert hm.latest_version(_uv()) == "2.1.241"


def test_uv_latest_when_current_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        {("uv", "tool", "list", "--outdated"): "", ("uv", "tool", "list"): _UV_LIST},
    )
    assert hm.latest_version(_uv()) == "2.1.240"


def test_uv_latest_unknown_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        {("uv", "tool", "list", "--outdated"): "", ("uv", "tool", "list"): "ruff v0.6.0\n"},
    )
    assert hm.latest_version(_uv()) is None


def test_uv_latest_unknown_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("uv", "tool", "list", "--outdated"): None})
    assert hm.latest_version(_uv()) is None


@pytest.mark.parametrize("line", ["vergil-tooling v2.1.240", "vergil-tooling v2.1.240 [latest: ]"])
def test_uv_outdated_line_without_latest_falls_back(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    _stub(
        monkeypatch,
        {("uv", "tool", "list", "--outdated"): line, ("uv", "tool", "list"): _UV_LIST},
    )
    assert hm.latest_version(_uv()) == "2.1.240"


def test_uv_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {("uv", "tool", "upgrade", "vergil-tooling"): "done"})
    hm.upgrade(_uv())
    assert fake.calls == [["uv", "tool", "upgrade", "vergil-tooling"]]


# -- custom --------------------------------------------------------------------------

_PROBES = {
    "installed_version": ["gcloud", "version", "--format=value(core)"],
    "latest_version": ["gcloud-latest"],
    "upgrade": ["gcloud", "components", "update", "--quiet"],
}


def _custom(**kw: Any) -> HostDependency:
    return _dep(name="gcloud", method="custom", probes=_PROBES, **kw)


def test_custom_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["installed_version"]): "520.0.0"})
    assert hm.installed_version(_custom()) == "520.0.0"


def test_custom_installed_probe_failure_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["installed_version"]): None})
    assert hm.installed_version(_custom()) is None


def test_custom_installed_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["installed_version"]): ""})
    with pytest.raises(hm.ProbeError, match="printed nothing"):
        hm.installed_version(_custom())


def test_custom_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["latest_version"]): "521.0.0"})
    assert hm.latest_version(_custom()) == "521.0.0"


@pytest.mark.parametrize("output", [None, ""])
def test_custom_latest_unknown(monkeypatch: pytest.MonkeyPatch, output: str | None) -> None:
    _stub(monkeypatch, {tuple(_PROBES["latest_version"]): output})
    assert hm.latest_version(_custom()) is None


def test_custom_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {tuple(_PROBES["upgrade"]): ""})
    hm.upgrade(_custom())
    assert fake.calls == [_PROBES["upgrade"]]


def test_custom_upgrade_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["upgrade"]): None})
    with pytest.raises(hm.UpgradeFailedError, match="gcloud"):
        hm.upgrade(_custom())


@pytest.mark.parametrize(
    ("func", "key"),
    [
        (hm.installed_version, "installed_version"),
        (hm.latest_version, "latest_version"),
        (hm.upgrade, "upgrade"),
    ],
)
def test_custom_missing_probe_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, func: Any, key: str
) -> None:
    fake = _stub(monkeypatch, {})
    with pytest.raises(ManifestError, match=f"requires probe '{key}'"):
        func(_dep(name="gcloud", method="custom"))
    assert fake.calls == []


# -- unknown method ------------------------------------------------------------------


@pytest.mark.parametrize("func", [hm.installed_version, hm.latest_version, hm.upgrade])
def test_unknown_method_fails_loudly(monkeypatch: pytest.MonkeyPatch, func: Any) -> None:
    fake = _stub(monkeypatch, {})
    with pytest.raises(ManifestError, match="unknown method 'pipx'"):
        func(_dep(method="pipx"))
    assert fake.calls == []
