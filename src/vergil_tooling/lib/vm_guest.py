"""Guest-side provisioning steps, transport-agnostic.

Every function here runs commands *inside* a guest via a
:class:`~vergil_tooling.lib.vm_transport.Transport`, so the same credential and
tooling logic serves a local Lima instance or a remote cloud host unchanged —
only the transport differs. Moved out of ``lima.py`` (#1706) so the Lima and
off-platform backends share one implementation rather than forking it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity
    from vergil_tooling.lib.vm_transport import Transport

_TOOLING_INSTALL = "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@{tag}"
_TOOLING_TAG_FILE = "~/.config/vergil/tooling-tag"


def _read_host_git_config(key: str) -> str | None:
    """Read a single value from the host's global git config."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "config", "--global", key],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _inject_host_git_identity(transport: Transport) -> None:
    """Copy user.name and user.email from host git config into the VM."""
    name = _read_host_git_config("user.name")
    email = _read_host_git_config("user.email")

    if name:
        print(f"  Setting git user.name: {name}")
        transport.run("git", "config", "--global", "user.name", name)
    if email:
        print(f"  Setting git user.email: {email}")
        transport.run("git", "config", "--global", "user.email", email)


def inject_credentials(transport: Transport, identity: Identity) -> None:
    """Inject GitHub App and Claude Code credentials into a running VM."""
    if identity.auth_type == "none":
        # Credential-less identity: skip the entire stage. No App key, no
        # app.env, no identity-mode file, no git identity, no HTTPS rewrite,
        # no Claude token. The box can only touch local files.
        print("  Skipping credential injection (credential-less identity)")
        return

    key_path = Path(identity.private_key_path).expanduser()
    if not key_path.exists():
        print(f"ERROR: private key not found: {key_path}", file=sys.stderr)
        raise SystemExit(1)

    if not identity.mode:
        print(
            "ERROR: cannot derive identity mode for VM — rename the"
            " identity in identities.toml so the name contains 'user' or 'audit'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    key_content = key_path.read_text()

    print("  Injecting App private key...")
    transport.run("bash", "-c", "mkdir -p ~/.config/vergil")
    transport.pipe(
        "cat > ~/.config/vergil/app.pem && chmod 600 ~/.config/vergil/app.pem",
        key_content,
    )

    print("  Injecting App configuration...")
    transport.pipe(
        "cat > ~/.config/vergil/app.env && chmod 600 ~/.config/vergil/app.env",
        f"APP_ID={identity.app_id}\n",
    )

    _inject_identity_mode(transport, identity.mode)

    _inject_host_git_identity(transport)

    print("  Configuring git for HTTPS GitHub access...")
    transport.run(
        "git",
        "config",
        "--global",
        "url.https://github.com/.insteadOf",
        "git@github.com:",
    )

    if identity.claude_token_path:
        _inject_claude_token(transport, identity.claude_token_path)


_BASHRC_MODE_LINE = (
    "[ -f ~/.config/vergil/identity-mode ]"
    ' && export VRG_IDENTITY_MODE="$(cat ~/.config/vergil/identity-mode)"'
)


def _inject_identity_mode(transport: Transport, mode: str) -> None:
    """Write the identity-mode file and export it from the shell profile.

    The plain-text mode file is the single source of truth: the bashrc
    line exports it as ``VRG_IDENTITY_MODE`` for interactive shells (and
    skill preflights), and ``identity_mode.current_mode()`` reads the
    file directly as a fallback for processes that never sourced bashrc.
    """
    print(f"  Injecting identity mode ({mode})...")
    transport.pipe(
        "cat > ~/.config/vergil/identity-mode && chmod 600 ~/.config/vergil/identity-mode",
        f"{mode}\n",
    )
    export_cmd = (
        f'grep -qF "identity-mode" ~/.bashrc 2>/dev/null'
        f" || echo '{_BASHRC_MODE_LINE}' >> ~/.bashrc"
    )
    transport.run("bash", "-c", export_cmd)


_BASHRC_SOURCE_LINE = "[ -f ~/.config/vergil/claude.env ] && . ~/.config/vergil/claude.env"


def _inject_claude_token(transport: Transport, token_path: str) -> None:
    path = Path(token_path).expanduser()
    if not path.exists():
        print(f"ERROR: Claude token not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    token = path.read_text().strip()

    print("  Injecting Claude Code token...")
    transport.pipe(
        "cat > ~/.config/vergil/claude.env && chmod 600 ~/.config/vergil/claude.env",
        f"export CLAUDE_CODE_OAUTH_TOKEN={token}\n",
    )

    source_cmd = (
        f'grep -qF "claude.env" ~/.bashrc 2>/dev/null'
        f" || echo '{_BASHRC_SOURCE_LINE}' >> ~/.bashrc"
    )
    transport.run("bash", "-c", source_cmd)

    credentials = json.dumps({"claudeAiOauth": {"accessToken": token}})
    transport.run("bash", "-c", "mkdir -p ~/.claude")
    transport.pipe(
        "cat > ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json",
        credentials + "\n",
    )

    onboarding = json.dumps({"hasCompletedOnboarding": True})
    transport.pipe(
        "cat > ~/.claude.json",
        onboarding + "\n",
    )


def get_tooling_version(transport: Transport) -> str | None:
    """Return the installed vergil-tooling version string, or None."""
    try:
        result = transport.run(
            "bash",
            "-c",
            'export PATH="$HOME/.local/bin:$PATH" && uv tool list 2>/dev/null',
        )
        for line in result.stdout.splitlines():
            if line.startswith("vergil-tooling "):
                return line.split()[1]
    except subprocess.CalledProcessError:
        pass
    return None


def _uv_tool_install(transport: Transport, install_spec: str, *, reinstall: bool) -> None:
    """Run ``uv tool install`` inside the VM, self-healing a poisoned uv cache.

    A corrupt cache entry — e.g. a zero-byte wheel ``METADATA`` left behind by
    an unclean VM stop — makes ``uv tool install`` fail with "wheel is invalid".
    On the reinstall path uv removes the existing tool *before* it fails, so a
    poisoned cache leaves the VM with no tooling at all and bricks every future
    ``vrg-vm session`` until the cache is cleared by hand. So on failure, clear
    the VM's uv cache and retry the install once.

    The retry escalates to ``--force``. The same unclean stop also corrupts the
    tool *receipt*: uv removes the entry but, unable to read the receipt, cannot
    enumerate the tool's entry points, so the ``vrg-*`` executables orphan in
    ``~/.local/bin``. Clearing the cache fixes the wheel, but the retry would
    then die with "Executable already exists" — only ``--force`` replaces
    existing entry points (``--reinstall`` alone does not), so the retry must
    force to fully recover.
    """
    flag = "--reinstall " if reinstall else ""
    install_cmd = f'export PATH="$HOME/.local/bin:$PATH" && uv tool install {flag}"{install_spec}"'
    retry_cmd = (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f'uv tool install --force --reinstall "{install_spec}"'
    )
    try:
        transport.run("bash", "-c", install_cmd)
        return
    except subprocess.CalledProcessError:
        print(
            "  uv tool install failed — clearing the VM uv cache and retrying once...",
            file=sys.stderr,
        )
        transport.run(
            "bash",
            "-c",
            'export PATH="$HOME/.local/bin:$PATH" && uv cache clean',
        )
        transport.run("bash", "-c", retry_cmd)


def install_tooling(transport: Transport, tag: str) -> None:
    """Install vergil-tooling inside the VM and record the tag."""
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Installing vergil-tooling ({tag})...")
    _uv_tool_install(transport, install_spec, reinstall=False)
    transport.run("bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
    transport.pipe(f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


def update_tooling(transport: Transport, tag: str | None = None, *, fallback_tag: str = "") -> None:
    """Reinstall vergil-tooling inside the VM.

    Uses *tag* if given, otherwise reads the tag from the marker file
    written by ``install_tooling``.  Falls back to *fallback_tag* when
    no marker exists (pre-existing VMs created before marker support).

    An explicit *tag* is treated as a temporary override and is not
    persisted to the marker file.
    """
    explicit = tag is not None
    if tag is None:
        result = transport.run("bash", "-c", f"cat {_TOOLING_TAG_FILE} 2>/dev/null || true")
        tag = result.stdout.strip() or fallback_tag
    if not tag:
        print("ERROR: no tooling tag found — run 'vrg-vm create' first", file=sys.stderr)
        raise SystemExit(1)
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Updating vergil-tooling ({tag})...")
    _uv_tool_install(transport, install_spec, reinstall=True)
    if not explicit:
        transport.run("bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
        transport.pipe(f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


# Prepended to every in-guest `claude` invocation. claude itself is on the base
# PATH (/usr/bin/claude, from apt node + `npm install -g`), but exporting PATH
# explicitly — exactly as update_tooling does — keeps resolution independent of
# the interactive environment. The guest's login shell is zsh (vergil-vm `chsh -s
# /bin/zsh`), configured via ~/.zshenv / /etc/environment, so a bash login shell
# would source none of its config; a non-login `bash -c` with an explicit PATH
# avoids depending on any of that.
_PLUGIN_PATH_EXPORT = 'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'


def _desired_enabled_plugins(transport: Transport) -> set[str]:
    """Return the plugin ids the guest settings.json marks enabled.

    The desired set is declarative: ``enabledPlugins`` in the guest
    ``~/.claude/settings.json`` (seeded from the host). Since Claude Code
    v2.1.195 an enabled entry no longer auto-installs, so this is the
    authoritative list of what *should* be present, independent of what is
    installed. Absent or unreadable settings yield an empty set — the caller
    still refreshes anything already installed-and-enabled.
    """
    try:
        result = transport.run(
            "bash",
            "-c",
            f"{_PLUGIN_PATH_EXPORT} && cat ~/.claude/settings.json",
        )
    except subprocess.CalledProcessError:
        return set()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    enabled = data.get("enabledPlugins", {})
    return {pid for pid, on in enabled.items() if on}


def update_plugins(transport: Transport) -> None:
    """Reconcile enabled Claude Code plugins inside the guest.

    Plugins are installed guest-locally from their GitHub marketplaces (declared
    in the copied settings.json); they are deliberately not shared from the host
    (see docs/site/docs/guides/agent-vm-claude-share-set.md).

    Reconcile, not just refresh (#2006): the enabled set is declared by
    ``enabledPlugins`` in the guest settings.json, but since Claude Code
    v2.1.195 declaring a plugin there no longer auto-installs it — an
    update-only pass leaves a fresh box with zero plugins. So install any
    enabled-but-missing plugin, then advance every enabled plugin to its latest
    version, mirroring how update_tooling advances vergil-tooling.

    Transport-generic (#1812): the same reconcile runs over a local Lima
    instance or a remote off-platform box, so an in-place off-platform
    ``update`` reaches the plugins over IAP without a rebuild.

    `claude plugin install`/`update` have no bulk form: each takes a specific
    id and honours a scope. Enabled-but-uninstalled ids are installed at user
    scope (they come from the user settings.json); already-installed ids are
    updated at their existing scope (from `claude plugin list --json`). Updates
    apply on the next Claude restart, which every new session triggers.

    Best-effort across the set: one plugin failing does not block the others;
    failures are collected and surfaced by raising afterwards (never swallowed).
    """
    print("  Refreshing Claude plugins...")
    transport.run(
        "bash",
        "-c",
        f"{_PLUGIN_PATH_EXPORT} && claude plugin marketplace update",
    )
    desired = _desired_enabled_plugins(transport)
    listing = transport.run(
        "bash",
        "-c",
        f"{_PLUGIN_PATH_EXPORT} && claude plugin list --json",
    )
    installed = {plugin["id"]: plugin for plugin in json.loads(listing.stdout)}

    # Reconcile the declared-enabled set with what is installed, while still
    # refreshing anything already installed-and-enabled (the pre-#2006 behaviour).
    targets = desired | {pid for pid, plugin in installed.items() if plugin.get("enabled")}

    failures: list[str] = []
    for pid in sorted(targets):
        if pid in installed:
            action = "update"
            scope = installed[pid].get("scope", "user")
        else:
            action = "install"
            scope = "user"
        print(f"    {action} {pid} ({scope})...")
        cmd = (
            f"{_PLUGIN_PATH_EXPORT} && claude plugin {action} "
            f"{shlex.quote(pid)} --scope {shlex.quote(scope)}"
        )
        try:
            transport.run("bash", "-c", cmd)
        except subprocess.CalledProcessError:
            failures.append(pid)

    if failures:
        joined = ", ".join(failures)
        msg = f"failed to reconcile plugin(s): {joined}"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)


_CLAUDE_CONFIG_FILES = ("CLAUDE.md", "settings.json")


def copy_claude_config(transport: Transport, claude_dir: Path) -> None:
    """Copy CLAUDE.md and settings.json from host into the VM."""
    if not claude_dir.is_dir():
        return
    transport.run("bash", "-c", "mkdir -p ~/.claude")
    for filename in _CLAUDE_CONFIG_FILES:
        source = claude_dir / filename
        if source.exists():
            content = source.read_text()
            transport.pipe(
                f"cat > ~/.claude/{filename}",
                content,
            )


# The agent-VM ~/.claude share set. See
# docs/site/docs/guides/agent-vm-claude-share-set.md for the full model.
#
# projects/ -> durable, host-shared transcript store. Resume-after-rebuild
#   reads these, so a conversation survives a VM rebuild (the data lives on
#   the host). Append-only writes, which work fine through the virtiofs mount.
# skills/   -> read-only reference mount.
# sessions/ -> NOT shared; see _CLAUDE_UNLINK_DIRS below. It is a disposable
#   VM-local roster (pid->session), regenerated each run. Resume does NOT
#   depend on it, so keeping it VM-local does not break resume.
# plugins/  -> NOT shared; kept current VM-locally via update_plugins (each VM
#   installs/refreshes from the GitHub marketplaces declared in the copied
#   settings.json). Sharing the host's materialized checkout across the
#   macOS/Linux boundary would be fragile and is unnecessary.
_CLAUDE_LINK_DIRS = ("projects", "skills")

# Subdirs that must NOT be symlinked onto the host mount and must stay
# VM-local. Claude writes the sessions roster atomically (temp file in the
# VM-local tmpdir, then rename() onto the target). Renaming across filesystems
# (VM-local ext -> virtiofs mount) fails with EXDEV, so the roster write
# silently fails and no file is ever produced. The roster is also per-platform
# (pids only mean anything on the owning machine), so there is no value in
# sharing it. We instead read each VM's local roster over the transport for
# session detection. See vergil-tooling #1301 and vergil-vm #73.
_CLAUDE_UNLINK_DIRS = ("sessions",)


def link_claude_dirs(transport: Transport, claude_dir: Path) -> None:
    """Point selected VM ~/.claude subdirs at the path-preserved host mounts.

    The .claude/{projects,skills} mounts are path-preserved
    (mountPoint == host location), but the VM's HOME differs from the host's,
    so Claude inside the VM reads ~/.claude/... instead of the mounted host
    path. Symlink those subdirs so they are shared with the host and survive
    VM rebuilds. Idempotent; an existing non-empty real directory is left in
    place with a warning rather than clobbered.

    sessions/ is deliberately left VM-local (see _CLAUDE_UNLINK_DIRS): a
    symlink onto the virtiofs mount breaks Claude's atomic roster write with
    EXDEV. Any pre-existing sessions/ symlink (from an older build) is removed
    so Claude recreates a real local directory; removing the symlink does not
    touch the host target's contents.
    """
    if not claude_dir.is_dir():
        return
    parts = ["mkdir -p ~/.claude"]
    for sub in _CLAUDE_LINK_DIRS:
        target = shlex.quote(str(claude_dir / sub))
        link = f'"$HOME/.claude/{sub}"'
        parts.append(
            f"if [ -L {link} ] || [ ! -e {link} ]; then ln -sfn {target} {link}; "
            f'elif [ -d {link} ] && [ -z "$(ls -A {link})" ]; then '
            f"rmdir {link} && ln -s {target} {link}; "
            f'else echo "WARNING: ~/.claude/{sub} exists and is not empty;'
            f' not linking" >&2; fi'
        )
    for sub in _CLAUDE_UNLINK_DIRS:
        link = f'"$HOME/.claude/{sub}"'
        parts.append(f"if [ -L {link} ]; then rm -f {link}; fi")
    transport.run("bash", "-c", " ; ".join(parts))


_FINGERPRINT_PATH = "/etc/vergil/vm-spec.fingerprint"


class VmUnreachableError(RuntimeError):
    """Raised when the shell transport to a VM fails (e.g. SSH connection
    refused) — the VM could not be contacted at all.

    Distinct from an absent spec marker: an unreachable VM says nothing about
    whether its spec drifted, so callers must not collapse this into the
    "needs-rebuild" signal. Doing so misreports a reachability failure as spec
    drift and tells the user to rebuild a VM that may just be mid-boot or wedged.
    """


def read_fingerprint(transport: Transport) -> str | None:
    """Return the spec fingerprint stamped into the VM, or None if the marker is absent.

    The in-guest read is masked (``cat ... 2>/dev/null || true``) so a missing
    marker yields empty stdout from a zero exit rather than a non-zero exit. Any
    remaining failure of the shell round-trip is therefore unambiguously a
    transport failure (the VM is unreachable), which is raised as
    ``VmUnreachableError`` instead of being collapsed into None — an unreachable
    VM is not a drifted VM.
    """
    try:
        result = transport.run("bash", "-c", f"cat {_FINGERPRINT_PATH} 2>/dev/null || true")
    except subprocess.CalledProcessError as exc:
        raise VmUnreachableError from exc
    value = result.stdout.strip()
    return value or None


def vm_spec_status(transport: Transport, expected_fingerprint: str) -> str:
    """Compare the VM's stamped fingerprint to the freshly composed one.

    Returns 'ok' on match, 'needs-rebuild' on drift (including a missing marker on a
    box that should carry one), and 'unreachable' when the VM cannot be contacted
    over the shell transport. 'unreachable' is deliberately *not* drift: the
    spec was never read, so the caller must remediate reachability, not rebuild.
    """
    try:
        actual = read_fingerprint(transport)
    except VmUnreachableError:
        return "unreachable"
    return "ok" if actual == expected_fingerprint else "needs-rebuild"


# In-VM classifier: count agent vs human login sessions by walking each logind user
# tty/pty session's process subtree for `claude`. Direct counts, no subtraction.
_OCCUPANCY_SCRIPT = r"""
set -u
has_claude() {
  local pids="$1" p comm next
  while [ -n "$pids" ]; do
    next=""
    for p in $pids; do
      comm=$(cat "/proc/$p/comm" 2>/dev/null || echo "")
      [ "$comm" = "claude" ] && return 0
      next="$next $(pgrep -P "$p" 2>/dev/null || true)"
    done
    pids="$next"
  done
  return 1
}
agents=0; humans=0
for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
  cls=$(loginctl show-session "$s" -p Class --value 2>/dev/null || echo "")
  typ=$(loginctl show-session "$s" -p Type --value 2>/dev/null || echo "")
  [ "$cls" = "user" ] || continue
  case "$typ" in tty|pty) ;; *) continue ;; esac
  leader=$(loginctl show-session "$s" -p Leader --value 2>/dev/null || echo "")
  [ -n "$leader" ] || continue
  if has_claude "$leader"; then agents=$((agents+1)); else humans=$((humans+1)); fi
done
echo "agents=$agents humans=$humans"
"""

_OCCUPANCY_RE = re.compile(r"agents=(\d+)\s+humans=(\d+)")

# Appended to the occupancy script so one shell round-trip yields both values.
# Failure to read the marker is masked in-script: an absent fingerprint is the
# empty string, matching read_fingerprint's absent/unreadable -> None contract.
_FINGERPRINT_PROBE = f'\necho "fingerprint=$(cat {_FINGERPRINT_PATH} 2>/dev/null || true)"\n'

_FINGERPRINT_RE = re.compile(r"^fingerprint=(.*)$", re.MULTILINE)


def vm_probe(transport: Transport, *, fingerprint: bool = False) -> tuple[int, int, str | None]:
    """Probe a running VM in a single shell round-trip.

    Returns (agents, humans, fingerprint). Occupancy keeps vm_occupancy's
    contract — (0, 0) only on parse/exec failure. The fingerprint is read in
    the same invocation when requested and is None when not requested, absent,
    or unreadable, matching read_fingerprint.
    """
    script = _OCCUPANCY_SCRIPT + _FINGERPRINT_PROBE if fingerprint else _OCCUPANCY_SCRIPT
    try:
        result = transport.run("bash", "-c", script)
    except subprocess.CalledProcessError:
        return (0, 0, None)
    match = _OCCUPANCY_RE.search(result.stdout)
    agents, humans = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    stamped: str | None = None
    if fingerprint:
        fp_match = _FINGERPRINT_RE.search(result.stdout)
        if fp_match:
            stamped = fp_match.group(1).strip() or None
    return (agents, humans, stamped)


def vm_occupancy(transport: Transport) -> tuple[int, int]:
    """Return (agents, humans) for a running VM by process-tree classification.

    Agents are login sessions whose subtree roots `claude`; humans are interactive
    user tty/pty sessions that are not agent-hosting. Returns (0, 0) on any parse/exec
    failure rather than guessing.
    """
    agents, humans, _ = vm_probe(transport)
    return (agents, humans)
