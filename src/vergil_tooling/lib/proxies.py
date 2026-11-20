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

Every reader fails loudly: a ``Primary:`` or ``Proxy:`` line that cannot be
parsed or resolved raises :class:`ProxyLinkError`, a failed GitHub read
propagates, and a failed trusted-author load propagates. None of them is ever a
skipped item.
"""

from __future__ import annotations

import re
from typing import Any

from vergil_tooling.lib import epic_size, epics, github
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.trusted_authors import trusted_comment_bodies

PRIMARY_MARKER = "Primary:"
PROXY_MARKER = "Proxy:"
PROXY_HOME_REPO = ".github"

_REF = r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#([0-9]+)"
_PRIMARY_RE = re.compile(rf"^{re.escape(PRIMARY_MARKER)} {_REF}$")
_PROXY_RE = re.compile(rf"^{re.escape(PROXY_MARKER)} {_REF}$")


class ProxyLinkError(RuntimeError):
    """A ``Primary:`` or ``Proxy:`` link that is missing, malformed or unresolvable."""


def primary_line(primary: IssueRef) -> str:
    """The proxy body's line naming *primary*: ``Primary: <owner>/<repo>#<N>``."""
    return f"{PRIMARY_MARKER} {primary.slug}"


def proxy_line(proxy: IssueRef) -> str:
    """The primary's comment line naming *proxy*: ``Proxy: <owner>/.github#<N>``."""
    return f"{PROXY_MARKER} {proxy.slug}"


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
    body = str(_view(proxy, "body").get("body") or "")
    lines = _marker_lines(body, PRIMARY_MARKER)
    if not lines:
        raise ProxyLinkError(f"proxy {proxy.slug} has no '{PRIMARY_MARKER}' line in its body")
    if len(lines) > 1:
        raise ProxyLinkError(
            f"proxy {proxy.slug} has {len(lines)} '{PRIMARY_MARKER}' lines in its body;"
            " exactly one is allowed"
        )
    primary = _parse(lines[0], _PRIMARY_RE, f"the body of proxy {proxy.slug}")
    if not epics.is_epic(primary):
        raise ProxyLinkError(
            f"proxy {proxy.slug} names {primary.slug} as its primary, which is not an epic"
        )
    return primary


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
    for body in trusted_comment_bodies(comments, trusted):
        for line in _marker_lines(body, PROXY_MARKER):
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
    labels = _view(ref, "labels").get("labels") or []
    return epics.is_proxy({str((label or {}).get("name") or "") for label in labels})
