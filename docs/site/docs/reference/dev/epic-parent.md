# vrg-epic-parent

**Installed as:** `vrg-epic-parent` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_epic_parent.py` (library:
`epics.task_epic` in `src/vergil_tooling/lib/epics.py`)

Prints a task's epic and the epic's kind. An epic past GitHub's limit of 100
sub-issues grows as a [chain](../../standards/github-issues.md#chained-epics),
and a task linked in one of its continuations has the continuation as its
native parent, not the epic. This command reads through the chain, so the
epic it names is always the chain's head and the kind is the head's. It is
the read the plugin skills use in place of a task's native parent and that
parent's labels. It is read-only: it never writes to GitHub, and an
orphaned continuation is reported, never adopted.

## Usage

```text
vrg-epic-parent <owner>/<repo>#<N> [--json]
```

| Argument | Meaning |
|---|---|
| `<owner>/<repo>#<N>` | The task; a bare `#<N>` is issue N in the current repo |
| `--json` | Print a JSON object instead of one line |

## Output

The plain output is one line, `<ref> <kind>`:

| Kind | The task's native parent | `<ref>` |
|---|---|---|
| `development` | An `epic`-labelled head that is none of the other kinds | The head |
| `proxy` | A proxy epic (`epic` + `proxy`), or a continuation of one | The head |
| `ad-hoc`, `intake`, `planning`, `deferred`, `hoshin`, `operations` | That standing epic, or a continuation of it | The head |
| `archive` | An archive (the `archive` label) | The archive |
| `not-epic` | An issue without the `epic` label, such as a sub-task's parent (one still labelled `continuation` is chain drift) | The parent |

A task with no native parent prints `none` alone. A body `Parent:` line is
never read: a task whose only link is that line is not attached on GitHub,
so it prints `none`.

With `--json` the output is one object:

```json
{"task": "org/repo#42", "epic": "org/.github#40", "kind": "intake", "container": "org/.github#50"}
```

| Key | Value |
|---|---|
| `task` | The task's ref |
| `epic` | The chain's head; the archive for `archive`; `null` for `not-epic` and `none` |
| `kind` | The kind from the table above, or `none` |
| `container` | The native parent: the head, the continuation that holds the task, the archive or the plain parent; `null` for `none` |

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | The answer was printed, `none` and `not-epic` included |
| 1 | A bad ref, a failed read, chain drift (the error names the container and the rule it broke), or a head carrying more than one kind marker |
