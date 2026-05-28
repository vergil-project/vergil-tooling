# CD Release: Separate Language from Container Suffix

**Date:** 2026-05-28
**Issue:** [#1272](https://github.com/vergil-project/vergil-tooling/issues/1272)

## Problem

The `cd-release.yml` workflow uses a single `language` input for two
unrelated purposes:

1. **Container image suffix** — selects the container image the release
   job runs in (e.g., `prod-python:3.14`, `prod-base:latest`).
2. **Programming language ecosystem** — determines build commands,
   publish commands, and credential metadata.

For the five supported programming languages (go, java, python, ruby,
rust), these happen to be the same value. For non-language projects
(vergil-docker, vergil-vm, vergil-claude-plugin), there is no
programming language, but the workflow still needs a container image.
Callers are forced to pass `language: base` — a lie that propagates
through validation and causes failures.

The CI workflows (`ci-quality.yml`, `ci-test.yml`, etc.) already
separate these concerns with distinct `language` and `container-suffix`
inputs. `cd-release.yml` is the only workflow that conflates them.

A previous fix (commit `1fa73f71`) attempted to work around this by
adding `_NON_LANGUAGE_TYPES = {"base"}` to `vrg-release-validate-inputs`.
This accepted `base` but then rejected `--container-tag` for it — the
exact flag the workflow always passes (default: `"latest"`). The hack
must be reverted.

## Design

Apply the same `container-suffix` / `language` separation that CI
already uses.

### Component 1: `cd-release.yml` inputs (vergil-actions)

Add a `container-suffix` input. Make `language` optional with an empty
default.

```yaml
inputs:
  language:
    description: >-
      Primary programming language (one of: go, java, python, ruby,
      rust). Leave empty for non-language projects.
    type: string
    default: ""
  container-suffix:
    description: >-
      Container image name suffix (e.g. python, go, base). Defaults
      to the language input when set, "base" otherwise.
    type: string
    default: ""
```

The container image line changes from:

```yaml
container: ghcr.io/vergil-project/${{ inputs.container-prefix || 'prod' }}-${{ inputs.language }}:${{ inputs.container-tag }}
```

to:

```yaml
container: ghcr.io/vergil-project/${{ inputs.container-prefix || 'prod' }}-${{ inputs.container-suffix || inputs.language || 'base' }}:${{ inputs.container-tag }}
```

The fallback chain: explicit `container-suffix` wins; otherwise use
`language` (works for Python/Go/etc.); otherwise fall back to `base`.

The "Build and publish" step (line 119) already gates on the five real
languages — no change needed.

### Component 2: validate-inputs action (vergil-actions)

Make the `language` input optional. Only pass `--language` to the CLI
when non-empty.

```yaml
inputs:
  language:
    description: Primary language string.
    required: false
    default: ""
  container-tag:
    description: Dev container image tag.
    required: true
  registry-publish:
    description: Whether publishing is enabled.
    required: true
```

```bash
args=()
if [ -n "$INPUT_LANGUAGE" ]; then
  args+=(--language "$INPUT_LANGUAGE")
fi

if [ -n "$INPUT_CONTAINER_TAG" ]; then
  args+=(--container-tag "$INPUT_CONTAINER_TAG")
fi

if [ "$INPUT_REGISTRY_PUBLISH" = "true" ]; then
  args+=(--registry-publish)
fi

vrg-release-validate-inputs "${args[@]}"
```

### Component 3: `vrg-release-validate-inputs` CLI (vergil-tooling)

Remove `_NON_LANGUAGE_TYPES`. Change `language` from a required
positional argument to an optional `--language` flag.

```python
parser.add_argument(
    "--language",
    default="",
    help="Programming language (go, java, python, ruby, rust). "
         "Omit for non-language projects.",
)
```

Validation logic:

- **Language is empty:** Return 0. No ecosystem to validate.
- **Language is one of the five:** Validate `--registry-publish` and
  `--container-tag` against ecosystem metadata, same as today.
- **Language is anything else:** Error. Typo protection — only the five
  real languages are valid when a language is specified.

### Component 4: Consuming repos

| Repo | Before | After |
|---|---|---|
| vergil-tooling | `language: python, container-tag: "3.14"` | Unchanged (language doubles as container-suffix) |
| vergil-docker | `language: base, container-tag: latest` | `container-suffix: base` (remove `language`) |
| vergil-vm | `language: base, container-tag: latest` | `container-suffix: base` (remove `language`) |
| vergil-claude-plugin | Omits both (relies on defaults) | Unchanged (new defaults: empty language, container resolves to `base`) |

### Component 5: Tests (vergil-tooling)

Update `test_vrg_release_validate_inputs.py`:

- **Remove** `test_base_language_accepted`, `test_base_rejects_registry_publish`,
  `test_base_rejects_container_tag` — `base` is no longer a concept.
- **Add** `test_no_language_passes`: `main([])` returns 0.
- **Add** `test_no_language_ignores_flags`: `main(["--registry-publish"])`
  and `main(["--container-tag", "latest"])` return 0 when no `--language`
  is provided. No ecosystem means nothing to validate.
- **Update** `test_no_args_fails` → becomes `test_no_language_passes`
  (no required args means `main([])` succeeds).
- **Keep** `test_unsupported_language_fails` — `main(["--language", "unknown"])`
  still errors.
- **Update** all existing tests to use `--language` flag syntax instead of
  positional argument.

## Rollout Order

The changes span three repositories. Deploy in this order to avoid
breaking the release pipeline:

1. **vergil-tooling** — make `--language` optional in validate-inputs
   CLI, remove `_NON_LANGUAGE_TYPES`. Release a new version.
2. **vergil-actions** — add `container-suffix` input to `cd-release.yml`,
   make `language` optional, update validate-inputs action. Tag a new
   minor version.
3. **Consuming repos** (vergil-docker, vergil-vm) — update `cd.yml`
   to pass `container-suffix: base` instead of `language: base`.
   vergil-claude-plugin needs no change (defaults are now correct).

No backward-compatibility shim is needed. Consuming repos pin
vergil-actions at a tag (e.g., `@v2.0`). They update the tag reference
and their `vergil.toml` dependency together via consumer-refresh, so
the old action (positional arg) always pairs with the old CLI and the
new action (`--language` flag) always pairs with the new CLI.

## Out of Scope

- CI workflow changes — CI already has the right pattern.
- Adding new languages or container image types.
- Changes to `vergil.toml` `primary-language` field handling.
