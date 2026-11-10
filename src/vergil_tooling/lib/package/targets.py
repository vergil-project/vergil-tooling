"""Registry of supported package targets (spec §5.1).

Each target is ``<distro>/<version>/<arch>``. The distro implies the package
format (Ubuntu → ``.deb``, RHEL → ``.rpm``); the registry also records each
target's glibc version (the build-time glibc guard) and its apt suite or EL
release. Its container image is derived, never listed: the managed toolchain
image ``ghcr.io/vergil-project/prod-pkg-<distro>:<version>`` built by
vergil-containers (epic vergil-project/.github#607), so no target can keep a
raw image. Adding a target (e.g. Ubuntu 28.04) needs that toolchain image
published, then a change to this file and a vergil-tooling release — no
workflow change.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

_RPM_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}

# Where vergil-containers publishes the package toolchain images.
TOOLCHAIN_REGISTRY = "ghcr.io/vergil-project"


@dataclass(frozen=True)
class Target:
    distro: str
    version: str
    arch: str
    glibc: tuple[int, int]
    suite: str

    @property
    def key(self) -> str:
        return f"{self.distro}/{self.version}/{self.arch}"

    @property
    def image(self) -> str:
        """The managed toolchain image this target's jobs run in."""
        return f"{TOOLCHAIN_REGISTRY}/prod-pkg-{self.distro}:{self.version}"

    @property
    def fmt(self) -> str:
        return "deb" if self.distro == "ubuntu" else "rpm"

    @property
    def rpm_arch(self) -> str:
        return _RPM_ARCH[self.arch]


ARCHES = ("amd64", "arm64")

# (version, suite, glibc). glibc is the upstream image's `ldd --version`
# (`ubuntu:<v>`, `ubi<v>/ubi`), verified against each image (arm64) when the
# registry was introduced (issue #3074); the toolchain images keep that glibc.
_UBUNTU: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("24.04", "noble", (2, 39)),
    ("26.04", "resolute", (2, 43)),
)
_RHEL: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("9", "el9", (2, 34)),
    ("10", "el10", (2, 39)),
)


def _build() -> dict[str, Target]:
    reg: dict[str, Target] = {}
    for distro, rows in (("ubuntu", _UBUNTU), ("rhel", _RHEL)):
        for version, suite, glibc in rows:
            for arch in ARCHES:
                t = Target(distro, version, arch, glibc, suite)
                reg[t.key] = t
    return reg


REGISTRY: dict[str, Target] = _build()


def all_targets() -> list[Target]:
    """Every registered target, sorted by key."""
    return sorted(REGISTRY.values(), key=lambda t: t.key)


def match(pattern: str) -> list[Target]:
    """Registered targets whose key matches the glob ``pattern``, sorted by key."""
    return [t for t in all_targets() if fnmatchcase(t.key, pattern)]
