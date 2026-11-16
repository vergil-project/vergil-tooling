"""Tests for vergil_tooling.lib.doc_links (issue #3831)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import doc_links

_HOST = "https://vergil-project.github.io"


@pytest.mark.parametrize(
    "url",
    [
        f"{_HOST}/vergil-tooling/latest/guides/x/",
        f"{_HOST}/vergil-tooling/dev/reference/",
        f"{_HOST}/vergil-actions/2.1/configuration/",
        f"{_HOST}/vergil-actions/2/configuration/",
        f"{_HOST}/vergil-actions/2.1.3/configuration/",
        f"{_HOST}/vergil-tooling/latest",
        f"{_HOST}/vergil-tooling/latest#anchor",
        f"{_HOST}/vergil-tooling/latest?q=1",
    ],
)
def test_versioned_link_allowed(url: str) -> None:
    assert doc_links.unversioned_links(f"See [the docs]({url}).\n") == []


@pytest.mark.parametrize(
    "url",
    [
        f"{_HOST}/vergil-tooling",
        f"{_HOST}/vergil-tooling/",
        f"{_HOST}/vergil-tooling#top",
        f"{_HOST}/vergil-tooling/#top",
        f"{_HOST}/vergil-tooling/?q=1",
    ],
)
def test_bare_root_allowed(url: str) -> None:
    assert doc_links.unversioned_links(f"<{url}>\n") == []


@pytest.mark.parametrize(
    "url",
    [
        f"{_HOST}/docs/guides/x/",
        f"{_HOST}/packages/simple/",
        f"{_HOST}/vergil-tooling-old/guides/x/",
        f"{_HOST}/standard-actions/guides/",
        "https://example.github.io/vergil-tooling/guides/x/",
    ],
)
def test_unversioned_site_not_flagged(url: str) -> None:
    assert doc_links.unversioned_links(f"[x]({url})\n") == []


@pytest.mark.parametrize("site", sorted(doc_links.VERSIONED_SITES))
def test_every_versioned_site_flagged(site: str) -> None:
    url = f"{_HOST}/{site}/guides/x/"
    assert doc_links.unversioned_links(f"[x]({url})\n") == [(1, url)]


def test_deep_link_flagged_with_line() -> None:
    text = (
        "# Title\n"
        "\n"
        f"Read [config]({_HOST}/vergil-actions/configuration/) first.\n"
        f"And <{_HOST}/vergil-tooling/latest/ok/> is fine.\n"
        f'<a href="{_HOST}/vergil-vm/install/">vm</a>\n'
    )
    assert doc_links.unversioned_links(text) == [
        (3, f"{_HOST}/vergil-actions/configuration/"),
        (5, f"{_HOST}/vergil-vm/install/"),
    ]


def test_version_alias_prefix_is_not_a_version() -> None:
    """``latestx`` and ``v2.1`` are ordinary path segments, not versions."""
    text = f"{_HOST}/vergil-tooling/latestx/a/ {_HOST}/vergil-tooling/v2.1/a/\n"
    assert [url for _line, url in doc_links.unversioned_links(text)] == [
        f"{_HOST}/vergil-tooling/latestx/a/",
        f"{_HOST}/vergil-tooling/v2.1/a/",
    ]


def test_code_span_and_reference_link_end_at_delimiter() -> None:
    text = f"`{_HOST}/vergil-tooling/guides/` and\n[ref]: {_HOST}/vergil-containers/architecture/\n"
    assert doc_links.unversioned_links(text) == [
        (1, f"{_HOST}/vergil-tooling/guides/"),
        (2, f"{_HOST}/vergil-containers/architecture/"),
    ]


def test_host_case_insensitive_and_http() -> None:
    url = "http://Vergil-Project.GitHub.io/vergil-tooling/guides/"
    assert doc_links.unversioned_links(url) == [(1, url)]


def test_query_or_fragment_first_segment_flagged() -> None:
    """A page segment followed by a fragment is still not a version."""
    url = f"{_HOST}/vergil-tooling/guides#x"
    assert doc_links.unversioned_links(url) == [(1, url)]


def test_versioned_form_inserts_latest() -> None:
    assert (
        doc_links.versioned_form(f"{_HOST}/vergil-actions/configuration/")
        == f"{_HOST}/vergil-actions/latest/configuration/"
    )


def test_versioned_form_rejects_other_url() -> None:
    with pytest.raises(ValueError, match="not a Vergil docs site URL"):
        doc_links.versioned_form("https://example.com/x/")
