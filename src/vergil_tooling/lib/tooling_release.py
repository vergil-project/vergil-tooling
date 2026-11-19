"""Resolve the vergil-tooling release a ``[dependencies].vergil`` pin installs (#4550).

A line pin (``v2.1``) installs whatever release is newest on the line when the
cached dev image is built. The cache key folds in the release the pin resolves
to *now*, so a new release on the line rebuilds every branch cache instead of
leaving it on the old tooling.

The resolution is :func:`vergil_tooling.lib.tooling_install.resolve`: one
``git ls-remote`` of the vergil-tooling repository, giving the newest tag on
the line and its commit SHA, the same commit the cache build installs (epic
vergil-project/.github#1350, spec §4.2). It is read on every run, never cached
and never replaced by a guess. A failed listing, or a line with no release
tag, raises :class:`ToolingResolveError`.

An exact pin (``v2.1.320``) makes no network call: its key is the tag itself,
which tag protection keeps immutable, and the cache build resolves its SHA
inside the build. So an exact pin is the explicit way to work offline. A dev
ref (``develop``) resolves to itself, also with no call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import tooling_install, vm_packages

if TYPE_CHECKING:
    from collections.abc import Callable

    from vergil_tooling.lib.tooling_install import ResolvedRef

# The listing seam: tests replace it with a fixture listing (conftest).
_ls_remote = tooling_install.git_ls_remote


class ToolingResolveError(RuntimeError):
    """The release a line pin resolves to could not be determined."""


def resolve_tooling_tag(
    pin: str,
    *,
    ls_remote: Callable[[str, str], str] | None = None,
) -> ResolvedRef:
    """Return what *pin* installs: ``v2.1`` becomes ``v2.1.320`` and its commit SHA.

    A line is resolved with one *ls_remote* call (default: ``git ls-remote``,
    :func:`~vergil_tooling.lib.tooling_install.git_ls_remote`). An exact release
    returns with its tag and no SHA, and a dev ref with neither, both without a
    call. Raises :class:`ToolingResolveError` when the listing fails or the
    line has no release tag.
    """
    try:
        if vm_packages.classify_ref(pin) == "exact":
            return tooling_install.unresolved(pin)
        return tooling_install.resolve(pin, ls_remote=ls_remote or _ls_remote)
    except tooling_install.ToolingInstallError as exc:
        raise ToolingResolveError(str(exc)) from exc
