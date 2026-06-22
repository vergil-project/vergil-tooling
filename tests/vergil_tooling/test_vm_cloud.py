from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.vm_cloud import cloud_labels, cloud_resource_name, fetch_modules

_RFC1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


class TestCloudName:
    def test_lowercases_and_replaces_dots(self) -> None:
        name = cloud_resource_name("vergil-user", "Logical-Minds", "MQ.Cluster")
        assert _RFC1035.fullmatch(name)
        assert "." not in name and name == name.lower()

    def test_deterministic(self) -> None:
        a = cloud_resource_name("vergil-user", "o", "r")
        b = cloud_resource_name("vergil-user", "o", "r")
        assert a == b

    def test_truncates_long_names_to_59_with_hash(self) -> None:
        name = cloud_resource_name("vergil-user", "a" * 40, "b" * 40)
        assert len(name) <= 59
        assert _RFC1035.fullmatch(name)

    def test_distinct_inputs_distinct_names_even_when_truncated(self) -> None:
        n1 = cloud_resource_name("vergil-user", "a" * 40, "b" * 40)
        n2 = cloud_resource_name("vergil-user", "a" * 40, "c" * 40)
        assert n1 != n2

    def test_prefixes_non_alpha_leading_name(self) -> None:
        name = cloud_resource_name("9user", "org", "repo")
        assert _RFC1035.fullmatch(name)
        assert name.startswith("v-")

    def test_empty_components_fall_back_to_placeholder(self) -> None:
        name = cloud_resource_name("...", "...", "...")
        assert _RFC1035.fullmatch(name)


class TestCloudLabels:
    def test_structured_recovery_labels(self) -> None:
        labels = cloud_labels("vergil-audit", "org", "repo")
        assert labels == {
            "vergil-identity": "vergil-audit",
            "vergil-org": "org",
            "vergil-repo": "repo",
        }


class TestFetchModules:
    def test_rejects_bad_tag(self) -> None:
        with pytest.raises(SystemExit):
            fetch_modules("not-a-tag")

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_builds_release_asset_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):  # tar extraction of fake bytes fails loudly
            fetch_modules("v2.1.50")
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/releases/download/"
            "v2.1.50/opentofu-modules-2.1.50.tar.gz"
        )

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_strips_leading_v_for_two_segment_tag(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):
            fetch_modules("v2.2")
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/releases/download/"
            "v2.2/opentofu-modules-2.2.tar.gz"
        )

    @patch("vergil_tooling.lib.vm_cloud.tarfile.open")
    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_returns_modules_path_when_present(
        self, mock_urlopen: MagicMock, mock_taropen: MagicMock, tmp_path: Path
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"data"
        mock_urlopen.return_value.__enter__.return_value = resp

        created: dict[str, Path] = {}

        class _FakeTar:
            def __enter__(self) -> _FakeTar:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def extractall(self, dest: str, filter: str) -> None:  # noqa: A002
                modules = Path(dest) / "opentofu" / "modules"
                modules.mkdir(parents=True)
                created["modules"] = modules

        mock_taropen.return_value = _FakeTar()
        result = fetch_modules("v2.1.50")
        assert result == created["modules"]
        assert result.is_dir()

    @patch("vergil_tooling.lib.vm_cloud.tarfile.open")
    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_missing_modules_dir_aborts(
        self, mock_urlopen: MagicMock, mock_taropen: MagicMock
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"data"
        mock_urlopen.return_value.__enter__.return_value = resp

        class _EmptyTar:
            def __enter__(self) -> _EmptyTar:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def extractall(self, dest: str, filter: str) -> None:  # noqa: A002
                return None

        mock_taropen.return_value = _EmptyTar()
        with pytest.raises(SystemExit):
            fetch_modules("v2.1.50")
