"""Tests for vergil_tooling.lib.vm_spec."""

from __future__ import annotations

import re

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

_REPO = {
    "name": "hashicorp",
    "key_url": "https://apt.releases.hashicorp.com/gpg",
    "uri": "https://apt.releases.hashicorp.com",
    "suite": "noble",
    "components": "main",
}


def _mq_stanza() -> VmStanza:
    return VmStanza(
        packages=["libvirt-clients", "qemu-system-x86"],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[_REPO],
        vagrant_plugins=[],
        roles={
            "vergil-user": RoleOverlay(
                packages=[],
                cpus=12,
                memory="64GiB",
                disk="300GiB",
                stale_days=7,
                apt_repos=[],
                vagrant_plugins=["vagrant-libvirt"],
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
        assert spec.apt_repos == (_REPO,)  # from [vm] tier
        assert spec.vagrant_plugins == ("vagrant-libvirt",)  # from the role tier
        assert spec.under == ()

    def test_audit_gets_packages_only_at_base_footprint(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-audit", base=BASE, stanza=_mq_stanza(), override=None
        )
        assert spec.dedicated is True  # packages + apt_repos customize it
        assert spec.cpus == 4
        assert spec.memory == "4GiB"  # base footprint, role overlay did not apply
        assert spec.packages == ("libvirt-clients", "qemu-system-x86")
        assert spec.apt_repos == (_REPO,)  # all-identity [vm] tier
        assert spec.vagrant_plugins == ()  # role-only, did not apply to audit
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


# Lima's instance-name validator. A valid identifier starts with an alphanumeric
# run and uses single '.', '_', or '-' separators, each followed by an alphanumeric
# run. Consecutive separators (e.g. '--') are rejected.
_LIMA_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


class TestInstanceName:
    def test_base_is_bare_identity(self) -> None:
        assert instance_name("vergil-user", None, None) == "vergil-user"

    def test_base_when_only_org_given(self) -> None:
        assert instance_name("vergil-user", "org", None) == "vergil-user"

    def test_dedicated_is_dot_joined(self) -> None:
        assert (
            instance_name("vergil-user", "logical-minds-foundry", "mq-cluster-tooling")
            == "vergil-user.logical-minds-foundry.mq-cluster-tooling"
        )

    def test_dedicated_name_is_valid_lima_identifier(self) -> None:
        name = instance_name("vergil-user", "logical-minds-foundry", "mq-cluster-tooling")
        assert _LIMA_IDENTIFIER.fullmatch(name)

    def test_roundtrip_dedicated(self) -> None:
        name = "vergil-user.logical-minds-foundry.mq-cluster-tooling"
        assert parse_instance_name(name) == (
            "vergil-user",
            "logical-minds-foundry",
            "mq-cluster-tooling",
        )

    def test_roundtrip_dedicated_repo_with_dots(self) -> None:
        # The repo is the final tier, so dots within it round-trip intact.
        name = instance_name("vergil-user", "acme", "foo.github.io")
        assert _LIMA_IDENTIFIER.fullmatch(name)
        assert parse_instance_name(name) == ("vergil-user", "acme", "foo.github.io")

    def test_roundtrip_base(self) -> None:
        assert parse_instance_name("vergil-user") == ("vergil-user", None, None)

    def test_identity_with_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain '.'"):
            instance_name("bad.identity", "org", "repo")

    def test_org_with_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain '.'"):
            instance_name("vergil-user", "bad.org", "repo")

    def test_unparseable_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable VM instance name"):
            parse_instance_name("a.b")


class TestFingerprint:
    def _spec(self, **over: object) -> ComposedSpec:
        base: dict[str, object] = {
            "cpus": 12,
            "memory": "64GiB",
            "disk": "300GiB",
            "stale_days": 7,
            "packages": ("a", "b"),
            "apt_repos": (_REPO,),
            "vagrant_plugins": ("vagrant-libvirt",),
            "dedicated": True,
            "under": (),
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

    def test_apt_repos_change_changes_fingerprint(self) -> None:
        other = {**_REPO, "uri": "https://example.com"}
        assert spec_fingerprint(self._spec(apt_repos=(_REPO,))) != spec_fingerprint(
            self._spec(apt_repos=(other,))
        )

    def test_vagrant_plugins_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec(vagrant_plugins=("a",))) != spec_fingerprint(
            self._spec(vagrant_plugins=("a", "b"))
        )
