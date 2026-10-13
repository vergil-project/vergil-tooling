# Python CLI Entry Points

## Purpose

Define how a Python console script is laid out so that every command is easy to
find from its name, easy to test without a subprocess, and consistent across the
tool suite.

## Scope

Applies to every console script declared in `[project.scripts]` in
`pyproject.toml`. In vergil-tooling these are the `vrg-*` commands, whose
modules live in `src/vergil_tooling/bin/`.

## Source file name matches the command name

The module for a command is the command name with every `-` replaced by `_`
(the only change Python module naming requires). The entry point is always the
module's `main` function.

| Command | Module | Entry point |
|---|---|---|
| `vrg-commit` | `bin/vrg_commit.py` | `vergil_tooling.bin.vrg_commit:main` |
| `vrg-submit-pr` | `bin/vrg_submit_pr.py` | `vergil_tooling.bin.vrg_submit_pr:main` |
| `vrg-vm-resolve-session` | `bin/vrg_vm_resolve_session.py` | `vergil_tooling.bin.vrg_vm_resolve_session:main` |

```toml
[project.scripts]
vrg-commit = "vergil_tooling.bin.vrg_commit:main"
```

Rules:

- No shortened, suffixed, or otherwise renamed modules (for example, no
  `vrg_vm_resolve.py` for `vrg-vm-resolve-session`, and no `_cli` suffix).
- A command's own test module follows the same name: `vrg_foo.py` is tested
  by `tests/vergil_tooling/test_vrg_foo.py`.
- Modules in `bin/` that are not themselves a console script (shared helpers
  such as `validate_common.py`) do not carry the command prefix.

`tests/vergil_tooling/test_cli_entry_points.py` enforces the mapping for every
`[project.scripts]` entry.

## The `main` function

### Signature

```python
def main(argv: list[str] | None = None) -> int:
```

- `argv` defaults to `None`, which `argparse` treats as `sys.argv[1:]`. Pass it
  straight through: `parser.parse_args(argv)`.
- The return value is the process exit code (`0` for success).

The `argv` parameter is what makes a command testable in-process: a test calls
`main(["--flag", "value"])` and asserts on the returned exit code and captured
output, with no subprocess and no patching of `sys.argv`.

```python
def test_rejects_unknown_issue(capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_foo.main(["--issue", "nope"]) == 2
```

A program that is never invoked with arguments by a person (for example, the
`vrg-hook-guard` PreToolUse hook, which reads a JSON event from stdin) may
declare `def main() -> int:` instead.

### Position

`main` is the last function defined in the module. Helpers (argument parser
construction, validation, the individual steps) are defined above it, and
`main` composes them. The `if __name__` guard follows `main` at the end of the
file. Reading a command module therefore means reading its building blocks
first and finding the orchestration that ties them together, plus the guard, at
the bottom.

### The `if __name__` guard

End the module with:

```python
if __name__ == "__main__":
    sys.exit(main())
```

`sys.exit(main())` propagates the returned code to the shell when the module is
run directly with `python -m`. Installed console scripts call `main` through the
generated wrapper, which applies `sys.exit` to the return value the same way.

### Coverage exclusion

The guard only runs under `python -m`, never under pytest, so it is excluded
from coverage centrally rather than with per-line pragmas:

```toml
[tool.coverage.report]
exclude_lines = [
    "if __name__ == .__main__.",
]
```

Do not add `# pragma: no cover` to the guard; the `exclude_lines` entry already
covers it, and everything above the guard, including `main` itself, is held to
the 100% coverage gate.
