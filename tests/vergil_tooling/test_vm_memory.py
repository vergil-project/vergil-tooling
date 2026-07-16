"""Tests for host->guest memory projection (vm_memory)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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


def test_project_memory_mkdirs_guest_memory_dir(tmp_path: Path) -> None:
    transport = MagicMock()
    claude = tmp_path / ".claude"
    _seed_memory(claude)

    vm_memory.project_memory(transport, claude_dir=claude, host_workdir=_WORKDIR)

    # The guest memory dir is created up front over the ``bash -c`` idiom so ``~``
    # expands in the guest shell (a bare ``mkdir`` arg would not expand it).
    script = transport.run.call_args.args[-1]
    assert transport.run.call_args.args[0] == "bash"
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
    # is projected at the matching relative path.
    script = transport.run.call_args.args[-1]
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
    # CLAUDE.md is projected regardless of per-repo memory.
    script = transport.run.call_args.args[-1]
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
