"""Tests for host->guest memory projection (vm_memory)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vergil_tooling.lib import vm_memory

_SLUG = "-Users-me-dev-projects-org-repo"
_WORKDIR = "/Users/me/dev/projects/org/repo"


def test_host_slug_matches_claude_convention() -> None:
    assert vm_memory.host_slug(_WORKDIR) == _SLUG


def test_host_memory_dir() -> None:
    d = vm_memory.host_memory_dir(Path("/Users/me/.claude"), _SLUG)
    assert d == Path(f"/Users/me/.claude/projects/{_SLUG}/memory")


def _seed_memory(claude: Path) -> Path:
    """Create a host memory dir with MEMORY.md and return the memory dir path."""
    memory = claude / "projects" / _SLUG / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("m")
    return memory


def test_project_memory_copies_memory_and_claude_md(tmp_path: Path) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    _seed_memory(claude)
    (claude / "CLAUDE.md").write_text("global")

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    piped = [c.args[0] for c in transport.pipe.call_args_list]
    # copy_claude_config idiom: pipe a ``cat > <dest>`` for each file, prefixed
    # with a chmod that clears any prior read-only lock before overwrite.
    assert any("cat > " in cmd and "/memory/MEMORY.md" in cmd for cmd in piped)
    assert any("cat > " in cmd and cmd.endswith("/CLAUDE.md") for cmd in piped)
    assert all(cmd.startswith("chmod u+w ") for cmd in piped)
    # The memory content is piped as the input payload.
    contents = [c.args[1] for c in transport.pipe.call_args_list]
    assert "m" in contents
    assert "global" in contents
    # The projection is locked read-only *after* the copy: the final transport
    # command is the surgical lock (a chmod of the memory subtree + CLAUDE.md).
    last_run = transport.run.call_args_list[-1].args
    assert last_run[0] == "bash"
    assert "chmod -R a-w" in last_run[-1]
    assert f'"$HOME/.claude/projects/{_SLUG}/memory"' in last_run[-1]
    assert '"$HOME/.claude/CLAUDE.md"' in last_run[-1]


def test_project_memory_mkdirs_guest_memory_dir(tmp_path: Path) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    _seed_memory(claude)

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    # The guest memory dir is created up front over the ``bash -c`` idiom so ``~``
    # expands in the guest shell (a bare ``mkdir`` arg would not expand it). The
    # mkdir is the *first* transport.run call (the last is now the lock).
    script = transport.run.call_args_list[0].args[-1]
    assert transport.run.call_args_list[0].args[0] == "bash"
    assert f"mkdir -p ~/.claude/projects/{_SLUG}/memory" in script


def test_project_memory_copies_nested_memory_file(tmp_path: Path) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    memory = _seed_memory(claude)
    nested = memory / "notes"
    nested.mkdir()
    (nested / "topic.md").write_text("n")

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    # Nested subdir is created (its parent added to the mkdir set) and the file
    # is projected at the matching relative path. The mkdir is the first run call.
    script = transport.run.call_args_list[0].args[-1]
    assert f"~/.claude/projects/{_SLUG}/memory/notes" in script
    piped = [c.args[0] for c in transport.pipe.call_args_list]
    assert any("/memory/notes/topic.md" in cmd for cmd in piped)


def test_project_memory_without_host_memory_dir_still_seeds_and_copies_claude_md(
    tmp_path: Path,
) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("global")
    # No projects/<slug>/memory dir on the host.

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    # The guest memory (slug) dir is still created — no error — and the global
    # CLAUDE.md is projected regardless of per-repo memory. mkdir is the first run.
    script = transport.run.call_args_list[0].args[-1]
    assert f"mkdir -p ~/.claude/projects/{_SLUG}/memory" in script
    piped = [c.args[0] for c in transport.pipe.call_args_list]
    assert any(cmd.endswith("/CLAUDE.md") for cmd in piped)
    # No memory files were copied.
    assert not any("/memory/" in cmd for cmd in piped)


def test_project_memory_without_global_claude_md(tmp_path: Path) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    _seed_memory(claude)
    # No global CLAUDE.md on the host.

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    piped = [c.args[0] for c in transport.pipe.call_args_list]
    # Memory is copied; CLAUDE.md is not (it does not exist on the host).
    assert any("/memory/MEMORY.md" in cmd for cmd in piped)
    assert not any(cmd.endswith("/CLAUDE.md") for cmd in piped)


def test_lock_projection_locks_memory_not_transcripts() -> None:
    transport = MagicMock()
    vm_memory.lock_projection(
        transport,
        claude_dir_guest="$HOME/.claude",
        slug=_SLUG,
        locked_set=["$HOME/.claude/CLAUDE.md"],
    )

    assert transport.run.call_args.args[0] == "bash"
    script = transport.run.call_args.args[-1]
    # The memory subtree is locked recursively; MEMORY.md and CLAUDE.md are locked.
    assert f'chmod -R a-w "$HOME/.claude/projects/{_SLUG}/memory"' in script
    assert f'chmod a-w "$HOME/.claude/projects/{_SLUG}/memory/MEMORY.md"' in script
    assert 'chmod a-w "$HOME/.claude/CLAUDE.md"' in script
    # SURGICAL: every reference to the slug dir is under /memory/ — the slug dir
    # itself is never chmod'd, so the session-transcript .jsonl files it holds
    # stay writable. (A blanket recursive chmod would break session logging.)
    slug_lines = [ln for ln in script.splitlines() if f"projects/{_SLUG}" in ln]
    assert slug_lines
    assert all("/memory" in ln for ln in slug_lines)
    assert f'chmod -R a-w "$HOME/.claude/projects/{_SLUG}"' not in script
    assert f'chmod a-w "$HOME/.claude/projects/{_SLUG}"' not in script


def test_lock_projection_guards_each_path_with_existence_test() -> None:
    transport = MagicMock()
    vm_memory.lock_projection(
        transport,
        claude_dir_guest="$HOME/.claude",
        slug=_SLUG,
        locked_set=["$HOME/.claude/CLAUDE.md"],
    )

    script = transport.run.call_args.args[-1]
    # Each chmod is guarded by an ``[ -e ]`` test so a missing optional file is a
    # no-op, not an error — and the guarded form always exits 0.
    assert f'[ -e "$HOME/.claude/projects/{_SLUG}/memory" ]' in script
    assert f'[ -e "$HOME/.claude/projects/{_SLUG}/memory/MEMORY.md" ]' in script
    assert '[ -e "$HOME/.claude/CLAUDE.md" ]' in script


def test_lock_projection_locks_each_extra_in_locked_set() -> None:
    transport = MagicMock()
    vm_memory.lock_projection(
        transport,
        claude_dir_guest="$HOME/.claude",
        slug=_SLUG,
        locked_set=["$HOME/.claude/CLAUDE.md", "$HOME/.claude/extra.md"],
    )

    script = transport.run.call_args.args[-1]
    # Every path in the audited locked_set is chmod'd read-only, guarded.
    assert 'chmod a-w "$HOME/.claude/CLAUDE.md"' in script
    assert 'chmod a-w "$HOME/.claude/extra.md"' in script


def test_verify_projection_raises_when_host_path_missing() -> None:
    # (Component 6) A broken Component-2a host-path indirection would otherwise
    # degrade *silently* to empty memory. verify_projection turns the first failed
    # in-guest ``test`` (a CalledProcessError over the transport) into a loud
    # ProjectionError carrying an actionable message.
    transport = MagicMock()
    transport.run.side_effect = subprocess.CalledProcessError(1, "test")

    with pytest.raises(vm_memory.ProjectionError) as excinfo:
        vm_memory.verify_projection(transport, host_workdir=_WORKDIR, slug=_SLUG)

    message = str(excinfo.value)
    # The message names the failed check (the host path) and the fix (re-run/rebuild).
    assert _WORKDIR in message
    assert "re-run" in message.lower()
    assert "rebuild" in message.lower()


def test_verify_projection_raises_when_memory_dir_missing() -> None:
    # The host path resolves (first ``test`` succeeds) but the projected memory dir
    # is absent (second ``test`` fails) — still a loud ProjectionError naming the
    # memory dir and the fix, not a silent empty read.
    transport = MagicMock()
    transport.run.side_effect = [
        subprocess.CompletedProcess(args=["bash"], returncode=0),
        subprocess.CalledProcessError(1, "test"),
    ]

    with pytest.raises(vm_memory.ProjectionError) as excinfo:
        vm_memory.verify_projection(transport, host_workdir=_WORKDIR, slug=_SLUG)

    message = str(excinfo.value)
    assert f"projects/{_SLUG}/memory" in message
    assert "re-run" in message.lower()


def test_verify_projection_passes_when_both_checks_succeed() -> None:
    # Both read-only ``test -d`` checks pass: the host path resolves and the slug
    # memory dir exists, so verify returns without raising (and never mutates —
    # safe against the read-only lock).
    transport = MagicMock()
    transport.run.return_value = subprocess.CompletedProcess(args=["bash"], returncode=0)

    vm_memory.verify_projection(transport, host_workdir=_WORKDIR, slug=_SLUG)

    # Exactly the two read-only ``test -d`` checks ran — the host path and the slug
    # memory dir — and nothing else (no write, no chmod).
    scripts = [c.args[-1] for c in transport.run.call_args_list]
    assert any(f"test -d {_WORKDIR}" in s for s in scripts)
    assert any(f"test -d ~/.claude/projects/{_SLUG}/memory" in s for s in scripts)
    assert all("test -d " in s for s in scripts)
