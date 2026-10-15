"""Tests for vergil_tooling.lib.host_status (two-axis classification + render)."""

from __future__ import annotations

from typing import Any

import pytest

from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib import host_status as hs
from vergil_tooling.lib.host_deps import AuxCheck, HostDependency, HostPin


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


def _pin(state: str = "active", constraint: str = "1.0") -> HostPin:
    return HostPin(
        constraint=constraint,
        inducing_release="2.0",
        deterministic=True,
        reason="2.0 breaks us",
        state=state,
        tracking_issue=None,
        pinned_date="2026-10-01",
    )


# -- plan Task 3 contract ----------------------------------------------------------


def test_missing_is_error() -> None:
    state, sev = hs.classify(_dep(), installed=None, latest="2.0", check_results=[])
    assert (state, sev) == ("missing", hs.ERROR)


def test_behind_is_warn() -> None:
    state, sev = hs.classify(_dep(), installed="1.0", latest="2.0", check_results=[])
    assert (state, sev) == ("behind", hs.WARN)


def test_current_is_ok() -> None:
    state, sev = hs.classify(_dep(), installed="2.0", latest="2.0", check_results=[])
    assert (state, sev) == ("current", hs.OK)


def test_latest_unmanaged_is_info_not_warn() -> None:
    state, sev = hs.classify(
        _dep(latest="unmanaged"), installed="1.0", latest=hm.UNMANAGED, check_results=[]
    )
    assert (state, sev) == ("latest-unmanaged", hs.OK)


def test_latest_unknown_is_warn() -> None:
    state, sev = hs.classify(_dep(), installed="1.0", latest=None, check_results=[])
    assert (state, sev) == ("latest-unknown", hs.WARN)


def test_unsatisfied_check_forces_error() -> None:
    dep = _dep(checks=(AuxCheck("numpy", ["true"], ["fix"]),))
    state, sev = hs.classify(dep, installed="1.0", latest="1.0", check_results=[("numpy", False)])
    assert (state, sev) == ("current", hs.ERROR)


def test_satisfied_check_keeps_version_severity() -> None:
    _, sev = hs.classify(_dep(), installed="1.0", latest="2.0", check_results=[("numpy", True)])
    assert sev == hs.WARN


@pytest.mark.parametrize(
    ("installed", "latest", "expected_state"),
    [
        ("1.0", hm.UNMANAGED, "latest-unmanaged"),
        ("1.0", None, "latest-unknown"),
        ("1.0", "2.0", "behind"),
        (None, "2.0", "missing"),
    ],
)
def test_unsatisfied_check_is_worst_on_every_state(
    installed: str | None, latest: hs.Latest, expected_state: str
) -> None:
    state, sev = hs.classify(_dep(), installed, latest, check_results=[("c", False)])
    assert (state, sev) == (expected_state, hs.ERROR)


def test_active_pin_at_constraint_is_ok_debt() -> None:
    state, sev = hs.classify(_dep(pin=_pin()), installed="1.0", latest="2.0", check_results=[])
    assert (state, sev) == ("pinned", hs.OK)


def test_active_pin_off_constraint_warns() -> None:
    # Interim "differs" comparator (refined in plan Task 7).
    state, sev = hs.classify(_dep(pin=_pin()), installed="1.1", latest="2.0", check_results=[])
    assert (state, sev) == ("pinned", hs.WARN)


def test_active_pin_with_unsatisfied_check_errors() -> None:
    _, sev = hs.classify(_dep(pin=_pin()), "1.0", "2.0", check_results=[("c", False)])
    assert sev == hs.ERROR


def test_freed_pin_classifies_on_version() -> None:
    state, _ = hs.classify(_dep(pin=_pin(state="freed")), "1.0", "2.0", check_results=[])
    assert state == "behind"


def test_version_states_cover_classify_outputs() -> None:
    states = set(hs.VERSION_STATES)
    assert states == {
        "missing",
        "current",
        "behind",
        "pinned",
        "latest-unmanaged",
        "latest-unknown",
    }
    assert hs.PROBE_ERROR not in hs.VERSION_STATES


# -- reports and rendering ---------------------------------------------------------


def test_probe_error_report_is_explicit_error() -> None:
    report = hs.probe_error_report(_dep(), "ProbeError: garbled")
    assert report.state == hs.PROBE_ERROR
    assert report.severity == hs.ERROR
    assert report.installed is None
    assert report.error == "ProbeError: garbled"


def test_worst_severity() -> None:
    ok = hs.Report(_dep(), "1", "1", "current", hs.OK)
    warn = hs.Report(_dep(), "1", "2", "behind", hs.WARN)
    assert hs.worst_severity([]) == hs.OK
    assert hs.worst_severity([ok, warn]) == hs.WARN
    assert hs.worst_severity([ok, warn, hs.probe_error_report(_dep(), "x")]) == hs.ERROR


def test_render_rows_and_details() -> None:
    reports = [
        hs.Report(_dep(name="gh"), "2.0", "2.0", "current", hs.OK),
        hs.Report(_dep(name="uv", method="uv-tool"), "0.4", "0.5", "behind", hs.WARN),
        hs.Report(
            _dep(name="mac", method="custom", latest="unmanaged"),
            "15.0",
            hm.UNMANAGED,
            "latest-unmanaged",
            hs.OK,
        ),
        hs.Report(_dep(name="lima"), "1.0", None, "latest-unknown", hs.WARN),
        hs.Report(_dep(name="docker"), None, "27.0", "missing", hs.ERROR),
        hs.Report(
            _dep(name="py", pin=_pin()),
            "1.0",
            "2.0",
            "pinned",
            hs.OK,
            [("numpy", True), ("ssl", False)],
        ),
        hs.probe_error_report(_dep(name="gcloud"), "ProbeError: garbled"),
    ]
    out = hs.render(reports)
    lines = out.splitlines()
    assert lines[0].startswith("[OK   ] gh")
    assert "2.0 -> 2.0" in lines[0] and lines[0].endswith("current")
    assert "behind (drift)" in out
    assert "-> unmanaged" in out and "latest-unmanaged (info: no latest feed)" in out
    assert "-> unknown" in out and "latest-unknown" in out
    assert "[ERROR] docker" in out and "missing" in out
    assert "pinned (tracked debt)" in out
    assert "pin: 1.0 (2.0 breaks us)" in out
    assert "check numpy: satisfied" in out
    assert "check ssl: UNSATISFIED" in out
    assert "[ERROR] gcloud" in out and "probe-error" in out
    assert "error: ProbeError: garbled" in out
    assert lines[-1] == "overall: ERROR (7 dependencies)"


def test_render_freed_pin_has_no_pin_line() -> None:
    out = hs.render([hs.Report(_dep(pin=_pin(state="freed")), "1", "1", "current", hs.OK)])
    assert "pin:" not in out
    assert out.splitlines()[-1] == "overall: OK (1 dependency)"


def test_render_empty() -> None:
    assert hs.render([]) == "overall: OK (0 dependencies)"
