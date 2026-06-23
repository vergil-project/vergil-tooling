"""Compose, name, and fingerprint per-repo VM specs (pure logic, no I/O)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vergil_tooling.lib.config import RoleOverlay, VmStanza

_DEFAULT_STALE_DAYS = 7

# Backend dispatch values. "local" is the default Lima driver; "off-platform" is the
# remote cloud (OpenTofu) driver (vergil-vm #199). The set is closed and tooling-owned
# — an unrecognized backend is a typo and must fail loudly (no-silent-failures).
_BACKEND_LOCAL = "local"
_BACKEND_OFF_PLATFORM = "off-platform"
_VALID_BACKENDS = (_BACKEND_LOCAL, _BACKEND_OFF_PLATFORM)

# Keys an off-platform profile must declare (the composer hard-errors if any is
# missing). `provider`/`region`/`instance` are provider-native opaque strings — the
# tooling does NOT enumerate them, so adding a provider stays a vergil-vm module
# change with no code change here. `volume` is the persistent-disk size and is
# format-checked (`<N>GiB`); it never falls back to `disk` (`disk` is not a cloud knob).
_OFF_PLATFORM_REQUIRED = ("provider", "region", "instance", "volume")

_SIZE_RE = re.compile(r"^\d+GiB$")


class SpecError(Exception):
    """A composed VM spec is internally invalid (e.g. a misconfigured off-platform profile).

    Raised by ``compose_vm_spec`` for a profile that cannot describe a buildable VM —
    an unknown ``backend``, or ``backend = "off-platform"`` missing a required key.
    Callers (``vrg-vm``) catch it and print the message rather than crash.
    """


def _gib(value: str) -> int:
    """Parse a ``"<N>GiB"`` string to its integer prefix."""
    return int(value.removesuffix("GiB"))


@dataclass
class ComposedSpec:
    cpus: int
    memory: str
    disk: str
    stale_days: int
    packages: tuple[str, ...]
    # Declarative non-apt install: extra apt repositories (key + source line) and Vagrant
    # plugins. The vergil-vm template owns *how* these install — no repo-supplied code.
    apt_repos: tuple[dict[str, str], ...]
    vagrant_plugins: tuple[str, ...]
    # "<port>|<host:port>" relay records the vergil-vm template proxies into the
    # guest (vergil-vm #170). Additive across the [vm]/[vm.<role>] cascade.
    port_forwards: tuple[str, ...]
    dedicated: bool
    under: tuple[str, ...]
    # Per-profile nested virtualization (issue #1447): default off, last-wins
    # through the [vm]/[vm.<role>] cascade like the footprint scalars.
    nested: bool = False
    # Off-platform (cloud) backend (vergil-vm #199 / #1706). `backend` defaults to
    # "local" (Lima); "off-platform" flips the downstream dispatcher to the OpenTofu
    # driver. provider/region/instance/volume are required when off-platform and
    # empty otherwise. `disk` (the Lima single-disk knob) is ignored on the
    # off-platform path — `volume` is the authoritative persistent-disk size.
    backend: str = _BACKEND_LOCAL
    provider: str = ""
    region: str = ""
    instance: str = ""
    volume: str = ""
    # Optional explicit zone (vergil-tooling #1797). Empty -> the volume module's
    # ${region}-b default; set it to dodge a per-zone capacity stockout in the region.
    zone: str = ""

    @property
    def off_platform(self) -> bool:
        """True when this spec resolves to the remote cloud (OpenTofu) backend."""
        return self.backend == _BACKEND_OFF_PLATFORM


@dataclass
class _Acc:
    """Mutable accumulator threaded through the overlay tiers."""

    cpus: int
    memory: str
    disk: str
    stale_days: int
    packages: list[str]
    apt_repos: list[dict[str, str]]
    vagrant_plugins: list[str]
    port_forwards: list[str]
    customized: bool
    nested: bool
    # Off-platform backend scalars, last-wins through the cascade. backend starts at
    # the "local" default; the rest are empty until a tier declares them.
    backend: str
    provider: str
    region: str
    instance: str
    volume: str
    zone: str
    # Repo-declared footprint (tiers 3+4 only) — the floor an override is measured
    # against. None means the repo never declared that scalar, so no floor applies.
    declared_cpus: int | None
    declared_mem: int | None
    declared_disk: int | None


def _apply_overlay(acc: _Acc, overlay: VmStanza | RoleOverlay) -> None:
    """Overlay a repo `[vm]` or `[vm.<role>]` table onto the accumulator."""
    if overlay.packages:
        acc.packages.extend(overlay.packages)
        acc.customized = True
    if overlay.apt_repos:
        acc.apt_repos.extend(overlay.apt_repos)
        acc.customized = True
    if overlay.vagrant_plugins:
        acc.vagrant_plugins.extend(overlay.vagrant_plugins)
        acc.customized = True
    if overlay.port_forwards:
        acc.port_forwards.extend(overlay.port_forwards)
        acc.customized = True
    if overlay.cpus is not None:
        acc.cpus = acc.declared_cpus = overlay.cpus
        acc.customized = True
    if overlay.memory is not None:
        acc.memory = overlay.memory
        acc.declared_mem = _gib(overlay.memory)
        acc.customized = True
    if overlay.disk is not None:
        acc.disk = overlay.disk
        acc.declared_disk = _gib(overlay.disk)
        acc.customized = True
    if overlay.stale_days is not None:
        acc.stale_days = overlay.stale_days
        acc.customized = True
    if overlay.nested is not None:
        acc.nested = overlay.nested
        acc.customized = True
    # Off-platform scalars: last-wins, and any declaration dedicates the box.
    if overlay.backend is not None:
        acc.backend = overlay.backend
        acc.customized = True
    if overlay.provider is not None:
        acc.provider = overlay.provider
        acc.customized = True
    if overlay.region is not None:
        acc.region = overlay.region
        acc.customized = True
    if overlay.instance is not None:
        acc.instance = overlay.instance
        acc.customized = True
    if overlay.volume is not None:
        acc.volume = overlay.volume
        acc.customized = True
    if overlay.zone is not None:
        acc.zone = overlay.zone
        acc.customized = True


def compose_vm_spec(
    *,
    identity: str,
    base: Mapping[str, object],
    stanza: VmStanza | None,
    override: Mapping[str, object] | None,
    instance: str | None = None,
) -> ComposedSpec:
    """Overlay the precedence tiers into the effective spec for one (identity, repo[, name])."""
    # Tier 1+2: built-in/base footprint from the identity.
    acc = _Acc(
        cpus=cast("int", base["cpus"]),
        memory=str(base["memory"]),
        disk=str(base["disk"]),
        stale_days=_DEFAULT_STALE_DAYS,
        packages=[],
        apt_repos=[],
        vagrant_plugins=[],
        port_forwards=[],
        customized=False,
        nested=False,
        backend=_BACKEND_LOCAL,
        provider="",
        region="",
        instance="",
        volume="",
        zone="",
        declared_cpus=None,
        declared_mem=None,
        declared_disk=None,
    )

    # Tiers 3 + 4: repo [vm] (all-identity), then [vm.<identity>] role overlay.
    role = None
    if stanza is not None:
        _apply_overlay(acc, stanza)
        role = stanza.roles.get(identity)
        if role is not None:
            _apply_overlay(acc, role)

    # Tier 5: the named-instance overlay, if a name was requested.
    if instance is not None:
        instances = role.instances if role is not None else {}
        overlay = instances.get(instance)
        if overlay is None:
            avail = ", ".join(sorted(instances)) if instances else "(none)"
            msg = (
                f"identity {identity!r}: no instance {instance!r} for this repo; available: {avail}"
            )
            raise SpecError(msg)
        _apply_overlay(acc, overlay)

    # Host override (wins). Flag any scalar pushed below the repo-declared floor.
    under: list[str] = []
    if override:
        acc.customized = True
        if "cpus" in override:
            acc.cpus = cast("int", override["cpus"])
            if acc.declared_cpus is not None and acc.cpus < acc.declared_cpus:
                under.append("cpus")
        if "memory" in override:
            acc.memory = str(override["memory"])
            if acc.declared_mem is not None and _gib(acc.memory) < acc.declared_mem:
                under.append("mem")
        if "disk" in override:
            acc.disk = str(override["disk"])
            if acc.declared_disk is not None and _gib(acc.disk) < acc.declared_disk:
                under.append("disk")
        if "stale_days" in override:
            acc.stale_days = cast("int", override["stale_days"])
        # The off-platform scalars also cascade through the host-override tier
        # (built-in → identity → [vm] → [vm.<identity>] → identities.toml override).
        for key in ("backend", "provider", "region", "instance", "volume", "zone"):
            if key in override:
                setattr(acc, key, str(override[key]))

    _validate_backend(identity, acc)

    return ComposedSpec(
        cpus=acc.cpus,
        memory=acc.memory,
        disk=acc.disk,
        stale_days=acc.stale_days,
        packages=tuple(sorted(set(acc.packages))),
        apt_repos=tuple(acc.apt_repos),
        vagrant_plugins=tuple(sorted(set(acc.vagrant_plugins))),
        port_forwards=tuple(sorted(set(acc.port_forwards))),
        dedicated=acc.customized,
        under=tuple(under),
        nested=acc.nested,
        backend=acc.backend,
        provider=acc.provider,
        region=acc.region,
        instance=acc.instance,
        volume=acc.volume,
        zone=acc.zone,
    )


def _validate_backend(identity: str, acc: _Acc) -> None:
    """Enforce the backend enum and the off-platform required-key contract.

    Runs after the full cascade is resolved, so a profile can legitimately split the
    keys across tiers (e.g. ``backend`` in ``[vm]``, ``instance`` in ``[vm.<identity>]``).
    Fails loudly — never silently defaults a missing cloud key.
    """
    if acc.backend not in _VALID_BACKENDS:
        valid = ", ".join(repr(b) for b in _VALID_BACKENDS)
        msg = f"identity {identity!r}: [vm] backend must be one of {valid}, got {acc.backend!r}"
        raise SpecError(msg)
    if acc.backend != _BACKEND_OFF_PLATFORM:
        return
    missing = [key for key in _OFF_PLATFORM_REQUIRED if not getattr(acc, key)]
    if missing:
        msg = (
            f'identity {identity!r}: backend = "off-platform" requires '
            f"{', '.join(_OFF_PLATFORM_REQUIRED)}; missing: {', '.join(missing)}"
        )
        raise SpecError(msg)
    if not _SIZE_RE.fullmatch(acc.volume):
        msg = (
            f"identity {identity!r}: [vm] volume must be '<number>GiB' "
            f'(e.g. "300GiB"), got {acc.volume!r}'
        )
        raise SpecError(msg)


# Lima instance names must match ^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$ — single
# separators only, so the previous ``--`` join produced names limactl rejects.
# The three tiers are joined with a single ``.``. The repo tier is encoded last
# and may itself contain ``.``/``_`` (GitHub allows them), so parsing splits on
# the first two dots and treats the remainder as the repo. That round-trip is
# only unambiguous if the identity and org tiers contain no dots; instance_name
# enforces that loudly rather than silently producing a name that won't decode.
_TIER_SEP = "."

_INSTANCE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_instance_name(name: str) -> None:
    """Reject an instance name that is not [a-z0-9]+ with single internal hyphens.

    A double dash would break the readable ``--``-joined state slug; an empty or
    upper-cased name is also rejected. Raises loudly (no-silent-failures).
    """
    if not _INSTANCE_NAME_RE.fullmatch(name):
        msg = (
            f"instance name {name!r} must be lowercase [a-z0-9-] with single internal "
            f"hyphens (no '--', no leading/trailing hyphen)"
        )
        raise ValueError(msg)


def validate_repo_segment(repo: str) -> None:
    """Reject a repo name containing '--', which would make the state slug ambiguous."""
    if "--" in repo:
        msg = (
            f"repo name {repo!r} must not contain '--' (it would make the "
            f"'--'-joined instance handle ambiguous)"
        )
        raise ValueError(msg)


_UNIX_PATH_MAX = 104
# Lima validates the longest socket path it might create:
#   <home>/.lima/<instance>/ssh.sock.<16-char worst-case reservation>
# The "/.lima/" and "/ssh.sock." segments and the 16-char reservation are fixed;
# only the instance name is ours to bound. Strict-less-than, hence the -1.
_SOCK_OVERHEAD = len("/.lima/") + len("/ssh.sock.") + 16  # 7 + 10 + 16 = 33


def lima_name_budget(home: str | None = None) -> int:
    """Max instance-name length that keeps Lima's socket path under UNIX_PATH_MAX."""
    home = home if home is not None else str(Path.home())
    return (_UNIX_PATH_MAX - 1) - len(home) - _SOCK_OVERHEAD


def instance_name(
    identity: str,
    org: str | None,
    repo: str | None,
    name: str | None = None,
    *,
    home: str | None = None,
) -> str:
    """Derive the Lima instance name. Bare identity = base; ``.``-joined = dedicated.

    A named instance appends ``.<name>`` as a fourth segment. Over budget, the name
    is truncated and hashed (the digest input includes ``name`` so distinct instances
    differ); ``recover_handle`` (vrg_vm) reverses a mangled name via the sidecar.
    """
    if org is None or repo is None:
        return identity
    for tier, value in (("identity", identity), ("org", org)):
        if _TIER_SEP in value:
            msg = f"{tier} name {value!r} must not contain '{_TIER_SEP}'"
            raise ValueError(msg)
    segments = [identity, org, repo]
    if name:
        segments.append(name)
    full = _TIER_SEP.join(segments)
    budget = lima_name_budget(home)
    if len(full) <= budget:
        return full
    if budget < len(identity) + 7:  # 6 hash chars + 1 separator
        msg = (
            f"home directory too long to fit a bounded VM name for identity "
            f"{identity!r}: budget {budget} < {len(identity) + 7}"
        )
        raise SpecError(msg)
    digest_src = "/".join(segments)
    digest = hashlib.sha256(digest_src.encode()).hexdigest()[:6]
    keep = budget - 7
    return f"{full[:keep].rstrip('._-')}-{digest}"


_SLUG_SEP = "--"


def state_slug(
    identity: str, org: str | None = None, repo: str | None = None, name: str | None = None
) -> str:
    """The readable '--'-joined handle: state-path key and cloud-name hash input.

    ``identity`` (base) / ``identity--org--repo`` (default dedicated) /
    ``identity--org--repo--name`` (named). Reversal is via labels (cloud) and the
    sidecar (Lima), not by splitting — but '--' keeps the path human-readable.
    """
    if org is None or repo is None:
        return identity
    segments = [identity, org, repo]
    if name:
        segments.append(name)
    return _SLUG_SEP.join(segments)


def split_state_slug(slug: str) -> tuple[str, str | None, str | None, str | None]:
    """Reverse state_slug. 1 segment = base; 3 = default dedicated; 4 = named instance.

    Unambiguous because identity/org/repo never contain '--' (repo names with '--'
    are rejected at parse time), so this is the exact inverse — no labels needed.

    Deliberate symmetry: ``split_state_slug`` uses ``'--'`` (cloud/state paths);
    ``parse_instance_name`` uses ``'.'`` (Lima instance names). The delimiters and
    segment rules differ — do NOT merge them.
    """
    parts = slug.split(_SLUG_SEP)
    if len(parts) == 1:
        return parts[0], None, None, None
    if len(parts) == 3:  # noqa: PLR2004
        return parts[0], parts[1], parts[2], None
    if len(parts) == 4:  # noqa: PLR2004
        return parts[0], parts[1], parts[2], parts[3]
    msg = f"unparseable state slug: {slug!r}"
    raise ValueError(msg)


def parse_instance_name(name: str) -> tuple[str, str | None, str | None]:
    """Reverse instance_name. Returns (identity, org, repo); org/repo are None for base."""
    parts = name.split(_TIER_SEP, 2)
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 3:  # noqa: PLR2004
        return parts[0], parts[1], parts[2]
    msg = f"unparseable VM instance name: {name!r}"
    raise ValueError(msg)


def _repo_key(repo: dict[str, str]) -> str:
    return "|".join(f"{k}={repo[k]}" for k in sorted(repo))


def spec_fingerprint(spec: ComposedSpec) -> str:
    """Stable SHA-256 over the declaration (NOT the resulting image bytes).

    ``under`` and ``dedicated`` are excluded: they are derived view-state, not part of
    what the VM is built from. Packages, apt repos, and vagrant plugins are sorted so
    order cannot change the hash. Editing any declarative field flips the fingerprint.
    """
    fields = [
        f"cpus={spec.cpus}",
        f"memory={spec.memory}",
    ]
    # `disk` is the Lima single-disk knob and is NOT a cloud knob: on the off-platform
    # path it is ignored (the persistent `volume` is authoritative), so it stays out of
    # the off-platform payload — editing it on a cloud profile must not trip a rebuild.
    # On the local path it remains in its historical position, byte-for-byte, so every
    # Lima fingerprint stored before this change stays valid.
    if not spec.off_platform:
        fields.append(f"disk={spec.disk}")
    fields.extend(
        (
            f"stale_days={spec.stale_days}",
            "packages=" + ",".join(sorted(spec.packages)),
            "apt_repos=" + ",".join(sorted(_repo_key(r) for r in spec.apt_repos)),
            "vagrant_plugins=" + ",".join(sorted(spec.vagrant_plugins)),
        )
    )
    # port_forwards enters the payload only when non-empty, for the same reason
    # as nested below: profiles that never declare forwards keep the fingerprint
    # they had before the knob existed (no spurious NEEDS-REBUILD on upgrade),
    # while adding/editing forwards flips the hash.
    if spec.port_forwards:
        fields.append("port_forwards=" + ",".join(sorted(spec.port_forwards)))
    # nested enters the payload only when true: profiles that never set it keep
    # the fingerprint they had before the knob existed (no spurious NEEDS-REBUILD
    # on upgrade), while toggling it flips the hash in both directions.
    if spec.nested:
        fields.append("nested=true")
    # The off-platform keys enter only when backend = "off-platform", so every local
    # (Lima) profile keeps its pre-existing fingerprint — the local default is byte-for-
    # byte unchanged (issue #1706 acceptance: the Lima path is untouched). Flipping a
    # repo Lima→cloud, or resizing the instance/volume, trips NEEDS-REBUILD as expected.
    if spec.off_platform:
        fields.append(f"backend={spec.backend}")
        fields.append(f"provider={spec.provider}")
        fields.append(f"region={spec.region}")
        fields.append(f"instance={spec.instance}")
        fields.append(f"volume={spec.volume}")
    payload = "\n".join(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
