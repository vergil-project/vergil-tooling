"""Tests for vergil_tooling.lib.host_status (two-axis classification + render)."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

import pytest

from vergil_tooling.lib import host_methods as hm
from vergil_tooling.lib import host_status as hs
from vergil_tooling.lib import host_versions as hv
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


def test_normalized_prose_and_bare_versions_are_current() -> None:
    """Spec §8: "gh version 2.40.0" == "2.40.0" once normalized — no false behind."""
    installed = hv.normalize("gh version 2.40.0")
    state, sev = hs.classify(_dep(), installed=installed, latest="2.40.0", check_results=[])
    assert (state, sev) == ("current", hs.OK)


@pytest.mark.parametrize(
    ("installed", "latest"),
    [("2.41.0", "2.40.0"), ("2.40.0_1", "2.40.0"), ("2.10", "2.9"), ("2024.10.18", "2024.10.4")],
)
def test_installed_newer_than_latest_is_current(installed: str, latest: str) -> None:
    state, sev = hs.classify(_dep(), installed=installed, latest=latest, check_results=[])
    assert (state, sev) == ("current", hs.OK)


@pytest.mark.parametrize(
    ("installed", "latest"),
    [("2.9", "2.10"), ("2.40.0", "2.40.0_1"), ("2024.10.04", "2024.10.18")],
)
def test_installed_older_than_latest_is_behind(installed: str, latest: str) -> None:
    state, sev = hs.classify(_dep(), installed=installed, latest=latest, check_results=[])
    assert (state, sev) == ("behind", hs.WARN)


def test_identical_unorderable_versions_are_current() -> None:
    state, _ = hs.classify(_dep(), installed="4.34.2,1", latest="4.34.2,1", check_results=[])
    assert state == "current"


def test_differing_unorderable_versions_raise_not_guess() -> None:
    with pytest.raises(hv.IncomparableVersionError, match="cannot compare version"):
        hs.classify(_dep(), installed="4.34.2,1", latest="4.35.0,2", check_results=[])


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
    state, sev = hs.classify(_dep(pin=_pin()), installed="1.1", latest="2.0", check_results=[])
    assert (state, sev) == ("pinned", hs.WARN)


def test_active_wildcard_pin_satisfied_is_ok() -> None:
    pin = dataclasses.replace(_pin(constraint="2.40.x"), inducing_release="2.41.0")
    state, sev = hs.classify(_dep(pin=pin), installed="2.40.3", latest="2.41.0", check_results=[])
    assert (state, sev) == ("pinned", hs.OK)


def test_active_wildcard_pin_drifted_warns() -> None:
    dep = _dep(pin=_pin(constraint="2.40.x"))
    state, sev = hs.classify(dep, installed="2.41.0", latest="2.41.0", check_results=[])
    assert (state, sev) == ("pinned", hs.WARN)


def test_active_pin_unsupported_constraint_raises() -> None:
    dep = _dep(pin=_pin(constraint="<=1.x"))
    with pytest.raises(ValueError, match="unsupported pin constraint '<=1.x'"):
        hs.classify(dep, installed="1.0", latest="2.0", check_results=[])


def test_missing_wins_over_unsupported_pin() -> None:
    dep = _dep(pin=_pin(constraint="<=1.x"))
    assert hs.classify(dep, None, "2.0", check_results=[]) == ("missing", hs.ERROR)


@pytest.mark.parametrize(
    ("installed", "constraint", "expected"),
    [
        ("2.40.0", "2.x", True),
        ("2.40.0", "2.40.x", True),
        ("2.40.0", "2.40.0", True),
        ("2.40.0_1", "2.40.0", True),
        ("2.40.0_1", "2.40.x", True),
        ("2.41.0_1", "2.40.x", False),
        ("2.40.0", "2.x.x", True),
        ("2.41.0", "2.40.x", False),
        ("1.9", "1.9.2", False),
        ("3.0.0", "2.x", False),
        ("2.40.0", "2.4.x", False),
        ("2.40.0rc1", "2.40.x", True),
    ],
)
def test_pin_satisfied(installed: str, constraint: str, expected: bool) -> None:
    assert hs.pin_satisfied(installed, constraint) is expected


@pytest.mark.parametrize(
    "constraint",
    ["<=1.x", ">=1.0", "~=1.2", "==1.0", "1.x.3", "x.1", "1..2", "", "x", "x.x", "1.", "1.*"],
)
def test_pin_satisfied_rejects_unsupported_syntax(constraint: str) -> None:
    with pytest.raises(ValueError, match="unsupported pin constraint") as exc:
        hs.pin_satisfied("1.0", constraint)
    assert repr(constraint) in str(exc.value)


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
    assert "pin: 1.0, holding back from 2.0 (inducing release 2.0)" in out
    assert "reason: 2.0 breaks us" in out
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


# -- plan Task 6: pin debt and the #155 re-evaluation trigger ----------------------


@pytest.mark.parametrize(
    ("latest", "review", "fragment"),
    [
        ("2.0", "not-due", None),
        ("1.5", "not-due", None),
        ("2.1", "due", "!! RE-EVALUATION DUE: leading edge 2.1 is past inducing release 2.0"),
        (None, "unknown", "!! RE-EVALUATION UNKNOWN: latest version unknown"),
        ("2.1-rc1", "unknown", "!! RE-EVALUATION UNKNOWN: cannot compare version '2.1-rc1'"),
        (hm.UNMANAGED, "no-feed", "re-evaluation: no latest feed"),
    ],
)
def test_pin_review(latest: hs.Latest, review: str, fragment: str | None) -> None:
    got, message = hs.pin_review(_pin(), latest)
    assert got == review
    if fragment is None:
        assert message is None
    else:
        assert message is not None and fragment in message


@pytest.mark.parametrize(
    ("latest", "severity"),
    [("2.0", hs.OK), ("2.1", hs.WARN), (None, hs.WARN), ("bad", hs.WARN), (hm.UNMANAGED, hs.OK)],
)
def test_active_pin_reevaluation_severity(latest: hs.Latest, severity: int) -> None:
    state, sev = hs.classify(_dep(pin=_pin()), installed="1.0", latest=latest, check_results=[])
    assert (state, sev) == ("pinned", severity)


def test_render_pin_debt_lines() -> None:
    pin = dataclasses.replace(_pin(), tracking_issue="https://github.com/o/r/issues/9")
    report = hs.Report(_dep(pin=pin), "1.0", "2.1", "pinned", hs.WARN)
    out = hs.render([report], today=datetime.date(2026, 10, 15))
    assert "pin: 1.0, holding back from 2.1 (inducing release 2.0)" in out
    assert "reason: 2.0 breaks us" in out
    assert "tracking issue: https://github.com/o/r/issues/9" in out
    assert "age: 14 days (pinned 2026-10-01)" in out
    assert "!! RE-EVALUATION DUE" in out


def test_render_pin_debt_not_due_has_no_marker() -> None:
    report = hs.Report(_dep(pin=_pin()), "1.0", "2.0", "pinned", hs.OK)
    out = hs.render([report], today=datetime.date(2026, 10, 2))
    assert "tracking issue: none" in out
    assert "age: 1 day (pinned 2026-10-01)" in out
    assert "RE-EVALUATION" not in out


def test_render_pin_future_date_and_default_today() -> None:
    report = hs.Report(_dep(pin=_pin()), "1.0", "2.0", "pinned", hs.OK)
    out = hs.render([report], today=datetime.date(2026, 9, 1))
    assert "age: pinned_date 2026-10-01 is in the future" in out
    assert "age:" in hs.render([report])


def test_render_under_evaluation_pin() -> None:
    report = hs.Report(_dep(pin=_pin(state="under-evaluation")), "1.1", "2.0", "behind", hs.WARN)
    out = hs.render([report])
    assert "pin under evaluation (not holding): 1.0 (2.0 breaks us); tracking issue: none" in out
    assert "holding back from" not in out
