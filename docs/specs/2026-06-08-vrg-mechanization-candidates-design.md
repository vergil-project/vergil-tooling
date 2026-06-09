# vrg-* Mechanization Candidates — transcript analysis (2026-06-08)

> Tracking issue: [#1543](https://github.com/vergil-project/vergil-tooling/issues/1543)
>
> **Evidence convention.** Agent narration and executed Bash commands are
> quoted verbatim (they are the precise technical proof). User-message
> evidence that was clearly voice-captured is paraphrased and marked
> `(user, paraphrased)` rather than reproduced verbatim, per the project's
> voice-to-text policy; short, cleanly-written user messages are quoted as
> typed. Session ids are preserved throughout so any claim can be traced
> back to the source transcript.

This spec ranks procedures worth mechanizing into new (or hardened)
`vrg-*` scripts, distilled from a corpus of 108 `vergil-tooling` Claude
Code sessions over the two weeks ending 2026-06-08. Fourteen miner agents
produced raw candidates from distilled transcripts; this document clusters
near-duplicates into canonical candidates, unions their evidence, drops
anything already covered by an existing `vrg-*` script, and ranks the
survivors weighting user-correction signal highest, then repetition across
distinct sessions, then variance. Every ranked candidate traces to
transcript evidence; nothing here is inferred without a source.

## Ranked candidates

### 1. Post-merge finalize fails from a worktree CWD → `vrg-finalize-pr`

**Signal.** CORRECTION (high) + error_retry + repetition. The strongest
individual signal in the corpus: a user directed the agent to perform
finalize itself rather than handing it back, and a separate user reported
that skipping or mis-running finalize was leaving every subsequent PR
out-of-date with its base branch. The wrong-CWD-then-`cd`-retry is
near-universal across sessions.

**Frequency.** ≥14 distinct sessions: 4763c960, 0952d5b1, 54901c7b,
ca70e14b, 256863dc, f51a8da8, 90787dd1, 83882e80, 253c73f3, 01b1a76e,
e03ef568, cfa85963, 495aefab, 5d356d5e, 38c311e4, 512b67db, e6ed1548,
24541e56, 6918371a, b7368477, 9f0ed5f5, dbe68ca9, 334653e8, 3eaf06a8,
c0384a01.

**What agents do today.** After a PR merges, the agent runs
`vrg-finalize-repo` / `vrg-finalize-pr` from the feature-worktree CWD that
persisted during implementation. It errors (finalize removes the secondary
worktree and cannot stand inside it), and the agent re-runs prefixed with
`cd <project-root> &&`. When agents instead substitute a manual
`worktree remove` + `branch -d`, they skip the develop fast-forward pull
and every later worktree branches from a stale base.

**Proposed change.** Harden the existing `vrg-finalize-pr` (and retire the
`vrg-finalize-repo` alias):

```text
vrg-finalize-pr [--issue N] [--pr URL]
  # Detect invocation from inside a .worktrees/<name>/ checkout.
  # Resolve the main worktree root via `git rev-parse --git-common-dir` /
  #   `git worktree list` and re-exec there, instead of erroring.
  # Always fetch + ff-pull the base branch so develop never drifts behind origin.
```

**Replaces.** The reflexive `cd <main-worktree-root> && vrg-finalize-…`
retry on essentially every merge, and the incomplete manual
`worktree remove` + `branch -d` shortcut that silently skips the develop
fast-forward.

**Evidence.**

```text
253c73f3  (user, paraphrased) directed the agent to run finalize itself
          rather than handing the step back, noting this is one of the rare
          cases where the agent stops short.
dbe68ca9  (user, paraphrased) reported that despite running finalize and
          updating develop locally, every new PR came up out-of-date with
          the base branch; asked to find the root cause.
0952d5b1  [A] Finalization must run from the main worktree. Running it there.
          $ cd /Users/.../vergil-tooling && vrg-finalize-repo
512b67db  (user, paraphrased) noted that in 2.1 the legacy vrg-finalize-repo
          script still exists alongside the new vrg-finalize-pr, and that
          keeping the old one around invites running it by accident.
```

**Overlap.** Extends existing `vrg-finalize-pr` (CWD self-location +
base-branch ff-pull). The legacy `vrg-finalize-repo` name should be removed
so it cannot be run by accident.

---

### 2. Hand-rolled start-work-on-an-issue worktree setup → `vrg-start-issue`

**Signal.** Repetition (high) + variance + error_retry, with two embedded
corrections. The single most-repeated procedure in the corpus. Corrections
fire when the agent writes a spec to the read-only main worktree before
setting up a worktree, and when `vrg-commit` rejects a non-issue-linked
branch and forces an after-the-fact issue+rename scramble.

**Frequency.** ≥20 distinct sessions: 3da5f489, 01ba8899, 256863dc,
39fa9eec, 7864b764, 14eae377, 253c73f3, 83882e80, b012d6a9, 6051a5f2,
c81b117d, bddab4d4, f4248acc, bd22e848, 7ddb16e0, e00fccc2, 90c234f5,
412fa690, c3242937, 27714027, de99f544, e945d74e, 38c311e4, 512b67db,
f153873d, fd119ea4, 24541e56, e6ed1548, 6918371a, 52fbf467, dbe68ca9,
0c02b4ed, 3eaf06a8, 7a4090f5, 4228275c, cfa85963.

**What agents do today.** On `implement <issue>`, the agent hunts for the
(often missing) `starting-work-on-an-issue` doc, invents a kebab slug and a
`feature/<N>-<slug>` branch name by hand, decides inconsistently between
base `develop` and `origin/develop`, may or may not `vrg-git fetch` first,
and frequently gets the `git worktree add` argument order wrong (positional
branch instead of `-b`) on the first attempt before retrying. When design
work precedes setup, the file is written to the read-only main worktree and
blocked, forcing a retroactive worktree+issue scramble.

**Proposed script.**

```text
vrg-start-issue <issue-number-or-url> [--slug <2-4 kebab tokens>] [--base develop]
  # Resolve the repo-local issue, derive slug from the issue title (overridable).
  # Fetch the base, detect an existing worktree / remote branch and refuse to clobber.
  # Run the canonical: git worktree add .worktrees/issue-<N>-<slug>
  #   -b feature/<N>-<slug> origin/<base>
  # Print the absolute worktree path + branch (cd-ready).
```

**Replaces.** Doc-hunting, slug/branch/path construction by hand, the
fetch-or-not decision, the develop-vs-origin/develop base inconsistency,
the wrong-arg-order first attempt and retry, and (because the branch is
born issue-linked) the downstream `vrg-commit` branch-name rejection +
`git branch -m` backfill.

**Evidence.**

```text
3da5f489  [A] The skill's referenced doc doesn't exist here
          $ vrg-git fetch origin develop && vrg-git worktree add
            .worktrees/issue-1477-consumer-refresh-visible
            -b feature/1477-consumer-refresh-visible origin/develop
de99f544  [$ ERR] vrg-git worktree add .worktrees/issue-1210-audit-diff
          feature/1210-audit-diff   (malformed: positional branch, not -b)
cfa85963  [A] vrg-commit requires an issue number in the branch name. I need
          to create child issues in each repo first, then rename the branches.
bd22e848  [A] The hook enforces the worktree convention — I can't write to the
          main worktree. (write-to-main blocked → retroactive worktree)
```

**Overlap.** None as a script. It operationalizes
`docs/development/starting-work-on-an-issue.md` (in the plugin repo), which
agents repeatedly fail to locate.

---

### 3. vrg-commit interface friction (Closes→Ref, flags, heredoc) → `vrg-commit` hardening

**Signal.** error_retry + repetition + CORRECTION (medium-high). Closes→Ref
is a deterministic repo rule the agent re-learns by failed retry in session
after session; agents also reach for raw `git commit` heredocs and guessed
flags (`--description`, `--subject`, `--issue`, `--closes`, `--co-author`,
`--file`) first, and the hook guard false-positives on its own `--scope git`
argument.

**Frequency.** ≥15 distinct sessions: 4763c960, f51a8da8, 256863dc,
fe5b24bd, f3da9a9a, 39fa9eec, 83882e80, 10a356ea, c81b117d, bddab4d4,
5d356d5e, ee6e97db, 01b1a76e, e03ef568, 73bf9b81, 38c311e4, 24541e56,
e6ed1548, 9f0ed5f5, b7368477, dbe68ca9, 3eaf06a8, 334653e8.

**What agents do today.** Per commit they burn 2–5 failed attempts: raw
`git commit -m "$(cat <<EOF…)"` (blocked), `git commit --file` /
`vrg-commit --file` (no such interface), guessed flag names,
`vrg-commit --help` rediscovery, and a `Closes #N` body that the repo's
single-tracking-issue policy rejects — finally landing on
`--type/--scope/--message/--body` with an embedded `Ref #N`. In one session
the agent had to run `echo` probes to reverse-engineer the hook guard's
trigger because `--scope git` matched the bare `git` token.

**Proposed change.** Harden the existing `vrg-commit` (no new script):

```text
vrg-commit ...
  # Accept --issue/--ref <N> and an explicit --linkage ref|closes; render the
  #   policy-correct `Ref #N` trailer itself.
  # Detect Closes/Fixes/Resolves #N in --body and either auto-rewrite to Ref
  #   (with a warning) or reject with a one-line corrected-command hint —
  #   never a generic failure.
  # Alias common wrong flags (--description→--message, --subject→--message);
  #   no-op or name the right flag for --co-author/--coauthor.
  # Add --body-file / --message-file so prose tokens are never re-parsed by the guard.
  # Whitelist vrg-commit's own --scope/--message values so vrg-hook-guard
  #   does not inspect them as git subcommands.
```

Pair with a hook-guard change: when raw `git commit` is denied, emit the
exact `vrg-commit --type … --scope … --message …` template so the agent is
redirected on the first failure.

**Replaces.** The raw-git-heredoc attempt, the `--file` attempt, the
`--help` rediscovery, the wrong-flag retries, the Closes→Ref rewrite, and
the `--scope git` guard self-collision.

**Evidence.**

```text
f51a8da8  [$ ERR] vrg-commit --type fix --scope trivy --message "..."
          --body "...Closes #1246"
          [$ ok]  ...same... --body "...Ref #1246"   (Closes→Ref rewrite)
73bf9b81  [A] Auto-close keywords are banned repo-wide. Retrying with Ref:
38c311e4  [A] The hook guard is detecting "git" in the --scope git argument.
          Let me quote it to bypass the quoted-string stripping.
24541e56  [$ ERR] vrg-commit --type feat --scope vm --description "..." --body
          "..." --issue 1239 --co-author "..."
          [$ ok]  ...same... --message "..." --body "..."   (wrong flags dropped)
```

**Overlap.** Extends existing `vrg-commit`. (`--allow-empty` already exists
per #1278 — see candidate 7. `vrg-pr-fix-body` already covers PR-body
linkage repair — see Honorable mentions.)

---

### 4. Multi-line bodies via heredoc fail → `vrg-issue-create` / `--body-stdin`

**Signal.** error_retry + repetition (high). Always-fails-first-then-
recovers (no correction), but extremely frequent and fully deterministic —
the project's own no-heredoc rule is exactly what these transcripts keep
tripping over.

**Frequency.** ≥10 distinct sessions: 4763c960, 0952d5b1, fe5b24bd,
3da5f489, 253c73f3, 837c42a9, 83882e80, dda8b458, 90c234f5, 26d4d08e,
512b67db, 6971265b, 417f4f9d, 52fbf467, 6918371a, 408dfe53, f316f154.

**What agents do today.** For any multi-line issue or PR body the agent
first tries `--body "$(cat <<'EOF' … )"` or `cat > /tmp/file <<'EOF'`. The
hook guard blocks it. The agent then writes the body to `/tmp/*.md` (Write
tool or a long `printf '%s\n' …`) and re-invokes with `--body-file` /
`--notes "$(cat …)"`, then sometimes `rm`s the temp file. On cross-owner
repos the `gh issue create` also fails because the wrong installation token
is selected.

**Proposed script.**

```text
vrg-issue-create --title <t> [--repo <slug>] [--label <l>...]
                 [--body-file <f> | --body-stdin]
  # Read the body from stdin or a file, route it through a temp file internally,
  #   never a heredoc. Resolve the installation token from the -R owner, not cwd.
# Mirror as a first-class --notes-stdin / --summary-file on vrg-submit-pr.
```

**Replaces.** The blocked inline/redirect heredoc, the manual
Write-to-`/tmp` + `--body-file` re-invocation and cleanup, and the
wrong-token failure on cross-owner repos.

**Evidence.**

```text
253c73f3  [A] Heredoc is blocked. Let me write the body to a temp file and
          use --body-file.  [Write] /tmp/issue-2.1-migration.md
dda8b458  [$ ERR] vrg-gh issue create --title "Add mv to ... allowlist"
          --body "$(cat <<'EOF'  ...
          [$ ok]  vrg-gh issue create --title "Add mv ..." --body-file
          /tmp/issue-body.md
408dfe53  [$ ERR] for repo in vergil-project/.github vergils-nemesis/.github
          ...; do vrg-gh issue create -R "$repo" ... --body-file ...; done
          (cross-owner loop ERRs on the wrong installation token)
6918371a  [A] Heredoc is blocked. Let me write the notes via the Write tool,
          then submit:
```

**Overlap.** Thin wrapper over `vrg-gh` / `vrg-submit-pr`. Distinct from
existing scripts — none of them own multi-line body capture.

---

### 5. Resume a release that died mid-phase → `vrg-release-resume`

**Signal.** CORRECTION (high) + repetition + error_retry. Some of the
strongest user feedback in the corpus. The user named the architectural gap
directly — that the release process is neither idempotent nor resilient and
must be made so — and a back-merge PR that was mis-targeted to `main`
(instead of `develop`) drew a sharp correction and a loss of trust in the
agent's branch targeting.

**Frequency.** 1 deep session (ca70e14b) plus the implemented follow-up
(54901c7b), with the failure reproduced across three repos (vergil-vm #59,
vergil-claude-plugin #422, vergil-docker #314) within that session.

**What agents do today.** When `vrg-release` aborts at `merge-release`, the
agent reverse-engineers the remaining phases from `vrg-release` source and
completes them by hand, per repo: confirm CD on main + tags; create a
`release/post-VERSION` worktree from origin/main, `vrg-version bump patch`,
commit, push, open a back-merge PR — which `vrg-submit-pr` mis-targets to
main because the branch tracks origin/main; verify CD on develop; move the
rolling `vX.Y` tag; close the tracking issue; finalize; print
consumer-refresh commands. Recovery from an already-broken release
additionally requires auditing open release PRs/branches/VERSION skew and
computing version-skip math (e.g. 2.1.1 → 2.1.3).

**Proposed script.**

```text
vrg-release-resume [--from-phase PHASE]
  # Read tracking-issue/phase state, detect the last completed phase, and re-run
  #   remaining phases idempotently (each a no-op if already satisfied):
  #   confirm-main, back-merge-bump, confirm-develop, promote, close-finalize,
  #   consumer-refresh.

vrg-back-merge-bump --issue N        # used by resume; also standalone
  # Create release/post-VERSION from origin/main, vrg-version bump patch, commit,
  #   push, open a back-merge PR with --base develop HARD-WIRED (never auto-detected),
  #   plus required Ref linkage.
```

**Replaces.** Hand-deriving the phase list from source; manual post-release
worktrees, bumps, back-merge PRs (and the main-vs-develop target mistake),
rolling-tag moves, issue closure, and finalize for every aborted release;
plus the stale-release audit + version-skip arithmetic.

**Evidence.**

```text
ca70e14b  (user, paraphrased) pointed out that completing the one merge PR
          does not finish the release, that the rest of the phases never
          ran, and that the process must be made idempotent and resilient.
ca70e14b  (user, paraphrased) after a back-merge PR was mis-targeted to main,
          said they would now have to double-check the target branch on every
          PR themselves.
ca70e14b  [A] Target is **main** — wrong again. The release/post-* branch was
          created from origin/main, so vrg-submit-pr is targeting main. These
          back-merge PRs need to go to **develop**.
ca70e14b  Release failed in phase 'merge-release'. ... Error: PR has merge
          conflicts.   (same failure recurred for vergil-vm #59, vergil-docker #314)
```

**Overlap.** Complements `vrg-release`, `vrg-promote`, `vrg-version`,
`vrg-finalize-pr` — it sequences them resumably. The `--base` override the
user requested for back-merge PRs was filed as #1277 and implemented in
session 54901c7b; `vrg-back-merge-bump` would consume it.

---

### 6. Post-merge CD-run watch ritual → `vrg-confirm-merge`

**Signal.** repetition + error_retry + variance (medium). No user
correction, but the hand-rolled jq is fragile (wrong job-name filter
requiring re-runs; `docs / docs` job name) and the sequence is reproduced
near-verbatim across slices and sessions.

**Frequency.** 2 distinct sessions: 0952d5b1, 5d356d5e.

**What agents do today.** After finalize, to confirm the post-merge CD
workflow, the agent chains: `vrg-git rev-parse origin/develop` for the merge
SHA → `vrg-gh run list --workflow cd.yml --branch develop … --jq '.[] |
select(.headSha == "$SHA")'` → `vrg-gh run watch <id>` → `vrg-gh run view
<id> --json jobs --jq 'select(.name == "docs")'` — often guessing the
job-name filter wrong (empty result because the job is `docs / docs`) and
re-running.

**Proposed script.**

```text
vrg-confirm-merge [--base develop] [--sha <merge-sha>]
  # Resolve the cd.yml run for current base HEAD (or given SHA), watch to
  #   completion, and print a per-job conclusion table (docs/release) with a
  #   clean pass/fail exit code. Optionally fold in the finalize relocation.
```

**Replaces.** The rev-parse + `run list` headSha jq-select + `run watch` +
`run view` job-name jq sequence (and its retries from the wrong job-name
filter) after every merge.

**Evidence.**

```text
0952d5b1  [$ ok] MERGE_SHA=$(vrg-git rev-parse origin/develop) && ...
          vrg-gh run list --workflow cd.yml --branch develop --limit 5
          --json databaseId,headSha,status,conclusion,url
          --jq ".[] | select(.headSha == \"${MERGE_SHA}\")"
5d356d5e  [$ ok] vrg-gh run view 26832662250 --json jobs
          --jq '.jobs[] | select(.name == "docs") | {name, conclusion}'
          [A] The docs-job query returned empty — let me confirm the actual
          job names in that CD run   (job is "docs / docs")
```

**Overlap.** Sits adjacent to existing `vrg-wait-until-green` (which watches
a PR's pre-merge checks); this watches the post-merge develop CD run.
Distinct enough to warrant its own command.

---

### 7. Re-trigger CI when checks never register → `vrg-ci-retrigger`

**Signal.** CORRECTION (medium) + error_retry + repetition. A user directly
asked for a manual CI re-trigger when the gate never fired on a submitted
PR, and in an earlier session the agent had to hand the human raw
`git commit --allow-empty` because the tooling lacked the flag.

**Frequency.** 2–3 distinct sessions: 26d4d08e, f3da9a9a, 46943891.

**What agents do today.** Diagnose with
`vrg-gh pr view --json statusCheckRollup`, then force a re-run via an empty
commit (`vrg-commit … --allow-empty`) + push, then `sleep` and re-poll until
checks register. This also arises when a moving upstream action tag (e.g.
`@v2.0`) is re-pointed and GitHub replays the originally-resolved SHA,
requiring a fresh trigger rather than a re-run.

**Proposed script.**

```text
vrg-ci-retrigger [<pr-url|pr-number>]
  # Check the head commit's statusCheckRollup; if no checks are registered,
  #   push an empty `chore: re-trigger CI` commit on the PR branch and poll
  #   until checks appear (or report they were already running).
  #   Refuses on protected branches; reports the new run.
```

**Replaces.** The manual `pr view --json statusCheckRollup`, hand-crafted
`vrg-commit --allow-empty` + push, and the `sleep` + re-poll loop — and
removes the human hand-off when the tooling could not make an empty commit.

**Evidence.**

```text
26d4d08e  [U] The CI gate never triggered for this PR that I submitted. We
          need to re-trigger it manually. This is the PR.
f3da9a9a  [U] The tooling does not support an empty commit. Give me the
          commands to do that manually and I will push it because you can't
          run them.
26d4d08e  [$ ok] vrg-commit --type ci --scope release --message "re-trigger PR
          CI" --body "...Ref #1470" --allow-empty && vrg-git push origin
          feature/1470-finalize-tty-stream
```

**Overlap.** Builds on `vrg-commit --allow-empty` (implemented per #1278)
and `vrg-gh`. The end-to-end re-trigger procedure is still unowned.

---

### 8. Coverage-gap hunt after validate → `vrg-coverage-gaps`

**Signal.** repetition + error_retry (medium). No correction, but a
recurring papercut: agents guess the `--cov` target spelling (path vs dotted
module vs `--cov=src`), get an empty row, re-run, then hand-decode
partial-branch arrows like `102->99`.

**Frequency.** ≥4 distinct sessions: 4763c960, 5d356d5e, f4248acc, de99f544,
27714027, c3242937.

**What agents do today.** After `vrg-validate` reports just-below-100%
coverage, the agent re-runs `pytest --cov=<guessed-target> --cov-branch
--cov-report=term-missing | grep <file>`, frequently re-spelling the `--cov`
target because the first form produced no row, then manually reads the named
line numbers to understand each uncovered branch, adds a targeted test or
`# pragma: no cover`, and re-validates.

**Proposed script.**

```text
vrg-coverage-gaps [paths...]
  # Run the project's canonical container coverage, auto-deriving the dotted
  #   --cov module path from the changed files. For each file under 100%, print
  #   the path, each uncovered statement/partial-branch arrow, and the actual
  #   source line text — sorted. No --cov target guessing, no term-missing parsing.
```

**Replaces.** The wrong-module-path retry, the ad-hoc
`--cov-report=term-missing | grep` invocations, and manual decoding of
partial-branch arrows into source locations.

**Evidence.**

```text
5d356d5e  [$ ok] pytest ... --cov=src/vergil_tooling/lib/pr_provenance ...
          [$ ok] pytest ... --cov=vergil_tooling.lib.pr_provenance ...
          (path-style target → empty; module-style target is the fix)
de99f544  [A] 99.99% — one remaining branch somewhere. Let me find it:
27714027  [$ ok] vrg-container-run -- uv run pytest
          tests/vergil_tooling/test_repo_config.py
          --cov=src/vergil_tooling/lib/repo_config.py --cov-branch
          --cov-report=term-missing -q   (path-style yields nothing → re-run)
```

**Overlap.** Complements `vrg-validate` / `vrg-container-test` — a focused
diagnostic for the coverage gate, not a replacement for the gate itself.

---

### 9. PR template hand-authored with unsupported YAML → `vrg-pr-template`

**Signal.** CORRECTION (high) + error_retry. A blunt user correction that
the generated PR template left the actual entries unfilled, and the agent
confirmed the parser silently mangles folded scalars (`summary='>-'`) past a
presence-only validator — a textbook silent failure.

**Frequency.** 3–4 distinct sessions: 7ddb16e0, e00fccc2, 412fa690,
c0384a01, 7a4090f5.

**What agents do today.** At implement handoff the agent hand-writes
`.vergil/pr-template.yml.tmp`, picks YAML scalar styles freely (folded
`>-`), and `mv`s it into place (`mkdir -p .vergil` first). The minimal
parser only supports flat `key: value` and `key: |` literal blocks, so
folded scalars mangle silently; `read_template` only checks key presence, so
a broken template passes validation and produces a PR body of literally
`- >-`. Heredoc authoring is also blocked, forcing a Write+`mv`.

**Proposed script.**

```text
vrg-pr-template --issue <N> --title <t> --summary <s>
                [--notes <n> | --notes-file <f>] [--linkage ref|closes]
  # Write a schema-valid .vergil/pr-template.yml atomically (tmp+rename), using
  #   only the supported flat/literal YAML subset; reject empty required fields;
  #   render + echo the resulting PR body so the agent sees exactly what
  #   vrg-submit-pr will produce.
```

**Replaces.** Hand-writing YAML (and choosing unsupported scalar styles),
the heredoc-then-Write+`mv` dance, and the silent mangling that ships a
broken PR body past validation.

**Evidence.**

```text
c0384a01  (user, paraphrased) gave a blunt correction that the generated PR
          template had none of the actual entries filled out.
c0384a01  [A] My template silently parsed to summary='>-', notes='>-', plus a
          stray TDD key, producing a PR body of literally "- >-". The minimal
          parser doesn't reject unsupported YAML; it mangles it silently, and
          read_template only checks that keys *exist*, so it passes validation.
7a4090f5  [$ ERR] cat > .vergil/pr-template.yml.tmp <<'EOF' ... EOF
          mv .vergil/pr-template.yml.tmp .vergil/pr-template.yml
          (heredoc blocked, forcing Write+mv)
```

**Overlap.** Feeds `vrg-submit-pr` template mode; pairs with
`vrg-pr-fix-body`. In-flight work on the non-empty-template validation gap
already exists (#1523); this candidate is the up-front generator/validator
that complements it.

## Honorable mentions / not worth a script yet

- **`vrg-validate-fix` (auto-apply ruff fixes after validate)** —
  repetition is real (session 4763c960, ~8 cycles) but evidenced in only one
  session, and `vrg-validate`/`vrg-container-*` already own the pipeline; the
  deterministic part is plain `ruff --fix`. Demote pending cross-session
  evidence.
- **`vrg-trivy-suppress` (recurring linux-libc-dev CVE)** — strong user
  framing but only one session (ca70e14b). Overlaps
  `vrg-trivy-scan`/`vrg-sarif-evaluate`; recast as an extension that emits a
  `.trivyignore` patch + issue-linked PR if it recurs.
- **`vrg-pr-required-status` / `vrg-ci-triage`** — 2 sessions (256863dc,
  b7368477); blocked partly by `vrg-gh` denying `run rerun`. Real but thin
  and entangled with a wrapper-permission decision. Consider folding "all
  required checks green?" into `vrg-wait-until-green`.
- **`vrg-fleet-status` (find all vergil.toml repos)** — 2 sessions
  (39fa9eec, 337d63c2), deterministic, but tied to a one-off fleet
  migration; low recurring value.
- **`vrg-migrate-version` (@v2.0→@v2.1 sweep)** — 1 session (837c42a9); a
  one-time major-bump playbook, not recurring churn.
- **`vrg-identity` / extend `vrg-whoami`** — 5 sessions of identity probing
  (c0384a01 etc.). Already covered: `vrg-whoami` exists (with in-flight CLI
  work #1520). Recast as "make `vrg-whoami` the single authoritative resolver
  and stop the env/file/python probing" — an extension, not a new script.
  Dropped as standalone.
- **`vrg-issue-status`** — 2 sessions (bec39902); useful but ad-hoc, and
  overlaps `vrg-resolve-tracking-issue`. Thin.
- **`vrg-test` (fast in-worktree pytest)** — 3 sessions (07731e82,
  01b1a76e), with one silent-false-pass correction (ran against unedited main
  source). Promising, but the canonical path is
  `vrg-container-run -- vrg-validate`; needs more evidence that a
  non-container fast loop is wanted.
- **`vrg-bump-version` (pyproject+VERSION+uv.lock together)** — 1 session
  (4b4c0345); largely subsumed by `vrg-version` and the release-recovery
  candidate.
- **`vrg-rollout` (cross-repo tracking-issue fan-out)** — 1 session
  (cfa85963); high-value but single-instance and overlaps `vrg-start-issue` +
  `vrg-finalize-pr` looped.
- **`vrg-docs-change` / `vrg-spec-new`** — docs-scaffolding (cb75f639,
  237e8fec, e6ed1548) overlaps existing `vrg-docs-stage` /
  `vrg-docs-patch-nav` and the `vrg-start-issue` candidate; the only distinct
  piece (canonical `docs/specs/` path) is a one-session correction.
- **Already covered, dropped outright:** `vrg-submit-pr --amend-body` (the
  linkage-repair need is already served by `vrg-pr-fix-body`; the remaining
  template-mode validation gap is a fix inside `vrg-submit-pr`, not a new
  script); the `--base` override for back-merge PRs (filed as #1277,
  implemented); `vrg-commit --allow-empty` (implemented per #1278).

## Method notes & caveats

- **Corpus & method.** 108 `vergil-tooling` sessions from the two weeks
  ending 2026-06-08, distilled to high-signal transcripts (user turns, agent
  narration, executed Bash commands with exec status), mined by 14 parallel
  agents, then clustered and ranked.
- **Thinking-blocks were dropped** from the distilled transcripts before
  mining; evidence is drawn only from user turns, agent narration, and
  executed commands.
- **Evidence provenance.** Agent-narration and command quotes are verbatim.
  Voice-captured user feedback is paraphrased and marked `(user,
  paraphrased)` per the project's voice-to-text policy; short cleanly-written
  user messages are quoted as typed. All load-bearing user-correction quotes
  were verified to exist verbatim in the source transcripts before
  paraphrasing.
- **Frequency counts are lower bounds.** They reflect only the sampled
  two-week window and only the sessions the miners surfaced for each
  procedure; true recurrence is at least this high. Distinct-session counts
  were de-duplicated by session id across miners before summing.
- **Ranking weighting:** CORRECTION signal dominates (candidates 1, 5, 9 lead
  partly on user corrections), then repetition across distinct sessions
  (candidates 2, 3, 4), then variance. A frequently-correct-but-never-
  corrected procedure (e.g. the body-file dance, candidate 4) ranks below a
  less-frequent but user-corrected one where the correction is decisive
  (e.g. finalize hand-off, candidate 1).
- **Hardening vs. new script:** candidates 1, 3, and 7 are enhancements to
  existing scripts (`vrg-finalize-pr`, `vrg-commit`, and the
  `vrg-commit`/`vrg-gh` substrate) rather than net-new commands; they are
  ranked on signal strength regardless.
