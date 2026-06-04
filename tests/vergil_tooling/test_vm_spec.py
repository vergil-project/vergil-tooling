"""Tests for vergil_tooling.lib.vm_spec."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.config import RoleOverlay, VmStanza
from vergil_tooling.lib.vm_spec import (
    ComposedSpec,
    compose_vm_spec,
    instance_name,
    parse_instance_name,
    spec_fingerprint,
)

BASE = {"cpus": 4, "memory": "4GiB", "disk": "50GiB"}


def _mq_stanza() -> VmStanza:
    return VmStanza(
        packages=["libvirt-clients", "qemu-system-x86"],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        provision=".vergil/provision.sh",
        roles={
            "vergil-user": RoleOverlay(
                packages=[],
                cpus=12,
                memory="64GiB",
                disk="300GiB",
                stale_days=7,
                provision=None,
            ),
        },
    )


class TestComposeVmSpec:
    def test_no_stanza_no_override_is_base(self) -> None:
        spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=None, override=None)
        assert isinstance(spec, ComposedSpec)
        assert spec.dedicated is False
        assert spec.cpus == 4
        assert spec.memory == "4GiB"
        assert spec.packages == ()
        assert spec.under == ()

    def test_user_gets_tuned_dedicated_box(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=_mq_stanza(), override=None
        )
        assert spec.dedicated is True
        assert spec.cpus == 12
        assert spec.memory == "64GiB"
        assert spec.disk == "300GiB"
        assert spec.stale_days == 7
        assert spec.packages == ("libvirt-clients", "qemu-system-x86")
        assert spec.provision == ".vergil/provision.sh"
        assert spec.under == ()

    def test_audit_gets_packages_only_at_base_footprint(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-audit", base=BASE, stanza=_mq_stanza(), override=None
        )
        assert spec.dedicated is True  # packages customize it
        assert spec.cpus == 4
        assert spec.memory == "4GiB"  # base footprint, role overlay did not apply
        assert spec.packages == ("libvirt-clients", "qemu-system-x86")
        assert spec.stale_days == 3

    def test_host_override_below_declared_flags_under(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=_mq_stanza(),
            override={"memory": "32GiB"},
        )
        assert spec.dedicated is True
        assert spec.memory == "32GiB"  # override wins
        assert spec.under == ("mem",)  # but flagged: 32 < declared 64

    def test_host_override_cpus_and_disk_and_stale(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=_mq_stanza(),
            override={"cpus": 8, "disk": "100GiB", "stale_days": 14},
        )
        assert spec.cpus == 8
        assert spec.disk == "100GiB"
        assert spec.stale_days == 14
        assert set(spec.under) == {"cpus", "disk"}  # 8<12, 100<300

    def test_override_on_base_box_no_declared_floor(self) -> None:
        # Override with no repo stanza: nothing declared, so nothing can be "under",
        # even when the override sizes below the identity's base footprint.
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=None,
            override={"cpus": 2, "memory": "2GiB", "disk": "10GiB"},
        )
        assert spec.dedicated is True
        assert spec.memory == "2GiB"
        assert spec.under == ()

    def test_override_above_declared_not_flagged(self) -> None:
        # An override that raises a scalar above the repo-declared value is not "under".
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=_mq_stanza(),
            override={"cpus": 16, "memory": "128GiB", "disk": "500GiB"},
        )
        assert spec.cpus == 16
        assert spec.under == ()


class TestInstanceName:
    def test_base_is_bare_identity(self) -> None:
        assert instance_name("vergil-user", None, None) == "vergil-user"

    def test_base_when_only_org_given(self) -> None:
        assert instance_name("vergil-user", "org", None) == "vergil-user"

    def test_dedicated_is_double_dash_joined(self) -> None:
        assert (
            instance_name("vergil-user", "logical-minds-foundry", "mq-cluster-tooling")
            == "vergil-user--logical-minds-foundry--mq-cluster-tooling"
        )

    def test_roundtrip_dedicated(self) -> None:
        name = "vergil-user--logical-minds-foundry--mq-cluster-tooling"
        assert parse_instance_name(name) == (
            "vergil-user",
            "logical-minds-foundry",
            "mq-cluster-tooling",
        )

    def test_roundtrip_base(self) -> None:
        assert parse_instance_name("vergil-user") == ("vergil-user", None, None)

    def test_unparseable_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable VM instance name"):
            parse_instance_name("a--b--c--d")


class TestFingerprint:
    def _spec(self, **over: object) -> ComposedSpec:
        base: dict[str, object] = {
            "cpus": 12,
            "memory": "64GiB",
            "disk": "300GiB",
            "stale_days": 7,
            "packages": ("a", "b"),
            "provision": ".vergil/provision.sh",
            "dedicated": True,
            "under": (),
            "provision_hash": "hookv1",
        }
        base.update(over)
        return ComposedSpec(**base)  # type: ignore[arg-type]

    def test_stable_for_same_declaration(self) -> None:
        assert spec_fingerprint(self._spec()) == spec_fingerprint(self._spec())

    def test_package_order_does_not_matter(self) -> None:
        assert spec_fingerprint(self._spec(packages=("a", "b"))) == spec_fingerprint(
            self._spec(packages=("b", "a"))
        )

    def test_footprint_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec(memory="64GiB")) != spec_fingerprint(
            self._spec(memory="32GiB")
        )

    def test_package_addition_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec(packages=("a",))) != spec_fingerprint(
            self._spec(packages=("a", "b"))
        )

    def test_provision_hook_content_change_changes_fingerprint(self) -> None:
        # Editing the script (same path) must flip the fingerprint — the security checkpoint.
        assert spec_fingerprint(self._spec(provision_hash="hookv1")) != spec_fingerprint(
            self._spec(provision_hash="hookv2")
        )

    def test_falls_back_to_path_when_no_content_hash(self) -> None:
        # No content hash: the path keeps the fingerprint stable and distinct from empty.
        with_path = spec_fingerprint(self._spec(provision_hash=None))
        no_hook = spec_fingerprint(self._spec(provision_hash=None, provision=None))
        assert with_path != no_hook
