# Epic home visibility flips

Epic homes are derived from repository visibility (see the
[epic/task convention][convention] and `epics.resolve_epic_home`):

| `.github` | member repo | epic home |
| --- | --- | --- |
| public | public | `<org>/.github` |
| public | **private** | **the member repo itself** |
| private | (any) | `<org>/.github` |

So a repo's epic home is stable only while its visibility is stable.
Visibility changes are supported in **one direction only**:

- **Private → public is supported.** Making a repo public discloses nothing
  new. Its resolved home moves to `<org>/.github`, and its existing epics
  must be relocated to match. This is a rare, corner-case operation; this
  guide is the runbook for when it happens.
- **Public → private is not supported.** See
  [Public to private is unsupported](#public-to-private-is-unsupported).

## What moves, and what does not

- **Epics move.** A private→public flip moves the repo's self-homed epics
  from the repo into `<org>/.github`. The epic issue is **transferred** (not
  re-created), so its history and comments follow it. It gets a new number
  in the destination repo.
- **Tasks do not move.** A task always lives in its member repo (1:1 with
  its PR), independent of visibility. Only the epic issue relocates.
- **Intake and planning queues move themselves.** A private repo self-homes
  its own `Epic (intake): <repo>` and `Epic (planning): <repo>`. Once the
  repo is public, the daily `vrg-epic-audit --close` sweep re-links every
  open item under the org's `.github` intake and planning epics (the items
  stay in their repo) and closes the emptied self-homed queue epics. No
  manual step is needed; a read-only `vrg-epic-audit` lists the queue epics
  it will move.

There is **no dedicated relocation wrapper** for epics — the procedure composes
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
   `<org>/.github` is the destination. The transfer is intra-org —
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

## Public to private is unsupported

Once a repo has been public, its contents — and anything about it in the
public `.github`, including epic bodies, specs, and plans — may already
have been cloned and redistributed. Making it private cannot undo that, and
relocating its epics would only give false assurance. So decide at repo
creation whether something is private; if it is public, it stays public.

**To take public work private, create a new repository that starts life
private and migrate the code into it.** The new repo self-homes its epics
and its intake and planning queues from day one; the public original is
left as it was (archived if appropriate).

If a repo is flipped to private anyway, `vrg-epic-audit` reports every
issue in a private repo that is parented under a public `.github` epic as a
likely unsupported flip, for a human to resolve. Tooling never migrates or
retracts anything in that direction.

## Cross-visibility linkage

A task may hard-link to an epic only if it is no more publicly visible than
the epic's home, so `vrg-epic-link` refuses a public task under a private
epic (it would leak the private repo's name into a public issue and break
cross-boundary roll-up). Express such a dependency as a soft `Blocked-by:`
reference from the private epic's body instead:

```text
Blocked-by: <owner>/<public-repo>#<TASK>
```

See the [epic/task convention][convention] for the asymmetric linkage rule.

[convention]: https://github.com/vergil-project/.github/issues/40
