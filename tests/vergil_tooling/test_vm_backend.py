import dataclasses

import pytest

from vergil_tooling.lib.vm_backend import LimaBackend, select_backend
from vergil_tooling.lib.vm_cloud import OffPlatformBackend
from vergil_tooling.lib.vm_spec import ComposedSpec
from vergil_tooling.lib.vm_transport import LimaTransport


def _spec(backend: str = "local", **kw: object) -> ComposedSpec:
    spec = ComposedSpec(
        cpus=4,
        memory="4GiB",
        disk="50GiB",
        stale_days=3,
        packages=(),
        apt_repos=(),
        vagrant_plugins=(),
        port_forwards=(),
        dedicated=False,
        under=(),
        nested=False,
        backend=backend,
    )
    return dataclasses.replace(spec, **kw)


class TestSelectBackend:
    def test_local_returns_lima_backend(self) -> None:
        backend = select_backend(_spec("local"))
        assert isinstance(backend, LimaBackend)
        assert backend.provider_label == "local"
        assert isinstance(backend.transport("vm-x"), LimaTransport)

    def test_off_platform_returns_off_platform_backend(self) -> None:
        spec = _spec(
            "off-platform",
            provider="gcp",
            region="us-central1",
            instance="n2-standard-16",
            volume="300GiB",
        )
        backend = select_backend(spec, identity="vergil-user", org="o", repo="r")
        assert isinstance(backend, OffPlatformBackend)
        assert backend.provider_label == "gcp"

    def test_off_platform_requires_identity_org_repo(self) -> None:
        spec = _spec(
            "off-platform",
            provider="gcp",
            region="us-central1",
            instance="n2-standard-16",
            volume="300GiB",
        )
        with pytest.raises(ValueError, match="identity, org, and repo"):
            select_backend(spec)


class TestLimaBackendStatus:
    def test_status_delegates_to_lima_vm_status(self) -> None:
        from unittest.mock import patch

        with patch("vergil_tooling.lib.vm_lima.lima.vm_status", return_value="Running") as m:
            assert LimaBackend().status("vm-x") == "Running"
            m.assert_called_once_with("vm-x")
