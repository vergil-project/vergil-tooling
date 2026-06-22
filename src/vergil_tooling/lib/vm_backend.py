"""Backend selection: route a composed spec to its lifecycle backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vergil_tooling.lib.vm_cloud import OffPlatformBackend
from vergil_tooling.lib.vm_lima import LimaBackend

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_spec import ComposedSpec
    from vergil_tooling.lib.vm_transport import Transport


@runtime_checkable
class Backend(Protocol):
    """The lifecycle surface ``vrg_vm`` drives, independent of Lima vs cloud."""

    provider_label: str

    def transport(self, instance: str) -> Transport: ...  # pragma: no cover

    def status(self, instance: str) -> str: ...  # pragma: no cover


def select_backend(
    spec: ComposedSpec,
    *,
    identity: str | None = None,
    org: str | None = None,
    repo: str | None = None,
) -> Backend:
    """Return the backend for a composed spec — the one dispatch decision point.

    Off-platform specs require ``identity``/``org``/``repo`` to derive the cloud
    resource name and labels; a missing one is a programming error and fails loudly.
    """
    if spec.off_platform:
        if identity is None or org is None or repo is None:
            msg = "off-platform backend requires identity, org, and repo"
            raise ValueError(msg)
        return OffPlatformBackend(spec, identity, org, repo)
    return LimaBackend()
