"""Tests for vergil_tooling.lib.tooling_release (#4550, epic vergil-project/.github#1350)."""

from __future__ import annotations

import pytest

from tests.vergil_tooling.conftest import fixture_ls_remote, fixture_tooling_sha
from vergil_tooling.lib.tooling_install import GIT_URL, ResolvedRef, ToolingInstallError
from vergil_tooling.lib.tooling_release import ToolingResolveError, resolve_tooling_tag


def _no_network(url: str, pattern: str) -> str:
    msg = f"unexpected ls-remote: {url} {pattern}"
    raise AssertionError(msg)


def test_a_line_resolves_to_its_newest_tag_and_its_commit() -> None:
    calls: list[tuple[str, str]] = []
    listing = fixture_ls_remote(("v2.1.9", "v2.1.10", "v2.1.320", "v2.2.0", "v2.10.0"))

    def ls_remote(url: str, pattern: str) -> str:
        calls.append((url, pattern))
        return listing(url, pattern)

    resolved = resolve_tooling_tag("v2.1", ls_remote=ls_remote)
    assert resolved == ResolvedRef(
        ref="v2.1", kind="line", tag="v2.1.320", sha=fixture_tooling_sha("v2.1.320")
    )
    assert calls == [(GIT_URL, "refs/tags/v2.1*")]


def test_a_line_resolves_through_the_default_listing_seam() -> None:
    # conftest swaps the seam for the fixture listing (v2.0.6, v2.0.7, v2.1.9).
    resolved = resolve_tooling_tag("v2.0")
    assert (resolved.tag, resolved.sha) == ("v2.0.7", fixture_tooling_sha("v2.0.7"))


def test_a_failed_listing_is_loud() -> None:
    def offline(url: str, pattern: str) -> str:
        msg = "could not resolve host: github.com"
        raise ToolingInstallError(msg)

    with pytest.raises(ToolingResolveError) as info:
        resolve_tooling_tag("v2.1", ls_remote=offline)
    assert GIT_URL in str(info.value)
    assert "v2.1" in str(info.value)
    assert "could not resolve host" in str(info.value)


def test_a_line_with_no_release_tag_is_loud() -> None:
    with pytest.raises(ToolingResolveError, match=r"no release tag for v2\.3"):
        resolve_tooling_tag("v2.3", ls_remote=fixture_ls_remote())


def test_an_exact_pin_needs_no_network() -> None:
    assert resolve_tooling_tag("v2.1.320", ls_remote=_no_network) == ResolvedRef(
        ref="v2.1.320", kind="exact", tag="v2.1.320", sha=None
    )


@pytest.mark.parametrize("ref", ["develop", "feature/1-x"])
def test_a_dev_ref_resolves_to_itself_with_no_call(ref: str) -> None:
    assert resolve_tooling_tag(ref, ls_remote=_no_network) == ResolvedRef(
        ref=ref, kind="dev", tag=None, sha=None
    )
