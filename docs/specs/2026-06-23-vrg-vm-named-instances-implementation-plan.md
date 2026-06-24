# vrg-vm Named Instances Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one `(identity, org/repo)` own several named VM instances via a `--name` flag, each with its own composed profile, backend, recorded state, and volume — fully backward compatible when no name is given.

**Architecture:** A four-part handle `(identity, org, repo, name)` derives three deterministic names — the Lima instance name (`.`-joined, reversed via the sidecar), the readable tofu state slug (`--`-joined), and the hashed cloud resource name (`vrg-<sha256(slug)[:12]>`, with identity in labels). Composition gains a tier-5 per-name overlay; `destroy` resolves from recorded state (all backends) while `stop`/`start`/`restart` stay Lima-only; `list`/`volumes` gain an INSTANCE column.

**Tech Stack:** Python 3.12+, `argparse`, `tomllib`, `pytest`, OpenTofu/GCP modules (owned by vergil-vm), Lima (`limactl`).

**Authoritative spec:** `docs/specs/2026-06-23-vrg-vm-named-instances-tooling-design.md` (this repo) and vergil-vm #242's `docs/specs/2026-06-23-multiple-vm-instances-per-repo-design.md`.

## Global Constraints

- **Working environment:** All work happens in the worktree `.worktrees/issue-1831-named-instances/` on branch `feature/1831-named-instances`. Use absolute worktree paths for Read/Edit/Write; `cd` into the worktree for Bash.
- **Git/GitHub wrappers only:** `vrg-git` not `git`; `vrg-commit` not `git commit`; `vrg-gh` not `gh`. Raw `git`/`gh` are denied by the hook guard.
- **Commit format:** `vrg-commit --type <type> --scope vm --message "<desc>" [--body "<body>"]`. Each task's final step commits with `Ref #1831` in the body.
- **Per-step tests:** run a single test with `vrg-container-run -- uv run pytest <path>::<test> -v` (this repo dogfoods unreleased code via the `[validation]` override, so `uv run` is required).
- **Full validation (end of each phase):** `vrg-container-run -- vrg-validate` — the only validation command; it runs lint, typecheck, the full test suite, and audits.
- **Backward compatibility is a hard requirement:** a repo that declares no `instances` and every call without `--name` must behave byte-for-byte as today. The existing `tests/` suite must stay green at every commit.
- **No silent failures:** invalid instance names, repo names containing `--`, and `--name X` against a missing instance must raise loudly. Never default or fall back silently.
- **Instance name grammar:** `[a-z0-9]+(?:-[a-z0-9]+)*` (lowercase alnum, single internal hyphens, no `--`, no leading/trailing hyphen).
- **Cloud name budget:** the hashed `vrg-<12 hex>` is 16 chars; the GCP `var.name` ≤ 58 guard lives in vergil-vm and is out of scope here.
- **Reserved, not built:** the per-name tier-6 host-override slot `[<identity>.<org>.<repo>.<name>]` is intentionally NOT parsed or consumed. Tier-6 stays keyed on `(org, repo)` exactly as today; a name-level override table is left unread (non-breaking to add later, per the spec). Do not implement it.

---

## Phase 1 — Config parsing & composition (pure logic)

### Task 1: Parse the per-identity `instances` namespace

**Files:**
- Modify: `src/vergil_tooling/lib/config.py` (`RoleOverlay` dataclass ~110-129; `_parse_role_overlay` 218-237; `parse_vm_stanza` 240-280)
- Test: `tests/vergil_tooling/test_config.py`

**Interfaces:**
- Consumes: existing `RoleOverlay`, `VmStanza`, `_parse_role_overlay`, `parse_vm_stanza`, `ConfigError`.
- Produces: `RoleOverlay.instances: dict[str, RoleOverlay]` (default `{}`), populated from `[vm.<identity>.instances.<name>]`. A role named `instances` directly under `[vm]` raises `ConfigError` (no all-identity tier). Invalid instance names raise `ConfigError` at parse time.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_config.py`:

```python
def test_parse_vm_stanza_named_instances():
    raw = {
        "vm": {
            "vergil-user": {
                "cpus": 12,
                "instances": {
                    "cloud-x86": {
                        "backend": "off-platform",
                        "provider": "gcp",
                        "region": "us-central1",
                        "instance": "n2-standard-16",
                        "volume": "300GiB",
                    }
                },
            }
        }
    }
    stanza = parse_vm_stanza(raw)
    role = stanza.roles["vergil-user"]
    assert role.cpus == 12
    assert set(role.instances) == {"cloud-x86"}
    inst = role.instances["cloud-x86"]
    assert inst.backend == "off-platform"
    assert inst.instance == "n2-standard-16"
    assert inst.instances == {}  # no nested instances


def test_parse_vm_stanza_rejects_all_identity_instances_tier():
    raw = {"vm": {"instances": {"cloud-x86": {"cpus": 4}}}}
    with pytest.raises(ConfigError, match="no all-identity .*instances"):
        parse_vm_stanza(raw)


def test_parse_vm_stanza_rejects_invalid_instance_name():
    raw = {"vm": {"vergil-user": {"instances": {"bad--name": {"cpus": 4}}}}}
    with pytest.raises(ConfigError, match="instance name"):
        parse_vm_stanza(raw)
```

Ensure `pytest` and `ConfigError`/`parse_vm_stanza` are imported at the top of the test module (they already are for existing tests).

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py::test_parse_vm_stanza_named_instances -v`
Expected: FAIL — `RoleOverlay.__init__` got an unexpected keyword `instances` (or AttributeError on `.instances`).

- [ ] **Step 3: Add the `instances` field to `RoleOverlay`**

In `src/vergil_tooling/lib/config.py`, add to the `RoleOverlay` dataclass (after `nested: bool | None = None`, keeping defaulted fields last):

```python
    # Named-instance overlays (vergil-tooling #1831). Each value is itself a
    # RoleOverlay parsed from [vm.<identity>.instances.<name>]; an instance overlay
    # never carries its own nested instances. Empty for the common (unnamed) case.
    instances: dict[str, "RoleOverlay"] = field(default_factory=dict)
```

Ensure `from dataclasses import dataclass, field` at the top of `config.py` (add `field` if missing).

- [ ] **Step 4: Add the instance-name validator import and parse logic**

At the top of `config.py`, add the runtime import (no import cycle — `vm_spec` imports `config` only under `TYPE_CHECKING`):

```python
from vergil_tooling.lib.vm_spec import validate_instance_name
```

(`validate_instance_name` is created in Task 2 Step 3 — if you implement Task 1 first, add a temporary stub `def validate_instance_name(name: str) -> None: ...` in `vm_spec.py` and replace it in Task 2. Subagent-driven execution runs tasks in order, so Task 2's validator will already exist.)

Rewrite `_parse_role_overlay` to parse a nested `instances` table:

```python
def _parse_role_overlay(
    name: str, raw: dict[str, Any], source: str = CONFIG_FILE, *, allow_instances: bool = True
) -> RoleOverlay:
    if _SHARED_FROM_KEY in raw:
        msg = f"{source}: shared_from is not allowed in a role overlay [vm.{name}]"
        raise ConfigError(msg)
    for key in raw:
        if key == "instances" and allow_instances:
            continue
        if key not in _VM_KEYS:
            print(f"{source}: unrecognized key '{key}' in [vm.{name}]", file=sys.stderr)
    scalars = {k: _vm_str_scalar(raw, k, f"[vm.{name}]", source) for k in _VM_STR_SCALARS}
    instances: dict[str, RoleOverlay] = {}
    if allow_instances:
        raw_instances = raw.get("instances", {})
        if not isinstance(raw_instances, dict):
            msg = f"{source}: [vm.{name}].instances must be a table"
            raise ConfigError(msg)
        for iname, itable in raw_instances.items():
            try:
                validate_instance_name(iname)
            except ValueError as exc:
                msg = f"{source}: [vm.{name}.instances.{iname}]: {exc}"
                raise ConfigError(msg) from exc
            if not isinstance(itable, dict):
                msg = f"{source}: [vm.{name}.instances.{iname}] must be a table"
                raise ConfigError(msg)
            instances[iname] = _parse_role_overlay(
                iname, itable, source, allow_instances=False
            )
    return RoleOverlay(
        packages=list(raw.get("packages", [])),
        cpus=raw.get("cpus"),
        memory=raw.get("memory"),
        disk=raw.get("disk"),
        stale_days=raw.get("stale_days"),
        apt_repos=list(raw.get("apt_repos", [])),
        vagrant_plugins=list(raw.get("vagrant_plugins", [])),
        port_forwards=list(raw.get("port_forwards", [])),
        nested=raw.get("nested"),
        instances=instances,
        **scalars,
    )
```

In `parse_vm_stanza`, reject the all-identity tier. Replace the `elif isinstance(value, dict):` branch:

```python
        elif isinstance(value, dict):
            if key == "instances":
                msg = (
                    f"{source}: no all-identity [vm.instances] tier — declare named "
                    f"instances under [vm.<identity>.instances.<name>]"
                )
                raise ConfigError(msg)
            roles[key] = _parse_role_overlay(key, value, source)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py -k "instances" -v`
Expected: PASS (all three new tests).

- [ ] **Step 6: Refactor**

Look for:
- Duplicated RoleOverlay construction between the top-level role parse and the instance-overlay parse — both build a `RoleOverlay` from the same scalar/list set. Confirm the recursive `_parse_role_overlay(..., allow_instances=False)` reuse is the only construction path (don't fork a second builder).
- Naming: the `instances={}` default on `RoleOverlay` must not break existing positional constructions in tests — grep `RoleOverlay(` in tests and confirm.

- [ ] **Step 7: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/config.py tests/vergil_tooling/test_config.py
vrg-commit --type feat --scope vm --message "parse [vm.<identity>.instances.<name>] namespace" \
  --body "Add RoleOverlay.instances parsed from per-identity instance overlays; reject the all-identity [vm.instances] tier and invalid instance names at parse time. Ref #1831"
```

---

### Task 2: Naming validators + tier-5 composition

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (add validators near 282-303; extend `compose_vm_spec` 169-251)
- Test: `tests/vergil_tooling/test_vm_spec.py`

**Interfaces:**
- Consumes: `_apply_overlay`, `_Acc`, `ComposedSpec`, `SpecError`, `RoleOverlay`.
- Produces:
  - `validate_instance_name(name: str) -> None` — raises `ValueError` unless `name` matches `^[a-z0-9]+(?:-[a-z0-9]+)*$`.
  - `validate_repo_segment(repo: str) -> None` — raises `ValueError` if `repo` contains `--`.
  - `compose_vm_spec(*, identity, base, stanza, override, instance: str | None = None) -> ComposedSpec` — applies the tier-5 named overlay; `--name X` with no such instance raises `SpecError` listing available names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_spec.py`:

```python
from vergil_tooling.lib.vm_spec import validate_instance_name, validate_repo_segment


@pytest.mark.parametrize("name", ["cloud-x86", "rdqm-rhel", "a", "x1"])
def test_validate_instance_name_accepts(name):
    validate_instance_name(name)  # no raise


@pytest.mark.parametrize("name", ["bad--name", "-lead", "trail-", "Up", "a_b", ""])
def test_validate_instance_name_rejects(name):
    with pytest.raises(ValueError, match="instance name"):
        validate_instance_name(name)


def test_validate_repo_segment_rejects_double_dash():
    with pytest.raises(ValueError, match="--"):
        validate_repo_segment("my--repo")
    validate_repo_segment("my-repo")  # single dash ok


def test_compose_named_instance_overlays_tier5():
    role = RoleOverlay(
        packages=[], cpus=12, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[],
        instances={
            "rdqm-rhel": RoleOverlay(
                packages=[], cpus=8, memory="32GiB", disk=None, stale_days=None,
                apt_repos=[], vagrant_plugins=[], port_forwards=[],
                backend="off-platform", provider="gcp", region="us-central1",
                instance="n2-standard-8", volume="200GiB",
            )
        },
    )
    stanza = VmStanza(
        packages=[], cpus=None, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[],
        roles={"vergil-user": role},
    )
    spec = compose_vm_spec(
        identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="rdqm-rhel"
    )
    assert spec.cpus == 8  # tier-5 overrides tier-4's 12
    assert spec.memory == "32GiB"
    assert spec.off_platform
    assert spec.instance == "n2-standard-8"


def test_compose_default_instance_unchanged_when_none():
    stanza = VmStanza(
        packages=[], cpus=12, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[], roles={},
    )
    spec = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
    assert spec.cpus == 12  # tiers 1-4 only, today's behavior


def test_compose_missing_named_instance_errors_with_available():
    role = RoleOverlay(
        packages=[], cpus=None, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[],
        instances={"cloud-x86": RoleOverlay(
            packages=[], cpus=None, memory=None, disk=None, stale_days=None,
            apt_repos=[], vagrant_plugins=[], port_forwards=[])},
    )
    stanza = VmStanza(
        packages=[], cpus=None, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[], roles={"vergil-user": role},
    )
    with pytest.raises(SpecError, match="cloud-x86"):
        compose_vm_spec(
            identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="nope"
        )


def test_fingerprint_excludes_instance_name():
    # The name is the handle, not fingerprint content: a named instance and the
    # default that resolve to the SAME effective footprint share a fingerprint, so
    # adding/renaming an instance never trips drift on the others.
    role = RoleOverlay(
        packages=[], cpus=8, memory="32GiB", disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[],
        instances={"rdqm-rhel": RoleOverlay(
            packages=[], cpus=8, memory="32GiB", disk=None, stale_days=None,
            apt_repos=[], vagrant_plugins=[], port_forwards=[])},
    )
    stanza = VmStanza(
        packages=[], cpus=None, memory=None, disk=None, stale_days=None,
        apt_repos=[], vagrant_plugins=[], port_forwards=[], roles={"vergil-user": role},
    )
    default = compose_vm_spec(identity="vergil-user", base=BASE, stanza=stanza, override=None)
    named = compose_vm_spec(
        identity="vergil-user", base=BASE, stanza=stanza, override=None, instance="rdqm-rhel"
    )
    assert spec_fingerprint(default) == spec_fingerprint(named)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k "validate_instance or validate_repo or named_instance or default_instance or fingerprint_excludes" -v`
Expected: FAIL — `ImportError` for `validate_instance_name` / `compose_vm_spec` got unexpected keyword `instance`.

- [ ] **Step 3: Add the validators**

In `src/vergil_tooling/lib/vm_spec.py`, after the `_TIER_SEP = "."` block (around line 289), add:

```python
_INSTANCE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_instance_name(name: str) -> None:
    """Reject an instance name that is not [a-z0-9]+ with single internal hyphens.

    A double dash would break the readable ``--``-joined state slug; an empty or
    upper-cased name is also rejected. Raises loudly (no-silent-failures).
    """
    if not _INSTANCE_NAME_RE.fullmatch(name):
        msg = (
            f"instance name {name!r} must be lowercase [a-z0-9-] with single internal "
            f"hyphens (no '--', no leading/trailing hyphen)"
        )
        raise ValueError(msg)


def validate_repo_segment(repo: str) -> None:
    """Reject a repo name containing '--', which would make the state slug ambiguous."""
    if "--" in repo:
        msg = (
            f"repo name {repo!r} must not contain '--' (it would make the "
            f"'--'-joined instance handle ambiguous)"
        )
        raise ValueError(msg)
```

- [ ] **Step 4: Extend `compose_vm_spec` with the `instance` parameter**

Change the signature (line 169-175) to add `instance`:

```python
def compose_vm_spec(
    *,
    identity: str,
    base: Mapping[str, object],
    stanza: VmStanza | None,
    override: Mapping[str, object] | None,
    instance: str | None = None,
) -> ComposedSpec:
    """Overlay the precedence tiers into the effective spec for one (identity, repo[, name])."""
```

Replace the tier-3/4 block (lines 200-205) with tier-3/4/5:

```python
    # Tiers 3 + 4: repo [vm] (all-identity), then [vm.<identity>] role overlay.
    role = None
    if stanza is not None:
        _apply_overlay(acc, stanza)
        role = stanza.roles.get(identity)
        if role is not None:
            _apply_overlay(acc, role)

    # Tier 5: the named-instance overlay, if a name was requested.
    if instance is not None:
        available = sorted(role.instances) if role is not None else []
        overlay = role.instances.get(instance) if role is not None else None
        if overlay is None:
            avail = ", ".join(available) if available else "(none)"
            msg = (
                f"identity {identity!r}: no instance {instance!r} for this repo; "
                f"available: {avail}"
            )
            raise SpecError(msg)
        _apply_overlay(acc, overlay)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k "validate_instance or validate_repo or named_instance or default_instance or missing_named or fingerprint_excludes" -v`
Expected: PASS.

- [ ] **Step 6: Refactor**

Look for:
- The tier-5 block accesses `role.instances` twice (available list + overlay lookup); bind it once to a local.
- `validate_instance_name`/`validate_repo_segment` error messages should match the existing `SpecError` message style in this module (identity-prefixed where relevant).

- [ ] **Step 7: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/vm_spec.py tests/vergil_tooling/test_vm_spec.py
vrg-commit --type feat --scope vm --message "compose tier-5 named-instance overlay" \
  --body "Add validate_instance_name / validate_repo_segment and an 'instance' parameter to compose_vm_spec that applies the [vm.<identity>.instances.<name>] overlay, erroring with available names when the instance is missing. Ref #1831"
```

- [ ] **Step 8: Phase 1 full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS (whole suite green; default path unchanged).

---

## Phase 2 — Handle & naming derivations

### Task 3: Lima instance name gains the fourth segment

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (`instance_name` 305-333)
- Test: `tests/vergil_tooling/test_vm_spec.py`

**Interfaces:**
- Produces: `instance_name(identity, org, repo, name=None, *, home=None) -> str` — appends `.name` for a named instance; the truncation+hash digest input includes `name` so distinct instances hash distinctly.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vm_spec.py`:

```python
def test_instance_name_named_appends_segment():
    assert (
        instance_name("vergil-user", "lmf", "mq", name="cloud-x86")
        == "vergil-user.lmf.mq.cloud-x86"
    )


def test_instance_name_default_unchanged():
    assert instance_name("vergil-user", "lmf", "mq") == "vergil-user.lmf.mq"
    assert instance_name("vergil-user", None, None) == "vergil-user"


def test_instance_name_named_hash_differs_from_default(monkeypatch):
    # Force the over-budget path with a tiny budget so both names are hashed.
    monkeypatch.setattr("vergil_tooling.lib.vm_spec.lima_name_budget", lambda home=None: 20)
    default = instance_name("vergil-user", "logical-minds-foundry", "mq-cluster-tooling")
    named = instance_name(
        "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", name="cloud-x86"
    )
    assert default != named
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k "instance_name_named or instance_name_default" -v`
Expected: FAIL — `instance_name() got an unexpected keyword argument 'name'`.

- [ ] **Step 3: Implement the fourth segment**

Replace `instance_name` (lines 305-333) with:

```python
def instance_name(
    identity: str,
    org: str | None,
    repo: str | None,
    name: str | None = None,
    *,
    home: str | None = None,
) -> str:
    """Derive the Lima instance name. Bare identity = base; ``.``-joined = dedicated.

    A named instance appends ``.<name>`` as a fourth segment. Over budget, the name
    is truncated and hashed (the digest input includes ``name`` so distinct instances
    differ); ``recover_handle`` (vrg_vm) reverses a mangled name via the sidecar.
    """
    if org is None or repo is None:
        return identity
    for tier, value in (("identity", identity), ("org", org)):
        if _TIER_SEP in value:
            msg = f"{tier} name {value!r} must not contain '{_TIER_SEP}'"
            raise ValueError(msg)
    segments = [identity, org, repo]
    if name:
        segments.append(name)
    full = _TIER_SEP.join(segments)
    budget = lima_name_budget(home)
    if len(full) <= budget:
        return full
    if budget < len(identity) + 7:  # 6 hash chars + 1 separator
        msg = (
            f"home directory too long to fit a bounded VM name for identity "
            f"{identity!r}: budget {budget} < {len(identity) + 7}"
        )
        raise SpecError(msg)
    digest_src = "/".join(segments)
    digest = hashlib.sha256(digest_src.encode()).hexdigest()[:6]
    keep = budget - 7
    return f"{full[:keep].rstrip('._-')}-{digest}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k "instance_name" -v`
Expected: PASS (new tests plus existing `instance_name` tests).

- [ ] **Step 5: Refactor**

Look for:
- `segments` is built once and reused for both `full` (the readable join) and `digest_src` (the hash input) — confirm there's no second place that re-joins identity/org/repo/name.
- Naming: the new `name` parameter should not shadow the module-level `name` usages; confirm the function body reads clearly.

- [ ] **Step 6: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/vm_spec.py tests/vergil_tooling/test_vm_spec.py
vrg-commit --type feat --scope vm --message "append named-instance segment to Lima instance name" \
  --body "instance_name takes an optional name appended as a fourth '.'-segment; the over-budget hash digest input includes name so instances differ. Ref #1831"
```

---

### Task 4: Readable state slug, hashed cloud name, instance label

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (add `state_slug`)
- Modify: `src/vergil_tooling/lib/vm_cloud.py` (`cloud_resource_name` 42-51; `cloud_labels` 54-60; `OffPlatformBackend.__init__` 920-931)
- Modify: `src/vergil_tooling/lib/vm_backend.py` (`select_backend` 26-43)
- Test: `tests/vergil_tooling/test_vm_spec.py`, `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Produces:
  - `vm_spec.state_slug(identity, org=None, repo=None, name=None) -> str` — `identity` / `identity--org--repo` / `identity--org--repo--name`.
  - `vm_cloud.cloud_resource_name(slug: str) -> str` — `vrg-<sha256(slug)[:12]>` (signature changed from `(identity, org, repo)`).
  - `vm_cloud.cloud_labels(identity, org, repo, name=None) -> dict[str,str]` — adds `vergil-instance` when `name` is set.
  - `OffPlatformBackend(spec, identity, org, repo, name=None)`; `.slug` (readable), `.name` (hashed), `.state_key == .slug`.
  - `vm_backend.select_backend(spec, *, identity=None, org=None, repo=None, name=None)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_spec.py`:

```python
from vergil_tooling.lib.vm_spec import state_slug


def test_state_slug_forms():
    assert state_slug("vergil-user") == "vergil-user"
    assert state_slug("vergil-user", "lmf", "mq") == "vergil-user--lmf--mq"
    assert (
        state_slug("vergil-user", "lmf", "mq", "cloud-x86")
        == "vergil-user--lmf--mq--cloud-x86"
    )
```

Add to `tests/vergil_tooling/test_vm_cloud.py`:

```python
from vergil_tooling.lib.vm_cloud import cloud_labels, cloud_resource_name


def test_cloud_resource_name_is_hashed_and_deterministic():
    slug = "vergil-user--logical-minds-foundry--mq-cluster-tooling--cloud-x86"
    name = cloud_resource_name(slug)
    assert name.startswith("vrg-")
    assert len(name) == 16  # "vrg-" + 12 hex
    assert name == cloud_resource_name(slug)  # deterministic
    assert cloud_resource_name(slug + "-other") != name


def test_cloud_labels_includes_instance_when_named():
    labels = cloud_labels("vergil-user", "lmf", "mq", "cloud-x86")
    assert labels["vergil-instance"] == "cloud-x86"
    assert "vergil-instance" not in cloud_labels("vergil-user", "lmf", "mq")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py::test_state_slug_forms tests/vergil_tooling/test_vm_cloud.py -k "hashed or instance_when_named" -v`
Expected: FAIL — `state_slug` import error; `cloud_resource_name` takes 3 args not 1.

- [ ] **Step 3: Add `state_slug` to vm_spec**

In `src/vergil_tooling/lib/vm_spec.py`, after `parse_instance_name` (line 344), add:

```python
_SLUG_SEP = "--"


def state_slug(
    identity: str, org: str | None = None, repo: str | None = None, name: str | None = None
) -> str:
    """The readable '--'-joined handle: state-path key and cloud-name hash input.

    ``identity`` (base) / ``identity--org--repo`` (default dedicated) /
    ``identity--org--repo--name`` (named). Reversal is via labels (cloud) and the
    sidecar (Lima), not by splitting — but '--' keeps the path human-readable.
    """
    if org is None or repo is None:
        return identity
    segments = [identity, org, repo]
    if name:
        segments.append(name)
    return _SLUG_SEP.join(segments)
```

- [ ] **Step 4: Rewrite `cloud_resource_name` and `cloud_labels`**

In `src/vergil_tooling/lib/vm_cloud.py`, replace `cloud_resource_name` (42-51) and `cloud_labels` (54-60):

```python
def cloud_resource_name(slug: str) -> str:
    """Deterministic short RFC1035 name 'vrg-<first 12 hex of sha256(slug)>' (16 chars).

    The four-segment slug overflows GCP's 63-char limit, so the readable slug keys
    the state path / labels while cloud resources take this opaque hash. Identity is
    carried in labels (which `list` and `tofu import` read), never in the name.
    """
    digest = hashlib.sha256(slug.encode()).hexdigest()[:12]
    return f"vrg-{digest}"


def cloud_labels(
    identity: str, org: str, repo: str, name: str | None = None
) -> dict[str, str]:
    """Structured labels for label-based recovery (independent of the hashed name)."""
    labels = {
        "vergil-identity": _slug(identity),
        "vergil-org": _slug(org),
        "vergil-repo": _slug(repo),
    }
    if name:
        labels["vergil-instance"] = _slug(name)
    return labels
```

The constants `_MAX_NAME`/`_HASH_LEN` (lines 33-34) are no longer used by `cloud_resource_name`. Leave them only if still referenced elsewhere; otherwise delete them (run `grep -n "_MAX_NAME\|_HASH_LEN" src/vergil_tooling/lib/vm_cloud.py` and remove if unused to keep lint clean).

- [ ] **Step 5: Update `OffPlatformBackend.__init__`**

In `src/vergil_tooling/lib/vm_cloud.py`, add the import at the top (with the other `vm_spec` import on line 25):

```python
from vergil_tooling.lib.vm_spec import spec_fingerprint, state_slug
```

Replace `OffPlatformBackend.__init__` (920-931):

```python
    def __init__(
        self,
        spec: ComposedSpec,
        identity: str,
        org: str,
        repo: str,
        name: str | None = None,
    ) -> None:
        self.spec = spec
        self.identity = identity
        self.org = org
        self.repo = repo
        self.instance_name = name
        # Readable slug keys the state path; the cloud resource name is its hash.
        self.slug = state_slug(identity, org, repo, name)
        self.name = cloud_resource_name(self.slug)
        self.labels = cloud_labels(identity, org, repo, name)
        self.state_key = self.slug
        self.ssh_user = _effective_ssh_user()
        self.provider_label = spec.provider
```

- [ ] **Step 6: Update `select_backend`**

In `src/vergil_tooling/lib/vm_backend.py`, replace `select_backend` (26-43):

```python
def select_backend(
    spec: ComposedSpec,
    *,
    identity: str | None = None,
    org: str | None = None,
    repo: str | None = None,
    name: str | None = None,
) -> Backend:
    """Return the backend for a composed spec — the one dispatch decision point.

    Off-platform specs require ``identity``/``org``/``repo`` to derive the cloud
    resource name and labels; ``name`` selects a named instance (None = default).
    """
    if spec.off_platform:
        if identity is None or org is None or repo is None:
            msg = "off-platform backend requires identity, org, and repo"
            raise ValueError(msg)
        return OffPlatformBackend(spec, identity, org, repo, name)
    return LimaBackend()
```

- [ ] **Step 7: Run the new tests and the existing cloud suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_cloud.py tests/vergil_tooling/test_vm_spec.py::test_state_slug_forms -v`
Expected: PASS. If existing `test_vm_cloud.py` tests call `cloud_resource_name(identity, org, repo)` or `cloud_labels(identity, org, repo)` positionally, update those call sites to the new signatures (the cloud name is now a hash of the slug; assertions on the old dashed name must change to `state_slug(...)` for the state key and `cloud_resource_name(state_slug(...))` for the resource name).

- [ ] **Step 8: Refactor**

Look for:
- Remove the now-unused `_MAX_NAME`/`_HASH_LEN` constants (grep first to confirm no other reference).
- `OffPlatformBackend` should obtain its slug via `state_slug(...)` (done) rather than re-joining segments — confirm no duplicate slug-building remains in `vm_cloud.py`.
- Update every `cloud_resource_name(...)`/`cloud_labels(...)` caller to the new signatures (grep both names across `src/` and `tests/`).

- [ ] **Step 9: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/vm_spec.py src/vergil_tooling/lib/vm_cloud.py src/vergil_tooling/lib/vm_backend.py tests/vergil_tooling/test_vm_spec.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope vm --message "derive readable state slug, hashed cloud name, instance label" \
  --body "state_slug joins the handle with '--' for the state path; cloud_resource_name becomes vrg-<sha256(slug)[:12]>; cloud_labels gains vergil-instance; OffPlatformBackend and select_backend thread the instance name. Ref #1831"
```

---

### Task 5: Sidecar carries `name`; `recover_handle`

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (`write_instance_meta` 284-293; `read_instance_meta` 296-309)
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`recover_triple` 288-300 → `recover_handle`; create-time write 614; `discover_dedicated` 1396-1404; `update --all` site 1116)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Produces:
  - `lima.write_instance_meta(instance, identity, org, repo, name=None)` — payload `{"schema": 2, "identity", "org", "repo", "name"}` (`name` is `""` when absent).
  - `vrg_vm.recover_handle(instance) -> tuple[str, str|None, str|None, str|None]` — `(identity, org, repo, name)`; legacy/parse fallback yields `name=None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_recover_handle_roundtrips_named_instance(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.lib.lima import write_instance_meta
    from vergil_tooling.bin.vrg_vm import recover_handle

    inst = "vergil-user.lmf.mq.cloud-x86"
    write_instance_meta(inst, "vergil-user", "lmf", "mq", "cloud-x86")
    assert recover_handle(inst) == ("vergil-user", "lmf", "mq", "cloud-x86")


def test_recover_handle_default_instance_name_none(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.lib.lima import write_instance_meta
    from vergil_tooling.bin.vrg_vm import recover_handle

    inst = "vergil-user.lmf.mq"
    write_instance_meta(inst, "vergil-user", "lmf", "mq")
    assert recover_handle(inst) == ("vergil-user", "lmf", "mq", None)


def test_recover_handle_parse_fallback_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.bin.vrg_vm import recover_handle

    assert recover_handle("vergil-user.lmf.mq") == ("vergil-user", "lmf", "mq", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "recover_handle" -v`
Expected: FAIL — `cannot import name 'recover_handle'`; `write_instance_meta` takes 4 args.

- [ ] **Step 3: Update the sidecar**

In `src/vergil_tooling/lib/lima.py`, replace `write_instance_meta` (284-293) and `read_instance_meta` (296-309):

```python
def write_instance_meta(
    instance: str, identity: str, org: str, repo: str, name: str | None = None
) -> None:
    """Record the handle beside the instance so a mangled name stays reversible.

    Lives in ``~/.lima/<instance>/``, removed when ``limactl delete --force`` deletes
    that dir. ``name`` is the optional fourth handle segment (empty for the default).
    """
    meta_dir = _serial_dir(instance)
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 2,
        "identity": identity,
        "org": org,
        "repo": repo,
        "name": name or "",
    }
    (meta_dir / _META_FILE).write_text(json.dumps(payload))


def read_instance_meta(instance: str) -> dict[str, object] | None:
    """Return the instance's recorded metadata, or None if no sidecar exists.

    Carries an int ``schema``, string ``identity``/``org``/``repo``, and (schema 2+)
    a string ``name``. Raises on a malformed sidecar rather than silently falling
    back. Schema-1 sidecars (no ``name``) are read as the default instance.
    """
    path = _serial_dir(instance) / _META_FILE
    if not path.exists():
        return None
    data: dict[str, object] = json.loads(path.read_text())
    _ = (data["identity"], data["org"], data["repo"])  # fail loudly on a garbled file
    data.setdefault("name", "")
    return data
```

- [ ] **Step 4: Rename `recover_triple` → `recover_handle`**

In `src/vergil_tooling/bin/vrg_vm.py`, replace `recover_triple` (288-300):

```python
def recover_handle(instance: str) -> tuple[str, str | None, str | None, str | None]:
    """Reverse an instance name into the four-part handle (identity, org, repo, name).

    Prefers the per-instance sidecar (reliable once a long name is truncated+hashed);
    falls back to parsing for legacy short names and base boxes that predate the
    sidecar, where ``name`` is None.
    """
    meta = read_instance_meta(instance)
    if meta is not None:
        name = str(meta.get("name") or "") or None
        return str(meta["identity"]), str(meta["org"]), str(meta["repo"]), name
    ident, org, repo = parse_instance_name(instance)
    return ident, org, repo, None
```

- [ ] **Step 5: Update the three call sites**

(a) Create-time write (line 614) — pass the handle name. In Task 6 the Target gains an `instance_name_arg`; for now keep the default-instance behavior. Replace line 614:

```python
        write_instance_meta(
            target.instance, target.identity_name, target.org, target.repo, target.instance_name_arg
        )
```

If Task 6 has not yet added `Target.instance_name_arg`, temporarily pass `None`; Task 6 wires the field. (Subagent-driven execution does Task 6 after this; if you prefer, pass `None` here and revisit in Task 6 Step 6.)

(b) `discover_dedicated` (1396-1404) — unpack four and carry the name. Replace the loop body:

```python
    rows: list[DedicatedRow] = []
    for name in instances:
        try:
            ident, org, repo, inst_name = recover_handle(name)
        except ValueError:
            continue
        if ident != identity_name or org is None or repo is None:
            continue
        state, stanza = _classify_instance(projects_dir, org, repo, name)
        rows.append(DedicatedRow(org, repo, name, state, stanza, inst_name))
    return rows
```

Add the `instance_name` field to `DedicatedRow` (1343-1349):

```python
@dataclass
class DedicatedRow:
    org: str
    repo: str
    instance: str
    state: str  # "present" | "orphaned"
    stanza: VmStanza | None = None
    instance_name: str | None = None
```

(c) `update --all` site (line 1116) — replace:

```python
                ident, org, repo, _inst_name = recover_handle(inst)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "recover_handle" -v`
Expected: PASS. Then run the full `test_vrg_vm.py` to catch any remaining `recover_triple` references: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -q` and fix any `recover_triple` import in the tests by renaming to `recover_handle` (adjusting expected tuples to four elements).

- [ ] **Step 7: Refactor**

Look for:
- Extract the name-normalization (`str(meta.get("name") or "") or None`) if it reads awkwardly inline.
- Grep `recover_triple` across `src/` and `tests/` to confirm zero remaining references after the rename to `recover_handle`.
- Confirm `read_instance_meta`'s schema-1 back-compat (`setdefault("name", "")`) is exercised by a test or note it.

- [ ] **Step 8: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/lima.py src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "make handle reversal name-aware (recover_handle, sidecar name)" \
  --body "Sidecar payload bumps to schema 2 with a name field; recover_triple becomes recover_handle returning the four-part handle, threaded through discover_dedicated and update --all. Ref #1831"
```

- [ ] **Step 9: Phase 2 full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS.

---

## Phase 3 — CLI `--name` wiring

### Task 6: Add `--name` to verbs and thread it through resolution

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`Target` 104-112; `_add_workspace_arg` 2134-2140 or a new `_add_name_arg`; verb parsers 2151-2255; `_resolve_target` 212-259; `_resolve_instance` 262-278)
- Test: `tests/vergil_tooling/test_vrg_vm.py`, `tests/vergil_tooling/test_vrg_vm_resolve.py`

**Interfaces:**
- Produces:
  - `Target.instance_name_arg: str | None` — the requested instance name (the handle's fourth segment).
  - `--name` flag on `create`/`session`/`rebuild`/`destroy`/`stop`/`start`/`restart`/`update`/`destroy-volume`.
  - `_resolve_target`/`_resolve_instance` read `args.name`, validate the repo segment, compose with `instance=`, and derive the named Lima instance + named backend.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vrg_vm_resolve.py` (mirror the existing resolve-test fixtures there; this asserts the resolver honors `--name`):

```python
def test_resolve_target_named_instance(monkeypatch, named_instance_config):
    # named_instance_config: a fixture building an identities/repo config where
    # vergil-user/lmf/mq declares an off-platform instance "cloud-x86".
    args = _make_args(workspace="lmf/mq", name="cloud-x86", identity="vergil-user")
    target = _resolve_target(args)
    assert target.instance_name_arg == "cloud-x86"
    assert target.instance == "vergil-user.lmf.mq.cloud-x86"
    assert target.spec.off_platform
    assert target.backend.slug == "vergil-user--lmf--mq--cloud-x86"


def test_destroy_volume_named_targets_instance_volume(named_instance_config):
    # destroy-volume --name must resolve the NAMED instance's volume state, not the
    # default's — it is irreversible and billable.
    args = _make_args(
        command="destroy-volume", workspace="lmf/mq", name="cloud-x86", identity="vergil-user"
    )
    target = _resolve_target(args)
    assert target.backend.state_key == "vergil-user--lmf--mq--cloud-x86"


def test_update_named_resolves_instance_lima_name(named_instance_local_config):
    # update (single) routes through _resolve_instance, which must honor --name.
    args = _make_args(
        command="update", workspace="lmf/mq", name="rdqm-rhel", identity="vergil-user"
    )
    _name, _identity, _config, instance = _resolve_instance(args)
    assert instance == "vergil-user.lmf.mq.rdqm-rhel"
```

If `test_vrg_vm_resolve.py` has no reusable fixture/`_make_args`, add a minimal `argparse.Namespace`-based helper and config fixtures following the patterns already in that file (read the file's existing resolve tests first and match them). `_make_args` must accept a `command` kwarg. The behavioral assertions above are the contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm_resolve.py -k "named_instance" -v`
Expected: FAIL — `Target` has no `instance_name_arg`; `_resolve_target` ignores `name`.

- [ ] **Step 3: Add the `Target` field**

In `src/vergil_tooling/bin/vrg_vm.py`, add to `Target` (104-112), as the last field with a default to avoid reordering:

```python
@dataclass
class Target:
    identity_name: str
    identity: Identity
    config: IdentityConfig
    org: str | None
    repo: str | None
    spec: ComposedSpec
    instance: str
    fingerprint: str
    backend: Backend
    instance_name_arg: str | None = None
```

- [ ] **Step 4: Add the `--name` argument helper and attach it to the verbs**

Add near `_add_workspace_arg` (2134):

```python
def _add_name_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Named VM instance for this repo (default: the unnamed default instance). "
            "Must be declared under [vm.<identity>.instances.<name>]."
        ),
    )
```

Call `_add_name_arg(p_*)` for each of: `p_create`, `p_start`, `p_stop`, `p_restart`, `p_update`, `p_destroy`, `p_destroy_volume`, `p_rebuild`, and add `--name` to `p_session` (it builds its own parser without `_add_workspace_arg`; add `p_session.add_argument("--name", default=None, help=...)` alongside its other options). Do **not** add it to `p_list` / `p_volumes` (global enumerators).

- [ ] **Step 5: Thread `--name` through `_resolve_target`**

In `_resolve_target` (212-259), read and validate the name, then compose and derive with it. Replace the body from line 221 onward:

```python
    name, identity, config = _resolve(args)
    workspace = getattr(args, "workspace", None)
    inst_name = getattr(args, "name", None)
    base = _base_footprint(identity)
    org, repo = _workspace_org_repo(workspace)

    if org is None or repo is None:
        if inst_name is not None:
            msg = "--name requires an <org>/<repo> workspace (named instances are per-repo)"
            raise SpecError(msg)
        spec = compose_vm_spec(identity=name, base=base, stanza=None, override=None)
        backend = select_backend(spec, identity=name, org=None, repo=None)
        return Target(
            name, identity, config, None, None, spec, identity.vm_instance, "", backend
        )

    validate_repo_segment(repo)

    requested_vm = _read_repo_vm(identity, org, repo)
    borrow = resolve_borrow(identity, org, repo, requested_vm)
    eff_vm: VmStanza | None
    if borrow is not None:
        if not borrow_allowed:
            raise BorrowError(_borrow_block_msg(args.command, org, repo, borrow.org, borrow.repo))
        eff_org, eff_repo, eff_vm = borrow.org, borrow.repo, borrow.stanza
    else:
        eff_org, eff_repo, eff_vm = org, repo, requested_vm

    override = identity.overrides.get((eff_org, eff_repo))
    spec = compose_vm_spec(
        identity=name, base=base, stanza=eff_vm, override=override, instance=inst_name
    )
    backend = select_backend(
        spec, identity=name, org=eff_org, repo=eff_repo, name=inst_name
    )

    if not spec.dedicated:
        return Target(
            name, identity, config, org, repo, spec, identity.vm_instance, "", backend
        )

    inst = instance_name(name, eff_org, eff_repo, inst_name)
    return Target(
        name,
        identity,
        config,
        eff_org,
        eff_repo,
        spec,
        inst,
        spec_fingerprint(spec),
        backend,
        inst_name,
    )
```

Add `validate_repo_segment` and `state_slug` (if needed later) to the `vm_spec` import block at the top of `vrg_vm.py` (it already imports `compose_vm_spec`, `instance_name`, `spec_fingerprint`, `parse_instance_name`). Add `validate_repo_segment`.

- [ ] **Step 6: Thread `--name` through `_resolve_instance` and the create-time write**

Replace `_resolve_instance` (262-278):

```python
def _resolve_instance(
    args: argparse.Namespace,
) -> tuple[str, Identity, IdentityConfig, str]:
    """Resolve the Lima instance NAME for lifecycle commands (stop/restart).

    Maps an ``org/repo`` (or its absence) plus an optional ``--name`` to a Lima
    instance name. A repo with no readable ``vergil.toml`` still resolves to its own
    instance name so an orphaned dedicated VM stays reachable.
    """
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    inst_name = getattr(args, "name", None)
    if org is not None and repo is not None:
        validate_repo_segment(repo)
        instance = instance_name(name, org, repo, inst_name)
    else:
        instance = identity.vm_instance
    return name, identity, config, instance
```

Confirm the create-time write (Task 5 Step 5a) passes `target.instance_name_arg` — it now exists.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm_resolve.py -k "named" -v`
Expected: PASS (resolve + destroy-volume + update named tests).

- [ ] **Step 8: Refactor**

Look for:
- The repeated `getattr(args, "name", None)` across `_resolve_target`/`_resolve_instance` — consider a tiny `_requested_name(args)` accessor.
- `validate_repo_segment` is now called in two resolvers; ensure consistent placement (right after `org`/`repo` are known).
- `_add_name_arg` is attached to many parsers — confirm one helper, not copy-pasted `add_argument` calls (session is the one intentional exception).

- [ ] **Step 9: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm_resolve.py
vrg-commit --type feat --scope vm --message "add --name flag across lifecycle verbs" \
  --body "Target carries instance_name_arg; --name is wired into create/session/rebuild/destroy/stop/start/restart/update/destroy-volume and threaded through _resolve_target/_resolve_instance into composition, the Lima name, and the backend. Ref #1831"
```

- [ ] **Step 10: Phase 3 full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS. Confirm a bare verb (no `--name`) still resolves the default instance unchanged.

---

## Phase 4 — Recorded-state lifecycle dispatch (highest-risk; most tests)

> Per the spec's sequencing callout, this phase carries the bulk of the lifecycle test surface. Test each branch.

### Task 7: Recorded-state enumerator for a handle

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (add `_recorded_state_for_handle` near `_off_platform_vms` 1623)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Produces: `_recorded_state_for_handle(identity, org, repo, name) -> RecordedState`, a dataclass with:
  - `lima_instance: str | None` — the derived Lima name if that instance exists.
  - `tofu_dirs: list[tuple[str, Path]]` — `(provider, provider_state_dir)` for every `~/.config/vergil/tofu/<slug>/<provider>/` carrying `volume.tfstate` or `vm.tfstate`.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_recorded_state_enumerates_lima_and_tofu(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.bin.vrg_vm import _recorded_state_for_handle, RecordedState
    from vergil_tooling.lib.vm_spec import state_slug

    slug = state_slug("vergil-user", "lmf", "mq", "cloud-x86")
    for provider in ("gcp", "azure"):
        d = tmp_path / ".config" / "vergil" / "tofu" / slug / provider
        d.mkdir(parents=True)
        (d / "vm.tfstate").write_text("{}")
    # Lima instance presence is mocked via list_vms
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_vm.list_vms",
        lambda: [{"name": "vergil-user.lmf.mq.cloud-x86", "status": "Running"}],
    )

    rs = _recorded_state_for_handle("vergil-user", "lmf", "mq", "cloud-x86")
    assert rs.lima_instance == "vergil-user.lmf.mq.cloud-x86"
    assert {p for p, _ in rs.tofu_dirs} == {"gcp", "azure"}


def test_recorded_state_no_lima_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("vergil_tooling.bin.vrg_vm.list_vms", lambda: [])
    from vergil_tooling.bin.vrg_vm import _recorded_state_for_handle

    rs = _recorded_state_for_handle("vergil-user", "lmf", "mq", "cloud-x86")
    assert rs.lima_instance is None
    assert rs.tofu_dirs == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "recorded_state" -v`
Expected: FAIL — `cannot import name '_recorded_state_for_handle'`.

- [ ] **Step 3: Implement the enumerator**

In `src/vergil_tooling/bin/vrg_vm.py`, near `_off_platform_vms` (before line 1623), add (ensure `state_slug` is imported from `vm_spec` at the top, and `instance_name`, `list_vms`, `Path` are in scope — they are):

```python
@dataclass
class RecordedState:
    """Everything actually built under one handle, discovered from disk + Lima."""

    lima_instance: str | None
    tofu_dirs: list[tuple[str, Path]]  # (provider, provider_state_dir)


def _recorded_state_for_handle(
    identity: str, org: str, repo: str, name: str | None
) -> RecordedState:
    """Enumerate the Lima box and every tofu provider state recorded for a handle.

    Acts on reality, not the live profile: the Lima instance named for the handle
    (if it exists) plus every ``~/.config/vergil/tofu/<slug>/<provider>/`` directory
    carrying recorded state. The slug is deterministic, so this is a direct glob of
    the handle's own subtree.
    """
    lima = instance_name(identity, org, repo, name)
    existing = {vm["name"] for vm in list_vms()}
    lima_instance = lima if lima in existing else None

    slug = state_slug(identity, org, repo, name)
    handle_root = Path.home() / ".config" / "vergil" / "tofu" / slug
    tofu_dirs: list[tuple[str, Path]] = []
    if handle_root.is_dir():
        for provider_dir in sorted(p for p in handle_root.iterdir() if p.is_dir()):
            if (provider_dir / "volume.tfstate").exists() or (
                provider_dir / "vm.tfstate"
            ).exists():
                tofu_dirs.append((provider_dir.name, provider_dir))
    return RecordedState(lima_instance, tofu_dirs)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "recorded_state" -v`
Expected: PASS.

- [ ] **Step 5: Refactor**

Look for:
- The `volume.tfstate`/`vm.tfstate` existence check now appears here and in `_off_platform_vms`; if it becomes a third site in Task 8/10, extract a small `_has_tofu_state(provider_dir)` helper.
- Naming: confirm `RecordedState` fields (`lima_instance`, `tofu_dirs`) read clearly at the call sites in Tasks 8/9.

- [ ] **Step 6: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "enumerate all recorded state for a handle" \
  --body "Add _recorded_state_for_handle: the handle's Lima box (if present) plus every <slug>/<provider>/ tofu state dir, discovered from disk and limactl rather than the live profile. Ref #1831"
```

---

### Task 8: `destroy` tears down all recorded backends with a confirmation contract

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_cmd_destroy` 1249-1268; `_cloud_destroy` 1235-1246; `p_destroy` parser 2210-2217)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `_recorded_state_for_handle`, `_resolve`, `vm_cloud.fetch_modules`/`destroy_vm`, `delete_vm`, `resolve_vm_tag`.
- Produces: `_cmd_destroy` enumerates recorded state for the handle and tears down the Lima box and every provider's `vm.tfstate`; prints the listing first; prompts on a TTY; `--yes` proceeds; non-interactive without `--yes` refuses. `--name` and a new `--yes` flag on `p_destroy`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_destroy_refuses_non_interactive_without_yes(monkeypatch, capsys):
    from vergil_tooling.bin import vrg_vm

    rs = vrg_vm.RecordedState(
        lima_instance="vergil-user.lmf.mq.cloud-x86", tofu_dirs=[]
    )
    monkeypatch.setattr(vrg_vm, "_recorded_state_for_handle", lambda *a: rs)
    monkeypatch.setattr(vrg_vm, "_resolve", lambda args: ("vergil-user", _FakeIdentity(), _FakeConfig()))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = _destroy_args(workspace="lmf/mq", name="cloud-x86", yes=False)

    rc = vrg_vm._cmd_destroy(args)
    assert rc == 1
    assert "--yes" in capsys.readouterr().err


def test_destroy_yes_tears_down_lima_and_all_providers(monkeypatch, capsys):
    from vergil_tooling.bin import vrg_vm
    from pathlib import Path

    rs = vrg_vm.RecordedState(
        lima_instance="vergil-user.lmf.mq.cloud-x86",
        tofu_dirs=[("gcp", Path("/x/gcp")), ("azure", Path("/x/azure"))],
    )
    monkeypatch.setattr(vrg_vm, "_recorded_state_for_handle", lambda *a: rs)
    monkeypatch.setattr(vrg_vm, "_resolve", lambda args: ("vergil-user", _FakeIdentity(), _FakeConfig()))
    deleted = []
    destroyed = []
    monkeypatch.setattr(vrg_vm, "delete_vm", lambda i: deleted.append(i))
    monkeypatch.setattr(vrg_vm.vm_cloud, "fetch_modules", lambda tag: Path("/m/modules"))
    monkeypatch.setattr(vrg_vm.vm_cloud, "destroy_vm", lambda root, d: destroyed.append(d))
    monkeypatch.setattr(vrg_vm.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(vrg_vm, "resolve_vm_tag", lambda c, i: "v1")
    args = _destroy_args(workspace="lmf/mq", name="cloud-x86", yes=True)

    rc = vrg_vm._cmd_destroy(args)
    assert rc == 0
    assert deleted == ["vergil-user.lmf.mq.cloud-x86"]
    assert destroyed == [Path("/x/gcp"), Path("/x/azure")]


def test_destroy_nothing_recorded_returns_one(monkeypatch, capsys):
    from vergil_tooling.bin import vrg_vm

    monkeypatch.setattr(
        vrg_vm, "_recorded_state_for_handle",
        lambda *a: vrg_vm.RecordedState(lima_instance=None, tofu_dirs=[]),
    )
    monkeypatch.setattr(vrg_vm, "_resolve", lambda args: ("vergil-user", _FakeIdentity(), _FakeConfig()))
    args = _destroy_args(workspace="lmf/mq", name="cloud-x86", yes=True)
    assert vrg_vm._cmd_destroy(args) == 1
    assert "no recorded" in capsys.readouterr().err.lower()
```

Add the small helpers `_destroy_args(...)`, `_FakeIdentity`, `_FakeConfig` at the top of the test module if not already present, following the existing test fixtures in `test_vrg_vm.py` (read them first; `_destroy_args` builds an `argparse.Namespace(command="destroy", workspace=..., name=..., yes=..., tag="", identity="vergil-user", config=None)`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "destroy_refuses or destroy_yes or destroy_nothing" -v`
Expected: FAIL — current `_cmd_destroy` resolves a single target, ignores recorded state and `--yes`.

- [ ] **Step 3: Rewrite `_cmd_destroy`**

Replace `_cmd_destroy` (1249-1268) and add a teardown helper:

```python
def _destroy_recorded(rs: RecordedState, args: argparse.Namespace, config, identity) -> int:
    """Tear down the Lima box and every recorded provider state for a handle."""
    if rs.lima_instance is not None:
        print(f"Destroying Lima VM '{rs.lima_instance}'...")
        delete_vm(rs.lima_instance)
    if rs.tofu_dirs:
        tag = args.tag if getattr(args, "tag", "") else resolve_vm_tag(config, identity)
        modules_root = vm_cloud.fetch_modules(tag)
        try:
            for provider, state_dir in rs.tofu_dirs:
                print(f"Destroying cloud VM under {provider} (volume preserved): {state_dir}")
                vm_cloud.destroy_vm(modules_root, state_dir)
        finally:
            shutil.rmtree(modules_root.parent, ignore_errors=True)
    print("Destroyed (persistent volumes preserved — use destroy-volume to remove them).")
    return 0


def _cmd_destroy(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    inst_name = getattr(args, "name", None)
    if org is None or repo is None:
        rs = RecordedState(
            lima_instance=(identity.vm_instance if identity.vm_instance in
                           {vm["name"] for vm in list_vms()} else None),
            tofu_dirs=[],
        )
    else:
        validate_repo_segment(repo)
        rs = _recorded_state_for_handle(name, org, repo, inst_name)

    if rs.lima_instance is None and not rs.tofu_dirs:
        print("No recorded state to destroy for this handle.", file=sys.stderr)
        return 1

    # Confirmation contract.
    print("Will destroy the following recorded boxes:")
    if rs.lima_instance:
        print(f"  - Lima: {rs.lima_instance}")
    for provider, state_dir in rs.tofu_dirs:
        print(f"  - {provider}: {state_dir}")
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print(
                "Refusing to destroy multiple recorded boxes non-interactively — "
                "re-run with --yes to confirm.",
                file=sys.stderr,
            )
            return 1
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.", file=sys.stderr)
            return 1

    return _destroy_recorded(rs, args, config, identity)
```

`_cloud_destroy` (1235-1246) is now unused by `destroy`; leave it only if `rebuild` still calls it (it does not — rebuild uses `_cloud_create(..., destroy_first=True)`). Run `grep -n "_cloud_destroy" src/vergil_tooling/bin/vrg_vm.py`; if `_cmd_destroy` was its only caller, delete `_cloud_destroy` to keep lint clean.

- [ ] **Step 4: Add `--yes` to the destroy parser**

In `main()`, after the `p_destroy` `--tag` argument (2213-2217), add:

```python
    p_destroy.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (required for a non-interactive multi-box destroy)",
    )
```

(`--name` was added in Task 6 Step 4.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "destroy_refuses or destroy_yes or destroy_nothing" -v`
Expected: PASS. Then run the existing destroy tests: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k destroy -v` and reconcile any that assumed the old single-target behavior (they should now expect the recorded-state path; update them to the new contract — this is the intended behavior change).

- [ ] **Step 6: Refactor**

Look for:
- `_destroy_recorded`'s fetch-modules / `shutil.rmtree(modules_root.parent)` pattern now mirrors `_cmd_destroy_volume` (and the deleted `_cloud_destroy`); if it is repeated, extract a `_with_fetched_modules(tag)` context manager.
- Confirm the dead `_cloud_destroy` is removed (grep to verify no remaining caller).
- The confirmation listing + prompt could be a single `_confirm_destroy(rs, args)` helper if it clutters `_cmd_destroy`.

- [ ] **Step 7: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "destroy tears down all recorded backends for a handle" \
  --body "_cmd_destroy enumerates recorded state and removes the Lima box plus every recorded provider's VM (volumes preserved), printing the teardown listing and honoring a confirmation contract: prompt on TTY, --yes to proceed, refuse non-interactively without --yes. Dissolves the backend-switch orphan. Ref #1831"
```

---

### Task 9: `stop`/`start`/`restart` resolve by handle, Lima-only

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_cmd_stop` 978-987; `_cmd_start` 922-957 already uses `_resolve_target`; `_cmd_restart` 990+)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- `stop`/`start`/`restart` keep rejecting off-platform (`_reject_if_off_platform` / `_EPHEMERAL_MSG`) and now address the handle's Lima instance via `--name`. No new enumeration — the `--name` threading from Task 6 already makes `_resolve_instance`/`_resolve_target` handle-aware; this task adds the regression test that confirms the Lima-only contract holds under a named instance.

- [ ] **Step 1: Write the failing/contract test**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_stop_named_instance_targets_handle_lima(monkeypatch, capsys):
    from vergil_tooling.bin import vrg_vm

    monkeypatch.setattr(vrg_vm, "_reject_if_off_platform", lambda args: False)
    monkeypatch.setattr(
        vrg_vm, "_resolve_instance",
        lambda args: ("vergil-user", _FakeIdentity(), _FakeConfig(), "vergil-user.lmf.mq.cloud-x86"),
    )
    stopped = []
    monkeypatch.setattr(vrg_vm, "stop_vm", lambda i: stopped.append(i))
    args = _stop_args(workspace="lmf/mq", name="cloud-x86")
    assert vrg_vm._cmd_stop(args) == 0
    assert stopped == ["vergil-user.lmf.mq.cloud-x86"]


def test_start_named_off_platform_still_rejected(monkeypatch, capsys):
    from vergil_tooling.bin import vrg_vm

    monkeypatch.setattr(
        vrg_vm, "_resolve_target",
        lambda args, **k: _FakeTarget(off_platform=True),
    )
    args = _start_args(workspace="lmf/mq", name="cloud-x86")
    assert vrg_vm._cmd_start(args) == 1
    assert "ephemeral" in capsys.readouterr().err.lower()
```

Add `_stop_args`/`_start_args`/`_FakeTarget` helpers matching the module's existing fixtures (a `_FakeTarget` exposing `.spec.off_platform`).

- [ ] **Step 2: Run tests to verify behavior**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "stop_named or start_named_off_platform" -v`
Expected: the stop test may already PASS (Task 6 made `_resolve_instance` name-aware); the off-platform rejection test should PASS unchanged. If the stop test fails because `_cmd_stop`'s message text differs, that is fine — adjust the test to the actual message, not the code. The point is the contract: stop targets the handle's Lima instance, start/restart reject off-platform.

- [ ] **Step 3: Confirm no code change is needed (or make the minimal one)**

`_cmd_stop` already calls `_resolve_instance(args)`, which is now handle-aware. `_cmd_start`/`_cmd_restart` already call `_resolve_target`, now handle-aware, and reject off-platform. If all three behave correctly, no production change is required — this task is the regression lock. If a verb still drops `--name`, ensure its handler reads through the updated resolvers (it should, after Task 6).

- [ ] **Step 4: Run the broader stop/start/restart suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "stop or start or restart" -v`
Expected: PASS.

- [ ] **Step 5: Refactor**

Look for:
- This is a test-only task — no production refactor expected. Reuse the existing `_FakeTarget`/`_FakeIdentity` fixtures rather than re-declaring them; grep the test module first.
- If a production change was needed (a verb dropping `--name`), recheck it routes through the shared resolvers rather than re-deriving the instance.

- [ ] **Step 6: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type test --scope vm --message "lock stop/start/restart to the handle's Lima box" \
  --body "Regression tests proving stop/start/restart address the handle's Lima instance via --name and continue to reject off-platform (no cloud stop/start). Ref #1831"
```

- [ ] **Step 7: Phase 4 full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS.

---

## Phase 5 — `list` and `volumes` surfaces

### Task 10: `list` INSTANCE column + `no-vm` volume rows

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_cmd_list` 1680-1717; `_list_rows`; `_cloud_list_rows` 1657-1677; `OffPlatformVm` ~1580; `_off_platform_vms` 1623-1654)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- `OffPlatformVm` gains `instance: str | None` (from the `vergil-instance` label).
- `_cloud_list_rows` emits an `instance` field and a `no-vm` STATUS row carrying the volume size when the volume state exists but the VM is gone.
- `_cmd_list` prints an INSTANCE column between SCOPE and BACKEND; local rows pass the dedicated row's `instance_name` (from Task 5's `DedicatedRow.instance_name`), `—` for the default.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_list_shows_instance_column_and_no_vm_row(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.bin import vrg_vm

    # Off-platform: a volume exists, VM is gone -> no-vm row with size.
    monkeypatch.setattr(
        vrg_vm, "_off_platform_vms",
        lambda: [
            vrg_vm.OffPlatformVm(
                name="vergil-user--lmf--mq--native-ha", provider="gcp",
                state_dir=tmp_path, identity="vergil-user", org="lmf", repo="mq",
                instance="native-ha", status="", volume_size="200GiB", vm_present=False,
            )
        ],
    )
    monkeypatch.setattr(vrg_vm, "list_vms", lambda: [])
    monkeypatch.setattr(vrg_vm, "load_config", lambda p: _empty_config())
    args = _list_args()
    assert vrg_vm._cmd_list(args) == 0
    out = capsys.readouterr().out
    assert "INSTANCE" in out
    assert "native-ha" in out
    assert "no-vm" in out
    assert "200GiB" in out
```

(The exact `OffPlatformVm` constructor fields are finalized in Step 3 — keep the test in sync.)

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "instance_column" -v`
Expected: FAIL — no INSTANCE column; `OffPlatformVm` has no `instance`/`volume_size`/`vm_present`.

- [ ] **Step 3: Extend `OffPlatformVm` and `_off_platform_vms`**

Read the `OffPlatformVm` dataclass (just above `_off_platform_vms`, ~line 1580) and add fields: `instance: str | None = None`, `volume_size: str | None = None`, `vm_present: bool = True`. Update its `scope` property (which currently uses identity/org/repo) to leave instance to a separate column.

In `_off_platform_vms` (1623-1654), populate the new fields from the parsed volume state and a VM-presence check:

```python
    for volume_state in sorted(tofu_root.glob("*/*/volume.tfstate")):
        provider_dir = volume_state.parent
        provider = provider_dir.name
        state_key = provider_dir.parent.name
        parsed = vm_cloud.parse_volume_state(volume_state)
        labels = parsed.labels if parsed else {}
        size = f"{parsed.size_gib}GiB" if parsed and parsed.size_gib else None
        vm_present = (provider_dir / "vm.tfstate").exists()
        status = _cloud_status(provider_dir, state_key) if vm_present else ""
        vms.append(
            OffPlatformVm(
                name=state_key,
                provider=provider,
                state_dir=provider_dir,
                identity=labels.get("vergil-identity"),
                org=labels.get("vergil-org"),
                repo=labels.get("vergil-repo"),
                instance=labels.get("vergil-instance"),
                status=status,
                volume_size=size,
                vm_present=vm_present,
            )
        )
```

- [ ] **Step 4: Emit the INSTANCE column and `no-vm` rows in the list renderers**

Update `_cloud_list_rows` (1657-1677) to carry instance, size, and the no-vm status:

```python
    rows: list[dict[str, object]] = []
    for vm in _off_platform_vms():
        if not vm.vm_present:
            status = "no-vm"
            disk = vm.volume_size or "—"
        else:
            status = vm.status or f"unknown (no {vm.provider} creds)"
            disk = "—"
        rows.append(
            {
                "identity": vm.identity or "—",
                "scope": vm.scope,
                "instance": vm.instance or "—",
                "backend": vm.provider,
                "status": status,
                "disk": disk,
            }
        )
    return rows
```

In `_cmd_list` (1680-1717), add INSTANCE to the header and both row loops. Replace the header and loops:

```python
    header = (
        f"{'IDENTITY':<14} {'SCOPE':<40} {'INSTANCE':<11} {'BACKEND':<13} {'STATUS':<11} "
        f"{'CPUS':<5} {'MEM':<7} {'DISK':<7} {'AGENTS':<7} {'HUMANS':<7} {'SPEC':<22}"
    )
    print(header)
    print("─" * len(header))

    for id_name, identity in config.identities.items():
        for r in _list_rows(id_name, identity, discovered[id_name], status, probes):
            print(
                f"{id_name:<14} {r['scope']!s:<40} {r.get('instance', '—')!s:<11} "
                f"{r['backend']!s:<13} {r['status']!s:<11} "
                f"{r['cpus']!s:<5} {r['memory']!s:<7} {r['disk']!s:<7} "
                f"{r['agents']!s:<7} {r['humans']!s:<7} {r['spec']!s:<22}"
            )

    for r in _cloud_list_rows():
        print(
            f"{r['identity']!s:<14} {r['scope']!s:<40} {r['instance']!s:<11} "
            f"{r['backend']!s:<13} {r['status']!s:<11} "
            f"{'—':<5} {'—':<7} {r['disk']!s:<7} {'—':<7} {'—':<7} {'—':<22}"
        )

    return 0
```

In `_list_rows` (the local-row builder — find it with `grep -n "def _list_rows"`), set each row's `instance` from the `DedicatedRow.instance_name` (`—` for base/default). Locate where it builds the per-`DedicatedRow` dict and add `"instance": row.instance_name or "—"`; base rows set `"instance": "—"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "instance_column or list" -v`
Expected: PASS. Reconcile any existing `list` tests that assert the old header/column count — update them to include INSTANCE (intended change).

- [ ] **Step 6: Refactor**

Look for:
- The per-row format string with the INSTANCE column is now duplicated between the local-row and cloud-row loops in `_cmd_list` (and again in `volumes`, Task 12). Extract a single row-formatting helper / shared width constants.
- `OffPlatformVm`'s new fields (`instance`, `volume_size`, `vm_present`) need defaults so existing constructions stay valid — grep `OffPlatformVm(` and confirm.

- [ ] **Step 7: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "add INSTANCE column and no-vm volume rows to list" \
  --body "list gains an INSTANCE column (from DedicatedRow.instance_name and the vergil-instance label) and enumerates from volume state: a surviving volume with no VM shows a no-vm row carrying its size, one row per recorded (slug, provider). Ref #1831"
```

---

### Task 11: `list` off-platform orphan classification (SPEC column for cloud rows)

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (add `split_state_slug` near `state_slug`)
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (add `_classify_off_platform`; thread it into `_cloud_list_rows` 1657-1677 and `_cmd_list` 1711-1715)
- Test: `tests/vergil_tooling/test_vm_spec.py`, `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `_off_platform_vms` (now carrying `instance`/`vm_present` from Task 10), `read_config`, `compose_vm_spec`, `_base_footprint`, `config.identities`.
- Produces:
  - `vm_spec.split_state_slug(slug) -> tuple[str, str|None, str|None, str|None]` — exact reverse of `state_slug` via `split('--')` (1 segment = base, 3 = default dedicated, 4 = named); unambiguous because identity/org/repo contain no `--`.
  - `vrg_vm._classify_off_platform(vm, config) -> str` — `"ok"` / `"orphaned"`: recovers the exact handle from `vm.name` (the readable slug), composes the repo's current profile for that handle, and returns `"orphaned"` when the repo dropped the stanza, no longer declares the instance, or no longer composes that `(off-platform, provider)`.
  - `_cloud_list_rows(config)` now emits a real `spec` field; `_cmd_list` prints it for cloud rows instead of `—`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_spec.py`:

```python
from vergil_tooling.lib.vm_spec import split_state_slug


def test_split_state_slug_roundtrips():
    assert split_state_slug("vergil-user") == ("vergil-user", None, None, None)
    assert split_state_slug("vergil-user--lmf--mq") == ("vergil-user", "lmf", "mq", None)
    assert split_state_slug("vergil-user--lmf--mq--cloud-x86") == (
        "vergil-user", "lmf", "mq", "cloud-x86",
    )
```

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_classify_off_platform_orphaned_when_provider_switched(monkeypatch, tmp_path):
    from vergil_tooling.bin import vrg_vm

    # The repo's current profile composes off-platform/azure, but the recorded box is gcp.
    vm = vrg_vm.OffPlatformVm(
        name="vergil-user--lmf--mq--cloud-x86", provider="gcp", state_dir=tmp_path,
        identity="vergil-user", org="lmf", repo="mq", instance="cloud-x86",
        status="Running", volume_size=None, vm_present=True,
    )
    config = _config_with_azure_instance("vergil-user", "lmf", "mq", "cloud-x86")
    assert vrg_vm._classify_off_platform(vm, config) == "orphaned"


def test_classify_off_platform_orphaned_when_stanza_dropped(monkeypatch, tmp_path):
    from vergil_tooling.bin import vrg_vm

    vm = vrg_vm.OffPlatformVm(
        name="vergil-user--lmf--mq--cloud-x86", provider="gcp", state_dir=tmp_path,
        identity="vergil-user", org="lmf", repo="mq", instance="cloud-x86",
        status="Running", volume_size=None, vm_present=True,
    )
    config = _config_no_repo_vm("vergil-user", "lmf", "mq")  # vergil.toml gone / no [vm]
    assert vrg_vm._classify_off_platform(vm, config) == "orphaned"


def test_classify_off_platform_ok_when_matches(monkeypatch, tmp_path):
    from vergil_tooling.bin import vrg_vm

    vm = vrg_vm.OffPlatformVm(
        name="vergil-user--lmf--mq--cloud-x86", provider="gcp", state_dir=tmp_path,
        identity="vergil-user", org="lmf", repo="mq", instance="cloud-x86",
        status="Running", volume_size=None, vm_present=True,
    )
    config = _config_with_gcp_instance("vergil-user", "lmf", "mq", "cloud-x86")
    assert vrg_vm._classify_off_platform(vm, config) == "ok"
```

Add the `_config_with_azure_instance` / `_config_with_gcp_instance` / `_config_no_repo_vm` helpers following the config fixtures already in `test_vrg_vm.py` (they build an `IdentityConfig` whose identity has a `projects_dir` containing a `vergil.toml` with the named instance overlay; read the existing config fixtures first and match them).

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k split_state_slug tests/vergil_tooling/test_vrg_vm.py -k classify_off_platform -v`
Expected: FAIL — `split_state_slug` import error; `_classify_off_platform` undefined.

- [ ] **Step 3: Add `split_state_slug`**

In `src/vergil_tooling/lib/vm_spec.py`, after `state_slug`, add:

```python
def split_state_slug(slug: str) -> tuple[str, str | None, str | None, str | None]:
    """Reverse state_slug. 1 segment = base; 3 = default dedicated; 4 = named instance.

    Unambiguous because identity/org/repo never contain '--' (repo names with '--'
    are rejected at parse time), so this is the exact inverse — no labels needed.
    """
    parts = slug.split(_SLUG_SEP)
    if len(parts) == 1:
        return parts[0], None, None, None
    if len(parts) == 3:  # noqa: PLR2004
        return parts[0], parts[1], parts[2], None
    if len(parts) == 4:  # noqa: PLR2004
        return parts[0], parts[1], parts[2], parts[3]
    msg = f"unparseable state slug: {slug!r}"
    raise ValueError(msg)
```

- [ ] **Step 4: Implement `_classify_off_platform`**

In `src/vergil_tooling/bin/vrg_vm.py`, near `_cloud_list_rows`, add (ensure `split_state_slug`, `compose_vm_spec`, `read_config`, `ConfigError`, `SpecError`, `_base_footprint` are imported/in scope):

```python
def _classify_off_platform(vm: OffPlatformVm, config: IdentityConfig) -> str:
    """Classify a recorded off-platform box against the repo's current profile.

    'orphaned' when the repo dropped its [vm], no longer declares this instance, or
    no longer composes this (off-platform, provider); 'ok' when it still matches.
    The handle is recovered exactly from the readable slug — no lossy label round-trip.
    """
    identity_name, org, repo, inst_name = split_state_slug(vm.name)
    if org is None or repo is None:
        return "ok"  # a base box carries no per-repo spec
    identity = config.identities.get(identity_name)
    if identity is None:
        return "orphaned"
    repo_dir = Path(identity.projects_dir) / org / repo
    if not (repo_dir / "vergil.toml").exists():
        return "orphaned"
    try:
        stanza = read_config(repo_dir).vm
        spec = compose_vm_spec(
            identity=identity_name,
            base=_base_footprint(identity),
            stanza=stanza,
            override=identity.overrides.get((org, repo)),
            instance=inst_name,
        )
    except (ConfigError, SpecError):
        return "orphaned"
    if not spec.off_platform or spec.provider != vm.provider:
        return "orphaned"
    return "ok"
```

- [ ] **Step 5: Thread the SPEC into the cloud list rows**

Change `_cloud_list_rows` to accept `config` and emit `spec`. Update its signature and the row dict (building on Task 10's version):

```python
def _cloud_list_rows(config: IdentityConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for vm in _off_platform_vms():
        if not vm.vm_present:
            status = "no-vm"
            disk = vm.volume_size or "—"
            spec = _classify_off_platform(vm, config)
        else:
            status = vm.status or f"unknown (no {vm.provider} creds)"
            disk = "—"
            spec = _classify_off_platform(vm, config)
        rows.append(
            {
                "identity": vm.identity or "—",
                "scope": vm.scope,
                "instance": vm.instance or "—",
                "backend": vm.provider,
                "status": status,
                "disk": disk,
                "spec": spec,
            }
        )
    return rows
```

In `_cmd_list`, pass `config` and print `spec` for cloud rows. Replace the cloud-row loop:

```python
    for r in _cloud_list_rows(config):
        print(
            f"{r['identity']!s:<14} {r['scope']!s:<40} {r['instance']!s:<11} "
            f"{r['backend']!s:<13} {r['status']!s:<11} "
            f"{'—':<5} {'—':<7} {r['disk']!s:<7} {'—':<7} {'—':<7} {r['spec']!s:<22}"
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vm_spec.py -k split_state_slug tests/vergil_tooling/test_vrg_vm.py -k "classify_off_platform or instance_column" -v`
Expected: PASS.

- [ ] **Step 7: Refactor**

Look for:
- `_classify_off_platform` and the local `_classify_instance` both answer "does the repo still compose this handle?"; if the compose+compare logic overlaps cleanly, extract a shared helper (but keep the off-platform provider comparison distinct).
- Note the deliberate symmetry between `split_state_slug` (`--`) and `parse_instance_name` (`.`) — document it, but do NOT merge them; the delimiters and segment rules differ.

- [ ] **Step 8: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/lib/vm_spec.py src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vm_spec.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "classify off-platform orphans in list SPEC column" \
  --body "Add split_state_slug (exact handle reversal from the readable slug) and _classify_off_platform: a recorded cloud box whose repo dropped the stanza or switched (backend, provider) now reads 'orphaned' in list, removable via destroy --name X. Closes acceptance #4/#6 for the cloud path. Ref #1831"
```

---

### Task 12: `volumes` INSTANCE column

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_volume_rows`; `_cmd_volumes` 1807-1843)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- `_volume_rows` emits an `instance` field (from the `vergil-instance` label); `_cmd_volumes` prints an INSTANCE column so sibling instances of one repo are distinguishable.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vrg_vm.py`:

```python
def test_volumes_shows_instance_column(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from vergil_tooling.bin import vrg_vm

    monkeypatch.setattr(
        vrg_vm, "_volume_rows",
        lambda: [
            {
                "identity": "vergil-user", "scope": "lmf/mq", "instance": "cloud-x86",
                "name": "vrg-abc123", "size": "300GiB", "zone": "us-central1-b",
                "region": "us-central1", "provider": "gcp",
            }
        ],
    )
    args = vrg_vm.argparse.Namespace(live=False)
    assert vrg_vm._cmd_volumes(args) == 0
    out = capsys.readouterr().out
    assert "INSTANCE" in out
    assert "cloud-x86" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "volumes_shows_instance" -v`
Expected: FAIL — no INSTANCE column in `volumes` output.

- [ ] **Step 3: Add `instance` to `_volume_rows`**

Find `_volume_rows` (`grep -n "def _volume_rows"`). It builds rows from `_off_platform_vms()` or directly from volume state labels. Add `"instance": <vergil-instance label or "—">` to each row dict (the label is now populated on `OffPlatformVm.instance` from Task 10, or read directly from `parsed.labels.get("vergil-instance")`).

- [ ] **Step 4: Render the INSTANCE column in `_cmd_volumes`**

In `_cmd_volumes` (1807-1843), add the column to the header and rows. Replace the header/width block and the per-row print:

```python
    scope_w = max([24, *(len(str(r["scope"])) for r in rows)])
    name_w = max([20, *(len(str(r["name"])) for r in rows)])
    inst_w = max([10, *(len(str(r.get("instance", "—"))) for r in rows)])
    header = (
        f"{'IDENTITY':<14} {'ORG/REPO':<{scope_w}} {'INSTANCE':<{inst_w}} "
        f"{'DISK NAME':<{name_w}} {'SIZE':<8} {'ZONE':<16} {'REGION':<14}"
    )
    if live:
        header += f" {'LIVE':<22}"
    print(header)
    print("─" * len(header))
    for r in rows:
        line = (
            f"{r['identity']!s:<14} {r['scope']!s:<{scope_w}} "
            f"{r.get('instance', '—')!s:<{inst_w}} {r['name']!s:<{name_w}} "
            f"{r['size']!s:<8} {r['zone']!s:<16} {r['region']!s:<14}"
        )
        if live:
            line += f" {r['live']!s:<22}"
        print(line)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -k "volumes" -v`
Expected: PASS. Reconcile existing `volumes` tests that assert the old header (intended change).

- [ ] **Step 6: Refactor**

Look for:
- The INSTANCE column width/format here duplicates Task 10's `list` rendering; consolidate into the shared column helper introduced in Task 10 (or introduce it now and back-apply).
- `_volume_rows`' `instance` field should source the `vergil-instance` label the same way `_off_platform_vms` does — avoid a second, divergent label lookup.

- [ ] **Step 7: Commit**

```bash
cd .worktrees/issue-1831-named-instances
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm --message "add INSTANCE column to volumes" \
  --body "vrg-vm volumes shows an INSTANCE column (from the vergil-instance label) so two volumes for one repo are distinguishable. Ref #1831"
```

- [ ] **Step 8: Phase 5 full validation + final sweep**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS (whole suite green).

Then do a manual smoke check of backward compatibility:
Run: `vrg-container-run -- uv run vrg-vm list` and `... vrg-vm volumes` to confirm headers render and existing (unnamed) rows are unaffected.

---

## Final acceptance check (map to the spec's acceptance criteria)

After Task 12, confirm each spec acceptance criterion has coverage:

1. `--name X` lifecycle independence → Tasks 6, 8, 9.
2. Two instances co-exist → Tasks 3, 4 (distinct Lima names, slugs, hashed cloud names, volumes).
3. Bare verb = default unchanged → Tasks 2, 6 (default composition + no-`--name` path); regression suite.
4. In-place backend/provider edit + `destroy --name X` tears down all recorded state; dropped stanza → `orphaned` → Tasks 7, 8, 10, 11.
5. Invalid instance name / repo `--` rejected at parse time → Tasks 1, 2, 6.
6. `list` INSTANCE + `no-vm` rows + per-instance orphan classification, O(instances) → Tasks 10, 11.
7. `--name X` against a missing instance errors with available names → Task 2.
8. Deterministic `vrg-<hash>` cloud name + identity labels → Task 4.
9. Full existing suite green → every phase's `vrg-validate`.

## PR handoff

After all tasks land and `vrg-validate` is green, record PR metadata via the oracle (agents do **not** run `vrg-submit-pr`):

```bash
cd .worktrees/issue-1831-named-instances
vrg-pr-workflow report-ready \
  --title "feat(vm): multiple VM instances per repo (named instances)" \
  --summary "<paraphrased summary of the change>" \
  --notes "<test evidence + reconciliations>" \
  --linkage Ref
```

Then hand off to the human to run `vrg-submit-pr`.
