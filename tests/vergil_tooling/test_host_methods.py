"""Tests for vergil_tooling.lib.host_methods (install-method handler registry)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.lib import host_deps
from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib import host_versions as hv
from vergil_tooling.lib.host_deps import HostDependency, ManifestError

# Probe-output fixtures. Most were captured on a live macOS host (#3261); the
# few that could not be are CONSTRUCTED and labelled so. See
# fixtures/host/README.md for each fixture's provenance.
_FIXTURES = Path(__file__).parent / "fixtures" / "host"


def _fixture(name: str) -> str:
    """Return a fixture as ``_run`` would: stdout with surrounding whitespace stripped."""
    return (_FIXTURES / name).read_text(encoding="utf-8").strip()


def _manifest_dep(name: str) -> HostDependency:
    return {d.name: d for d in host_deps.load_manifest()}[name]


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


def test_run_returns_stdout_only_never_stderr() -> None:
    # gcloud prints an SDK-version notice on stderr next to the version on
    # stdout (#3261); only stdout may reach a parser. A real child process.
    code = "import sys; print('2026.10.09'); print('Updates are available', file=sys.stderr)"
    assert hm._run([sys.executable, "-c", code]) == "2026.10.09"


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
    _stub(monkeypatch, {("brew", "list", "--formula", "--versions", "gh"): "gh 2.40.0 2.40.1"})
    assert hm.installed_version(_dep()) == "2.40.1"


def test_brew_cask_installed_uses_cask_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "list", "--cask", "--versions", "app"): "app 4.1"})
    assert hm.installed_version(_dep(name="app", method="brew-cask")) == "4.1"


def test_brew_installed_empty_output_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "list", "--formula", "--versions", "gh"): ""})
    assert hm.installed_version(_dep()) is None


@pytest.mark.parametrize("output", ["gh", "other 1.0"])
def test_brew_installed_unparseable_raises(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    _stub(monkeypatch, {("brew", "list", "--formula", "--versions", "gh"): output})
    with pytest.raises(hm.ProbeError, match="brew list"):
        hm.installed_version(_dep())


def test_brew_formula_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    info = json.dumps({"formulae": [{"versions": {"stable": "2.41.0"}}], "casks": []})
    _stub(monkeypatch, {("brew", "info", "--json=v2", "--formula", "gh"): info})
    assert hm.latest_version(_dep()) == "2.41.0"


def test_brew_cask_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    info = json.dumps({"formulae": [], "casks": [{"version": "4.2"}]})
    _stub(monkeypatch, {("brew", "info", "--json=v2", "--cask", "app"): info})
    assert hm.latest_version(_dep(name="app", method="brew-cask")) == "4.2"


def test_brew_latest_unknown_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("brew", "info", "--json=v2", "--formula", "gh"): None})
    assert hm.latest_version(_dep()) is None


@pytest.mark.parametrize(
    ("output", "match"),
    [
        ("", "not JSON"),
        ("not json", "not JSON"),
        ("[]", "no formulae entry"),
        (json.dumps({"formulae": []}), "no formulae entry"),
        (json.dumps({"formulae": ["x"]}), "no formulae entry"),
        (json.dumps({"formulae": [{"versions": "x"}]}), "has no version"),
        (json.dumps({"formulae": [{"versions": {"stable": ""}}]}), "has no version"),
        (json.dumps({"formulae": [{"versions": {"stable": None}}]}), "has no version"),
        (json.dumps({"formulae": [{"versions": {"stable": "1.0"}, "revision": "1"}]}), "revision"),
        (json.dumps({"formulae": [{"versions": {"stable": "1.0"}, "revision": True}]}), "revision"),
        (json.dumps({"formulae": [{"versions": {"stable": "1.0"}, "revision": -1}]}), "revision"),
        (json.dumps({"formulae": [{"versions": {"stable": "HEAD-abc"}}]}), "no version"),
    ],
)
def test_brew_latest_unparseable_answer_is_probe_error(
    monkeypatch: pytest.MonkeyPatch, output: str, match: str
) -> None:
    # brew info answered, so this is a probe-error (ERROR), never latest-unknown.
    _stub(monkeypatch, {("brew", "info", "--json=v2", "--formula", "gh"): output})
    with pytest.raises(hm.ProbeError, match=match):
        hm.latest_version(_dep())


def test_brew_formula_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {("brew", "upgrade", "gh"): ""})
    hm.upgrade(_dep())
    assert fake.calls == [["brew", "upgrade", "gh"]]


_CASK_INFO = ("brew", "info", "--json=v2", "--cask", "app")


def _cask_info(auto_updates: object) -> str:
    return json.dumps({"formulae": [], "casks": [{"version": "4.2", "auto_updates": auto_updates}]})


def test_brew_cask_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(
        monkeypatch, {_CASK_INFO: _cask_info(None), ("brew", "upgrade", "--cask", "app"): ""}
    )
    hm.upgrade(_dep(name="app", method="brew-cask"))
    assert fake.calls == [list(_CASK_INFO), ["brew", "upgrade", "--cask", "app"]]


@pytest.mark.parametrize(
    ("info", "match"),
    [
        (_cask_info(True), "auto_updates \\(the app updates itself\\)"),
        (None, "cannot read cask metadata"),
        ("not json", "not JSON"),
        (_cask_info("yes"), "not a boolean"),
    ],
)
def test_brew_cask_upgrade_refuses_self_updating_or_unreadable_cask(
    monkeypatch: pytest.MonkeyPatch, info: str | None, match: str
) -> None:
    # A self-updating cask is never pushed through brew (brew would silently
    # skip it without --greedy); unreadable metadata is a failure, not a guess.
    fake = _stub(monkeypatch, {_CASK_INFO: info})
    with pytest.raises(hm.UpgradeFailedError, match=match):
        hm.upgrade(_dep(name="app", method="brew-cask"))
    assert fake.calls == [list(_CASK_INFO)]  # brew upgrade never ran


@pytest.mark.parametrize("method", ["brew-formula", "brew-cask", "uv-tool"])
def test_builtin_method_installed_probe_override(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    # A self-updating app reads its installed version from its own probe, never
    # from the package manager's (stale) record.
    dep = _dep(name="app", method=method, probes={"installed_version": ["app-version"]})
    fake = _stub(monkeypatch, {("app-version",): "App 4.71.0"})
    assert hm.installed_version(dep) == "4.71.0"
    assert fake.calls == [["app-version"]]


def test_builtin_method_installed_probe_override_failure_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dep = _dep(name="app", method="brew-cask", probes={"installed_version": ["app-version"]})
    _stub(monkeypatch, {("app-version",): None})
    assert hm.installed_version(dep) is None


def test_builtin_method_installed_probe_override_empty_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dep = _dep(name="app", method="brew-cask", probes={"installed_version": ["app-version"]})
    _stub(monkeypatch, {("app-version",): ""})
    with pytest.raises(hm.ProbeError, match="printed nothing"):
        hm.installed_version(dep)


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


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("vergil-tooling v2.1.240", "has no latest"),
        ("vergil-tooling v2.1.240 [latest: ]", "no version"),
        ("vergil-tooling v2.1.240 [latest: soon]", "no version"),
    ],
)
def test_uv_outdated_listed_without_readable_latest_raises(
    monkeypatch: pytest.MonkeyPatch, line: str, match: str
) -> None:
    # Listed as outdated but unreadable: never silently read as current.
    _stub(monkeypatch, {("uv", "tool", "list", "--outdated"): line})
    with pytest.raises(hm.ProbeError, match=match):
        hm.latest_version(_uv())


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


def test_custom_latest_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["latest_version"]): None})
    assert hm.latest_version(_custom()) is None


@pytest.mark.parametrize(("output", "match"), [("", "printed nothing"), ("no idea", "no version")])
def test_custom_latest_unreadable_answer_raises(
    monkeypatch: pytest.MonkeyPatch, output: str, match: str
) -> None:
    _stub(monkeypatch, {tuple(_PROBES["latest_version"]): output})
    with pytest.raises(hm.ProbeError, match=match):
        hm.latest_version(_custom())


def test_custom_versions_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        {
            tuple(_PROBES["installed_version"]): "tool version 520.0.0 (2025-01-01)",
            tuple(_PROBES["latest_version"]): "v521.0.0",
        },
    )
    assert hm.installed_version(_custom()) == "520.0.0"
    assert hm.latest_version(_custom()) == "521.0.0"


def test_custom_installed_unreadable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {tuple(_PROBES["installed_version"]): "a 1.0 b 2.0"})
    with pytest.raises(hm.ProbeError, match="several versions"):
        hm.installed_version(_custom())


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


# -- fixture-backed parsing ---------------------------------------------------------
# Unless a test says otherwise, these fixtures were captured on a live macOS
# host on 2026-10-16 (#3261); see fixtures/host/README.md.


def test_fixture_brew_list_versions() -> None:
    assert hm._parse_brew_list_versions("gh", _fixture("brew-list-versions-gh.txt")) == "2.102.0"


def test_fixture_brew_list_picks_newest_keg_from_unsorted_list() -> None:
    # Real multi-keg output is NOT sorted (3.14.6 is listed before 3.14.4_1),
    # so the newest keg is chosen by the comparator, never by position.
    out = _fixture("brew-list-versions-multi-keg-python314.txt")
    assert out.split()[1:] == ["3.14.6", "3.14.4_1"]
    assert hm._parse_brew_list_versions("python@3.14", out) == "3.14.6"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # Constructed: order independence, and a `_N` rebuild beating its base.
        ("python@3.14 3.14.4_1 3.14.6", "3.14.6"),
        ("python@3.14 3.14.6_1 3.14.6", "3.14.6_1"),
        ("python@3.14 3.14.6 3.14.6_2 3.14.6_1", "3.14.6_2"),
    ],
)
def test_brew_list_newest_keg_includes_rebuild_suffix(output: str, expected: str) -> None:
    assert hm._parse_brew_list_versions("python@3.14", output) == expected


def test_brew_list_unreadable_keg_raises() -> None:
    with pytest.raises(hm.ProbeError, match="no version"):
        hm._parse_brew_list_versions("gh", "gh 2.40.0 HEAD-1a2b3c")


def test_fixture_brew_info_formula() -> None:
    out = _fixture("brew-info-formula-gh.json")
    assert hm._parse_brew_info_latest("gh", "brew-formula", out) == "2.102.0"


def test_fixture_brew_info_revision_is_suffixed_and_installed_kegs_are_behind() -> None:
    info = _fixture("brew-info-formula-python314.json")
    formula = json.loads(info)["formulae"][0]
    assert (formula["versions"]["stable"], formula["revision"]) == ("3.14.8", 2)
    latest = hm._parse_brew_info_latest("python@3.14", "brew-formula", info)
    assert latest == "3.14.8_2"
    listed = _fixture("brew-list-versions-multi-keg-python314.txt")
    # brew info records the same kegs brew list printed.
    assert sorted(k["version"] for k in formula["installed"]) == sorted(listed.split()[1:])
    installed = hm._parse_brew_list_versions("python@3.14", listed)
    assert hv.compare(installed, latest) < 0


def test_brew_revision_only_rebuild_reads_as_drift() -> None:
    # Constructed: the same upstream version at an older revision is behind.
    info = json.dumps({"formulae": [{"versions": {"stable": "3.14.8"}, "revision": 2}]})
    latest = hm._parse_brew_info_latest("python@3.14", "brew-formula", info)
    assert hv.compare("3.14.8_1", latest) < 0
    assert hv.compare("3.14.8_2", latest) == 0


def test_fixture_brew_info_cask_drops_build_suffix() -> None:
    out = _fixture("brew-info-cask-docker-desktop.json")
    cask = json.loads(out)["casks"][0]
    assert (cask["version"], cask["installed"]) == ("4.94.0,241994", "4.71.0,225177")
    assert hm._parse_brew_info_latest("docker-desktop", "brew-cask", out) == "4.94.0"
    assert hm._parse_brew_cask_auto_updates("docker-desktop", out) is True


@pytest.mark.parametrize(("value", "expected"), [(None, False), (False, False), (True, True)])
def test_brew_cask_auto_updates(value: bool | None, expected: bool) -> None:
    out = json.dumps({"formulae": [], "casks": [{"version": "1.0", "auto_updates": value}]})
    assert hm._parse_brew_cask_auto_updates("app", out) is expected


def test_brew_cask_auto_updates_absent_is_false() -> None:
    out = json.dumps({"formulae": [], "casks": [{"version": "1.0"}]})
    assert hm._parse_brew_cask_auto_updates("app", out) is False


def test_brew_cask_auto_updates_not_boolean_raises() -> None:
    out = json.dumps({"formulae": [], "casks": [{"version": "1.0", "auto_updates": "yes"}]})
    with pytest.raises(hm.ProbeError, match="not a boolean"):
        hm._parse_brew_cask_auto_updates("app", out)


def test_fixture_brew_formula_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = _fixture("brew-list-versions-gh.txt")
    info = _fixture("brew-info-formula-gh.json")
    _stub(
        monkeypatch,
        {
            ("brew", "list", "--formula", "--versions", "gh"): listed,
            ("brew", "info", "--json=v2", "--formula", "gh"): info,
        },
    )
    assert hm.installed_version(_dep()) == "2.102.0"
    assert hm.latest_version(_dep()) == "2.102.0"


def test_fixture_uv_tool_list() -> None:
    out = _fixture("uv-tool-list.txt")
    assert hm._parse_uv_tool_installed("vergil-tooling", out) == "2.1.241"
    assert hm._parse_uv_tool_installed("vrg-commit", out) is None  # an entry point, not a tool
    assert hm._parse_uv_tool_installed("ruff", out) is None


def test_fixture_uv_tool_list_outdated_empty() -> None:
    # The real host printed nothing (exit 0): nothing outdated.
    out = _fixture("uv-tool-list-outdated.txt")
    assert out == ""
    assert hm._parse_uv_tool_outdated("vergil-tooling", out) is None


def test_fixture_uv_tool_latest_from_empty_outdated_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        {
            ("uv", "tool", "list", "--outdated"): _fixture("uv-tool-list-outdated.txt"),
            ("uv", "tool", "list"): _fixture("uv-tool-list.txt"),
        },
    )
    assert hm.latest_version(_uv()) == "2.1.241"


def test_fixture_uv_tool_list_outdated_listed() -> None:
    # CONSTRUCTED fixture: no real host has listed an outdated tool yet.
    out = _fixture("uv-tool-list-outdated-listed.txt")
    assert hm._parse_uv_tool_outdated("ruff", out) == "0.7.2"
    assert hm._parse_uv_tool_outdated("vergil-tooling", out) is None


def test_fixture_gh_version_prose_matches_brew_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §8: gh's own "gh version 2.102.0 (...)" output equals brew's bare "2.102.0"."""
    _stub(monkeypatch, {("gh", "--version"): _fixture("gh-version.txt")})
    dep = _dep(method="custom", probes={"installed_version": ["gh", "--version"]})
    installed = hm.installed_version(dep)
    assert installed == "2.102.0"
    brew = hm._parse_brew_list_versions("gh", _fixture("brew-list-versions-gh.txt"))
    assert hv.compare(installed, brew) == 0


def test_fixture_uv_version_shows_homebrew_install() -> None:
    # The evidence for managing uv as a brew formula (#3261).
    out = _fixture("uv-version.txt")
    assert "(Homebrew " in out
    brew = hm._parse_brew_list_versions("uv", _fixture("brew-list-versions-uv.txt"))
    assert hv.normalize(out) == brew == "0.11.8"


def test_fixture_docker_cli_version_is_not_the_desktop_version() -> None:
    # Why the entry tracks Docker Desktop's Info.plist, not `docker --version`.
    assert hv.normalize(_fixture("docker-version.txt")) == "29.4.1"
    assert hv.normalize(_fixture("docker-desktop-info-plist-version.txt")) == "4.71.0"


# -- the curated manifest's entries, against fixtures --------------------------------


def _probe_stub(
    monkeypatch: pytest.MonkeyPatch, dep: HostDependency, outputs: dict[str, str]
) -> _FakeRun:
    return _stub(monkeypatch, {tuple(dep.probes[key]): value for key, value in outputs.items()})


def test_manifest_uv_is_a_brew_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    uv = _manifest_dep("uv")
    assert (uv.method, uv.upgrade, uv.latest, uv.probes) == ("brew-formula", "auto", "probe", {})
    listed, info = _fixture("brew-list-versions-uv.txt"), _fixture("brew-info-formula-uv.json")
    fake = _stub(
        monkeypatch,
        {
            ("brew", "list", "--formula", "--versions", "uv"): listed,
            ("brew", "info", "--json=v2", "--formula", "uv"): info,
            ("brew", "upgrade", "uv"): "",
        },
    )
    installed, latest = hm.installed_version(uv), hm.latest_version(uv)
    assert (installed, latest) == ("0.11.8", "0.13.0")  # Homebrew stable, not upstream 0.13.1
    assert isinstance(installed, str)
    assert isinstance(latest, str)
    assert hv.compare(installed, latest) < 0
    hm.upgrade(uv)
    assert fake.calls[-1] == ["brew", "upgrade", "uv"]  # never `uv self update`


def test_manifest_gcloud_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    gcloud = _manifest_dep("gcloud")
    _probe_stub(
        monkeypatch,
        gcloud,
        {
            "installed_version": _fixture("gcloud-version-core.txt"),
            "latest_version": _fixture("gcloud-components-list-core-latest.txt"),
        },
    )
    assert hm.installed_version(gcloud) == "2026.09.25"
    assert hm.latest_version(gcloud) == "2026.10.09"
    assert "--greedy" in gcloud.why  # the never-`brew upgrade --greedy` warning


def test_manifest_docker_desktop_tracks_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _manifest_dep("docker-desktop")
    assert (docker.method, docker.upgrade, docker.latest) == ("brew-cask", "manual", "probe")
    fake = _stub(
        monkeypatch,
        {
            tuple(docker.probes["installed_version"]): _fixture(
                "docker-desktop-info-plist-version.txt"
            ),
            ("brew", "info", "--json=v2", "--cask", "docker-desktop"): _fixture(
                "brew-info-cask-docker-desktop.json"
            ),
        },
    )
    installed, latest = hm.installed_version(docker), hm.latest_version(docker)
    assert (installed, latest) == ("4.71.0", "4.94.0")
    assert isinstance(installed, str)
    assert isinstance(latest, str)
    assert hv.compare(installed, latest) < 0
    assert fake.calls[0][:2] == ["defaults", "read"]
    assert not any(call[:2] == ["brew", "list"] for call in fake.calls)  # never the stale record
    with pytest.raises(hm.UpgradeUnsupportedError):
        hm.upgrade(docker)


def test_manifest_vergil_tooling_latest_is_unmanaged(monkeypatch: pytest.MonkeyPatch) -> None:
    vt = _manifest_dep("vergil-tooling")
    fake = _stub(monkeypatch, {("uv", "tool", "list"): _fixture("uv-tool-list.txt")})
    assert hm.installed_version(vt) == "2.1.241"
    assert hm.latest_version(vt) is hm.UNMANAGED
    assert fake.calls == [["uv", "tool", "list"]]  # never asks uv's index feed


def test_manifest_macos_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    macos = _manifest_dep("macos")
    _probe_stub(monkeypatch, macos, {"installed_version": _fixture("sw-vers-product-version.txt")})
    assert hm.installed_version(macos) == "26.5.2"
    with pytest.raises(hm.UpgradeUnsupportedError):
        hm.upgrade(macos)


def test_parse_offers_reads_one_bare_version_per_line() -> None:
    assert hm._parse_offers("macos", "26.7.1\nv27.0.1") == hm.Offers(("26.7.1", "27.0.1"))


@pytest.mark.parametrize(
    "output",
    [
        "26.7.1\n\n27.0.1",  # a blank line is not silently skipped
        "26.7.1\nmacOS Tahoe 26.7.1",  # prose is never fished for a version
        "26.7.1 (a)",
    ],
)
def test_parse_offers_unreadable_line_raises(output: str) -> None:
    with pytest.raises(hm.ProbeError, match="unreadable offer"):
        hm._parse_offers("macos", output)


def _levels_dep() -> HostDependency:
    return _dep(
        name="macos",
        method="custom",
        upgrade="manual",
        probes={"installed_version": ["sw_vers"], "latest_versions": ["offers"]},
    )


def test_custom_latest_versions_returns_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _stub(monkeypatch, {("offers",): "26.7.1\n27.0.1"})
    assert hm.latest_version(_levels_dep()) == hm.Offers(("26.7.1", "27.0.1"))
    assert fake.calls == [["offers"]]


def test_custom_latest_versions_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("offers",): None})
    assert hm.latest_version(_levels_dep()) is None


def test_custom_latest_versions_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {("offers",): ""})
    with pytest.raises(hm.ProbeError, match="latest_versions probe printed nothing"):
        hm.latest_version(_levels_dep())


def _fake_macos_tools(
    bin_dir: Path, softwareupdate_fixture: str | None, *, exit_code: int = 0
) -> None:
    """Install stub ``softwareupdate`` / ``sw_vers`` scripts serving fixtures."""
    bin_dir.mkdir()
    if softwareupdate_fixture is None:
        body = f"exit {exit_code}\n"
    else:
        body = f"cat '{_FIXTURES / softwareupdate_fixture}'\nexit {exit_code}\n"
    sw_vers = _FIXTURES / "sw-vers-product-version.txt"
    scripts = {"softwareupdate": body, "sw_vers": f"cat '{sw_vers}'\n"}
    for name, script in scripts.items():
        path = bin_dir / name
        path.write_text(f"#!/usr/bin/env bash\n{script}", encoding="utf-8")
        path.chmod(0o755)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        # Captured: 26.7.1 (same major) and 27.0.1 (next major) plus non-OS
        # items; every macOS offer is reported, in the order listed.
        ("softwareupdate-list-minor-and-major-available.txt", ("26.7.1", "27.0.1")),
        # Constructed: a single same-minor (patch) offer.
        ("softwareupdate-list-patch-only.txt", ("26.5.3",)),
        # Constructed: nothing pending / only a non-OS update -> installed.
        ("softwareupdate-list-none.txt", ("26.5.2",)),
        ("softwareupdate-list-non-macos-only.txt", ("26.5.2",)),
    ],
)
def test_manifest_macos_latest_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture: str, expected: tuple[str, ...]
) -> None:
    """Run the manifest's real softwareupdate-parsing probe against fixtures."""
    bin_dir = tmp_path / "bin"
    _fake_macos_tools(bin_dir, fixture)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    assert hm.latest_version(_manifest_dep("macos")) == hm.Offers(expected)


@pytest.mark.parametrize(
    ("title", "fragment"),
    [
        # Constructed: a macOS title whose Version is not a bare version.
        ("Title: macOS Tahoe 26.7.1 (a), Version: 26.7.1 (a), Size: 1KiB, ", "'26.7.1 (a)'"),
        # Constructed: a macOS title with no Version field at all.
        ("Title: macOS Tahoe 26.7.1, Size: 1KiB, ", "unreadable macOS offer"),
        # Constructed: a blank Version (never silently dropped as an empty line).
        ("Title: macOS Tahoe, Version:  , Size: 1KiB, ", "unreadable macOS offer"),
    ],
)
def test_manifest_macos_unreadable_offer_is_a_probe_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, title: str, fragment: str
) -> None:
    """A macOS offer the probe cannot read is a loud probe error, never dropped."""
    listing = tmp_path / "softwareupdate.txt"
    listing.write_text(
        "Software Update found the following new or updated software:\n"
        "* Label: macOS 27.0.1-26A434\n"
        "\tTitle: macOS 27.0.1, Version: 27.0.1, Size: 1KiB, Recommended: YES, \n"
        "* Label: macOS-odd\n"
        f"\t{title}\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    _fake_macos_tools(bin_dir, str(listing))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    with pytest.raises(hm.ProbeError, match="unreadable offer") as exc:
        hm.latest_version(_manifest_dep("macos"))
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    ("fixture", "exit_code"),
    [(None, 1), ("sw-vers-product-version.txt", 0)],
)
def test_manifest_macos_latest_unknown_when_unrecognized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture: str | None, exit_code: int
) -> None:
    """A failing or unrecognized softwareupdate is latest-unknown, never current."""
    bin_dir = tmp_path / "bin"
    _fake_macos_tools(bin_dir, fixture, exit_code=exit_code)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    assert hm.latest_version(_manifest_dep("macos")) is None
