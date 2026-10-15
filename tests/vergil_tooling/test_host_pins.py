"""Tests for vergil_tooling.lib.host_pins (host-local pin overlay, #155 lifecycle)."""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import host_deps, host_pins
from vergil_tooling.lib.host_deps import HostPin, ManifestError

if TYPE_CHECKING:
    from pathlib import Path

# Captured at import time, before the autouse conftest fixture patches it.
_REAL_DEFAULT_PINS_PATH = host_pins.default_pins_path

_TODAY = datetime.date(2026, 10, 15)


def _pin(**kw: object) -> HostPin:
    base: dict[str, object] = {
        "constraint": "2.40.x",
        "inducing_release": "2.41.0",
        "deterministic": True,
        "reason": "2.41.0 breaks vrg-gh auth",
        "state": "active",
        "tracking_issue": "https://github.com/org/repo/issues/1",
        "pinned_date": "2026-10-01",
    }
    base.update(kw)
    return HostPin(**base)  # type: ignore[arg-type]


@pytest.fixture
def pins_path(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "host-pins.toml"


# -- default path ------------------------------------------------------------------


def test_default_pins_path_is_beside_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _REAL_DEFAULT_PINS_PATH() == tmp_path / ".config" / "vergil" / "host-pins.toml"


# -- constraint grammar -------------------------------------------------------------


@pytest.mark.parametrize(
    ("constraint", "parts"),
    [("1.x", ["1"]), ("2.40.x", ["2", "40"]), ("2.40.0", ["2", "40", "0"]), ("1.x.x", ["1"])],
)
def test_parse_constraint(constraint: str, parts: list[str]) -> None:
    assert host_pins.parse_constraint(constraint) == parts


@pytest.mark.parametrize("constraint", ["<=1.x", ">=2", "~=1.2", "==1.0", "1.x.2", "x", "1..2", ""])
def test_parse_constraint_refuses_unsupported(constraint: str) -> None:
    with pytest.raises(ValueError, match="unsupported pin constraint"):
        host_pins.parse_constraint(constraint)


# -- #155 trigger ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2.0", "2.0.0", 0),
        ("v2.1", "2.0", 1),
        ("1.9.9", "2.0", -1),
        ("2.10", "2.9", 1),
    ],
)
def test_compare_versions(left: str, right: str, expected: int) -> None:
    assert host_pins.compare_versions(left, right) == expected


@pytest.mark.parametrize(("left", "right"), [("2.0-beta", "2.0"), ("2.0", "2.0_1"), ("", "1")])
def test_compare_versions_refuses_non_numeric(left: str, right: str) -> None:
    with pytest.raises(host_pins.IncomparableVersionError, match="cannot compare version"):
        host_pins.compare_versions(left, right)


def test_reevaluation_due_when_leading_past_inducing() -> None:
    pin = _pin(constraint="1.x", inducing_release="2.0")
    assert host_pins.is_reevaluation_due(pin, current_leading="2.1") is True
    assert host_pins.is_reevaluation_due(pin, current_leading="2.0") is False
    assert host_pins.is_reevaluation_due(pin, current_leading="1.9") is False


def test_reevaluation_incomparable_raises() -> None:
    with pytest.raises(host_pins.IncomparableVersionError):
        host_pins.is_reevaluation_due(_pin(), current_leading="nightly")


# -- make_pin ----------------------------------------------------------------------


def _make(**kw: object) -> HostPin:
    base: dict[str, object] = {
        "constraint": "1.x",
        "inducing_release": "2.0",
        "deterministic": True,
        "reason": "breaks us",
        "tracking_issue": None,
        "today": _TODAY,
    }
    base.update(kw)
    return host_pins.make_pin(**base)  # type: ignore[arg-type]


def test_pin_requires_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        _make(reason="")
    with pytest.raises(ValueError, match="reason"):
        _make(reason="   ")


def test_make_pin_refuses_unsupported_constraint() -> None:
    with pytest.raises(ValueError, match="unsupported pin constraint '<=1.x'"):
        _make(constraint="<=1.x")


def test_make_pin_refuses_blank_inducing_and_tracking() -> None:
    with pytest.raises(ValueError, match="inducing release"):
        _make(inducing_release=" ")
    with pytest.raises(ValueError, match="tracking issue"):
        _make(tracking_issue="")


def test_make_pin_stamps_active_and_date() -> None:
    pin = _make(tracking_issue="https://x/1", deterministic=False)
    assert pin == HostPin("1.x", "2.0", False, "breaks us", "active", "https://x/1", "2026-10-15")


def test_make_pin_defaults_to_today() -> None:
    pin = host_pins.make_pin(
        constraint="1.x",
        inducing_release="2.0",
        deterministic=True,
        reason="r",
        tracking_issue=None,
    )
    assert pin.pinned_date == datetime.date.today().isoformat()


# -- load_pins ---------------------------------------------------------------------


def test_missing_pins_file_means_no_pins(pins_path: Path) -> None:
    assert host_pins.load_pins(pins_path) == {}


def test_load_pins_defaults_to_default_path() -> None:
    path = host_pins.default_pins_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '[pin.gh]\nconstraint = "1.x"\ninducing_release = "2.0"\nreason = "r"\n'
        "pinned_date = 2026-10-01\n"
    )
    pins = host_pins.load_pins()
    assert pins["gh"].constraint == "1.x"
    assert pins["gh"].state == "active"
    assert pins["gh"].pinned_date == "2026-10-01"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("not toml [", "not valid TOML"),
        ('[dependency]\nname = "evil"\n', "unknown key"),
        ('pin = "gh"\n', "'pin' must be a table"),
        (
            '[pin.gh]\nconstraint = "1.x"\ninducing_release = "2"\npinned_date = "2026-10-01"\n',
            "pin without a reason is refused",
        ),
        ('[pin]\ngh = "1.x"\n', "must be a table"),
        ('[pin.gh]\nreason = "r"\nprobe = ["sh"]\n', "unknown key"),
    ],
)
def test_load_pins_malformed_is_loud(pins_path: Path, text: str, match: str) -> None:
    pins_path.parent.mkdir(parents=True)
    pins_path.write_text(text)
    with pytest.raises(host_pins.PinsError, match=match):
        host_pins.load_pins(pins_path)


def test_pins_error_is_a_manifest_error() -> None:
    assert issubclass(host_pins.PinsError, ManifestError)


# -- write_pin / clear_pin ---------------------------------------------------------


def test_write_pin_round_trip_and_load_effective_manifest(pins_path: Path) -> None:
    pin = _pin()
    host_pins.write_pin("gh", pin, pins_path)
    assert host_pins.load_pins(pins_path) == {"gh": pin}
    merged = {d.name: d for d in host_pins.load_effective_manifest(pins_path)}
    assert merged["gh"].pin == pin
    assert merged["uv"].pin is None
    # The packaged manifest itself is untouched.
    assert all(d.pin is None for d in host_deps.load_manifest())


def test_write_pin_escapes_and_omits_none(pins_path: Path) -> None:
    pin = _pin(
        reason='quote " backslash \\ tab\t del\x7f ünïcode',
        tracking_issue=None,
        deterministic=False,
    )
    host_pins.write_pin("uv", pin, pins_path)
    text = pins_path.read_text()
    assert "tracking_issue" not in text
    assert "deterministic = false" in text
    assert host_pins.load_pins(pins_path) == {"uv": pin}


def test_write_pin_replaces_existing_and_keeps_others(pins_path: Path) -> None:
    host_pins.write_pin("gh", _pin(), pins_path)
    host_pins.write_pin("uv", _pin(constraint="0.5.x"), pins_path)
    host_pins.write_pin("gh", _pin(constraint="2.x"), pins_path)
    pins = host_pins.load_pins(pins_path)
    assert pins["gh"].constraint == "2.x"
    assert pins["uv"].constraint == "0.5.x"


def test_write_pin_defaults_to_default_path() -> None:
    host_pins.write_pin("gh", _pin())
    assert host_pins.load_pins(host_pins.default_pins_path()) == {"gh": _pin()}


def test_write_pin_refuses_undeclared_dependency(pins_path: Path) -> None:
    with pytest.raises(host_pins.PinsError, match="'nope' is not a dependency declared"):
        host_pins.write_pin("nope", _pin(), pins_path)
    assert not pins_path.exists()


def test_write_pin_never_overwrites_malformed_file(pins_path: Path) -> None:
    pins_path.parent.mkdir(parents=True)
    pins_path.write_text("garbage [")
    with pytest.raises(host_pins.PinsError, match="not valid TOML"):
        host_pins.write_pin("gh", _pin(), pins_path)
    assert pins_path.read_text() == "garbage ["


def test_save_failure_removes_temp_file(pins_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_pins: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(host_pins, "_dump", boom)
    with pytest.raises(OSError, match="disk full"):
        host_pins.write_pin("gh", _pin(), pins_path)
    assert list(pins_path.parent.iterdir()) == []


def test_clear_pin_frees_and_keeps_history(pins_path: Path) -> None:
    host_pins.write_pin("gh", _pin(), pins_path)
    freed = host_pins.clear_pin("gh", pins_path)
    assert freed == dataclasses.replace(_pin(), state="freed")
    assert host_pins.load_pins(pins_path) == {"gh": freed}
    merged = {d.name: d for d in host_pins.load_effective_manifest(pins_path)}
    assert merged["gh"].pin is not None and merged["gh"].pin.state == "freed"


def test_clear_pin_defaults_to_default_path() -> None:
    host_pins.write_pin("gh", _pin())
    assert host_pins.clear_pin("gh").state == "freed"


def test_clear_pin_refuses_missing_and_already_freed(pins_path: Path) -> None:
    with pytest.raises(host_pins.PinsError, match="'gh' has no pin"):
        host_pins.clear_pin("gh", pins_path)
    host_pins.write_pin("gh", _pin(), pins_path)
    host_pins.clear_pin("gh", pins_path)
    with pytest.raises(host_pins.PinsError, match="already freed"):
        host_pins.clear_pin("gh", pins_path)


# -- overlay invariants --------------------------------------------------------------


def test_effective_manifest_without_pins_is_the_manifest(pins_path: Path) -> None:
    assert host_pins.load_effective_manifest(pins_path) == host_deps.load_manifest()


def test_effective_manifest_defaults_to_default_path() -> None:
    assert host_pins.load_effective_manifest() == host_deps.load_manifest()


def _write_raw(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'[pin.{n}]\nconstraint = "1.x"\ninducing_release = "2"\nreason = "r"\n'
        f'pinned_date = "2026-10-01"\n'
        for n in names
    )
    path.write_text(body)


def test_effective_manifest_refuses_undeclared_pin(pins_path: Path) -> None:
    _write_raw(pins_path, ["evil"])
    with pytest.raises(host_pins.PinsError, match="undeclared dependency evil;"):
        host_pins.load_effective_manifest(pins_path)
    _write_raw(pins_path, ["evil", "worse"])
    with pytest.raises(host_pins.PinsError, match="undeclared dependencies evil, worse"):
        host_pins.load_effective_manifest(pins_path)


def test_effective_manifest_refuses_double_pin(
    pins_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = (
        '[[dependency]]\nname = "gh"\nmethod = "brew-formula"\n'
        '[dependency.pin]\nconstraint = "1.x"\ninducing_release = "2"\nreason = "r"\n'
        'pinned_date = "2026-10-01"\n'
    )
    monkeypatch.setattr(host_deps, "_read_manifest_text", lambda: manifest)
    _write_raw(pins_path, ["gh"])
    with pytest.raises(host_pins.PinsError, match="pinned both in the packaged manifest"):
        host_pins.load_effective_manifest(pins_path)
