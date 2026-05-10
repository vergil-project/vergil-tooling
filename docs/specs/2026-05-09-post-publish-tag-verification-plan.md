# Post-Publish Tag Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-healing verification step to `cd-release.yml` in standard-actions that confirms a newly pushed tag is resolvable from the remote, resyncing missing objects if needed.

**Architecture:** A single workflow step inserted after "Tag and release" in `cd-release.yml`. It shallow-clones the tag from the public remote URL. If the clone fails, it force-pushes the tag with `--no-thin` to resync objects, then retries.

**Tech Stack:** GitHub Actions workflow YAML, bash, git

**Spec:** `docs/specs/2026-05-09-post-publish-tag-verification-design.md`

---

## File Structure

- Modify: `/Users/pmoore/dev/github/standard-actions/.github/workflows/cd-release.yml:269` (insert after line 268)

This is a single-file, single-step change.

---

### Task 1: Add the verification step to cd-release.yml

**Files:**
- Modify: `/Users/pmoore/dev/github/standard-actions/.github/workflows/cd-release.yml:269`

The step goes between "Tag and release" (ends at line 268) and "Generate app token for bump PR" (starts at line 270).

- [ ] **Step 1: Insert the verification step**

Open `/Users/pmoore/dev/github/standard-actions/.github/workflows/cd-release.yml` and insert the following block after line 268 (the end of "Tag and release") and before line 270 ("Generate app token for bump PR"):

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

After insertion, the file should read (lines 263–286):

```yaml
      - name: Tag and release
        if: steps.tag_check.outputs.exists == 'false'
        uses: wphillipmoore/standard-actions/actions/publish/tag-and-release@develop
        with:
          version: ${{ steps.version.outputs.version }}
          release-artifacts: ${{ steps.resolved.outputs.release-artifacts }}

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

      - name: Generate app token for bump PR
```

- [ ] **Step 2: Validate the workflow YAML is syntactically correct**

Run from the standard-actions repo root:

```bash
cd /Users/pmoore/dev/github/standard-actions && python3 -c "import yaml; yaml.safe_load(open('.github/workflows/cd-release.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 3: Validate with actionlint**

```bash
cd /Users/pmoore/dev/github/standard-actions && actionlint .github/workflows/cd-release.yml
```

Expected: no errors. (If actionlint is not installed on the host, skip — CI will catch it.)

- [ ] **Step 4: Commit**

```bash
cd /Users/pmoore/dev/github/standard-actions
git add .github/workflows/cd-release.yml
git commit -m "fix(cd): verify tag is resolvable after push, resync if needed

Adds a post-publish shallow-clone check in cd-release.yml. If the
newly pushed tag has missing objects on the remote, the step resyncs
with --no-thin and retries.

Ref wphillipmoore/standard-tooling#664"
```

---

### Task 2: Commit the spec and plan in standard-tooling

**Files:**
- Commit: `/Users/pmoore/dev/github/standard-tooling/docs/specs/2026-05-09-post-publish-tag-verification-design.md`
- Commit: `/Users/pmoore/dev/github/standard-tooling/docs/specs/2026-05-09-post-publish-tag-verification-plan.md`

- [ ] **Step 1: Commit the spec and plan**

```bash
cd /Users/pmoore/dev/github/standard-tooling
git add docs/specs/2026-05-09-post-publish-tag-verification-design.md \
        docs/specs/2026-05-09-post-publish-tag-verification-plan.md
git commit -m "docs: add spec and plan for post-publish tag verification (#664)"
```
