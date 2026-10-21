"""Language version resolution from the published support window.

The window is the set of language versions ``vergil-containers`` builds,
published as ``docker/versions/window.json`` at each Vergil ref (epic
vergil-project/.github#407, spec §6.1). :mod:`.window` loads it and
:mod:`.resolve` turns a repo's ``vergil.toml`` into the versions its CI builds.
That resolver is the only implementation of version resolution: the container
image, ``vrg-validate``, the GitHub gates and (through ``vrg-ci-resolve``) the
CI setup step all call it (spec §6.3, "One resolver").

Shared code here has no language branches. Language specifics live only in
:mod:`.extensions`, keyed by the ``primary-language`` value.
"""

from __future__ import annotations


class VersionsError(Exception):
    """Base for every version-window and resolution failure.

    Callers that turn a failure into an exit code catch this one type.
    """


class ResolutionError(VersionsError):
    """A repo's version configuration does not resolve against the window.

    Defined here, not in :mod:`.resolve`, so a language extension can raise it
    without importing the resolver that imports the extensions.
    """
