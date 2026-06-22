"""Backend selection: route a composed spec to its lifecycle backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vergil_tooling.lib.vm_lima import LimaBackend

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_spec import ComposedSpec
    from vergil_tooling.lib.vm_transport import Transport


@runtime_checkable
class Backend(Protocol):
    """The lifecycle surface ``vrg_vm`` drives, independent of Lima vs cloud."""

    provider_label: str

    def transport(self, instance: str) -> Transport: ...

    def status(self, instance: str) -> str: ...


def select_backend(spec: ComposedSpec) -> Backend:
    """Return the backend for a composed spec — the one dispatch decision point."""
    if spec.off_platform:
        msg = "off-platform backend not yet available"
        raise NotImplementedError(msg)
    return LimaBackend()
