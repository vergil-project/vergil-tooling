# Post-Publish Tag Verification

**Issue:** [#664](https://github.com/wphillipmoore/standard-tooling/issues/664)
**Date:** 2026-05-09

## Problem

After the v1.4.32 release, consumers could not install from the
published tag because the tagged commit referenced git objects that
were missing from the remote. The root cause was a gap in git pack
negotiation during the release branch push — git concluded the remote
already had objects that it did not, likely due to state left by an
abandoned v1.4.31 release.

The failure was discovered by a consumer, not by the release process.

## Solution

Add a self-healing verification step to `cd-release.yml` (in
standard-actions) that runs immediately after `tag-and-release` pushes
the tag. The step shallow-clones the tag from the remote to confirm
all objects are resolvable. If the clone fails, it resyncs the tag
with `--no-thin` (which disables pack negotiation delta reuse, forcing
git to send all objects) and verifies again.

### Placement

In `cd-release.yml`, between "Tag and release" and "Generate app token
for bump PR":

```text
... Tag and release          (existing, line 263)
... Verify tag is resolvable  ← NEW
... Generate app token        (existing, line 270)
... Version bump PR           (existing, line 278)
```

### Step

```yaml
- name: Verify tag is resolvable from remote
  if: steps.tag_check.outputs.exists == 'false'
  env:
    TAG: ${{ steps.version.outputs.tag }}
  run: |
    repo_url="https://github.com/${{ github.repository }}"
    tmp="$(mktemp -d)"
    if git clone --depth 1 --branch "$TAG" "$repo_url" "$tmp" 2>/dev/null; then
      rm -rf "$tmp"
      exit 0
    fi
    echo "::warning::Tag $TAG has missing objects on remote — resyncing"
    git push --no-thin origin "$TAG" --force
    git clone --depth 1 --branch "$TAG" "$repo_url" "$tmp"
    rm -rf "$tmp"
```

### Behavior

| Scenario | Outcome |
|---|---|
| Tag resolves cleanly | Step passes silently |
| Missing objects, resync fixes it | Warning annotation, step passes |
| Missing objects, resync fails | Step fails, CD job goes red, bump PR not created |

### Why `--no-thin`

Normal `git push` uses thin packs: git negotiates with the remote to
find common objects and only sends deltas. This is the mechanism that
failed in v1.4.32 — git assumed the remote had objects it did not.
`--no-thin` disables delta reuse, forcing git to include all objects
the tag needs regardless of what the remote claims to have.

### Why shallow clone (not `ls-remote` or `git fetch`)

`git ls-remote` only checks that the ref exists, not that the objects
it points to are intact. A shallow clone (`--depth 1`) exercises the
full object resolution path — the same path consumers hit when
installing — with minimal transfer cost.

## Scope

- **One file changed:** `.github/workflows/cd-release.yml` in
  standard-actions
- **One step added:** ~10 lines of shell
- **No new commands** in standard-tooling
- **No new dependencies**

## Limitations

- The unauthenticated clone only works for public repositories. If
  standard-actions is used with private repos in the future, the step
  would need to use a token-authenticated URL.
- This catches missing objects but not other classes of install failure
  (e.g., broken package metadata). The issue specifically scoped the
  fix to the observed bug.
