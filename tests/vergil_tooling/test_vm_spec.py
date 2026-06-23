"""Tests for vergil_tooling.lib.vm_spec."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from vergil_tooling.lib.config import RoleOverlay, VmStanza
from vergil_tooling.lib.vm_spec import (
    ComposedSpec,
    SpecError,
    compose_vm_spec,
    instance_name,
    lima_name_budget,
    parse_instance_name,
    spec_fingerprint,
    split_state_slug,
    state_slug,
    validate_instance_name,
    validate_repo_segment,
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
        port_forwards=["3000|10.50.0.2:3000"],
        roles={
            "vergil-user": RoleOverlay(
                packages=[],
                cpus=12,
                memory="64GiB",
                disk="300GiB",
                stale_days=7,
                apt_repos=[],
                vagrant_plugins=["vagrant-libvirt"],
                port_forwards=["8080|10.50.0.2:8080"],
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
        # base [vm] tier + role tier accumulate, deduped and sorted
        assert spec.port_forwards == ("3000|10.50.0.2:3000", "8080|10.50.0.2:8080")
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
        assert spec.port_forwards == ("3000|10.50.0.2:3000",)  # all-identity [vm] tier only
        assert spec.stale_days == 7

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

    def test_nested_defaults_false(self) -> None:
        base_spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=None, override=None)
        assert base_spec.nested is False
        mq_spec = compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=_mq_stanza(), override=None
        )
        assert mq_spec.nested is False

    def test_nested_true_at_vm_tier_applies_and_dedicates(self) -> None:
        stanza = VmStanza(
            packages=[],
            cpus=None,
            memory=None,
            disk=None,
            stale_days=None,
            apt_repos=[],
            vagrant_plugins=[],
            port_forwards=[],
            roles={},
            nested=True,
        )
        spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
        assert spec.nested is True
        assert spec.dedicated is True

    def test_role_nested_overrides_vm_tier_last_wins(self) -> None:
        stanza = VmStanza(
            packages=[],
            cpus=None,
            memory=None,
            disk=None,
            stale_days=None,
            apt_repos=[],
            vagrant_plugins=[],
            port_forwards=[],
            nested=True,
            roles={
                "vergil-user": RoleOverlay(
                    packages=[],
                    cpus=None,
                    memory=None,
                    disk=None,
                    stale_days=None,
                    apt_repos=[],
                    vagrant_plugins=[],
                    port_forwards=[],
                    nested=False,
                ),
            },
        )
        spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
        assert spec.nested is False

    def test_role_nested_alone_applies_only_to_that_role(self) -> None:
        stanza = VmStanza(
            packages=[],
            cpus=None,
            memory=None,
            disk=None,
            stale_days=None,
            apt_repos=[],
            vagrant_plugins=[],
            port_forwards=[],
            roles={
                "vergil-user": RoleOverlay(
                    packages=[],
                    cpus=None,
                    memory=None,
                    disk=None,
                    stale_days=None,
                    apt_repos=[],
                    vagrant_plugins=[],
                    port_forwards=[],
                    nested=True,
                ),
            },
        )
        user = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
        audit = compose_vm_spec(identity="vergil-audit", base=BASE, stanza=stanza, override=None)
        assert user.nested is True
        assert audit.nested is False

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


def _off_platform_stanza(**role_over: Any) -> VmStanza:
    """A canonical off-platform [vm] with backend at the [vm] tier and the rest in the role.

    `role_over` overrides the vergil-user role's keys (e.g. to omit one for a
    missing-required test). Defaults compose to a complete, valid off-platform profile.
    """
    role_fields: dict[str, Any] = {
        "provider": "gcp",
        "region": "us-central1",
        "instance": "n2-standard-16",
        "volume": "300GiB",
    }
    role_fields.update(role_over)
    return VmStanza(
        packages=[],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        backend="off-platform",
        roles={
            "vergil-user": RoleOverlay(
                packages=[],
                cpus=None,
                memory=None,
                disk=None,
                stale_days=None,
                apt_repos=[],
                vagrant_plugins=[],
                port_forwards=[],
                **role_fields,
            ),
        },
    )


class TestOffPlatformCompose:
    def test_local_is_the_default_backend(self) -> None:
        spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=None, override=None)
        assert spec.backend == "local"
        assert spec.off_platform is False
        assert spec.provider == ""
        assert spec.volume == ""

    def test_off_platform_keys_compose_across_tiers(self) -> None:
        # backend declared at the [vm] tier, the rest at the role tier — last-wins merge.
        spec = compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=_off_platform_stanza(), override=None
        )
        assert spec.off_platform is True
        assert spec.backend == "off-platform"
        assert spec.provider == "gcp"
        assert spec.region == "us-central1"
        assert spec.instance == "n2-standard-16"
        assert spec.volume == "300GiB"
        assert spec.dedicated is True  # declaring a backend dedicates the box

    def test_zone_is_optional_defaults_empty_and_overrides(self) -> None:
        # Omitted -> "" (the volume module falls back to ${region}-b).
        default_spec = compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=_off_platform_stanza(), override=None
        )
        assert default_spec.zone == ""
        # Declared in the role tier -> carried through the cascade (#1797).
        zoned = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=_off_platform_stanza(zone="us-central1-a"),
            override=None,
        )
        assert zoned.zone == "us-central1-a"

    def test_disk_is_carried_but_volume_is_authoritative_on_cloud(self) -> None:
        # `disk` stays at the base footprint (Lima knob); `volume` is the cloud size.
        spec = compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=_off_platform_stanza(), override=None
        )
        assert spec.disk == "50GiB"  # untouched base — ignored on the cloud path
        assert spec.volume == "300GiB"

    def test_missing_required_key_raises(self) -> None:
        # The role declares everything except `instance` → off-platform is missing one key.
        stanza = _off_platform_stanza(instance=None)
        with pytest.raises(SpecError, match="missing: instance"):
            compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)

    def test_missing_multiple_required_keys_listed(self) -> None:
        stanza = VmStanza(
            packages=[],
            cpus=None,
            memory=None,
            disk=None,
            stale_days=None,
            apt_repos=[],
            vagrant_plugins=[],
            port_forwards=[],
            backend="off-platform",
            roles={},
        )
        with pytest.raises(SpecError, match="provider, region, instance, volume"):
            compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)

    def test_unknown_backend_raises(self) -> None:
        stanza = VmStanza(
            packages=[],
            cpus=None,
            memory=None,
            disk=None,
            stale_days=None,
            apt_repos=[],
            vagrant_plugins=[],
            port_forwards=[],
            backend="frobnicate",
            roles={},
        )
        with pytest.raises(SpecError, match="backend must be one of"):
            compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)

    def test_bad_volume_format_raises(self) -> None:
        with pytest.raises(SpecError, match="volume must be '<number>GiB'"):
            compose_vm_spec(
                identity="vergil-user",
                base=BASE,
                stanza=_off_platform_stanza(volume="300"),
                override=None,
            )

    def test_audit_role_without_keys_does_not_satisfy_required(self) -> None:
        # backend is all-identity ([vm] tier) but the cloud keys live only in the
        # vergil-user role; vergil-audit therefore composes off-platform WITHOUT them
        # and must fail loudly rather than build an underspecified box.
        with pytest.raises(SpecError, match="missing: provider, region, instance, volume"):
            compose_vm_spec(
                identity="vergil-audit", base=BASE, stanza=_off_platform_stanza(), override=None
            )

    def test_host_override_can_flip_to_off_platform(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=None,
            override={
                "backend": "off-platform",
                "provider": "azure",
                "region": "eastus",
                "instance": "Standard_D16s_v5",
                "volume": "500GiB",
            },
        )
        assert spec.off_platform is True
        assert spec.provider == "azure"
        assert spec.instance == "Standard_D16s_v5"

    def test_host_override_can_resize_instance(self) -> None:
        spec = compose_vm_spec(
            identity="vergil-user",
            base=BASE,
            stanza=_off_platform_stanza(),
            override={"instance": "n2-standard-32"},
        )
        assert spec.instance == "n2-standard-32"
        assert spec.provider == "gcp"  # unchanged tiers survive


# Lima's instance-name validator. A valid identifier starts with an alphanumeric
# run and uses single '.', '_', or '-' separators, each followed by an alphanumeric
# run. Consecutive separators (e.g. '--') are rejected.
_LIMA_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


def test_lima_name_budget_subtracts_home_and_socket_overhead() -> None:
    # 104 - 1 - len(home) - (len("/.lima/")=7 + len("/ssh.sock.")=10 + 16) == 70 - len(home)
    assert lima_name_budget("/Users/pmoore") == 57
    assert lima_name_budget("/root") == 65
    assert lima_name_budget("/home/runner") == 58


class TestInstanceName:
    def test_base_is_bare_identity(self) -> None:
        assert instance_name("vergil-user", None, None) == "vergil-user"

    def test_base_when_only_org_given(self) -> None:
        assert instance_name("vergil-user", "org", None) == "vergil-user"

    def test_dedicated_is_dot_joined(self) -> None:
        # Pinned home keeps the within-budget format deterministic regardless of
        # the test runner's home-directory length.
        assert (
            instance_name(
                "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", home="/root"
            )
            == "vergil-user.logical-minds-foundry.mq-cluster-tooling"
        )

    def test_dedicated_name_is_valid_lima_identifier(self) -> None:
        name = instance_name(
            "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", home="/root"
        )
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
        name = instance_name("vergil-user", "acme", "foo.github.io", home="/root")
        assert _LIMA_IDENTIFIER.fullmatch(name)
        assert parse_instance_name(name) == ("vergil-user", "acme", "foo.github.io")

    def test_unchanged_when_within_budget(self) -> None:
        # 52 chars, fits the 57 budget for /Users/pmoore -> returned verbatim.
        name = instance_name(
            "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", home="/Users/pmoore"
        )
        assert name == "vergil-user.logical-minds-foundry.mq-cluster-tooling"

    def test_truncates_and_hashes_when_over_budget(self) -> None:
        # The reported failure: 61-char full name, 57 budget for /Users/pmoore.
        name = instance_name(
            "vergil-user",
            "logical-minds-foundry",
            "mq-resiliency-lab-for-linux",
            home="/Users/pmoore",
        )
        assert len(name) <= lima_name_budget("/Users/pmoore")
        assert _LIMA_IDENTIFIER.fullmatch(name)  # valid Lima instance name
        assert name.startswith("vergil-user.logical")  # readable prefix retained
        assert re.search(r"-[0-9a-f]{6}$", name)  # 6-char hash suffix

    def test_truncation_is_deterministic(self) -> None:
        args = ("vergil-user", "logical-minds-foundry", "mq-resiliency-lab-for-linux")
        assert instance_name(*args, home="/Users/pmoore") == instance_name(
            *args, home="/Users/pmoore"
        )

    def test_raises_when_budget_cannot_fit_identity(self) -> None:
        with pytest.raises(SpecError):
            instance_name("vergil-user", "o", "r", home="/" + "x" * 70)

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

    def test_instance_name_named_appends_segment(self) -> None:
        assert (
            instance_name("vergil-user", "lmf", "mq", name="cloud-x86")
            == "vergil-user.lmf.mq.cloud-x86"
        )

    def test_instance_name_default_unchanged(self) -> None:
        assert instance_name("vergil-user", "lmf", "mq") == "vergil-user.lmf.mq"
        assert instance_name("vergil-user", None, None) == "vergil-user"

    def test_instance_name_named_hash_differs_from_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the over-budget path with a tiny budget so both names are hashed.
        monkeypatch.setattr("vergil_tooling.lib.vm_spec.lima_name_budget", lambda home=None: 20)
        default = instance_name("vergil-user", "logical-minds-foundry", "mq-cluster-tooling")
        named = instance_name(
            "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", name="cloud-x86"
        )
        assert default != named


class TestFingerprint:
    def _spec(self, **over: Any) -> ComposedSpec:
        base: dict[str, Any] = {
            "cpus": 12,
            "memory": "64GiB",
            "disk": "300GiB",
            "stale_days": 7,
            "packages": ("a", "b"),
            "apt_repos": (_REPO,),
            "vagrant_plugins": ("vagrant-libvirt",),
            "port_forwards": (),
            "dedicated": True,
            "under": (),
        }
        base.update(over)
        return ComposedSpec(**base)

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

    def test_port_forwards_set_changes_fingerprint(self) -> None:
        # Going from no forwards to a declared forward flips the hash.
        assert spec_fingerprint(self._spec(port_forwards=())) != spec_fingerprint(
            self._spec(port_forwards=("3000|10.50.0.2:3000",))
        )

    def test_port_forwards_edit_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec(port_forwards=("3000|10.50.0.2:3000",))) != (
            spec_fingerprint(self._spec(port_forwards=("8080|10.50.0.2:8080",)))
        )

    def test_port_forwards_empty_keeps_legacy_fingerprint(self) -> None:
        """Profiles that never declare forwards must not flip on upgrade.

        Pins the encoding: ``port_forwards`` enters the payload only when
        non-empty, so every fingerprint stored before the knob existed
        stays valid.
        """
        legacy_payload = "\n".join(
            (
                "cpus=12",
                "memory=64GiB",
                "disk=300GiB",
                "stale_days=7",
                "packages=a,b",
                "apt_repos=" + "|".join(f"{k}={_REPO[k]}" for k in sorted(_REPO)),
                "vagrant_plugins=vagrant-libvirt",
            )
        )
        expected = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
        assert spec_fingerprint(self._spec(port_forwards=())) == expected

    def test_nested_toggle_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec(nested=True)) != spec_fingerprint(
            self._spec(nested=False)
        )

    def test_nested_false_keeps_legacy_fingerprint(self) -> None:
        """Profiles that never set nested must not flip to NEEDS-REBUILD on upgrade.

        Pins the encoding: ``nested`` enters the payload only when true, so
        every fingerprint stored before the knob existed stays valid.
        """
        legacy_payload = "\n".join(
            (
                "cpus=12",
                "memory=64GiB",
                "disk=300GiB",
                "stale_days=7",
                "packages=a,b",
                "apt_repos=" + "|".join(f"{k}={_REPO[k]}" for k in sorted(_REPO)),
                "vagrant_plugins=vagrant-libvirt",
            )
        )
        expected = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
        assert spec_fingerprint(self._spec(nested=False)) == expected

    # -- off-platform (cloud) keys (vergil-vm #199 / #1706) ------------------

    _CLOUD = {
        "backend": "off-platform",
        "provider": "gcp",
        "region": "us-central1",
        "instance": "n2-standard-16",
        "volume": "300GiB",
    }

    def _op_spec(self, **over: Any) -> ComposedSpec:
        return self._spec(**{**self._CLOUD, **over})

    def test_default_backend_keeps_legacy_fingerprint(self) -> None:
        """A local (default-backend) profile keeps its exact pre-#1706 fingerprint.

        Pins the acceptance invariant: the Lima path is byte-for-byte unchanged, so
        existing local VMs never falsely read NEEDS-REBUILD after this upgrade.
        """
        legacy_payload = "\n".join(
            (
                "cpus=12",
                "memory=64GiB",
                "disk=300GiB",
                "stale_days=7",
                "packages=a,b",
                "apt_repos=" + "|".join(f"{k}={_REPO[k]}" for k in sorted(_REPO)),
                "vagrant_plugins=vagrant-libvirt",
            )
        )
        expected = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
        assert spec_fingerprint(self._spec()) == expected

    def test_flip_local_to_off_platform_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._spec()) != spec_fingerprint(self._op_spec())

    def test_instance_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._op_spec(instance="n2-standard-16")) != spec_fingerprint(
            self._op_spec(instance="n2-standard-32")
        )

    def test_volume_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._op_spec(volume="300GiB")) != spec_fingerprint(
            self._op_spec(volume="500GiB")
        )

    def test_provider_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._op_spec(provider="gcp")) != spec_fingerprint(
            self._op_spec(provider="azure")
        )

    def test_region_change_changes_fingerprint(self) -> None:
        assert spec_fingerprint(self._op_spec(region="us-central1")) != spec_fingerprint(
            self._op_spec(region="us-east1")
        )

    def test_disk_ignored_in_off_platform_fingerprint(self) -> None:
        # `disk` is not a cloud knob: editing it on an off-platform profile must NOT
        # trip NEEDS-REBUILD.
        assert spec_fingerprint(self._op_spec(disk="300GiB")) == spec_fingerprint(
            self._op_spec(disk="999GiB")
        )

    def test_disk_still_matters_on_local(self) -> None:
        # The Lima path keeps `disk` in the fingerprint, unchanged.
        assert spec_fingerprint(self._spec(disk="300GiB")) != spec_fingerprint(
            self._spec(disk="500GiB")
        )

    def test_off_platform_payload_pinned(self) -> None:
        """Pins the off-platform encoding: no `disk`, cloud keys appended at the end."""
        payload = "\n".join(
            (
                "cpus=12",
                "memory=64GiB",
                "stale_days=7",
                "packages=a,b",
                "apt_repos=" + "|".join(f"{k}={_REPO[k]}" for k in sorted(_REPO)),
                "vagrant_plugins=vagrant-libvirt",
                "backend=off-platform",
                "provider=gcp",
                "region=us-central1",
                "instance=n2-standard-16",
                "volume=300GiB",
            )
        )
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert spec_fingerprint(self._op_spec()) == expected


# ---------------------------------------------------------------------------
# Task 2: Naming validators + tier-5 composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cloud-x86", "rdqm-rhel", "a", "x1"])
def test_validate_instance_name_accepts(name: str) -> None:
    validate_instance_name(name)  # no raise


@pytest.mark.parametrize("name", ["bad--name", "-lead", "trail-", "Up", "a_b", ""])
def test_validate_instance_name_rejects(name: str) -> None:
    with pytest.raises(ValueError, match="instance name"):
        validate_instance_name(name)


def test_validate_repo_segment_rejects_double_dash() -> None:
    with pytest.raises(ValueError, match="--"):
        validate_repo_segment("my--repo")
    validate_repo_segment("my-repo")  # single dash ok


def test_compose_named_instance_overlays_tier5() -> None:
    role = RoleOverlay(
        packages=[],
        cpus=12,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        instances={
            "rdqm-rhel": RoleOverlay(
                packages=[],
                cpus=8,
                memory="32GiB",
                disk=None,
                stale_days=None,
                apt_repos=[],
                vagrant_plugins=[],
                port_forwards=[],
                backend="off-platform",
                provider="gcp",
                region="us-central1",
                instance="n2-standard-8",
                volume="200GiB",
            )
        },
    )
    stanza = VmStanza(
        packages=[],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        roles={"vergil-user": role},
    )
    spec = compose_vm_spec(
        identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="rdqm-rhel"
    )
    assert spec.cpus == 8  # tier-5 overrides tier-4's 12
    assert spec.memory == "32GiB"
    assert spec.off_platform
    assert spec.instance == "n2-standard-8"


def test_compose_default_instance_unchanged_when_none() -> None:
    stanza = VmStanza(
        packages=[],
        cpus=12,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        roles={},
    )
    spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
    assert spec.cpus == 12  # tiers 1-4 only, today's behavior


def test_compose_missing_named_instance_errors_with_available() -> None:
    role = RoleOverlay(
        packages=[],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        instances={
            "cloud-x86": RoleOverlay(
                packages=[],
                cpus=None,
                memory=None,
                disk=None,
                stale_days=None,
                apt_repos=[],
                vagrant_plugins=[],
                port_forwards=[],
            )
        },
    )
    stanza = VmStanza(
        packages=[],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        roles={"vergil-user": role},
    )
    with pytest.raises(SpecError, match="cloud-x86"):
        compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="nope"
        )


def test_fingerprint_excludes_instance_name() -> None:
    # The name is the handle, not fingerprint content: a named instance and the
    # default that resolve to the SAME effective footprint share a fingerprint, so
    # adding/renaming an instance never trips drift on the others.
    role = RoleOverlay(
        packages=[],
        cpus=8,
        memory="32GiB",
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        instances={
            "rdqm-rhel": RoleOverlay(
                packages=[],
                cpus=8,
                memory="32GiB",
                disk=None,
                stale_days=None,
                apt_repos=[],
                vagrant_plugins=[],
                port_forwards=[],
            )
        },
    )
    stanza = VmStanza(
        packages=[],
        cpus=None,
        memory=None,
        disk=None,
        stale_days=None,
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        roles={"vergil-user": role},
    )
    default = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
    named = compose_vm_spec(
        identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="rdqm-rhel"
    )
    assert spec_fingerprint(default) == spec_fingerprint(named)


def test_state_slug_forms() -> None:
    assert state_slug("vergil-user") == "vergil-user"
    assert state_slug("vergil-user", "lmf", "mq") == "vergil-user--lmf--mq"
    assert state_slug("vergil-user", "lmf", "mq", "cloud-x86") == "vergil-user--lmf--mq--cloud-x86"


def test_split_state_slug_roundtrips() -> None:
    assert split_state_slug("vergil-user") == ("vergil-user", None, None, None)
    assert split_state_slug("vergil-user--lmf--mq") == ("vergil-user", "lmf", "mq", None)
    assert split_state_slug("vergil-user--lmf--mq--cloud-x86") == (
        "vergil-user",
        "lmf",
        "mq",
        "cloud-x86",
    )


def test_split_state_slug_invalid_raises() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        split_state_slug("a--b")  # 2 segments — not a valid form
