# Issue-creation shapes — restoring epic and triage creation

- **Issue:** vergil-project/vergil-tooling#2069
- **Regression from:** #2017
- **Status:** Approved design (2026-07-01)

## 1. Problem

#2017 denied raw `vrg-gh issue create` for every identity and routed all
creation through `vrg-issue-create`, which **requires** `--epic`. That assumed
every issue is a task born linked to an epic. Two legitimate shapes then had no
sanctioned path:

- **Epics** — top-level issues in `<owner>/.github`, labelled `epic`, with no
  parent (used by the `epic-create` and `migrate-repo` skills).
- **Triage** — standalone issues labelled `triage`, deliberately **unlinked**
  and routed to an epic later during triage-review (used by `triage-capture`).

## 2. Decision

Keep #2017's guarantee — **no arbitrary path around linkage** — but recognise
that there are **three** creation shapes, each with its own single-purpose,
self-enforcing tool. (Option A from the issue; Option B — relaxing the `vrg-gh`
denial to sniff `--label` — was rejected because it reopens the bypass and
relies on fragile argv parsing.)

| Tool | Shape | Repo | Forced label | Linked |
|---|---|---|---|---|
| `vrg-issue-create` (exists) | task | current repo | — | under `--epic` |
| `vrg-epic-create` (new) | epic | `<org>/.github` | `epic` | no |
| `vrg-triage-create` (new) | triage | current repo (`--repo` override) | `triage` | no |

Each tool forces its shape's label, so there is no generic unlinked-create
escape hatch.

## 3. Components

### 3.1 `vrg-epic-create`

Creates a top-level epic. Targets `<org>/.github`, where `<org>` comes from
`github.detect_org()` (the current repo's remote); **errors clearly** if the org
cannot be determined, matching `vrg-epic-audit`. Labels = `["epic", *--label]`
(extra labels such as `standing` allowed). No `--epic` (it is the epic) and no
`--repo` (epics always live in the org's `.github`).

Args: `--title` (required), `--body`, `--body-file`, `--label` (repeatable),
`--assignee` (repeatable). Creates via `github.create_issue(...)`, prints the
URL. No linking.

### 3.2 `vrg-triage-create`

Creates an unlinked triage issue in the current repo (or `--repo`, since triage
capture files in the most-relevant repo, or the org `.github` for project-level
seeds). Labels = `["triage", *--label]`.

Args: `--title` (required), `--body`, `--body-file`, `--label` (repeatable),
`--assignee` (repeatable), `--repo` (default current repo). Creates via
`github.create_issue(...)`, prints the URL. No linking.

### 3.3 Wiring

- `pyproject.toml`: register `vrg-epic-create` and `vrg-triage-create` console
  scripts.
- `vrg-gh` `issue create` denial message updated to name all three sanctioned
  tools (task / epic / triage) instead of only `vrg-issue-create`.
- Both new tools use `argparse`, so they answer `--help` and are automatically
  verified by the `test_help_coverage` gate (epic #72); `KNOWN_GAPS` stays
  empty, no gate edit needed.

## 4. Testing

- `test_vrg_epic_create.py`: targets `<org>/.github` with the `epic` label;
  extra labels are added; undetectable org errors clearly.
- `test_vrg_triage_create.py`: creates unlinked with the `triage` label in the
  current repo; `--repo` override; extra labels.

Mocks `github.detect_org` / `github.current_repo` / `github.create_issue`.

## 5. Out of scope

Updating the plugin skills (`epic-create`, `migrate-repo`, `triage-capture`) to
call the new tools — that is the cross-referenced `vergil-claude-plugin`
companion issue, a separate repo.
