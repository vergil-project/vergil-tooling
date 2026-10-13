# Shell Error Handling

## Purpose

Make shell failures loud. A failure that a script quietly absorbs turns
into wrong output further down the line, and when an AI agent consumes
that output the error becomes a hallucination. Every rule on this page
serves one principle: **no silent failures**.

The `set -euo pipefail` safety header required by the
[overview](overview.md#tooling-expectations) is the baseline. This page
covers the cases where that header is defeated and the rules for doing
it deliberately.

## Tolerating Failure: `|| true` and `|| :`

`|| true`, `|| :`, and equivalents (`|| exit 0`, `|| echo`, `; true`) are
**prohibited as a blanket "make this line pass."** They throw away a real
non-zero exit status, including failures you did not anticipate.

Tolerating a *specific, expected* failure is allowed only when all three
of these hold:

1. **Narrow scope.** Guard exactly one command whose failure you expect,
   not a pipeline, a function call, or a block.
2. **Explicit check.** If the command produces a result the script relies
   on, check that result explicitly afterwards.
3. **Comment.** State why the failure is acceptable, at the line where it
   happens.

```bash
# Correct: expected failure, narrow scope, checked, commented
# grep exits 1 when nothing matches; zero matches is a valid outcome here.
match_count="$(grep -c '^ERROR' "$log_file" || true)"
if [[ -z "$match_count" ]]; then
  echo "error: could not count errors in ${log_file}" >&2
  exit 1
fi

# Correct: best-effort cleanup, commented
# The VM may already be stopped; any other problem surfaces at delete.
limactl stop "$vm_name" || true
limactl delete "$vm_name"

# Wrong: mutes every failure, including a missing file or a bad pattern
grep -c '^ERROR' "$log_file" || true
deploy_release || true
```

`grep` reports *no match* (exit 1) and *error* (exit 2) differently. When
that distinction matters, capture the status instead of muting it:

```bash
status=0
grep -q "$pattern" "$file" || status=$?
case "$status" in
  0) echo "found" ;;
  1) echo "not found" ;;
  *) echo "error: grep failed on ${file}" >&2; exit "$status" ;;
esac
```

Prefer `if command; then` over `command || true` whenever the script
needs to branch on the outcome.

## How `set -euo pipefail` Gets Defeated

The safety header does not cover every case. Each of the following lets
a failure pass silently.

### Assignment combined with `local`, `export`, `readonly`, or `declare`

The exit status of the command substitution is replaced by the status of
the builtin, which is almost always 0. ShellCheck reports this as SC2155.

```bash
# Wrong: fetch_token failure is ignored
local token="$(fetch_token)"

# Correct: declare first, then assign so set -e sees the failure
local token
token="$(fetch_token)"
```

### Command substitution inside another command

`set -e` does not check a substitution embedded in a larger command; the
outer command's status wins.

```bash
# Wrong: if get_version fails, this prints "version: " and carries on
echo "version: $(get_version)"

# Correct: capture first, then use
version="$(get_version)"
echo "version: ${version}"
```

### Conditional contexts

`set -e` is suspended for every command run inside an `if`/`while`
condition or to the left of `&&` or `||`, **including everything inside a
function called there**.

```bash
build_and_test() {
  make build   # a failure here does NOT stop the function...
  make test    # ...because the caller is in a conditional context
}

# Wrong: build failures are masked
if build_and_test; then echo "ok"; fi

# Correct: check each step explicitly inside the function
build_and_test() {
  make build || return 1
  make test || return 1
}
```

### Early-closing readers and SIGPIPE

With `pipefail`, a writer that is killed by SIGPIPE because the reader
exited early (`head`, `grep -q`, `grep -m 1`) makes the whole pipeline fail
with status 141. The fix is not `|| true`: bound the writer's output, or
write to a file first.

```bash
# Fails intermittently under pipefail: generate_report may get SIGPIPE
first_line="$(generate_report | head -n 1)"

# Correct: write to a temporary file, then read it
report_file="$(mktemp)"
trap 'rm -f "$report_file"' EXIT
generate_report > "$report_file"
first_line="$(head -n 1 "$report_file")"
```

If a script deliberately relies on SIGPIPE (for example
`printf ... | head -c`), comment the pipeline and why it is safe.

### Process substitution

`pipefail` does not cover `<(...)`. A failure inside process substitution
is never reported to the script.

```bash
# Wrong: list_files failure is invisible
while read -r file; do process "$file"; done < <(list_files)

# Correct: write to a file first so set -e sees the failure
files_list="$(mktemp)"
trap 'rm -f "$files_list"' EXIT
list_files > "$files_list"
while read -r file; do process "$file"; done < "$files_list"
```

## Omitting the Safety Header

A script that intentionally omits any part of `set -euo pipefail` must
say so in a comment at the top of the script and again at the point
where the omission matters. An unexplained omission is a defect.

## GitHub Actions `run:` Steps

A `run:` step's error behavior depends on how its shell is selected, not
on the script text:

| Step configuration | Command GitHub runs | `pipefail` |
| --- | --- | --- |
| No `shell:` (Linux/macOS default) | `bash -e {0}` | **No** |
| `shell: bash` | `bash --noprofile --norc -eo pipefail {0}` | Yes |
| `shell: sh` | `sh -e {0}` | **No** |

Rules:

- Set `shell: bash` explicitly, either on each step or once through
  `defaults.run.shell: bash` at the workflow or job level. Do not rely on
  the implicit default; it does not enable `pipefail`.
- `-u` is never set by Actions. Add `set -u` in the step if it relies on
  variables being defined.
- A script invoked from a `run:` step (`run: ./scripts/build.sh`) runs
  under its own shebang and header, not the step's flags. It must carry
  its own `set -euo pipefail`.
- `sh` steps have no `pipefail`. Avoid pipelines in them, or use bash.
