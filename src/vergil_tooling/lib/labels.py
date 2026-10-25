"""Label registry: load the canonical label set from ``data/labels.json``.

Also home of :func:`ensure_labels`, the one point-of-use primitive every
label-applying command calls before it applies a label (#3373), of
:data:`RETIRED_LABELS`, and of :func:`sync_labels`, the one whole-registry sync
shared by ``vrg-ensure-label --sync`` and ``vrg-repo-init`` (#3558).
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Sequence

# The pre-epic-#176 intake kind labels: parent, not label, is now the state. No
# command applies them, the sync deletes them, and the orphan pass strips them.
RETIRED_LABELS = frozenset({"triage", "idea", "research"})

# Generous ceiling for ``gh label list``: a managed repo carries a few dozen.
_LABEL_LIST_LIMIT = "1000"


class UnknownLabelError(ValueError):
    """A label a command was asked to apply is not in the canonical registry."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)
        quoted = ", ".join(repr(name) for name in self.names)
        noun = "label" if len(self.names) == 1 else "labels"
        super().__init__(
            f"unknown {noun} {quoted}: not in the canonical label registry "
            "(vergil-tooling data/labels.json). Use a registered label (see "
            "`vrg-ensure-label --sync`), or add it to the registry in "
            "vergil-tooling first."
        )


def load_labels() -> dict[str, Any]:
    """Return the parsed label registry.

    The returned dict has two keys:

    * ``labels`` — list of dicts with *name*, *color*, and *description*.
    * ``delete`` — list of label names to remove.
    """
    ref = resources.files("vergil_tooling.data").joinpath("labels.json")
    text = ref.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def ensure_labels(repo: str, names: Sequence[str]) -> None:
    """Create each of *names* in *repo* if absent, from the canonical registry.

    ``gh`` refuses to apply a label the target repo does not have (``'<label>'
    not found``), and a repo that was never label-synced lacks most of the
    convention labels. So every command that applies a label ensures it first,
    at the point of use: an idempotent ``gh label create --force`` with the
    registry's colour and description, which creates a missing label and
    leaves a present one at its canonical definition.

    Every name is checked against the registry before any ``gh`` call, so an
    unknown label raises :class:`UnknownLabelError` and nothing is created —
    no silent creation of an off-registry label, and no dropping it. A ``gh``
    failure propagates as :class:`github.GitHubAPIError`.

    History: the ad-hoc/archive machinery's copy of this fixed the cross-org
    epic-rollup failures (vergil-project/.github#305); the issue creators'
    ``'triage' not found`` failure (#3373) is why it is now shared.
    """
    specs = {entry["name"]: entry for entry in load_labels()["labels"]}
    wanted = list(dict.fromkeys(names))
    unknown = [name for name in wanted if name not in specs]
    if unknown:
        raise UnknownLabelError(unknown)
    for name in wanted:
        spec = specs[name]
        github.run(
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--force",
            "--color",
            spec["color"],
            "--description",
            spec["description"],
        )


def ensure_labels_or_report(prog: str, repo: str, names: Sequence[str]) -> bool:
    """CLI wrapper of :func:`ensure_labels`: report a failure on stderr, return success.

    For a command that must not go on to apply the labels when ensuring them
    failed (the issue creators): an unknown label or a failing ``gh label
    create`` prints one actionable ``<prog>: ...`` line and returns False, so
    the caller exits non-zero without creating anything. A missing GitHub token
    propagates to the :func:`cli.cli_main` boundary, which owns that message.
    """
    try:
        ensure_labels(repo, names)
    except UnknownLabelError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return False
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        detail = " ".join(error_text(exc).split())
        print(
            f"{prog}: could not ensure label(s) {', '.join(names)} exist in {repo}, "
            f"so no issue was created: {detail}",
            file=sys.stderr,
        )
        return False
    return True


def labels_to_delete(registry: dict[str, Any]) -> list[str]:
    """Return every label a sync de-provisions, in a stable order.

    That is the registry's ``delete`` list (GitHub default cruft and deprecated
    aliases) plus the retired intake kind labels (epic .github#176), whose one
    source of truth is :data:`RETIRED_LABELS`.
    """
    names = [*registry.get("delete", []), *sorted(RETIRED_LABELS)]
    return list(dict.fromkeys(names))


def _existing_labels(repo: str) -> set[str]:
    """Return the names of the labels *repo* currently has."""
    data = github.read_json(
        "label", "list", "--repo", repo, "--json", "name", "--limit", _LABEL_LIST_LIMIT
    )
    if not isinstance(data, list):
        msg = f"unexpected `gh label list` output for {repo}: {data!r}"
        raise TypeError(msg)
    return {entry["name"] for entry in cast("list[dict[str, str]]", data)}


def sync_labels(repo: str) -> None:
    """Provision all canonical labels into *repo*; delete deprecated and retired ones.

    Each registry label is created or updated by ``gh label create --force``
    with its canonical colour and description. Deletion is idempotent: only
    labels the repo actually has are deleted, so an already-clean repo is a
    no-op, and a ``gh`` failure (a real API error, not an absent label)
    propagates rather than being swallowed. Deleting a label also strips it
    from every issue still carrying it, which is intended.
    """
    registry = load_labels()
    print(f"Syncing labels for {repo}:")
    for label in registry["labels"]:
        github.run(
            "label",
            "create",
            label["name"],
            "--repo",
            repo,
            "--force",
            "--color",
            label["color"],
            "--description",
            label["description"],
        )
        print(f"  {label['name']}")
    present = _existing_labels(repo)
    for name in labels_to_delete(registry):
        if name in present:
            github.run("label", "delete", name, "--repo", repo, "--yes")
            print(f"  deleted {name}")
