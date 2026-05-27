"""Version divergence comparison between head and main branches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DivergenceStatus(Enum):
    DIVERGED = "diverged"
    FIRST_RELEASE = "first-release"
    EQUAL = "equal"


@dataclass(frozen=True)
class DivergenceResult:
    status: DivergenceStatus
    head_version: str
    main_version: str


def compare_versions(head: str, main: str | None) -> DivergenceResult:
    """Compare head and main version strings.

    Returns a result indicating whether versions have diverged,
    are equal (not bumped), or this is a first release (no prior
    version on main).
    """
    if not main:
        return DivergenceResult(
            status=DivergenceStatus.FIRST_RELEASE,
            head_version=head,
            main_version="",
        )
    if head != main:
        return DivergenceResult(
            status=DivergenceStatus.DIVERGED,
            head_version=head,
            main_version=main,
        )
    return DivergenceResult(
        status=DivergenceStatus.EQUAL,
        head_version=head,
        main_version=main,
    )
