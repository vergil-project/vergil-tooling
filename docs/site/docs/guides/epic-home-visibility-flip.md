# Epic home visibility flips

Epic homes are derived from repository visibility (see the
[epic/task convention][convention] and `epics.resolve_epic_home`):

| `.github` | member repo | epic home |
| --- | --- | --- |
| public | public | `<org>/.github` |
| public | **private** | **the member repo itself** |
| private | (any) | `<org>/.github` |

So a repo's epic home is stable only while its visibility is stable.
When you **flip a repo between public and private** (with a public
`.github`), its resolved home changes, and its existing epics must be
relocated to match. This is a rare, corner-case operation; this guide is
the runbook for when it happens.

## What moves, and what does not

- **Epics move.** A public→private flip moves the repo's epics from
  `<org>/.github` into the repo itself; a private→public flip moves them
  the other way.
- **Tasks do not move.** A task always lives in its member repo (1:1 with
  its PR), independent of visibility. Only the epic issue relocates, and
  each task's parent link is re-pointed at the epic's new location.

There is **no dedicated relocation tool** — the procedure composes the
existing commands. (A convenience `--to-home` wrapper is deliberately
deferred unless the manual procedure proves painful in practice.)

## Procedure

Do the following once per epic that the flipped repo owns. `vrg-epic-move`
re-parents a *task* under a different epic; it does not transfer an epic
between repos, so the relocation is re-create + re-parent + close.

1. **Confirm the new home.** Run `vrg-epic-create --repo <owner>/<repo>`
   (or check the table above) — it echoes the resolved home and its
   visibility, e.g. `-> epic home: <owner>/<repo> [PRIVATE]`.

2. **Re-create the epic in the new home.** Copy the old epic's title,
   body, and labels:

   ```bash
   vrg-epic-create --repo <owner>/<repo> \
     --title "Epic: <same title>" --body-file <copied-body.md>
   ```

   Note the new epic's number `<NEW>`.

3. **Re-parent each child task** from the old epic to the new one:

   ```bash
   vrg-epic-move --task <owner>/<repo>#<TASK> --epic <new-home>#<NEW>
   ```

   List the old epic's children first with the audit/roadmap tooling if
   you need the full set.

4. **Close the old epic** with a comment pointing at the replacement, so
   the drift audit does not re-surface it.

## Cross-visibility caveat

After a **public→private** flip, the epic is now private. Any child task
that lives in a repo that is **still public** can no longer hard-link
under it — `vrg-epic-link` refuses a public task under a private epic
(it would leak the private repo's name into a public issue and break
cross-boundary roll-up). Re-express those dependencies as a soft
`Blocked-by:` reference from the private epic's body instead:

```text
Blocked-by: <owner>/<public-repo>#<TASK>
```

See the [epic/task convention][convention] for the asymmetric linkage
rule (a task may hard-link to an epic only if it is no more publicly
visible than the epic's home).

[convention]: https://github.com/vergil-project/.github/issues/40
