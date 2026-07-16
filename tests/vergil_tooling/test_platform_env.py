"""Tests for the empirical, fail-closed platform resolver."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import platform_env
from vergil_tooling.lib.platform_env import (
    Platform,
    current_platform,
    is_cloud,
    resolve_platform,
)

# Capture the real probes before the autouse fixture replaces the module
# attributes, so the low-level helper tests exercise the actual bodies.
_REAL_CLOUD_METADATA_REACHABLE = platform_env._cloud_metadata_reachable
_REAL_LIMA_MARKER_PRESENT = platform_env._lima_marker_present


@pytest.fixture(autouse=True)
def _neutral_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every empirical signal to a benign value.

    Individual tests override exactly the signals they exercise, so an
    unmocked probe can never reach the real host and make a test depend
    on where it runs.
    """
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._lima_marker_present", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._identity_is_agent", lambda: False)


def test_darwin_no_vergil_is_physical_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    assert resolve_platform().platform is Platform.PHYSICAL_HOST


def test_vergil_plus_cloud_metadata_is_cloud_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: True)
    assert resolve_platform().platform is Platform.CLOUD_VM


def test_vergil_plus_lima_marker_is_local_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._lima_marker_present", lambda: True)
    assert resolve_platform().platform is Platform.LOCAL_VM


def test_vm_without_local_confirmation_fails_closed_to_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._lima_marker_present", lambda: False)
    assert resolve_platform().platform is Platform.CLOUD_VM  # never PHYSICAL_HOST


def test_non_darwin_no_vergil_fails_closed_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    # A box we cannot positively confirm as the physical host must never
    # be reported as PHYSICAL_HOST (fail closed for the memory control).
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    assert resolve_platform().platform is not Platform.PHYSICAL_HOST


def test_darwin_with_vergil_is_treated_as_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    # /vergil present dominates the OS signal: presence of the mount means
    # a VM, and without local confirmation it fails closed to cloud.
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._lima_marker_present", lambda: False)
    assert resolve_platform().platform is Platform.CLOUD_VM


def test_cloud_metadata_wins_over_lima_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reachable cloud metadata endpoint is the stronger cloud signal.
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._lima_marker_present", lambda: True)
    assert resolve_platform().platform is Platform.CLOUD_VM


def test_agent_identity_on_physical_host_flags_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._identity_is_agent", lambda: True)
    assert resolve_platform().disagreement is True


def test_human_identity_in_vm_flags_disagreement(monkeypatch: pytest.MonkeyPatch) -> None:
    # The correlation is host<->human, VM<->agent; a human on a VM is a
    # mismatch worth surfacing.
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._identity_is_agent", lambda: False)
    assert resolve_platform().disagreement is True


def test_no_disagreement_when_identity_matches_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Human on the physical host: the expected correlation, no warning.
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._identity_is_agent", lambda: False)
    assert resolve_platform().disagreement is False


def test_resolution_records_signals_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    resolution = resolve_platform()
    assert resolution.resolved_from  # non-empty provenance token
    assert isinstance(resolution.signals, dict)
    assert resolution.signals  # every signal recorded


def test_current_platform_returns_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    assert current_platform() is Platform.PHYSICAL_HOST


def test_is_cloud_true_only_on_cloud_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: True)
    monkeypatch.setattr("vergil_tooling.lib.platform_env._cloud_metadata_reachable", lambda: True)
    assert is_cloud() is True


def test_is_cloud_false_on_physical_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("vergil_tooling.lib.platform_env._vergil_mount_present", lambda: False)
    assert is_cloud() is False


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def test_cloud_metadata_reachable_true_on_successful_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: _FakeConn())
    assert _REAL_CLOUD_METADATA_REACHABLE() is True


def test_cloud_metadata_reachable_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("unreachable")

    monkeypatch.setattr("socket.create_connection", _boom)
    assert _REAL_CLOUD_METADATA_REACHABLE() is False


def test_lima_marker_present_true_when_a_marker_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # __file__ is a real, existing path, so the any(...) predicate is True.
    monkeypatch.setattr("vergil_tooling.lib.platform_env._LIMA_MARKERS", (__file__,))
    assert _REAL_LIMA_MARKER_PRESENT() is True


def test_lima_marker_present_false_when_no_marker_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vergil_tooling.lib.platform_env._LIMA_MARKERS",
        ("/nonexistent/vrg-lima-marker-should-not-exist",),
    )
    assert _REAL_LIMA_MARKER_PRESENT() is False
