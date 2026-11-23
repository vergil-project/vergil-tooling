"""The outsider report: untrusted activity across a Vergil org's repos.

Epic vergil-project/.github#1467 (spec §6.5). :func:`outsider_activity` lists,
for one listed org (an ``[orgs.<org>]`` stanza of the configuration), every
untrusted thing done since a time, newest first:

- issues and PRs opened by an untrusted account;
- untrusted comments, reviews and inline review comments;
- untrusted edits to trusted content (a body, a comment, a review or an
  inline review comment) and untrusted renames of a trusted item, possible
  tampering;
- untrusted closes (possible tampering) and reopens.

Every trust decision is the trust layer's (:mod:`vergil_tooling.lib.trust`).
The items come from a GitHub search of the org's issues and PRs updated since
the time (:func:`trust.search_items`, which refuses a window GitHub's search
would cut short), then each item's body, comments, reviews, every edit, and
every close, reopen and rename are read in full through the layer
(:func:`trust.read_issue_edits`, :func:`trust.read_review_edits`,
:func:`trust.read_events`), so no untrusted act hides behind another. Each
item is metadata and a link only, never the untrusted text. Nothing here writes to GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import trust
from vergil_tooling.lib.trusted_authors import load_trusted_authors, org_config

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The kinds an OutsiderItem carries, beyond the layer's skip kinds.
CLOSE = "close"
REOPEN = "reopen"
RENAME = "rename"
EDIT = "edit"

# An untrusted edit, rename or close changes trusted content or state.
_TAMPER_KINDS = frozenset({EDIT, RENAME, CLOSE})

_EVENT_KINDS = {"closed": CLOSE, "reopened": REOPEN, "renamed": RENAME}

_ITEM_KINDS: tuple[trust.ItemKind, ...] = ("issue", "pr")

# The URL fragment of each piece of content a skip's field names.
_ANCHORS = {
    "comment": "issuecomment-",
    "review": "pullrequestreview-",
    "review comment": "discussion_r",
}


@dataclass(frozen=True)
class OutsiderItem:
    """One untrusted thing, as metadata and a link: never its text.

    *kind* is ``issue`` or ``pr`` (opened by an untrusted account),
    ``comment``, ``review``, ``review-comment``, ``edit``, ``rename``,
    ``close`` or ``reopen``. *login* is bare, ``None`` for a deleted account.
    *cause* is the layer's (``untrusted``, ``deleted``, ``wrong-type``): a
    ``wrong-type`` login looks like a trusted one but is another account.
    *tamper* marks an untrusted edit, rename or close of trusted content.
    """

    org: str
    repo: str
    number: int
    kind: str
    login: str | None
    at: str
    url: str
    cause: str
    tamper: bool

    def describe(self) -> str:
        """The one-line listing of the item."""
        who = self.login if self.login is not None else "a deleted account"
        return f"{self.at} {self.kind} {self.repo}#{self.number} by {who} ({self.cause}) {self.url}"


def parse_since(raw: str) -> datetime:
    """*raw*, an ISO 8601 date or a time with a zone, as an aware UTC datetime.

    A date alone is midnight UTC. A time with no zone is refused rather than
    guessed.
    """
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        msg = f"--since must be an ISO 8601 date or time, got {raw!r}"
        raise ValueError(msg) from None
    if value.tzinfo is None:
        if "T" in raw or " " in raw:
            msg = f"--since needs a time zone (for example 'Z'), got {raw!r}"
            raise ValueError(msg)
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item_url(repo: str, number: int, kind: trust.ItemKind) -> str:
    return f"https://github.com/{repo}/{'issues' if kind == 'issue' else 'pull'}/{number}"


def _url(item_url: str, field: str) -> str:
    """The link to the content *field* names (``body``, ``comment <id>``…)."""
    label, _, content_id = field.rpartition(" ")
    anchor = _ANCHORS.get(label)
    return item_url if anchor is None else f"{item_url}#{anchor}{content_id}"


def _from_skip(org: str, skip: trust.Skipped, item_url: str) -> OutsiderItem:
    return OutsiderItem(
        org=org,
        repo=skip.repo,
        number=skip.number,
        kind=skip.kind,
        login=skip.login,
        at=skip.at,
        url=_url(item_url, skip.field),
        cause=skip.cause,
        tamper=skip.kind in _TAMPER_KINDS,
    )


def _event_items(
    org: str,
    repo: str,
    number: int,
    item_url: str,
    authors: TrustedAuthors,
    *,
    trusted_item: bool,
) -> list[OutsiderItem]:
    """Every untrusted close and reopen of *repo*#*number*, and rename of a *trusted_item*."""
    items: list[OutsiderItem] = []
    for event in trust.read_events(repo, number, renames=True):
        kind = _EVENT_KINDS.get(event.kind)
        if kind is None or (kind == RENAME and not trusted_item):
            continue
        judgement = trust.role_of(event.actor, org=org, authors=authors)
        if judgement.cause is None:
            continue
        login = None if event.actor.login is None else trust.normalise_login(event.actor.login)
        items.append(
            OutsiderItem(
                org=org,
                repo=repo,
                number=number,
                kind=kind,
                login=login,
                at=event.at,
                url=item_url,
                cause=judgement.cause,
                tamper=kind in _TAMPER_KINDS,
            )
        )
    return items


def _search_window(
    org: str, since: str, kind: trust.ItemKind, authors: TrustedAuthors
) -> list[tuple[str, int]]:
    """The org's items of *kind* updated since *since*, trusted or not.

    The search only finds the items: its judgement of each stops at the first
    untrusted thing, so each item is then read in full.
    """
    hits, skipped = trust.search_items(f"org:{org} updated:>={since}", kind=kind, authors=authors)
    keys = {(hit.repo, hit.number) for hit in hits} | {(s.repo, s.number) for s in skipped}
    return sorted(keys)


def outsider_activity(
    org: str, *, since: str, authors: TrustedAuthors | None = None
) -> list[OutsiderItem]:
    """Every untrusted thing done in *org*'s repos since *since*, newest first.

    *org* must be a listed org: one with no stanza is not Vergil's, and raises
    :class:`ValueError`. *since* is an ISO 8601 date or time with a zone
    (:func:`parse_since`). The items read are the org's issues and PRs updated
    since then; of each, only what was done at or after *since* is listed. A
    read failure, a search GitHub would cut short included, raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`: never a partial list.
    """
    if authors is None:
        authors = load_trusted_authors()
    if org_config(authors, org) is None:
        msg = f"{org} is not a Vergil org: the configuration has no [orgs.{org}] stanza"
        raise ValueError(msg)
    start = parse_since(since)
    stamp = _stamp(start)
    found: set[OutsiderItem] = set()
    for kind in _ITEM_KINDS:
        for repo, number in _search_window(org, stamp, kind, authors):
            url = _item_url(repo, number, kind)
            content, edits = trust.read_issue_edits(repo, number, authors=authors)
            skips = [*content.skipped, *edits]
            if kind == "pr":
                reviews, review_edits = trust.read_review_edits(repo, number, authors=authors)
                skips += [*reviews.skipped, *review_edits]
            found.update(_from_skip(org, skip, url) for skip in skips)
            trusted_item = content.author_role is not None
            found.update(_event_items(org, repo, number, url, authors, trusted_item=trusted_item))
    recent = [item for item in found if parse_since(item.at) >= start]
    return newest_first(recent)


def newest_first(items: Iterable[OutsiderItem]) -> list[OutsiderItem]:
    """*items*, newest first; ties in a fixed order, so a listing is stable."""
    ordered = sorted(items, key=lambda i: (i.repo, i.number, i.kind, i.url, i.login or ""))
    return sorted(ordered, key=lambda i: parse_since(i.at), reverse=True)


def render(items: Sequence[OutsiderItem], *, orgs: Sequence[str], since: str) -> str:
    """The report: possible tampering first, then the rest, each newest first."""
    scope = ", ".join(orgs)
    stamp = _stamp(parse_since(since))
    if not items:
        return f"No untrusted activity in {scope} since {stamp}."
    tamper = newest_first(i for i in items if i.tamper)
    rest = newest_first(i for i in items if not i.tamper)
    lines = [
        f"Untrusted activity in {scope} since {stamp}: {len(items)} item(s),"
        f" {len(tamper)} possible tampering."
    ]
    for title, group in (("Possible tampering", tamper), ("Other untrusted activity", rest)):
        if group:
            lines += ["", f"{title}:", *(f"- {item.describe()}" for item in group)]
    return "\n".join(lines)


def render_counts(items: Sequence[OutsiderItem], *, orgs: Sequence[str], since: str) -> str:
    """The report as counts by kind only, for a log others can read."""
    scope = ", ".join(orgs)
    stamp = _stamp(parse_since(since))
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    detail = ", ".join(f"{kind}: {n}" for kind, n in sorted(counts.items()))
    tamper = sum(1 for item in items if item.tamper)
    return (
        f"Untrusted activity in {scope} since {stamp}: {len(items)} item(s)"
        f"{f' ({detail})' if detail else ''}, {tamper} possible tampering;"
        " listed only outside GitHub Actions."
    )
