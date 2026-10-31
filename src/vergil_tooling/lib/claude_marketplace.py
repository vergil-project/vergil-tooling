"""The shared Claude Code marketplace refresh, run on the host and in VMs.

One sequence serves both places (epic vergil-project/.github#760, spec §6):
for each marketplace the settings declare, ``claude plugin marketplace add
<repo>@<ref>``, then ``claude plugin marketplace update``, then the
per-install ``claude plugin update``. Once ``settings.json`` declares a new
ref, the re-add re-clones the marketplace at that ref and rewrites its
registration, which is how a machine switches ref (proven on Claude Code
2.1.312 by vergil-project/vergil-tooling#3850).

The sequence ends by **verifying** that each declared GitHub marketplace is
registered at the declared repo and ref, read from ``claude plugin
marketplace list --json``. A mismatch is a reported failure, so a Claude
Code release that stops re-adding at a new ref fails loudly instead of
leaving a machine on the old ref.

Every function takes a *run* callable with the shape of ``Transport.run``
and a shell *prefix* (the VM's ``PATH`` export, or ``""`` on the host). Every
Claude CLI call goes through :func:`shell`, with each declared value passed
through :func:`shlex.quote`. Nothing here edits Claude Code's internal
registration files.

Failures are collected and returned as labels, never swallowed: the caller
decides how to report them (``vm_guest.update_plugins`` raises, ``vrg-host
plugins`` exits 2).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The shape of ``Transport.run(*argv)``: run a command, raise on a nonzero exit.
type Run = Callable[..., subprocess.CompletedProcess[str]]

_OFFICIAL_MARKETPLACE = "claude-plugins-official"
_OFFICIAL_MARKETPLACE_SOURCE = "anthropics/claude-plugins-official"

# Scopes whose install belongs to one project. `claude plugin update` has no
# project flag (only `--scope`); the CLI takes the project from the working
# directory, so these are updated from inside the entry's projectPath (#3764).
_PROJECT_SCOPES = frozenset({"project", "local"})


class MarketplaceError(RuntimeError):
    """The Claude CLI answered in a shape the refresh cannot read."""


@dataclass(frozen=True)
class Declared:
    """One GitHub marketplace declared in ``extraKnownMarketplaces``."""

    name: str
    repo: str
    ref: str | None


def local_run(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run *argv* on this machine, raising on a nonzero exit (the host's ``Run``).

    Runs from the home directory, so a project ``.claude/settings.json`` in the
    caller's working directory never changes what the Claude CLI sees (the VM
    transport likewise runs from a neutral directory). On a failure the
    child's stderr is echoed before the error is raised, as ``Transport.run``
    does, so the cause is never lost.
    """
    try:
        return subprocess.run(  # noqa: S603
            argv, check=True, capture_output=True, text=True, cwd=Path.home()
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def shell(prefix: str, command: str) -> list[str]:
    """Return the ``bash -c`` argv for *command*, after *prefix* when one is given."""
    return ["bash", "-c", f"{prefix} && {command}" if prefix else command]


def _report(label: str, exc: subprocess.CalledProcessError) -> None:
    """Print which step failed and its exit status.

    The step's own stderr has already been echoed by the ``Run`` (``local_run``
    or ``Transport.run``), so it is not printed again here.
    """
    print(f"ERROR: {label} failed (exit status {exc.returncode})", file=sys.stderr)


def parse_settings(text: str, where: str) -> dict[str, Any]:
    """Parse a ``settings.json`` text read from *where* (named in every error).

    Shared by the host (``vrg-host plugins``) and the guest
    (``vm_guest.update_plugins``) so both reject the same inputs the same way.

    Raises:
        MarketplaceError: *text* is not valid JSON or not a JSON object.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{where} is not valid JSON: {exc}"
        raise MarketplaceError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{where} is not a JSON object"
        raise MarketplaceError(msg)
    return data


def read_settings_file(path: Path) -> dict[str, Any]:
    """Read and parse a host ``settings.json`` at *path* (named in every error).

    Shared by ``vrg-host plugins``, which refreshes from it, and ``vrg-vm
    update``, which checks it before copying it into any VM.

    Raises:
        MarketplaceError: the file is missing, unreadable, not UTF-8, not
            valid JSON, or not a JSON object.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path}: {exc}"
        raise MarketplaceError(msg) from exc
    return parse_settings(text, str(path))


def _section(settings: dict[str, Any], key: str) -> dict[str, Any]:
    """Return the settings object *key*, or ``{}`` when it is absent.

    Raises:
        MarketplaceError: the value is present but not a JSON object.
    """
    value = settings.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = f"settings.json {key} is not a JSON object"
        raise MarketplaceError(msg)
    return value


_SOURCE_STRINGS = ("source", "repo", "url", "path", "ref")


def _declared_sources(settings: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return each ``extraKnownMarketplaces`` entry's name and ``source`` object.

    An entry with no ``source`` yields ``{}``. An empty ``ref`` reads as no ref.

    Raises:
        MarketplaceError: an entry, its ``source``, or one of the source's
            string fields has the wrong JSON type.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    for name, spec in _section(settings, "extraKnownMarketplaces").items():
        where = f"settings.json extraKnownMarketplaces.{name}"
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            msg = f"{where} is not a JSON object"
            raise MarketplaceError(msg)
        src = spec.get("source")
        if src is None:
            src = {}
        if not isinstance(src, dict):
            msg = f"{where}.source is not a JSON object"
            raise MarketplaceError(msg)
        for field in _SOURCE_STRINGS:
            if src.get(field) is not None and not isinstance(src[field], str):
                msg = f"{where}.source.{field} is not a string"
                raise MarketplaceError(msg)
        entries.append((name, {**src, "ref": src.get("ref") or None}))
    return entries


def declared_marketplaces(settings: dict[str, Any]) -> list[Declared]:
    """Return the GitHub marketplaces ``extraKnownMarketplaces`` declares.

    Only a ``github`` source with a ``repo`` is returned: that is the source
    shape whose registration ``marketplace list --json`` reports as a repo and
    ref, so it is the one the refresh can verify. An empty ``ref`` reads as no
    ref, as it does for the add.

    Raises:
        MarketplaceError: ``extraKnownMarketplaces`` has the wrong shape.
    """
    declared: list[Declared] = []
    for name, src in _declared_sources(settings):
        if src.get("source") != "github" or not src.get("repo"):
            continue
        declared.append(Declared(name, src["repo"], src["ref"]))
    return declared


def _enabled_plugin_ids(settings: dict[str, Any]) -> set[str]:
    """Return the plugin ids ``enabledPlugins`` marks true.

    Declarative desired set: the authoritative list of what *should* be
    present, independent of what is installed. An enabled entry is not proof
    of an install: the Claude Code v2.1.195 changelog says only that external
    plugins enabled solely by a project ``.claude/settings.json`` need explicit
    install consent. Whether user-enabled plugins install on their own is
    still unverified (vergil-project/.github#578).

    Raises:
        MarketplaceError: ``enabledPlugins`` is not a JSON object, or one of
            its values is not a boolean.
    """
    enabled: set[str] = set()
    for pid, on in _section(settings, "enabledPlugins").items():
        if not isinstance(on, bool):
            msg = f"settings.json enabledPlugins.{pid} is not a boolean"
            raise MarketplaceError(msg)
        if on:
            enabled.add(pid)
    return enabled


def _marketplace_sources(settings: dict[str, Any]) -> list[str]:
    """Return the ``marketplace add`` arguments needed for the enabled plugins, quoted.

    The arguments of :func:`_marketplace_adds`, without their names.
    """
    return [arg for _, arg in _marketplace_adds(settings)]


def _marketplace_adds(settings: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(marketplace name, quoted add-arg)`` for each marketplace to add.

    Primarily the ``extraKnownMarketplaces`` entries (each ``source`` -> an
    add-arg: ``owner/repo[@ref]``, a URL ``[#ref]``, or a path). Headless
    ``claude plugin marketplace update``/``list`` do NOT register these on a
    fresh box (#2021); only ``marketplace add <source>`` clones the catalog.
    Each declared value is quoted on its own, so ``repo@ref`` stays one shell
    word.

    Hardening (#2029): an enabled plugin can reference a marketplace not
    declared in ``extraKnownMarketplaces``, notably the built-in
    ``claude-plugins-official``, which is not always listed. For such a
    plugin, derive a source where we can (official -> its Anthropic repo); an
    undecidable third-party marketplace has no derivable source and is left
    for the install to surface, never invented.
    """
    declared = _declared_sources(settings)
    extra = {name for name, _ in declared}
    adds: list[tuple[str, str]] = []
    for name, src in declared:
        # #3751: keep a declared `ref`. Claude Code refuses an add whose source
        # does not match the extraKnownMarketplaces entry (failureCode
        # "declared"). A github repo pins with `@ref` (the form `marketplace
        # list` shows); a git URL pins with `#ref` (Claude Code docs, "Add a
        # marketplace"). A path has no ref.
        ref = src["ref"]
        if src.get("repo"):
            base, sep = src["repo"], "@"
        elif src.get("url"):
            base, sep = src["url"], "#"
        elif src.get("path"):
            base, sep = src["path"], ""
        else:
            continue
        arg = shlex.quote(base)
        if ref and sep:
            arg = f"{arg}{sep}{shlex.quote(ref)}"
        adds.append((name, arg))
    official = (_OFFICIAL_MARKETPLACE, _OFFICIAL_MARKETPLACE_SOURCE)
    for pid in _enabled_plugin_ids(settings):
        if "@" not in pid:
            continue
        market = pid.split("@", 1)[1]
        if market in extra:
            continue
        if market == _OFFICIAL_MARKETPLACE and official not in adds:
            adds.append(official)
    return adds


def registered_marketplaces(run: Run, prefix: str) -> dict[str, tuple[str | None, str | None]]:
    """Return each registered marketplace's ``(repo, ref)``, by name.

    Read from ``claude plugin marketplace list --json``, whose entries carry
    ``name``, ``repo`` and ``ref`` (the shape recorded by
    vergil-project/vergil-tooling#3850). A missing ``repo`` or ``ref`` reads
    as ``None``.

    Raises:
        MarketplaceError: the output is not a JSON list of objects that each
            carry a string ``name``.
    """
    result = run(*shell(prefix, "claude plugin marketplace list --json"))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"claude plugin marketplace list --json printed invalid JSON: {exc}"
        raise MarketplaceError(msg) from exc
    if not isinstance(data, list):
        msg = f"claude plugin marketplace list --json printed {type(data).__name__}, not a list"
        raise MarketplaceError(msg)
    registered: dict[str, tuple[str | None, str | None]] = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            msg = f"claude plugin marketplace list --json has an entry without a name: {entry!r}"
            raise MarketplaceError(msg)
        registered[entry["name"]] = (entry.get("repo"), entry.get("ref"))
    return registered


def _describe(repo: str | None, ref: str | None) -> str:
    return f"repo {repo or '(none)'} ref {ref or '(none)'}"


def verify(run: Run, settings: dict[str, Any], prefix: str) -> list[str]:
    """Check each declared GitHub marketplace is registered at its declared repo and ref.

    Returns one ``marketplace:<name>`` label per declared marketplace that is
    not registered, or whose registered repo or ref differs from the declared
    one. A registration with no ``ref`` differs from a declared ref.

    Raises:
        MarketplaceError: ``marketplace list --json`` is malformed.
    """
    declared = declared_marketplaces(settings)
    if not declared:
        return []
    registered = registered_marketplaces(run, prefix)
    failures: list[str] = []
    for want in declared:
        label = f"marketplace:{want.name}"
        if want.name not in registered:
            print(f"ERROR: {label} is declared but not registered", file=sys.stderr)
            failures.append(label)
            continue
        repo, ref = registered[want.name]
        if (repo, ref) != (want.repo, want.ref):
            print(
                f"ERROR: {label} is registered at {_describe(repo, ref)}, "
                f"but settings.json declares {_describe(want.repo, want.ref)}",
                file=sys.stderr,
            )
            failures.append(label)
    return failures


def _plugin_entry_label(entry: dict[str, Any]) -> str:
    """Name one install entry by id, scope and (for a project install) project."""
    scope = entry.get("scope", "user")
    if scope in _PROJECT_SCOPES:
        project = entry.get("projectPath") or "<no projectPath>"
        return f"{entry['id']} ({scope}: {project})"
    return f"{entry['id']} ({scope})"


def _update_plugin_entry(run: Run, entry: dict[str, Any], prefix: str) -> str | None:
    """Update one install entry at its own scope; return a failure label or None.

    A project-scoped entry runs from inside its projectPath. If that directory
    no longer exists, ``cd`` fails and the entry is reported as a failure
    naming its scope and project, never skipped. A project-scoped entry with
    no projectPath cannot be targeted at all (running it from the home
    directory would update the wrong project), so it is reported as a failure
    without running anything.
    """
    pid = entry["id"]
    scope = entry.get("scope", "user")
    label = _plugin_entry_label(entry)
    print(f"    update {label}...")
    update = f"claude plugin update {shlex.quote(pid)} --scope {shlex.quote(scope)}"
    if scope in _PROJECT_SCOPES:
        project = entry.get("projectPath")
        if not project:
            print(f"ERROR: {label}: no projectPath to update it in", file=sys.stderr)
            return label
        update = f"cd {shlex.quote(project)} && {update}"
    try:
        run(*shell(prefix, update))
    except subprocess.CalledProcessError as exc:
        _report(f"update {label}", exc)
        return label
    return None


# The install-entry fields the refresh reads, and the JSON type each must have
# when present (`id` is checked on its own, since it is required).
_INSTALL_FIELD_TYPES: tuple[tuple[str, type], ...] = (
    ("scope", str),
    ("projectPath", str),
    ("enabled", bool),
)


def _installed_entries(run: Run, prefix: str) -> list[dict[str, Any]]:
    """Return ``claude plugin list --json``: one entry per install.

    Raises:
        MarketplaceError: the output is not a JSON list of objects that each
            carry a string ``id``, or an entry's ``scope`` or ``projectPath``
            is present but not a string, or its ``enabled`` is present but not
            a boolean.
    """
    listing = run(*shell(prefix, "claude plugin list --json"))
    try:
        entries = json.loads(listing.stdout)
    except json.JSONDecodeError as exc:
        msg = f"claude plugin list --json printed invalid JSON: {exc}"
        raise MarketplaceError(msg) from exc
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries
    ):
        msg = "claude plugin list --json is not a list of objects that each carry an id"
        raise MarketplaceError(msg)
    for entry in entries:
        for field, kind in _INSTALL_FIELD_TYPES:
            if field in entry and not isinstance(entry[field], kind):
                msg = (
                    f"claude plugin list --json entry {entry['id']!r} has a "
                    f"{field} that is not a {kind.__name__}: {entry[field]!r}"
                )
                raise MarketplaceError(msg)
    return entries


def update_installed_plugins(run: Run, settings: dict[str, Any], prefix: str) -> list[str]:
    """Update every install of an enabled plugin and install enabled-but-missing ids.

    `claude plugin install`/`update` have no bulk form: each takes a specific
    id and honours a scope. ``claude plugin list --json`` has one entry per
    install, so one id can appear at user scope and again at project scope for
    each project (#3764). Every install entry is updated at its own scope; a
    project-scope entry is updated from inside its ``projectPath``. An enabled
    id with no install entry at any scope is installed at user scope (it comes
    from the user settings.json).

    Best-effort across the set: one failure does not block the others.
    Returns the failure labels, naming each entry's scope and project.
    """
    failures: list[str] = []
    desired = _enabled_plugin_ids(settings)
    # One entry per INSTALL, not per id (#3764): never key by id.
    entries = _installed_entries(run, prefix)
    installed_ids = {entry["id"] for entry in entries}

    # Refresh every install of a declared-enabled id, plus any install that is
    # itself enabled (the pre-#2006 behaviour), each at its own scope/project.
    updates = [e for e in entries if e["id"] in desired or e.get("enabled")]
    for entry in sorted(updates, key=_plugin_entry_label):
        failure = _update_plugin_entry(run, entry, prefix)
        if failure:
            failures.append(failure)

    for pid in sorted(desired - installed_ids):
        label = f"{pid} (user)"
        print(f"    install {label}...")
        try:
            run(*shell(prefix, f"claude plugin install {shlex.quote(pid)} --scope user"))
        except subprocess.CalledProcessError as exc:
            _report(f"install {label}", exc)
            failures.append(label)
    return failures


def refresh(run: Run, settings: dict[str, Any], prefix: str) -> list[str]:
    """Re-add each declared marketplace, update, update the installs, then verify.

    The re-add at the declared ref is what switches a registered marketplace
    to a new ref (spec §6). Returns every failure label, the verification's
    included; an empty list means the refresh succeeded and every declared
    GitHub marketplace is registered at its declared repo and ref.

    Raises:
        MarketplaceError: a ``list --json`` output is malformed.
        subprocess.CalledProcessError: a ``list --json`` command failed, so
            the state could not be read at all.
    """
    failures: list[str] = []
    for name, source in _marketplace_adds(settings):
        print(f"    registering marketplace {source}...")
        try:
            run(*shell(prefix, f"claude plugin marketplace add {source}"))
        except subprocess.CalledProcessError as exc:
            _report(f"marketplace add {source}", exc)
            failures.append(f"marketplace:{name}")
    try:
        run(*shell(prefix, "claude plugin marketplace update"))
    except subprocess.CalledProcessError as exc:
        _report("marketplace update", exc)
        failures.append("marketplace update")
    failures.extend(update_installed_plugins(run, settings, prefix))
    failures.extend(verify(run, settings, prefix))
    # A marketplace whose add failed usually fails its verification too; both
    # carry the same `marketplace:<name>` label, so it is named once.
    return list(dict.fromkeys(failures))
