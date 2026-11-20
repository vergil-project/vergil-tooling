"""The proxy epic's two-way link to its primary (vergil-project/.github#1417).

A **proxy epic** is a same-org umbrella for the tasks a development epic in
another org (its **primary**) has in this org. It carries the labels ``epic`` +
``proxy`` (:data:`epics.PROXY_LABEL`, :func:`epics.is_proxy`) and always homes
in a public ``<owner>/.github``.

The link runs both ways, each side on a machine-read line alone on its line:

- the proxy's **body** carries exactly one ``Primary: <owner>/<repo>#<N>``
  line (:func:`primary_line`, read by :func:`primary_of`). Nothing rewrites a
  proxy's body;
- the primary records each proxy in a **comment**, never its body (agents
  rewrite an epic's body), carrying ``Proxy: <owner>/.github#<N>``
  (:func:`proxy_line`, read by :func:`proxies_of`). Only comments whose author
  ``vrg-trusted-authors`` lists count (a leading ``app/`` stripped), so a
  forged comment on the public primary counts for nothing. A proxy is never
  removed from the list: one created by mistake is closed and stays listed.

:func:`create_proxy` (``vrg-epic-create --proxy-for``) is the sanctioned way
to make a proxy and post its ``Proxy:`` comment. It refuses, with
:class:`ProxyRefusedError` and before any write, a primary in the target's own
org, a primary that is not an open development epic, and a private primary,
target or proxy home: a proxy exists only between public repos.

Every reader fails loudly: a ``Primary:`` or ``Proxy:`` line that cannot be
parsed or resolved raises :class:`ProxyLinkError`, a failed GitHub read
propagates, and a failed trusted-author load propagates. None of them is ever a
skipped item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epic_size, epics, github
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.trusted_authors import trusted_comment_lines

PRIMARY_MARKER = "Primary:"
PROXY_MARKER = "Proxy:"
PROXY_HOME_REPO = ".github"

_REF = r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#([0-9]+)"
_PRIMARY_RE = re.compile(rf"^{re.escape(PRIMARY_MARKER)} {_REF}$")
_PROXY_RE = re.compile(rf"^{re.escape(PROXY_MARKER)} {_REF}$")


class ProxyLinkError(RuntimeError):
    """A ``Primary:`` or ``Proxy:`` link that is missing, malformed or unresolvable."""


class ProxyRefusedError(RuntimeError):
    """:func:`create_proxy` refused before any write; the message says why."""


def primary_line(primary: IssueRef) -> str:
    """The proxy body's line naming *primary*: ``Primary: <owner>/<repo>#<N>``."""
    return f"{PRIMARY_MARKER} {primary.slug}"


def proxy_line(proxy: IssueRef) -> str:
    """The primary's comment line naming *proxy*: ``Proxy: <owner>/.github#<N>``."""
    return f"{PROXY_MARKER} {proxy.slug}"


def unlisted_message(proxy: IssueRef, primary: IssueRef) -> str:
    """The error for a *proxy* naming *primary*, whose trusted comments do not name it back."""
    return (
        f"proxy {proxy.slug} names {primary.slug} as its primary, but {primary.slug} has no"
        f" trusted '{proxy_line(proxy)}' comment"
    )


def _view(ref: IssueRef, fields: str) -> dict[str, Any]:
    raw = github.read_json(
        "issue", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", fields
    )
    if not isinstance(raw, dict):
        raise ProxyLinkError(f"unexpected 'gh issue view' response for {ref.slug}: {raw!r}")
    return raw


def _marker_lines(text: str, marker: str) -> list[str]:
    """Every line of *text* that begins with *marker*, stripped.

    A marker mentioned inside prose (not at the start of its line) is not a
    machine-read line and is not returned.
    """
    return [line.strip() for line in text.splitlines() if line.strip().startswith(marker)]


def _parse(line: str, pattern: re.Pattern[str], where: str) -> IssueRef:
    match = pattern.match(line)
    if match is None:
        raise ProxyLinkError(f"malformed line in {where}: {line!r}")
    return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))


def primary_of(proxy: IssueRef) -> IssueRef:
    """The primary epic *proxy*'s body names on its one ``Primary:`` line.

    Raises :class:`ProxyLinkError` when the body has no ``Primary:`` line, more
    than one, a malformed one, or one naming an issue without the ``epic``
    label.
    """
    primary = _primary_in_body(proxy, str(_view(proxy, "body").get("body") or ""))
    if not epics.is_epic(primary):
        raise ProxyLinkError(
            f"proxy {proxy.slug} names {primary.slug} as its primary, which is not an epic"
        )
    return primary


def _primary_in_body(proxy: IssueRef, body: str) -> IssueRef:
    """The ref on *body*'s one ``Primary:`` line; *proxy* names the body in errors."""
    lines = _marker_lines(body, PRIMARY_MARKER)
    if not lines:
        raise ProxyLinkError(f"proxy {proxy.slug} has no '{PRIMARY_MARKER}' line in its body")
    if len(lines) > 1:
        raise ProxyLinkError(
            f"proxy {proxy.slug} has {len(lines)} '{PRIMARY_MARKER}' lines in its body;"
            " exactly one is allowed"
        )
    return _parse(lines[0], _PRIMARY_RE, f"the body of proxy {proxy.slug}")


def proxies_of(primary: IssueRef) -> tuple[IssueRef, ...]:
    """Every proxy *primary*'s trusted comments name, sorted, without repeats.

    The union of the ``Proxy:`` lines in comments by trusted authors
    (:func:`epic_size.trusted_logins`); others are ignored. A trusted
    ``Proxy:`` line that is malformed, or names an issue outside an
    ``<owner>/.github`` home, raises :class:`ProxyLinkError`.
    """
    trusted = epic_size.trusted_logins()
    comments = _view(primary, "comments").get("comments") or []
    found: dict[str, IssueRef] = {}
    for line in trusted_comment_lines(comments, trusted):
        if not line.startswith(PROXY_MARKER):
            continue
        ref = _parse(line, _PROXY_RE, f"a comment on {primary.slug}")
        if ref.repo != PROXY_HOME_REPO:
            raise ProxyLinkError(
                f"a comment on {primary.slug} names {ref.slug} as a proxy, which is not"
                f" in an <owner>/{PROXY_HOME_REPO} home"
            )
        found.setdefault(ref.slug.lower(), ref)
    return tuple(found[key] for key in sorted(found))


def is_proxy_ref(ref: IssueRef) -> bool:
    """True iff the issue *ref* is a proxy epic (:func:`epics.is_proxy` on its labels)."""
    return epics.is_proxy(_label_names(_view(ref, "labels")))


def _label_names(view: dict[str, Any]) -> set[str]:
    return {str((label or {}).get("name") or "") for label in view.get("labels") or []}


# --- creation (vrg-epic-create --proxy-for) ----------------------------------------

PROXY_TITLE_PREFIX = "Epic (proxy): "
_EPIC_TITLE_PREFIX = "Epic: "
_NOT_ADHOC = "The task must not be filed in an ad-hoc epic instead."


@dataclass(frozen=True)
class ProxyResult:
    """What :func:`create_proxy` did.

    *proxy* is the proxy epic; *created*, *reopened*, *retitled* and
    *commented* say whether it was created, reopened, retitled to the derived
    title (:func:`proxy_title`), or had its ``Proxy:`` comment posted on the
    primary. All four False is a re-run that wrote nothing.
    """

    proxy: IssueRef
    created: bool
    reopened: bool
    retitled: bool
    commented: bool


def _same_ref(a: IssueRef, b: IssueRef) -> bool:
    return a.slug.lower() == b.slug.lower()


def proxy_title(primary: IssueRef, primary_title: str) -> str:
    """``Epic (proxy): <owner>/<repo>#<N> — <primary title without its "Epic: " prefix>``.

    The primary's ref comes first, so the title names which epic the proxy
    stands in for.
    """
    subject = primary_title.removeprefix(_EPIC_TITLE_PREFIX)
    return f"{PROXY_TITLE_PREFIX}{primary.slug} — {subject}"


def proxy_body(primary: IssueRef, target_owner: str) -> str:
    """The proxy's body: its ``Primary:`` line and one explanation line."""
    return (
        f"{primary_line(primary)}\n\n"
        f"Holds the {target_owner} tasks of the primary epic {primary.slug}, which "
        "describes the work, carries its spec, plan and retrospective, and drives "
        "these tasks; this epic is never driven on its own.\n"
    )


def proxy_comment(proxy: IssueRef) -> str:
    """The comment on the primary recording *proxy*, its ``Proxy:`` line alone on its line."""
    return (
        f"This epic's tasks in {proxy.owner} live under the proxy epic {proxy.slug}.\n\n"
        f"{proxy_line(proxy)}\n"
    )


def _refuse_private(repo: str, role: str) -> None:
    if not github.is_public(repo):
        raise ProxyRefusedError(
            f"the {role} {repo} is not public; a proxy exists only between public repos,"
            f" so cross-org work involving a private repo has no proxy. {_NOT_ADHOC}"
        )


def _require_open_development_epic(primary: IssueRef) -> str:
    """Refuse unless *primary* is an open development epic; return its title."""
    view = _view(primary, "labels,state,title")
    labels = _label_names(view)
    title = str(view.get("title") or "")
    problem = None
    if "epic" not in labels:
        problem = "not an epic"
    elif epics.is_proxy(labels):
        problem = "a proxy epic"
    elif epics.is_standing(labels):
        problem = "a standing epic"
    elif epics.is_archive(labels, title):
        problem = "an archive"
    elif str(view.get("state") or "").upper() != "OPEN":
        problem = "closed"
    if problem is not None:
        raise ProxyRefusedError(
            f"{primary.slug} is {problem}; a proxy stands in only for an open development epic"
        )
    return title


def _proxy_rows(home: str) -> list[dict[str, Any]]:
    raw = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--label",
        "epic",
        "--label",
        epics.PROXY_LABEL,
        "--state",
        "all",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,state,title,body",
    )
    if not isinstance(raw, list):
        raise ProxyLinkError(f"unexpected 'gh issue list' response for {home}: {raw!r}")
    epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the proxy epics of {home}")
    return [row for row in raw if isinstance(row, dict)]


def _is_open(view: dict[str, Any]) -> bool:
    return str(view.get("state") or "").upper() == "OPEN"


@dataclass(frozen=True)
class _Existing:
    """A proxy found in the home: its ref, whether it is open, and its title."""

    ref: IssueRef
    is_open: bool
    title: str


def _existing_proxies(
    primary: IssueRef, home: str, listed: tuple[IssueRef, ...]
) -> list[_Existing]:
    """Every proxy in *home* for *primary*, with its state and title, lowest first.

    Read from the home's ``epic`` + ``proxy`` issues, plus any proxy in *home*
    the primary's trusted comments name (*listed*) that the listing missed (a
    just-created proxy can lag the listing). Every candidate's ``Primary:``
    line is parsed, so a malformed one fails loudly; a listed proxy that is not
    a proxy naming *primary* raises :class:`ProxyLinkError`.
    """
    owner, repo = home.split("/", 1)
    found: dict[int, _Existing] = {}
    for row in _proxy_rows(home):
        ref = IssueRef(owner, repo, int(row["number"]))
        if _same_ref(_primary_in_body(ref, str(row.get("body") or "")), primary):
            found[ref.number] = _Existing(ref, _is_open(row), str(row.get("title") or ""))
    for ref in listed:
        if ref.owner.lower() != owner.lower() or ref.number in found:
            continue
        view = _view(ref, "labels,state,title,body")
        named = _primary_in_body(ref, str(view.get("body") or ""))
        if not epics.is_proxy(_label_names(view)) or not _same_ref(named, primary):
            raise ProxyLinkError(
                f"a trusted comment on {primary.slug} names {ref.slug} as its proxy, but"
                f" {ref.slug} is not a proxy naming {primary.slug}"
            )
        found[ref.number] = _Existing(ref, _is_open(view), str(view.get("title") or ""))
    return [found[number] for number in sorted(found)]


def create_proxy(primary: IssueRef, target_repo: str) -> ProxyResult:
    """Ensure *primary*'s proxy in *target_repo*'s org home, and its ``Proxy:`` comment.

    Every refusal (:class:`ProxyRefusedError`) and every read of both orgs
    comes before any write, so a refusal, or a token that cannot be minted for
    either org, writes nothing. The proxy is found among the home's proxies by
    its ``Primary:`` line: an open one is reused, a closed one is reopened, and
    none means it is created, its labels ensured first. A reused or reopened
    proxy whose title differs from :func:`proxy_title` is retitled, so a re-run
    brings an older proxy's title up to date; a proxy is found by its
    ``Primary:`` line and labels, never its title, so retitling is safe. The
    comment is posted
    on the primary only when no trusted comment there names the proxy, so a
    re-run that finds both writes nothing. Each call runs under
    :func:`github.target_org` of the org it touches.
    """
    if "/" not in target_repo:
        raise ProxyRefusedError(f"the target repo must be 'owner/repo' (got {target_repo!r})")
    owner = target_repo.split("/", 1)[0]
    if owner.lower() == primary.owner.lower():
        raise ProxyRefusedError(
            f"{primary.slug} is in the same org as {target_repo}, so no proxy is needed:"
            f" file the task under {primary.slug} itself"
        )
    with github.target_org(primary.owner):
        _refuse_private(f"{primary.owner}/{primary.repo}", "primary epic's repo")
        title = _require_open_development_epic(primary)
        listed = proxies_of(primary)
    home = f"{owner}/{PROXY_HOME_REPO}"
    with github.target_org(owner):
        _refuse_private(target_repo, "target repo")
        _refuse_private(home, "proxy home")
        existing = _existing_proxies(primary, home, listed)
        derived = proxy_title(primary, title)
        open_proxies = [found for found in existing if found.is_open]
        open_refs = [found.ref for found in open_proxies]
        if len(open_refs) > 1:
            raise ProxyLinkError(
                f"{home} has {len(open_refs)} open proxies for {primary.slug}"
                f" ({', '.join(ref.slug for ref in open_refs)}); keep one and close the rest"
            )
        created = reopened = retitled = False
        if existing:
            found = open_proxies[0] if open_proxies else existing[0]
            proxy = found.ref
            if not found.is_open:
                github.run("issue", "reopen", str(proxy.number), "--repo", home)
                reopened = True
            if found.title != derived:
                github.run("issue", "edit", str(proxy.number), "--repo", home, "--title", derived)
                retitled = True
        else:
            labels = ["epic", epics.PROXY_LABEL]
            ensure_labels(home, labels)
            url = github.create_issue(
                repo=home, title=derived, body=proxy_body(primary, owner), labels=labels
            )
            proxy = IssueRef(owner, PROXY_HOME_REPO, int(url.rstrip("/").rsplit("/", 1)[-1]))
            created = True
    commented = not any(_same_ref(ref, proxy) for ref in listed)
    if commented:
        with github.target_org(primary.owner):
            github.run(
                "issue",
                "comment",
                str(primary.number),
                "--repo",
                f"{primary.owner}/{primary.repo}",
                "--body",
                proxy_comment(proxy),
            )
    return ProxyResult(
        proxy=proxy, created=created, reopened=reopened, retitled=retitled, commented=commented
    )
