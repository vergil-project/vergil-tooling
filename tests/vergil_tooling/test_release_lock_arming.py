"""The arming refs, ``refs/vergil/release-lock-arming/<pr>`` (epic .github#1664, Task 17).

Exercised against a local bare repository served as ``origin``; see
:mod:`tests.vergil_tooling.release_lock_stubs`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, FlakyPush, Origin, make_origin
from vergil_tooling.lib import retry
from vergil_tooling.lib.release_lock import RefReadError, RefWriteError, arming, refs

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    return make_origin(tmp_path, monkeypatch)


def test_arming_ref_name() -> None:
    assert arming.arming_ref(12) == "refs/vergil/release-lock-arming/12"
    for bad in (0, -1):
        with pytest.raises(ValueError, match="PR number"):
            arming.arming_ref(bad)


def test_arming_create_only_and_renew_cas(origin: Origin) -> None:
    ref = arming.arming_ref(12)
    created = arming.create(REPO, 12)
    assert created is not None
    assert origin.sha(ref) == created
    assert json.loads(origin.file(ref, arming.ARMING_FILE))["pr"] == 12
    # Create-only: a second caller arming the same PR loses.
    assert arming.create(REPO, 12) is None
    assert origin.sha(ref) == created

    renewed = arming.renew(REPO, 12, expected=created)
    assert renewed is not None
    assert renewed != created  # a renewal always changes the value
    assert arming.renew(REPO, 12, expected=created) is None
    assert origin.sha(ref) == renewed

    assert arming.delete(REPO, 12, expected=created) is False
    assert origin.sha(ref) == renewed
    assert arming.delete(REPO, 12, expected=renewed) is True
    assert origin.sha(ref) is None


def test_renew_of_a_gone_ref_is_a_mismatch(origin: Origin) -> None:
    del origin
    assert arming.renew(REPO, 5, expected="1" * 40) is None


def test_list_arming(origin: Origin) -> None:
    assert arming.list_arming(REPO) == {}
    first = arming.create(REPO, 3)
    second = arming.create(REPO, 41)
    assert arming.list_arming(REPO) == {3: first, 41: second}
    # A ref outside the namespace that merely ends like one is not listed.
    origin.set_ref("refs/heads/x/refs/vergil/release-lock-arming/9", str(first))
    assert arming.list_arming(REPO) == {3: first, 41: second}


@pytest.mark.parametrize("suffix", ["abc", "0", "007", "3/4"])
def test_list_arming_refuses_a_malformed_ref(origin: Origin, suffix: str) -> None:
    sha = origin.commit(arming.ARMING_FILE, "{}")
    origin.set_ref(f"refs/vergil/release-lock-arming/{suffix}", sha)
    with pytest.raises(RefReadError, match=suffix):
        arming.list_arming(REPO)


def test_arming_applied_then_failed_push_reconciled_as_success(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = arming.arming_ref(7)
    flaky = FlakyPush(monkeypatch)
    created = arming.create(REPO, 7)
    assert flaky.pushes == 2
    assert created is not None
    assert origin.sha(ref) == created

    flaky = FlakyPush(monkeypatch)
    renewed = arming.renew(REPO, 7, expected=created)
    assert flaky.pushes == 2
    assert renewed is not None
    assert origin.sha(ref) == renewed

    flaky = FlakyPush(monkeypatch)
    assert arming.delete(REPO, 7, expected=renewed) is True
    assert flaky.pushes == 2
    assert origin.sha(ref) is None


def test_arming_rejection_with_foreign_value_is_mismatch(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = arming.arming_ref(7)
    foreign = origin.commit(arming.ARMING_FILE, json.dumps({"pr": 7, "nonce": "x"}))
    FlakyPush(monkeypatch, apply=False, meanwhile=lambda: origin.set_ref(ref, foreign))
    assert arming.create(REPO, 7) is None
    assert origin.sha(ref) == foreign


def test_arming_transport_failure_raises(
    origin: Origin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del origin

    missing = str(tmp_path / "no-such-origin.git")
    monkeypatch.setattr(refs, "origin_url", lambda repo: missing)
    monkeypatch.setattr(retry, "compute_delay", lambda attempt: 0.0)
    with pytest.raises(RefWriteError, match="release-lock-arming/7"):
        arming.create(REPO, 7)
    with pytest.raises(RefReadError):
        arming.list_arming(REPO)
