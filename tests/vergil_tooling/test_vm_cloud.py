from __future__ import annotations

import re

from vergil_tooling.lib.vm_cloud import cloud_labels, cloud_resource_name

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
