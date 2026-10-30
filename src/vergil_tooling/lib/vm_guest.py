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
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import claude_marketplace, vm_packages
from vergil_tooling.lib.trusted_authors import VM_FILE_NAME, render_vm_file
from vergil_tooling.lib.vm_transport import TransportTimeoutError

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity
    from vergil_tooling.lib.trusted_authors import TrustedAuthors
    from vergil_tooling.lib.vm_transport import Transport

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

    if identity.conan_audit_token_path:
        _inject_conan_token(transport, identity.conan_audit_token_path)


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


_TRUSTED_AUTHORS_FILE = f"~/.config/vergil/{VM_FILE_NAME}"


def inject_trusted_authors(transport: Transport, authors: TrustedAuthors | None) -> None:
    """Deliver the host's ``[trusted_authors]`` stanza into the VM, or remove it.

    Writes ``~/.config/vergil/trusted-authors.toml`` (mode 600) when *authors* is
    set. When the host has no stanza (*authors* is ``None``) the VM file is
    **deleted**, so ``vrg-trusted-authors`` in the VM fails loudly instead of
    trusting a stale list from an earlier delivery.
    """
    if authors is None:
        print("  Removing trusted-authors file (host has no [trusted_authors] stanza)...")
        transport.run("bash", "-c", f"rm -f {_TRUSTED_AUTHORS_FILE}")
        return
    print("  Injecting trusted authors...")
    transport.run("bash", "-c", "mkdir -p ~/.config/vergil")
    transport.pipe(
        f"(umask 077 && cat > {_TRUSTED_AUTHORS_FILE}) && chmod 600 {_TRUSTED_AUTHORS_FILE}",
        render_vm_file(authors),
    )


_BASHRC_CONAN_SOURCE_LINE = "[ -f ~/.config/vergil/conan.env ] && . ~/.config/vergil/conan.env"


def _inject_conan_token(transport: Transport, token_path: str) -> None:
    """Inject the Conan audit provider token as an env file sourced from bashrc.

    Mirrors ``_inject_claude_token``: read the bare token from the host file,
    write ``export CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER=<token>`` into
    ``~/.config/vergil/conan.env`` (peer to ``claude.env``), and add an
    idempotent ``~/.bashrc`` source line so interactive shells pick it up. The
    token value is shell-quoted so any special characters survive the export.

    ``conan.env`` is the canonical copy: ``vrg-container-run`` parses it at the
    point of use (``lib/conan_token``), so the container gets the token under any
    shell (#2902). The bashrc line is only a convenience for running ``conan``
    by hand in an interactive bash; nothing in the tooling depends on it.
    """
    path = Path(token_path).expanduser()
    if not path.exists():
        print(f"ERROR: Conan audit token not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    token = path.read_text().strip()

    print("  Injecting Conan audit token...")
    transport.pipe(
        "cat > ~/.config/vergil/conan.env && chmod 600 ~/.config/vergil/conan.env",
        f"export CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER={shlex.quote(token)}\n",
    )

    source_cmd = (
        f'grep -qF "conan.env" ~/.bashrc 2>/dev/null'
        f" || echo '{_BASHRC_CONAN_SOURCE_LINE}' >> ~/.bashrc"
    )
    transport.run("bash", "-c", source_cmd)


def get_tooling_version(transport: Transport) -> str | None:
    """Return the vergil-tooling version on the VM's PATH, or None.

    A uv-installed copy (a dev install, or a legacy pre-package install) sits in
    ``~/.local/bin`` and shadows the packaged ``/usr/bin`` one, so it is reported
    first; otherwise the installed deb's version is reported.
    """
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
    result = transport.run(
        "bash",
        "-c",
        "dpkg-query -W -f='${Version}' vergil-tooling 2>/dev/null || true",
    )
    return result.stdout.strip() or None


def _install(transport: Transport, tag: str) -> None:
    """Install vergil-tooling for *tag*: packaged for a release, uv for a dev ref."""
    if vm_packages.classify_ref(tag) == "dev":
        vm_packages.dev_install(transport, tag)
    else:
        vm_packages.packaged_install(transport, tag)


def _write_tag_file(transport: Transport, tag: str) -> None:
    transport.run("bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
    transport.pipe(f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


def install_tooling(transport: Transport, tag: str) -> None:
    """Install vergil-tooling inside the VM and record the tag."""
    print(f"  Installing vergil-tooling ({tag})...")
    _install(transport, tag)
    _write_tag_file(transport, tag)


def update_tooling(transport: Transport, tag: str | None = None, *, fallback_tag: str = "") -> None:
    """Reinstall or upgrade vergil-tooling inside the VM.

    Uses *tag* if given, otherwise reads the tag from the marker file
    written by ``install_tooling``.  Falls back to *fallback_tag* when
    no marker exists (pre-existing VMs created before marker support).

    An explicit *tag* is treated as a temporary override and is not
    persisted to the marker file, so a later plain update returns to the
    persisted (identity) version — which, for a release version, is the
    packaged install and removes any dev install.
    """
    explicit = tag is not None
    if tag is None:
        result = transport.run("bash", "-c", f"cat {_TOOLING_TAG_FILE} 2>/dev/null || true")
        tag = result.stdout.strip() or fallback_tag
    if not tag:
        print("ERROR: no tooling tag found — run 'vrg-vm create' first", file=sys.stderr)
        raise SystemExit(1)
    print(f"  Updating vergil-tooling ({tag})...")
    _install(transport, tag)
    if not explicit:
        _write_tag_file(transport, tag)


# Answers whether the box's installed vrg-container-cache can prune, with one token
# on stdout and exit 0 in every case: a box on a release older than the prune
# subcommand (#3481) is expected, not an error, so it must be told apart from a
# prune that ran and failed. `prune --help` exits before the command needs a
# container runtime or a repo set.
_PRUNE_PROBE = (
    'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; '
    "if ! command -v vrg-container-cache >/dev/null 2>&1; then echo absent; "
    "elif vrg-container-cache prune --help >/dev/null 2>&1; then echo ok; "
    "else echo no-prune; fi"
)


def _print_indented(text: str, *, err: bool = False) -> None:
    for line in text.splitlines():
        print(f"    {line}" if line else "", file=sys.stderr if err else sys.stdout)


def prune_cache_images(transport: Transport, roots: list[str] | None = None) -> bool:
    """Remove the box's cached images of finished branches; report, never raise.

    Runs the box's own installed ``vrg-container-cache prune --apply`` (#3604), so
    it is the freshly updated version that decides. *roots* become ``--root``
    arguments; ``None`` keeps the box's platform default repo set. A Lima box
    needs them: its default is derived from the current checkout, and the
    transport runs from ``/tmp``.

    Best-effort by contract: the prune is housekeeping riding on an update, so
    a failure is printed loudly (the command's own output and the exit) and
    reported by returning ``False``, never raised into the update. A box whose
    tooling has no ``prune`` yet is reported plainly and skipped.
    """
    print("  Pruning cached images of finished branches...")
    try:
        answer = transport.run("bash", "-c", _PRUNE_PROBE).stdout.strip()
    except (subprocess.CalledProcessError, TransportTimeoutError) as exc:
        print(
            f"ERROR: cache prune skipped: could not check the box's vrg-container-cache "
            f"({_failure_reason(exc)}). The update itself is unaffected.",
            file=sys.stderr,
        )
        return False
    if answer != "ok":
        reasons = {
            "absent": "vrg-container-cache is not installed in the box",
            "no-prune": (
                "the box's vrg-container-cache has no 'prune' subcommand "
                "(its vergil-tooling predates it)"
            ),
        }
        reason = reasons.get(answer, f"unexpected probe output {answer!r}")
        print(f"WARNING: cache prune skipped: {reason}.", file=sys.stderr)
        return False

    root_args = "".join(f" --root {shlex.quote(r)}" for r in roots or [])
    cmd = f"{_PLUGIN_PATH_EXPORT} && vrg-container-cache prune --apply{root_args}"
    try:
        result = transport.run("bash", "-c", cmd)
    except (subprocess.CalledProcessError, TransportTimeoutError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stdout:
            _print_indented(exc.stdout, err=True)
        print(
            f"ERROR: cache prune failed in the box ({_failure_reason(exc)}). The update "
            "itself is unaffected; run 'vrg-container-cache prune' in the box to "
            "investigate.",
            file=sys.stderr,
        )
        return False
    _print_indented(result.stdout)
    return True


def _failure_reason(exc: subprocess.CalledProcessError | TransportTimeoutError) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit status {exc.returncode}"
    return f"timed out after {exc.timeout:.0f}s"


# Prepended to every in-guest `claude` invocation. claude itself is on the base
# PATH (/usr/bin/claude, from apt node + `npm install -g`), but exporting PATH
# explicitly — exactly as update_tooling does — keeps resolution independent of
# the interactive environment. The guest's login shell is zsh (vergil-vm `chsh -s
# /bin/zsh`), configured via ~/.zshenv / /etc/environment, so a bash login shell
# would source none of its config; a non-login `bash -c` with an explicit PATH
# avoids depending on any of that.
_PLUGIN_PATH_EXPORT = 'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'


_GUEST_SETTINGS = "~/.claude/settings.json"


def _read_guest_settings(transport: Transport) -> dict[str, Any]:
    """Return the guest ``~/.claude/settings.json`` as a dict.

    The file is required: ``vrg-vm`` copies the host's in before every
    refresh, so a missing, unreadable or invalid one means something is
    broken, never "nothing declared" (spec §6 of epic
    vergil-project/.github#760).

    Raises:
        RuntimeError: the file cannot be read, is not valid JSON, or is not a
            JSON object. The message names the file.
    """
    try:
        result = transport.run("bash", "-c", f"cat {_GUEST_SETTINGS}")
    except subprocess.CalledProcessError as exc:
        msg = (
            f"cannot read the guest {_GUEST_SETTINGS}: it is missing or unreadable "
            f"(exit status {exc.returncode})"
        )
        raise RuntimeError(msg) from exc
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"the guest {_GUEST_SETTINGS} is not valid JSON: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(data, dict):
        msg = f"the guest {_GUEST_SETTINGS} is not a JSON object"
        raise RuntimeError(msg)
    return data


def update_plugins(transport: Transport) -> None:
    """Refresh and verify the Claude Code plugins inside the guest.

    Plugins are installed guest-locally from their GitHub marketplaces (declared
    in the copied settings.json); they are deliberately not shared from the host
    (see docs/site/docs/guides/agent-vm-claude-share-set.md).

    Runs the shared refresh (:func:`claude_marketplace.refresh`), the same
    sequence ``vrg-host plugins`` runs on the host: re-add each declared
    marketplace at its declared ref, update the marketplaces, update every
    install of an enabled plugin at its own scope and install any enabled id
    that is missing, then verify each declared marketplace is registered at its
    declared repo and ref. Transport-generic (#1812): the same refresh runs over
    a local Lima instance or a remote off-platform box.

    Raises:
        RuntimeError: the guest settings.json is missing, unreadable or
            invalid, or any step or the verification failed. The message names
            every failure.
    """
    print("  Refreshing Claude plugins...")
    settings = _read_guest_settings(transport)
    failures = claude_marketplace.refresh(transport.run, settings, _PLUGIN_PATH_EXPORT)
    if failures:
        msg = f"plugin refresh failed: {', '.join(failures)}"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)


# Each copied file, and whether the guest copy is locked read-only after the
# write. The guest CLAUDE.md is a read-only copy of the host's on every
# transport (epic vergil-project/.github#760, spec §6); settings.json stays
# writable, because provisioning and the Claude runtime both write it.
_CLAUDE_CONFIG_FILES = (("CLAUDE.md", True), ("settings.json", False))


def copy_claude_config(
    transport: Transport, claude_dir: Path, *, require_settings: bool = False
) -> None:
    """Copy CLAUDE.md and settings.json from host into the VM.

    The sole owner of the guest ``~/.claude/CLAUDE.md`` file mode: after
    writing it, the copy runs ``chmod a-w`` on it, on Lima as well as cloud,
    so every path that copies it (start, rebuild, session, cloud create,
    update) leaves it read-only. When the host has no ``CLAUDE.md``, any guest
    copy is removed, so the guest never keeps one the host does not have. The
    mode is a guard that makes an accidental write fail loudly; it is not a
    security boundary, since the guest user owns the file.

    Each target is made writable (``chmod u+w``) before the overwrite, so a
    copy locked by an earlier run does not fail the ``cat >`` with "permission
    denied" (#2436). The ``chmod u+w`` is a harmless no-op on a fresh file
    (guarded with ``2>/dev/null``).

    With *require_settings* (the update path, which refreshes plugins from the
    copied file right after), a missing host ``settings.json`` is an error
    naming the host file, not a skip.

    Raises:
        RuntimeError: *require_settings* is set and the host settings.json is
            missing, or a host file cannot be read.
    """
    settings = claude_dir / "settings.json"
    if require_settings and not settings.is_file():
        msg = (
            f"the host {settings} is missing; it is required, because the plugin "
            "refresh reads the copy of it in the VM"
        )
        raise RuntimeError(msg)
    if not claude_dir.is_dir():
        return
    transport.run("bash", "-c", "mkdir -p ~/.claude")
    for filename, locked in _CLAUDE_CONFIG_FILES:
        source = claude_dir / filename
        dest = f"~/.claude/{filename}"
        if not source.exists():
            if locked:
                transport.run("bash", "-c", f"rm -f {dest}")
            continue
        try:
            content = source.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read the host {source}: {exc}"
            raise RuntimeError(msg) from exc
        cmd = f"chmod u+w {dest} 2>/dev/null; cat > {dest}"
        if locked:
            cmd += f" && chmod a-w {dest}"
        transport.pipe(cmd, content)


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
