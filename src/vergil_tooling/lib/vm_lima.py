"""LimaBackend — the local limactl lifecycle behind the Backend protocol."""

from __future__ import annotations

from vergil_tooling.lib import lima
from vergil_tooling.lib.vm_transport import LimaTransport, Transport


class LimaBackend:
    """The default local backend: a Lima VM reached over ``limactl shell``."""

    provider_label = "local"

    def transport(self, instance: str) -> Transport:
        return LimaTransport(instance)

    def status(self, instance: str) -> str:
        return lima.vm_status(instance)
