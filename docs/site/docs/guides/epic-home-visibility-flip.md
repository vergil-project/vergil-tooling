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
  the other way. The epic issue is **transferred** (not re-created), so
  its history and comments follow it. It gets a new number in the
  destination repo.
- **Tasks do not move.** A task always lives in its member repo (1:1 with
  its PR), independent of visibility. Only the epic issue relocates.

There is **no dedicated relocation wrapper** — the procedure composes
`vrg-gh issue transfer` (an intra-org issue move) with `vrg-epic-link`.

## Procedure

Do the following once per epic that the flipped repo owns.

1. **Confirm the new home.** Run `vrg-epic-create --repo <org>/<repo>`
   (or check the table above) — it echoes the resolved home and its
   visibility, e.g. `-> epic home: <org>/<repo> [PRIVATE]`.

2. **Transfer the epic into its new home.** An intra-org `issue transfer`
   moves the epic between repos under the same org, carrying its history
   with it:

   ```bash
   vrg-gh issue transfer <n> <org>/.github -R <org>/<src-repo>
   ```

   **Always pin the source with `-R`.** The `-R`/`--repo` owner selects
   the GitHub App installation and names the source repo unambiguously;
   `<n>` is the epic's number *in that source repo* and the trailing
   `<org>/.github` is the destination. (For a public→private flip the
   destination is the member repo instead.) The transfer is intra-org —
   one installation token cannot reach two owners. GitHub assigns the
   epic a new number in the destination.

3. **Relink reflink-only children.** Children linked as **native
   sub-issues** ride the transfer — their parent link is preserved
   automatically. Children linked only by a portable `Parent:` body
   reflink still point at the epic's old location, so re-point each with:

   ```bash
   vrg-epic-link --epic <org>/.github#<NEW> --task <org>/<repo>#<TASK>
   ```

4. **Verify.** Run `vrg-epic-audit` and confirm no invariant violation
   remains — no epic outside `.github` for a public repo, no stray
   `.github` issue, no closed epic with an open child.

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
