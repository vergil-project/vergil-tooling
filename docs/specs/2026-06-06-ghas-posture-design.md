# Declared GHAS Posture Design

**Issue:** vergil-project/vergil-tooling#1481
**Refs:** vergil-project/vergil-actions#693, vergil-project/vergil-actions#698,
logical-minds-foundry/mq-cluster-tooling#34

## Problem

The vergil CI stack half-supports repos without GitHub Advanced Security
(GHAS). vergil-actions v2.1.3 added the consumer-side off-switch
(`upload-sarif: false` on `ci-security.yml`, with SARIF preserved as
build artifacts and CodeQL skipped), but three gaps remain in
vergil-tooling:

1. **The CI gates ruleset blocks merges on non-GHAS repos.**
   `desired_ci_gates_ruleset()` unconditionally requires the GHAS check
   runs `Trivy` and `Semgrep OSS` (integration 57789) and, for
   CodeQL-supported languages, the `security / codeql` + `CodeQL` pair.
   On a repo without GHAS these check runs never materialize, so the
   strict required-checks policy blocks merges forever even when CI is
   green.
2. **The CI generator predates the v2.1.3 caller contract.**
   `render_ci_workflow()` emits neither the `actions: read` grant on the
   `security:` job (so new repos of any visibility are born
   startup-broken against `ci-security.yml@v2.1.4` — the
   vergil-actions#698 failure mode) nor `upload-sarif: false` for repos
   without GHAS. It also still pins reusable workflows at `@v2.0` while
   live consumers are on `@v2.1`.
3. **The GHAS posture is inferred, not declared.**
   `github_config.py` hardcodes `ghas_available = visibility !=
   "private"`. That inference breaks as soon as an org pays for GHAS on
   private repos — planned for logical-minds-foundry.

## Posture resolution

One resolved boolean, `ghas_available`, derived in a single helper and
threaded everywhere GHAS matters:

```text
ghas_available = config.project.ghas  if declared in vergil.toml
                 else visibility != "private"
```

- Undeclared preserves today's behavior exactly: public ⇒ GHAS,
  private ⇒ no GHAS. Zero migration for existing repos.
- A declared value wins over visibility. `ghas = true` on a private
  repo is the paid-org case; `ghas = false` on a public repo is
  permitted but pointless (GHAS features are free on public repos) —
  allowed, not validated against.

Turning GHAS on for a private repo becomes a one-key flip:

1. Set `ghas = true` under `[project]` in `vergil.toml`.
2. Update `ci.yml`: remove `upload-sarif: false` from the `security:`
   job (hand edit; the generator only runs at repo init).
3. Run `vrg-github-repo-config apply` to restore the GHAS required
   checks and secret-scanning settings.

## Components

### 1. `vergil.toml`: `[project] ghas` (lib/config.py)

- `ProjectConfig` gains `ghas: bool | None` (default `None` =
  undeclared).
- Parse from `project_raw.get("ghas")` in `_parse_raw_config()`;
  non-bool values raise `ConfigError` (consistent with the section's
  validation style — no silent coercion).
- Add `"ghas"` to the `[project]` entry in `_KNOWN_KEYS` so
  `_warn_unrecognized_keys()` accepts it.
- Optional key: not added to `_REQUIRED_PROJECT_FIELDS`, and the repo
  scaffold (`render_vergil_toml`) does not emit it — inference is the
  default posture.

### 2. Posture helper (lib/github_config.py)

```python
def ghas_available(config: VergilConfig, *, visibility: str) -> bool:
    if config.project.ghas is not None:
        return config.project.ghas
    return visibility != "private"
```

`compute_desired_state()` resolves this once and passes the boolean
down; callers of `desired_security_settings()` and
`desired_ci_gates_ruleset()` stop reasoning about visibility for GHAS
purposes.

### 3. `desired_security_settings()` (lib/github_config.py)

Signature changes from `(*, visibility: str)` to
`(*, ghas: bool)`. The body's `ghas_available = visibility !=
"private"` line is deleted; the parameter is used directly. Behavior
for undeclared configs is unchanged by construction.

### 4. `desired_ci_gates_ruleset()` (lib/github_config.py)

Gains `*, ghas: bool`. When `ghas` is false, omit:

- `_make_ghas_check("Trivy")` and `_make_ghas_check("Semgrep OSS")`
- the CodeQL pair (`security / codeql` and `_make_ghas_check("CodeQL")`)
  regardless of language

Unconditionally kept: `quality / common`, `security / trivy`,
`security / semgrep`, `security / standards`, and all versioned
quality/test/audit and version-bump checks. The trivy and semgrep jobs
gate via scanner exit codes (trivy `exit-code: 1` on CRITICAL/HIGH;
`vrg-semgrep-scan` non-zero on findings), so security gating survives
without the alert-based layer.

### 5. `render_ci_workflow()` (lib/repo_init.py)

- **Always** emit `actions: read` (with the explanatory comment used in
  vergil-tooling#1473) in the `security:` job's `permissions:` block —
  it is part of the `ci-security.yml@v2.1.3+` caller contract for all
  callers, independent of GHAS posture.
- Resolve `ghas_available` from the wizard context (`ctx.visibility`;
  newly initialized repos have no declared `ghas`, so inference
  applies). When false, emit `upload-sarif: false` in the `security:`
  job's `with:` block, with a comment pointing at
  vergil-actions#693 and the flip-back procedure.
- Bump every emitted reusable-workflow pin `@v2.0` → `@v2.1`
  (`ci-audit`, `ci-quality`, `ci-security`, `ci-test`,
  `ci-version-bump`, and `cd-docs` in `render_cd_workflow()` if still
  at `@v2.0`). These are among the last stale 2.0 references.

### 6. Remediation (post-release, human-run)

- `vrg-github-repo-config apply` on
  `logical-minds-foundry/mq-cluster-tooling` to reconcile its CI gates
  ruleset (drops the four GHAS-dependent required checks).
- **Verification first:** mq-cluster-tooling PRs currently merge
  despite required GHAS check runs that cannot materialize, which
  implies the CI gates ruleset was never applied there (or was
  bypassed). Run the audit mode before apply and record what it
  reports.

## Error handling

- Non-bool `ghas` in `vergil.toml` → `ConfigError` at parse time (no
  silent fallback).
- No runtime detection or API calls are added; the resolved posture is
  pure config + the visibility value `vrg-github-repo-config` already
  fetches.

## Testing

Mirror the public/private test pairs from the 826 visibility-gating
work (`tests/vergil_tooling/test_github_config_lib.py`):

- Config: `ghas` absent → `None`; `ghas = true/false` parsed; non-bool
  raises; key recognized (no unrecognized-key warning).
- Posture helper: declared-true/private, declared-false/public,
  undeclared-public, undeclared-private.
- Ruleset: with `ghas=True` unchanged from today (golden comparison);
  with `ghas=False` no integration-57789 checks and no codeql contexts,
  while `security / trivy` / `security / semgrep` /
  `security / standards` remain.
- Security settings: existing public/private tests re-expressed in
  terms of the resolved flag.
- Generator: emitted `ci.yml` contains `actions: read` always;
  contains `upload-sarif: false` for a private-visibility context and
  not for public; all `uses:` pins are `@v2.1`.

## Out of scope

- vergil-actions changes (the `upload-sarif` input and caller-contract
  docs shipped in v2.1.3–v2.1.5; no runtime visibility auto-detect).
- vergil-actions#699 (consumer-refresh release stage) and #700
  (permissions-diff release gate) process hardening.
- A regeneration command for existing consumers' `ci.yml` (hand-edit
  remains the mechanism; the generator only runs at repo init).
- Fork-PR SARIF upload failures on public repos (pre-existing,
  unrelated token limitation).

## Acceptance

- A private repo without GHAS gets a CI gates ruleset that its CI can
  actually satisfy, and merges are gated on scanner exit codes plus the
  standard quality/test checks.
- A newly initialized repo of any visibility produces a `ci.yml` that
  starts cleanly against `ci-security.yml@v2.1.4`.
- Setting `[project] ghas = true` on a private repo restores the full
  GHAS posture (required checks + secret scanning) via
  `vrg-github-repo-config apply` with no code changes.
- Public-repo consumers see no behavioral change anywhere.
