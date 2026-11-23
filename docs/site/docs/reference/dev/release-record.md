# vrg-release-record

**Installed as:** `vrg-release-record` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_release_record.py` (library:
`src/vergil_tooling/lib/release/records.py`)

Reads the `Released:` record that `vrg-release` posts on every issue a
release contains (see
[`Released:` records](../../guides/releasing.md#released-records)), and
confirms it against git. It is the one reader of the records: a deployment
task that depends on a blocked-by development task having been released
confirms that release with it. Every other precondition is checked as
before.

Any identity may run it. It only reads: GitHub, through the tool's own
access, and the task's repo, through a temporary bare fetch.

## Usage

```text
vrg-release-record check OWNER/REPO#N
```

| Argument | Meaning |
|---|---|
| `OWNER/REPO#N` | The task issue whose record is checked. `OWNER/REPO` may be typed in any case: it is matched case-insensitively everywhere in the check, as GitHub matches it, and the output spells it as GitHub does |

## What it does

1. Reads every comment on the task, page by page, through the trust
   layer.
2. Finds the records of the task's repo. A record counts only when its
   comment's author and, if it was edited, its last editor hold the
   `human` role: a `human` login of `vrg-trusted-authors` whose account
   is a GitHub user. Any other comment, an agent's included, is never
   read for a record: it is reported on stderr by login, time and
   comment only (counts by cause under GitHub Actions), and is not in
   `warnings`. A record marker of another repo is ignored with a
   warning.
3. Uses the newest counting record, by version (`v2.1.10` is newer than
   `v2.1.9`). Each other counting record is named in a warning. With no
   counting record, the verdict is `none`.
4. Reads the task's closing PRs from GitHub. A merged PR counts only when
   its closing reference to the task passes the release's trust rule, the
   one that builds a release's issue list (see
   [the issue list](../../guides/releasing.md#the-issue-list)); each other merged closing
   PR is named in a warning. Their merge commits are read from GitHub,
   never from the record.
5. Reads the record's tag with `git ls-remote`, fetches it and the merge
   commits into a temporary bare repository (commits only, no trees or
   blobs), and checks with `git merge-base --is-ancestor` that every
   merge commit is an ancestor of the tag.

The verdict is `pass` only when every merge commit is an ancestor of the
tag. A record the ancestry check does not confirm is a `fail`, never a
pass: a merge commit the tag does not contain, a tag the repo does not have,
or no counting closing PR to check at all.

## Output

One JSON object on stdout:

```json
{
  "task": "vergil-project/vergil-tooling#4840",
  "verdict": "pass",
  "record": {
    "version": "2.1.340",
    "tag": "v2.1.340",
    "url": "https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340"
  },
  "ancestry": "pass",
  "warnings": []
}
```

| Field | Meaning |
|---|---|
| `task` | The task checked, its `OWNER/REPO` spelled as GitHub spells it, whatever case it was typed in |
| `verdict` | `pass`, `fail` or `none` |
| `record` | The record used: its version, its tag and the tag's GitHub Release URL; `null` for `none` |
| `ancestry` | `pass` or `fail`; `null` for `none` |
| `warnings` | Every record marker of another repo, every other counting record, every closing PR that does not count, and why the ancestry check failed |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `pass`: the record counts and git confirms it |
| 1 | An error: a GitHub or git read failed or was incomplete, or the trusted-authors configuration could not be loaded. The error is on stderr and nothing is printed on stdout |
| 2 | A usage error |
| 3 | `fail`: the record counts but the ancestry check does not confirm it |
| 4 | `none`: the task carries no counting record (a release from before records, or a repo that does not release with `vrg-release`) |

A failed read is never taken as "no record" or "no closing PR": it is
exit 1.
