"""A PR's closing references, as the trust layer judges them.

Epic vergil-project/.github#1467. GitHub's ``closingIssuesReferences`` are
derived from a PR's body and its hand links, so anyone who can write either
can name an issue. :func:`read_closing_refs` reads a PR by number through the
trust layer (:func:`~vergil_tooling.lib.trust.read_pr_closing`) and counts a
reference only when
:func:`~vergil_tooling.lib.pending_release.trusted_closing_refs` does: a
sanctioned closing line in a trusted body names it, or a trusted actor linked
it by hand, and it is in the PR's own repo. Every other reference GitHub
lists is returned apart, by ref and state only, never acted on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import trust
from vergil_tooling.lib.github import ClosingIssue
from vergil_tooling.lib.pending_release import PendingReleaseError, trusted_closing_refs

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors


@dataclass(frozen=True)
class ClosingRefs:
    """A PR's state and its closing references, judged.

    *counted* are the references the trust layer counts, *uncounted* every
    other one GitHub lists, each with its state. *skipped* holds a
    :class:`~vergil_tooling.lib.trust.Skipped` record for an untrusted body
    and for each untrusted hand link, metadata only.
    """

    repo: str
    number: int
    state: str
    merged_at: str | None
    counted: tuple[ClosingIssue, ...]
    uncounted: tuple[ClosingIssue, ...]
    skipped: tuple[trust.Skipped, ...]

    @property
    def ref(self) -> str:
        """The PR as ``OWNER/REPO#N``."""
        return f"{self.repo}#{self.number}"


def _reference(node: object, call: str) -> ClosingIssue:
    """One of GitHub's closing references: its repo, number and state."""
    ref: dict[str, Any] = node if isinstance(node, dict) else {}
    number = ref.get("number")
    state = ref.get("state")
    home = ref.get("repository")
    repo = home.get("nameWithOwner") if isinstance(home, dict) else None
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise trust.TrustReadError(call, "a closing reference has no usable number")
    if state not in ("OPEN", "CLOSED"):
        raise trust.TrustReadError(call, "a closing reference has no usable state")
    if not isinstance(repo, str) or "/" not in repo:
        raise trust.TrustReadError(call, "a closing reference has no repository")
    return ClosingIssue(repo=repo, number=number, state=state)


def read_closing_refs(repo: str, number: int, *, authors: TrustedAuthors) -> ClosingRefs:
    """Read PR *repo*#*number* and judge each of its closing references.

    The roles are ``trusted_closing_refs``' defaults (the human and the
    agents). A read or a judgement the layer cannot complete (a missing field,
    a truncated list, an edit history not read in full) raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`, never a partial answer.
    """
    call = f"read_closing_refs {repo}#{number}"
    pr = trust.read_pr_closing(repo, number)
    try:
        numbers, skipped = trusted_closing_refs(pr.node, repo=pr.repo, authors=authors)
    except PendingReleaseError as exc:
        raise trust.TrustReadError(call, str(exc)) from None
    except KeyError, TypeError:
        raise trust.TrustReadError(
            call, "the pull request's closing fields are malformed"
        ) from None
    # trusted_closing_refs has read the list in full: its nodes are a list.
    nodes = cast("list[object]", pr.node["closingIssuesReferences"]["nodes"])
    issues = [_reference(node, call) for node in nodes]
    kept = set(numbers)
    counted = tuple(
        issue for issue in issues if issue.repo.lower() == pr.repo.lower() and issue.number in kept
    )
    return ClosingRefs(
        repo=pr.repo,
        number=pr.number,
        state=pr.state,
        merged_at=pr.merged_at,
        counted=counted,
        uncounted=tuple(issue for issue in issues if issue not in counted),
        skipped=tuple(skipped),
    )
