"""The GitHub identities whose issues and comments automation may trust.

The trusted identities are **configuration, never hard-coded** in a skill, the
doctrine or a tool. They live in the ``[trusted_authors]`` stanza and the
``[orgs.<org>]`` stanzas of the host's ``~/.config/vergil/identities.toml``::

    [trusted_authors]
    human = ["<human-login>"]
    agents = ["<agent-login>", "<agent-login>"]

    [orgs.<org>]
    automation_app = "<automation-app-slug>"

They give three roles (epic vergil-project/.github#1467, spec §5.1): ``human``
and ``agents``, trusted in every Vergil org, and each org's ``automation`` App
(login ``<slug>[bot]``), trusted only in that org's repos. The orgs with a
stanza are the Vergil orgs (:func:`vergil_orgs`): an org without one is not
managed by Vergil.

The configuration reaches each place it is read (spec §5.1a):

- the host reads ``identities.toml`` in place;
- ``vrg-vm`` delivers it into every agent VM as
  ``~/.config/vergil/trusted-authors.toml`` (``human``/``agents`` at top level
  and the same ``[orgs.<org>]`` tables), and removes that file when the host
  has no ``[trusted_authors]`` stanza;
- a GitHub Actions job gets the compact JSON of :func:`render_actions_value`
  in ``$VRG_TRUSTED_AUTHORS``.

Everything reads it through :func:`load_trusted_authors` (the
``vrg-trusted-authors`` command), which fails loudly when nothing is configured
and never falls back to a default.

Logins and slugs are stored bare. GitHub reports an issue opened by a GitHub
App as ``app/<login>``, that App's comments as ``<login>``, and some reads as
``<login>[bot]``, so a caller strips a leading ``app/`` (and a trailing
``[bot]``) before comparing; an ``app/`` or ``[bot]`` entry in the
configuration is rejected rather than silently normalized.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

STANZA = "trusted_authors"
ORGS_STANZA = "orgs"
AUTOMATION_APP_KEY = "automation_app"
VM_FILE_NAME = "trusted-authors.toml"
# The delivered copy a GitHub Actions job reads (spec §5.1a), and the variable
# that says the process runs under Actions.
ACTIONS_ENV = "VRG_TRUSTED_AUTHORS"
GITHUB_ACTIONS_ENV = "GITHUB_ACTIONS"
_APP_PREFIX = "app/"
_BOT_SUFFIX = "[bot]"


def bare_login(login: str) -> str:
    """*login* with a leading ``app/`` stripped, the form the configuration stores."""
    return login.removeprefix(_APP_PREFIX)


def trusted_comment_bodies(
    comments: Iterable[Mapping[str, object]], trusted: Collection[str]
) -> list[str]:
    """The bodies of *comments* whose author is in *trusted*, in their order.

    *comments* are ``gh issue view --json comments`` entries (``author.login``
    and ``body``). A comment with no author (a deleted account) has the login
    ``""``, which no trusted login matches.
    """
    bodies: list[str] = []
    for comment in comments:
        author = comment.get("author")
        login = str(author.get("login") or "") if isinstance(author, dict) else ""
        if bare_login(login) in trusted:
            bodies.append(str(comment.get("body") or ""))
    return bodies


def trusted_comment_lines(
    comments: Iterable[Mapping[str, object]], trusted: Collection[str]
) -> list[str]:
    """Every line of the trusted *comments* (:func:`trusted_comment_bodies`), stripped, in order.

    The one reader of the machine-read comment lines, each alone on its line
    (``Proxy:``, ``Standalone-confirmed``): a caller matches a stripped line,
    so a marker inside prose never counts.
    """
    return [
        line.strip()
        for body in trusted_comment_bodies(comments, trusted)
        for line in body.splitlines()
    ]


class TrustedAuthorsNotConfiguredError(Exception):
    """No source configures trusted authors: no VM file, host stanza or Actions copy."""


@dataclass(frozen=True)
class OrgConfig:
    """One Vergil org's stanza: the slug of its automation App (login ``<slug>[bot]``)."""

    automation_app: str


@dataclass(frozen=True)
class TrustedAuthors:
    human: tuple[str, ...]
    agents: tuple[str, ...]
    # The Vergil orgs, in configuration order. Empty only when built directly
    # (a test): every loaded configuration lists at least one org, and an empty
    # mapping trusts no automation identity anywhere.
    orgs: Mapping[str, OrgConfig] = field(default_factory=dict)


def vergil_orgs(authors: TrustedAuthors) -> tuple[str, ...]:
    """The orgs Vergil manages: those with an ``[orgs.<org>]`` stanza, in order."""
    return tuple(authors.orgs)


def org_config(authors: TrustedAuthors, org: str) -> OrgConfig | None:
    """*org*'s stanza, matched case-insensitively as GitHub matches org names; else ``None``."""
    wanted = org.casefold()
    for name, config in authors.orgs.items():
        if name.casefold() == wanted:
            return config
    return None


def _check_bare(where: str, entry: str) -> None:
    """Reject an *entry* carrying the ``app/`` prefix or the ``[bot]`` suffix."""
    if entry.startswith(_APP_PREFIX):
        msg = (
            f"{where} entry {entry!r} has the 'app/' prefix;"
            f" store the bare login {entry[len(_APP_PREFIX) :]!r}"
        )
        raise ValueError(msg)
    if entry.endswith(_BOT_SUFFIX):
        msg = (
            f"{where} entry {entry!r} has the '[bot]' suffix;"
            f" store the bare login {entry[: -len(_BOT_SUFFIX)]!r}"
        )
        raise ValueError(msg)


def _parse_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    if key not in raw:
        msg = f"trusted_authors: '{key}' is missing (it must list at least one GitHub login)"
        raise ValueError(msg)
    value = raw[key]
    if not isinstance(value, list):
        msg = f"trusted_authors: '{key}' must be a list of GitHub logins, got {value!r}"
        raise ValueError(msg)
    if not value:
        msg = f"trusted_authors: '{key}' is empty (it must list at least one GitHub login)"
        raise ValueError(msg)
    logins: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            msg = f"trusted_authors: '{key}' entries must be non-empty strings, got {entry!r}"
            raise ValueError(msg)
        if entry != entry.strip():
            msg = f"trusted_authors: '{key}' entry {entry!r} has surrounding whitespace"
            raise ValueError(msg)
        _check_bare(f"trusted_authors: '{key}'", entry)
        logins.append(entry)
    return tuple(logins)


def _parse_org(org: str, raw: object) -> OrgConfig:
    where = f"{ORGS_STANZA}.{org}"
    if not org.strip() or org != org.strip():
        msg = f"{ORGS_STANZA}: org name {org!r} must be non-empty with no surrounding whitespace"
        raise ValueError(msg)
    if not isinstance(raw, dict):
        msg = f"[{where}] must be a table, got {raw!r}"
        raise ValueError(msg)
    if AUTOMATION_APP_KEY not in raw:
        msg = f"{where}: '{AUTOMATION_APP_KEY}' is missing (the slug of the org's automation App)"
        raise ValueError(msg)
    slug = raw[AUTOMATION_APP_KEY]
    if not isinstance(slug, str) or not slug.strip():
        msg = f"{where}: '{AUTOMATION_APP_KEY}' must be a non-empty string, got {slug!r}"
        raise ValueError(msg)
    if slug != slug.strip():
        msg = f"{where}: '{AUTOMATION_APP_KEY}' {slug!r} has surrounding whitespace"
        raise ValueError(msg)
    _check_bare(f"{where}: '{AUTOMATION_APP_KEY}'", slug)
    return OrgConfig(automation_app=slug)


def parse_trusted_authors(raw: Mapping[str, object], orgs: Mapping[str, object]) -> TrustedAuthors:
    """Validate a ``[trusted_authors]`` mapping and the ``[orgs.*]`` tables, and return them.

    *orgs* maps each org to its stanza (``{"<org>": {"automation_app":
    "<slug>"}}``). Raises :class:`ValueError` naming the problem when ``human``
    or ``agents`` is missing or empty, an entry is not a non-empty string, an
    entry or slug carries the ``app/`` prefix or the ``[bot]`` suffix, a login
    is in two roles, there is no org at all, two orgs differ only in case, or
    an org's stanza has no valid ``automation_app``.
    """
    human = _parse_list(raw, "human")
    agents = _parse_list(raw, "agents")
    both = sorted(set(human) & set(agents))
    if both:
        msg = f"trusted_authors: login(s) in both 'human' and 'agents': {', '.join(both)}"
        raise ValueError(msg)
    if not orgs:
        msg = (
            f"no [{ORGS_STANZA}.<org>] stanza: list every org Vergil manages,"
            f" each with its '{AUTOMATION_APP_KEY}'"
        )
        raise ValueError(msg)
    role_logins = set(human) | set(agents)
    parsed: dict[str, OrgConfig] = {}
    seen: dict[str, str] = {}
    for org, stanza in orgs.items():
        config = _parse_org(org, stanza)
        folded = org.casefold()
        if folded in seen:
            msg = f"{ORGS_STANZA}: orgs {seen[folded]!r} and {org!r} differ only in case"
            raise ValueError(msg)
        seen[folded] = org
        if config.automation_app in role_logins:
            msg = (
                f"{ORGS_STANZA}.{org}: '{AUTOMATION_APP_KEY}' {config.automation_app!r}"
                " is also a 'human' or 'agents' login"
            )
            raise ValueError(msg)
        parsed[org] = config
    return TrustedAuthors(human=human, agents=agents, orgs=parsed)


def orgs_table(raw: Mapping[str, object]) -> Mapping[str, object]:
    """The ``[orgs]`` table of a parsed configuration, ``{}`` when it has none.

    Raises :class:`ValueError` when ``orgs`` is present but not a table.
    """
    value = raw.get(ORGS_STANZA, {})
    if not isinstance(value, dict):
        msg = f"[{ORGS_STANZA}] must be a table of [{ORGS_STANZA}.<org>] tables, got {value!r}"
        raise ValueError(msg)
    return value


def vm_file_path() -> Path:
    """The trusted-authors file ``vrg-vm`` delivers into an agent VM."""
    return Path.home() / ".config" / "vergil" / VM_FILE_NAME


def identities_path() -> Path:
    """The host's ``identities.toml``."""
    return Path.home() / ".config" / "vergil" / "identities.toml"


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: invalid TOML: {exc}"
        raise ValueError(msg) from exc


def _parse_document(raw: Mapping[str, object], stanza: Mapping[str, object]) -> TrustedAuthors:
    """Parse *stanza* (the ``human``/``agents`` mapping) with *raw*'s ``[orgs]`` table."""
    return parse_trusted_authors(stanza, orgs_table(raw))


def _load_actions_value() -> TrustedAuthors:
    """The delivered copy in ``$VRG_TRUSTED_AUTHORS``, under GitHub Actions only."""
    value = os.environ.get(ACTIONS_ENV, "")
    if not value.strip():
        msg = (
            f"trusted authors are not configured: ${ACTIONS_ENV} is missing or empty"
            " in this GitHub Actions job (the org Actions variable it comes from is"
            " unset or not passed into the job)"
        )
        raise TrustedAuthorsNotConfiguredError(msg)
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"${ACTIONS_ENV}: invalid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"${ACTIONS_ENV}: must be a JSON object, got {type(raw).__name__}"
        raise ValueError(msg)
    try:
        return _parse_document(raw, raw)
    except ValueError as exc:
        msg = f"${ACTIONS_ENV}: {exc}"
        raise ValueError(msg) from exc


def load_trusted_authors() -> TrustedAuthors:
    """Load the trusted authors from the first source that exists.

    The VM file if it exists; else the host ``identities.toml``
    (``[trusted_authors]`` plus ``[orgs.*]``); else, only when
    ``$GITHUB_ACTIONS`` is ``true`` and neither file exists,
    ``$VRG_TRUSTED_AUTHORS``. Raises :class:`TrustedAuthorsNotConfiguredError`
    naming what it looked for when nothing configures them, and
    :class:`ValueError` naming the file (or variable) and the key when the
    configuration that is present is invalid. Never returns a default.
    """
    vm_file = vm_file_path()
    if vm_file.exists():
        raw = _read_toml(vm_file)
        try:
            return _parse_document(raw, raw)
        except ValueError as exc:
            msg = f"{vm_file}: {exc}"
            raise ValueError(msg) from exc

    identities = identities_path()
    if identities.exists():
        raw = _read_toml(identities)
        stanza = raw.get(STANZA)
        if stanza is not None:
            if not isinstance(stanza, dict):
                msg = f"{identities}: [{STANZA}] must be a table"
                raise ValueError(msg)
            try:
                return _parse_document(raw, stanza)
            except ValueError as exc:
                msg = f"{identities}: {exc}"
                raise ValueError(msg) from exc
    elif os.environ.get(GITHUB_ACTIONS_ENV) == "true":
        return _load_actions_value()

    msg = (
        f"trusted authors are not configured: no {vm_file} and no [{STANZA}] stanza in {identities}"
    )
    raise TrustedAuthorsNotConfiguredError(msg)


def _toml_string(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def render_vm_file(authors: TrustedAuthors) -> str:
    """Render the VM's ``trusted-authors.toml``: top-level ``human``/``agents``, then the orgs."""

    def _array(logins: tuple[str, ...]) -> str:
        return "[" + ", ".join(_toml_string(login) for login in logins) + "]"

    lines = [
        "# Delivered by vrg-vm from the host's identities.toml [trusted_authors]"
        " and [orgs.*]. Do not edit.",
        f"human = {_array(authors.human)}",
        f"agents = {_array(authors.agents)}",
    ]
    for org, config in authors.orgs.items():
        lines += [
            "",
            f"[{ORGS_STANZA}.{_toml_string(org)}]",
            f"{AUTOMATION_APP_KEY} = {_toml_string(config.automation_app)}",
        ]
    return "\n".join(lines) + "\n"


def as_json_object(authors: TrustedAuthors) -> dict[str, object]:
    """*authors* as one JSON object, the shape ``vrg-trusted-authors --json`` prints.

    ``{"human": [...], "agents": [...], "orgs": {"<org>": {"automation_app": "<slug>"}}}``
    """
    return {
        "human": list(authors.human),
        "agents": list(authors.agents),
        ORGS_STANZA: {
            org: {AUTOMATION_APP_KEY: config.automation_app} for org, config in authors.orgs.items()
        },
    }


def render_actions_value(authors: TrustedAuthors) -> str:
    """The compact JSON a GitHub Actions job reads from ``$VRG_TRUSTED_AUTHORS``.

    It carries every listed org's ``automation_app``, so a job reading another
    org's content (a proxy's primary) judges it as the host does.
    """
    return json.dumps(as_json_object(authors), ensure_ascii=False, separators=(",", ":"))
