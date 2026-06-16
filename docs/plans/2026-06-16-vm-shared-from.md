# `[vm] shared_from` Borrowing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one repo borrow another repo's identity-VM by declaring `shared_from = "org/repo"` in its `[vm]` stanza, so `vrg-vm session <org>/<borrower>` shells into the lender's box while `cd`-ing into the borrower's checkout.

**Architecture:** Parsing gains a `VmStanza.shared_from` field (mutually exclusive with every other `[vm]` key). In `vrg_vm.py`, a `resolve_borrow` helper turns a borrower into its lender; `_resolve_target` redirects instance + spec to the lender for **USE** commands (`session`, `start`) and raises `BorrowError` for **MANAGE** commands (`create`, `rebuild`); `_resolve_instance` raises `BorrowError` for its MANAGE commands (`stop`, `restart`, `update`, `destroy`). `BorrowError` is caught in `main` and printed as a clean exit-1 error. The session working directory is unaffected — it always derives from `args.workspace`.

**Tech Stack:** Python 3.12+, `tomllib`, `argparse`, `pytest`, `dataclasses`.

**Spec:** `docs/specs/2026-06-16-vm-shared-from-design.md`

> **Key-name note:** the spec renders the key as `shared-from` (hyphen) in prose, but every existing `[vm]` key uses underscores (`stale_days`, `vagrant_plugins`, `port_forwards`). This plan uses **`shared_from`** for consistency; Task 5 fixes the spec and reference doc to match.

> **Running tests:** per-step commands use `uv run pytest …` (works in the dev container and under the `.venv-host` dev-tree override). The canonical full gate is `vrg-container-run -- vrg-validate`; run it once at the end (Task 6). All `git` operations use `vrg-git`; commit with `vrg-commit`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/vergil_tooling/lib/config.py` | Parse & validate `vergil.toml`, incl. `[vm]` | Add `VmStanza.shared_from` field; parse + validate `shared_from` (format, mutual exclusivity, role rejection) |
| `src/vergil_tooling/bin/vrg_vm.py` | Resolve VM targets & run lifecycle commands | Add `BorrowError`, `Borrow`, `_read_repo_vm`, `resolve_borrow`, `_borrow_block_msg`; redirect/block in `_resolve_target` & `_resolve_instance`; catch in `main` |
| `tests/vergil_tooling/test_config.py` | Config-parser tests | Add `shared_from` parse/validation tests |
| `tests/vergil_tooling/test_vrg_vm.py` | Target-resolution & command tests | Add borrow redirect (USE) + block (MANAGE) tests |
| `docs/site/docs/reference/vm-spec.md` | User-facing `[vm]` reference | Document `shared_from` |
| `docs/specs/2026-06-16-vm-shared-from-design.md` | Design spec | Fix `shared-from` → `shared_from` |

---

## Task 1: Parse and validate `shared_from` in `config.py`

**Files:**
- Modify: `src/vergil_tooling/lib/config.py` (`VmStanza` dataclass ~line 122; `_parse_role_overlay` ~line 158; `parse_vm_stanza` ~line 175)
- Test: `tests/vergil_tooling/test_config.py` (`TestParseVmStanza` class ~line 665)

- [ ] **Step 1: Write the failing tests**

Add these methods inside the existing `class TestParseVmStanza:` in `tests/vergil_tooling/test_config.py`. `ConfigError` is already imported at the top of the file via the `from vergil_tooling.lib.config import (...)` block — add `ConfigError` to that import list if it is not already present.

```python
    def test_shared_from_parsed_to_org_repo(self) -> None:
        stanza = parse_vm_stanza({"vm": {"shared_from": "lmf/mq-resiliency-lab"}})
        assert stanza is not None
        assert stanza.shared_from == ("lmf", "mq-resiliency-lab")
        assert stanza.packages == []
        assert stanza.roles == {}

    def test_shared_from_absent_is_none(self) -> None:
        stanza = parse_vm_stanza({"vm": {"packages": []}})
        assert stanza is not None
        assert stanza.shared_from is None

    def test_shared_from_not_flagged_unrecognized(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parse_vm_stanza({"vm": {"shared_from": "lmf/mq"}})
        assert "unrecognized" not in capsys.readouterr().err

    def test_shared_from_bare_repo_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "mq-resiliency-lab"}})

    def test_shared_from_empty_side_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/"}})

    def test_shared_from_extra_slash_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq/extra"}})

    def test_shared_from_whitespace_rejected(self) -> None:
        with pytest.raises(ConfigError, match="whitespace"):
            parse_vm_stanza({"vm": {"shared_from": "lmf / mq"}})

    def test_shared_from_non_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            parse_vm_stanza({"vm": {"shared_from": 123}})

    def test_shared_from_with_footprint_key_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "cpus": 8}})

    def test_shared_from_with_packages_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "packages": ["x"]}})

    def test_shared_from_with_role_overlay_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "vergil-user": {"cpus": 8}}})

    def test_shared_from_inside_role_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from is not allowed in a role"):
            parse_vm_stanza({"vm": {"vergil-user": {"shared_from": "lmf/mq"}}})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_config.py::TestParseVmStanza -v -k shared_from`
Expected: FAIL — `TypeError: VmStanza.__init__() got an unexpected keyword argument 'shared_from'` / `AttributeError`, and the `ConfigError` cases not raising.

- [ ] **Step 3: Add the `shared_from` field to `VmStanza`**

In `src/vergil_tooling/lib/config.py`, update the `VmStanza` dataclass (the `RoleOverlay` dataclass is left unchanged — roles cannot borrow):

```python
@dataclass
class VmStanza:
    packages: list[str]
    cpus: int | None
    memory: str | None
    disk: str | None
    stale_days: int | None
    apt_repos: list[dict[str, str]]
    vagrant_plugins: list[str]
    port_forwards: list[str]
    roles: dict[str, RoleOverlay]
    nested: bool | None = None
    shared_from: tuple[str, str] | None = None
```

- [ ] **Step 4: Add the `shared_from` parser/validator and constants**

In `src/vergil_tooling/lib/config.py`, immediately above `_parse_role_overlay` (~line 158), add:

```python
_SHARED_FROM_KEY = "shared_from"
_ORG_REPO_PARTS = 2


def _parse_shared_from(value: Any, source: str) -> tuple[str, str]:
    """Validate a ``[vm].shared_from`` value and return ``(org, repo)``."""
    if not isinstance(value, str):
        msg = f"{source}: [vm].shared_from must be a string"
        raise ConfigError(msg)
    if any(c.isspace() for c in value):
        msg = f"{source}: [vm].shared_from must not contain whitespace (got {value!r})"
        raise ConfigError(msg)
    parts = value.split("/")
    if len(parts) != _ORG_REPO_PARTS or not all(parts):
        msg = f"{source}: [vm].shared_from must be 'org/repo' (got {value!r})"
        raise ConfigError(msg)
    return (parts[0], parts[1])
```

- [ ] **Step 5: Reject `shared_from` inside a role overlay**

In `_parse_role_overlay`, add the guard as the first statement of the function body (before the existing `for key in raw:` loop):

```python
def _parse_role_overlay(name: str, raw: dict[str, Any], source: str = CONFIG_FILE) -> RoleOverlay:
    if _SHARED_FROM_KEY in raw:
        msg = f"{source}: shared_from is not allowed in a role overlay [vm.{name}]"
        raise ConfigError(msg)
    for key in raw:
        if key not in _VM_KEYS:
            print(f"{source}: unrecognized key '{key}' in [vm.{name}]", file=sys.stderr)
    ...
```

(Leave the rest of `_parse_role_overlay` unchanged.)

- [ ] **Step 6: Parse + enforce mutual exclusivity in `parse_vm_stanza`**

Replace the body of `parse_vm_stanza` (the loop and the `return`) so it recognizes `shared_from` and enforces exclusivity:

```python
def parse_vm_stanza(raw: dict[str, Any], source: str = CONFIG_FILE) -> VmStanza | None:
    """Parse the repo ``[vm]`` cascade. Returns None when no ``[vm]`` section exists."""
    vm_raw = raw.get("vm")
    if vm_raw is None:
        return None
    roles: dict[str, RoleOverlay] = {}
    fields: dict[str, Any] = {}
    shared_from: tuple[str, str] | None = None
    for key, value in vm_raw.items():
        if key == _SHARED_FROM_KEY:
            shared_from = _parse_shared_from(value, source)
        elif isinstance(value, dict):
            roles[key] = _parse_role_overlay(key, value, source)
        elif key in _VM_KEYS:
            fields[key] = value
        else:
            print(f"{source}: unrecognized key '{key}' in [vm]", file=sys.stderr)

    if shared_from is not None and (fields or roles):
        offenders = sorted([*fields, *(f"[vm.{r}]" for r in roles)])
        msg = (
            f"{source}: [vm].shared_from cannot be combined with other [vm] keys "
            f"({', '.join(offenders)}); a repo either describes a VM or borrows one"
        )
        raise ConfigError(msg)

    return VmStanza(
        packages=list(fields.get("packages", [])),
        cpus=fields.get("cpus"),
        memory=fields.get("memory"),
        disk=fields.get("disk"),
        stale_days=fields.get("stale_days"),
        apt_repos=list(fields.get("apt_repos", [])),
        vagrant_plugins=list(fields.get("vagrant_plugins", [])),
        port_forwards=list(fields.get("port_forwards", [])),
        roles=roles,
        nested=fields.get("nested"),
        shared_from=shared_from,
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_config.py::TestParseVmStanza -v`
Expected: PASS (all existing + new `shared_from` tests).

- [ ] **Step 8: Commit**

```bash
vrg-git add src/vergil_tooling/lib/config.py tests/vergil_tooling/test_config.py
vrg-commit --type feat --scope config \
  --message "parse and validate [vm] shared_from" \
  --body "Add VmStanza.shared_from (org, repo), parsed from a fully-qualified 'org/repo' string. Mutually exclusive with every other [vm] key and rejected inside role overlays. Refs #1668."
```

---

## Task 2: Borrow resolution helpers in `vrg_vm.py`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (after the `Target` dataclass ~line 99; imports already include `ConfigError, VmStanza, read_config` and `resolve_workspace`)
- Test: `tests/vergil_tooling/test_vrg_vm.py` (after `TestResolveTarget` ~line 1835, reusing `_identities`/`_make_repo`/`_args`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vrg_vm.py`. First extend the import from `vergil_tooling.bin.vrg_vm` (the existing `from vergil_tooling.bin.vrg_vm import (...)` block) to add four names — `BorrowError`, `resolve_borrow`, `_read_repo_vm`, and `_resolve` (none are currently imported there). Then add this class after `TestResolveTarget`:

```python
_LENDER_VM = '\n[vm]\npackages = ["qemu-system-x86"]\ncpus = 12\n'
_BORROW_VM = '\n[vm]\nshared_from = "lmf/lab"\n'


class TestResolveBorrow:
    def test_no_shared_from_returns_none(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _LENDER_VM)
        cfg = _identities(tmp_path, projects)
        from vergil_tooling.lib.config import read_config

        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = read_config(projects / "lmf" / "lab").vm
        assert resolve_borrow(identity, "lmf", "lab", requested_vm) is None

    def test_borrow_resolves_to_lender(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _LENDER_VM)
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        borrow = resolve_borrow(identity, "lmf", "tooling", requested_vm)
        assert borrow is not None
        assert (borrow.org, borrow.repo) == ("lmf", "lab")
        assert borrow.stanza.cpus == 12

    def test_self_reference_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/tooling"\n')
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="its own VM"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_missing_lender_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)  # lmf/lab does not exist
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="declares no \\[vm\\] stanza"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_lender_without_vm_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab")  # vergil.toml but no [vm]
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="declares no \\[vm\\] stanza"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_chain_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", '\n[vm]\nshared_from = "lmf/base"\n')
        _make_repo(projects, "lmf", "base", _LENDER_VM)
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="chains are not allowed"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestResolveBorrow -v`
Expected: FAIL — `ImportError: cannot import name 'BorrowError'` (and `resolve_borrow`, `_read_repo_vm`).

- [ ] **Step 3: Add `BorrowError`, `Borrow`, `_read_repo_vm`, `resolve_borrow`, `_borrow_block_msg`**

In `src/vergil_tooling/bin/vrg_vm.py`, immediately after the `Target` dataclass (~line 99), add:

```python
class BorrowError(Exception):
    """A vrg-vm command cannot proceed because of a [vm] shared_from redirect.

    Raised for an invalid borrow (self-reference, chain, or a lender that declares
    no VM) and for a MANAGE command invoked against a borrowing repo. Caught in
    main(), which prints the message and returns 1.
    """


@dataclass
class Borrow:
    """A resolved redirect: the lender repo and its [vm] stanza."""

    org: str
    repo: str
    stanza: VmStanza


def _read_repo_vm(identity: Identity, org: str, repo: str) -> VmStanza | None:
    """Return the [vm] stanza of projects_dir/<org>/<repo>, or None if no vergil.toml."""
    repo_dir = Path(resolve_workspace(f"{org}/{repo}", identity.projects_dir))
    try:
        return read_config(repo_dir).vm
    except FileNotFoundError:
        return None


def resolve_borrow(
    identity: Identity,
    req_org: str,
    req_repo: str,
    requested_vm: VmStanza | None,
) -> Borrow | None:
    """Resolve a [vm] shared_from redirect on the requested repo to its lender.

    Returns None when the requested repo does not borrow. Raises BorrowError on a
    self-reference, a borrow chain, or a lender that declares no VM.
    """
    if requested_vm is None or requested_vm.shared_from is None:
        return None
    lender_org, lender_repo = requested_vm.shared_from
    if (lender_org, lender_repo) == (req_org, req_repo):
        msg = f"{req_org}/{req_repo} cannot borrow its own VM (shared_from points at itself)"
        raise BorrowError(msg)
    lender_vm = _read_repo_vm(identity, lender_org, lender_repo)
    if lender_vm is None:
        msg = (
            f"{req_org}/{req_repo} borrows the VM of {lender_org}/{lender_repo}, "
            f"but that repo declares no [vm] stanza"
        )
        raise BorrowError(msg)
    if lender_vm.shared_from is not None:
        msg = (
            f"{req_org}/{req_repo} borrows {lender_org}/{lender_repo}, which itself "
            f"borrows another VM; shared_from chains are not allowed"
        )
        raise BorrowError(msg)
    return Borrow(lender_org, lender_repo, lender_vm)


def _borrow_block_msg(
    command: str, req_org: str, req_repo: str, lender_org: str, lender_repo: str
) -> str:
    """Message for a MANAGE command blocked because the repo borrows a VM."""
    return (
        f"{req_org}/{req_repo} borrows the VM of {lender_org}/{lender_repo}.\n"
        f"Manage that box via the lender:\n"
        f"  vrg-vm {command} {lender_org}/{lender_repo}"
    )
```

`Identity` is already imported (used by `_base_footprint`); `Path`, `dataclass`, `VmStanza`, `read_config`, and `resolve_workspace` are already imported at the top of the module.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestResolveBorrow -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm \
  --message "add borrow resolution helpers for [vm] shared_from" \
  --body "Add BorrowError, Borrow, _read_repo_vm, resolve_borrow, and _borrow_block_msg. resolve_borrow turns a borrowing repo into its lender and rejects self-reference, chains, and a lender with no VM. Refs #1668."
```

---

## Task 3: Redirect USE commands (`session`, `start`) in `_resolve_target`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_resolve_target` ~line 127; `_cmd_start` ~line 514; `_cmd_session` ~line 1074)
- Test: `tests/vergil_tooling/test_vrg_vm.py` (`TestResolveTarget`)

- [ ] **Step 1: Write the failing tests**

Add to `class TestResolveTarget` in `tests/vergil_tooling/test_vrg_vm.py`:

```python
    def test_borrow_redirects_instance_and_spec(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "lmf/tooling"), borrow_allowed=True)
        # Instance + spec resolve to the LENDER, not the borrower.
        assert target.org == "lmf"
        assert target.repo == "lab"
        assert target.instance == "vergil-user.lmf.lab"
        assert target.spec.dedicated is True
        assert target.spec.cpus == 12

    def test_borrow_fingerprint_matches_lender(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        cfg = _identities(tmp_path, projects)
        lender = _resolve_target(_args(cfg, "lmf/lab"))
        borrower = _resolve_target(_args(cfg, "lmf/tooling"), borrow_allowed=True)
        assert borrower.fingerprint == lender.fingerprint
        assert borrower.instance == lender.instance
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestResolveTarget -v -k borrow`
Expected: FAIL — `TypeError: _resolve_target() got an unexpected keyword argument 'borrow_allowed'`.

- [ ] **Step 3: Rewrite `_resolve_target` to redirect on a borrow**

Replace the whole `_resolve_target` function (~lines 127-150) with:

```python
def _resolve_target(args: argparse.Namespace, *, borrow_allowed: bool = False) -> Target:
    """Resolve (identity, optional org/repo) to a base or dedicated VM target.

    When the requested repo declares ``[vm] shared_from`` and ``borrow_allowed`` is
    True (USE commands: session, start), the instance and spec redirect to the
    lender. With ``borrow_allowed`` False (MANAGE commands: create, rebuild) a
    borrow raises ``BorrowError``. The session working directory is unaffected —
    it is always derived from ``args.workspace`` by the caller.
    """
    name, identity, config = _resolve(args)
    workspace = getattr(args, "workspace", None)
    base = _base_footprint(identity)
    org, repo = _workspace_org_repo(workspace)

    if org is None or repo is None:
        spec = compose_vm_spec(identity=name, base=base, stanza=None, override=None)
        return Target(name, identity, config, None, None, spec, identity.vm_instance, "")

    requested_vm = _read_repo_vm(identity, org, repo)
    borrow = resolve_borrow(identity, org, repo, requested_vm)
    if borrow is not None:
        if not borrow_allowed:
            raise BorrowError(_borrow_block_msg(args.command, org, repo, borrow.org, borrow.repo))
        eff_org, eff_repo, eff_vm = borrow.org, borrow.repo, borrow.stanza
    else:
        eff_org, eff_repo, eff_vm = org, repo, requested_vm

    override = identity.overrides.get((eff_org, eff_repo))
    spec = compose_vm_spec(identity=name, base=base, stanza=eff_vm, override=override)

    if not spec.dedicated:
        return Target(name, identity, config, org, repo, spec, identity.vm_instance, "")

    inst = instance_name(name, eff_org, eff_repo)
    return Target(name, identity, config, eff_org, eff_repo, spec, inst, spec_fingerprint(spec))
```

This preserves existing behavior exactly when `borrow is None` (the common case): `eff_*` equals the requested `org`/`repo`/`requested_vm`, and the `FileNotFoundError`→`None` handling now lives in `_read_repo_vm`.

- [ ] **Step 4: Pass `borrow_allowed=True` from the USE commands**

In `_cmd_start` (~line 515) change:

```python
    target = _resolve_target(args, borrow_allowed=True)
```

In `_cmd_session` (~line 1074) change:

```python
    target = _resolve_target(args, borrow_allowed=True)
```

Leave `_cmd_create` and `_cmd_rebuild` calling `_resolve_target(args)` (default `borrow_allowed=False`) — they block, handled in Task 4.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestResolveTarget -v`
Expected: PASS (existing target tests + the two new borrow tests).

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm \
  --message "redirect session/start to the lender VM on [vm] shared_from" \
  --body "When the requested repo borrows a VM, _resolve_target with borrow_allowed=True resolves the instance, spec, fingerprint, and host override to the lender. session and start pass borrow_allowed=True. The session working directory still derives from args.workspace. Refs #1668."
```

---

## Task 4: Block MANAGE commands and wire `BorrowError` into `main`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_resolve_instance` ~line 153; `main` dispatch ~line 1345)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Write the failing tests**

Add a new class to `tests/vergil_tooling/test_vrg_vm.py` (reuses `_identities`, `_make_repo`; uses the package `main` and `capsys`):

```python
class TestBorrowBlocks:
    def _setup(self, tmp_path: Path) -> Path:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        return _identities(tmp_path, projects)

    @pytest.mark.parametrize("command", ["create", "stop", "restart", "destroy", "rebuild"])
    def test_manage_command_blocked_on_borrower(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._setup(tmp_path)
        result = main([command, "lmf/tooling", "--config", str(cfg)])
        assert result == 1
        err = capsys.readouterr().err
        assert "borrows the VM of lmf/lab" in err
        assert f"vrg-vm {command} lmf/lab" in err

    def test_update_blocked_on_borrower(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._setup(tmp_path)
        result = main(["update", "lmf/tooling", "--config", str(cfg)])
        assert result == 1
        assert "borrows the VM of lmf/lab" in capsys.readouterr().err
```

`update` is parametrized separately because it accepts `--all`; the other MANAGE commands share the same shape. `create`/`rebuild` reach the block through `_resolve_target` (default `borrow_allowed=False`); `stop`/`restart`/`update`/`destroy` reach it through `_resolve_instance`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestBorrowBlocks -v`
Expected: FAIL — commands do not raise/handle `BorrowError`; `stop`/`restart`/`destroy`/`update` attempt real `limactl` calls, `create`/`rebuild` proceed instead of returning 1.

- [ ] **Step 3: Block borrowers in `_resolve_instance`**

Replace `_resolve_instance` (~lines 153-166) with:

```python
def _resolve_instance(args: argparse.Namespace) -> tuple[str, Identity, IdentityConfig, str]:
    """Resolve just the instance NAME for lifecycle commands (stop/restart/destroy/update).

    These are all MANAGE commands: if the requested repo borrows a VM via
    ``[vm] shared_from`` they are blocked with ``BorrowError`` pointing at the
    lender. A repo with no readable ``vergil.toml`` (a true orphan) is unaffected
    and resolves to its own instance name, so orphaned VMs stay reachable.
    """
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    if org is not None and repo is not None:
        requested_vm = _read_repo_vm(identity, org, repo)
        if requested_vm is not None and requested_vm.shared_from is not None:
            lender_org, lender_repo = requested_vm.shared_from
            raise BorrowError(
                _borrow_block_msg(args.command, org, repo, lender_org, lender_repo)
            )
        instance = instance_name(name, org, repo)
    else:
        instance = identity.vm_instance
    return name, identity, config, instance
```

`_resolve_instance` deliberately does **not** call `resolve_borrow` — it does not validate the lender (the command is refused regardless), so a borrower with a broken lender still gets the clean "manage via the lender" message rather than a chain/missing-lender error.

- [ ] **Step 4: Catch `BorrowError` in `main`**

In `main`, replace the final dispatch line (~line 1356):

```python
    return dispatch[args.command](args)
```

with:

```python
    try:
        return dispatch[args.command](args)
    except BorrowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestBorrowBlocks -v`
Expected: PASS.

- [ ] **Step 6: Run the full `vrg_vm` and `config` suites for regressions**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py tests/vergil_tooling/test_config.py -v`
Expected: PASS (no regressions in `TestCreate`, `TestStart`, `TestStop`, `TestRestart`, `TestDestroy`, `TestRebuild`, `TestUpdate`, `TestSession`, etc.).

- [ ] **Step 7: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm \
  --message "block manage commands on a borrowing repo" \
  --body "create, stop, restart, update, destroy, and rebuild raise BorrowError when the requested repo borrows a VM, pointing the user at the lender. main() catches BorrowError and exits 1. Refs #1668."
```

---

## Task 5: Document `shared_from` and fix the spec key naming

**Files:**
- Modify: `docs/site/docs/reference/vm-spec.md`
- Modify: `docs/specs/2026-06-16-vm-shared-from-design.md`

- [ ] **Step 1: Add `shared_from` to the reference keys table and a section**

In `docs/site/docs/reference/vm-spec.md`, add a row to the `## Keys` table (after the `nested` row):

```markdown
| `shared_from` | string `"org/repo"` | _(none)_ | Borrow another repo's VM instead of declaring one. Mutually exclusive with every other `[vm]` key (see below) |
```

Then add a section after `## Nested virtualization (`nested`)`:

```markdown
## Borrowing a VM (`shared_from`)

A repo with no VM of its own can run its sessions inside another repo's
dedicated VM:

```toml
[vm]
shared_from = "logical-minds-foundry/mq-resiliency-lab"
```

`vrg-vm session <org>/<borrower>` then shells into the **lender's** box and
`cd`s into the borrower's checkout (the whole projects directory is mounted
into every VM, so both checkouts are present).

- The value must be a fully-qualified `org/repo`.
- `shared_from` is the **only** key allowed under `[vm]` — combining it with a
  footprint/package key or a `[vm.<role>]` overlay is a config error. A repo
  either describes a VM or borrows one.
- The borrower may **use** the shared box (`session`, `start`) but not
  **manage** it: `create`, `stop`, `restart`, `update`, `destroy`, and
  `rebuild` on the borrower are refused and point at the lender repo, which
  owns the box.
- One hop only — the lender may not itself declare `shared_from`.
```

(Match the exact heading punctuation used elsewhere in the file; preserve the surrounding code-fence style.)

- [ ] **Step 2: Fix the key spelling in the design spec**

In `docs/specs/2026-06-16-vm-shared-from-design.md`, replace `shared-from` with `shared_from` everywhere it denotes the TOML key (the `[vm]` code block, the `VmStanza.shared_from` references, and prose). Leave the issue/PR cross-references intact.

Run to confirm none remain: `grep -n "shared-from" docs/specs/2026-06-16-vm-shared-from-design.md` → expected: no output.

- [ ] **Step 3: Commit**

```bash
vrg-git add docs/site/docs/reference/vm-spec.md docs/specs/2026-06-16-vm-shared-from-design.md
vrg-commit --type docs --scope vm \
  --message "document [vm] shared_from borrowing" \
  --body "Add the shared_from key to the VM spec reference and align the design spec with the underscore key name used by every other [vm] key. Refs #1668."
```

---

## Task 6: Full validation

**Files:** none (verification only)

- [ ] **Step 1: Run the canonical validation gate**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck, tests, markdownlint, and all common checks green. Fix any lint/type findings (e.g. `ruff`/`mypy`) inline; the new code uses explicit constants (`_ORG_REPO_PARTS`) to avoid magic-number warnings and typed signatures throughout.

- [ ] **Step 2: Commit any validation fixups (if needed)**

```bash
vrg-git add -A
vrg-commit --type chore --scope vm \
  --message "satisfy validation for [vm] shared_from" \
  --body "Lint/type fixups surfaced by vrg-validate. Refs #1668."
```

(Skip this step if validation passed clean with no changes.)

---

## Self-Review Notes

- **Spec coverage:** config schema + mutual exclusivity (Task 1); resolution incl. chains/self/missing-lender (Task 2); USE redirect with fingerprint/override alignment (Task 3); MANAGE block + `main` catch (Task 4); reference docs + spec key fix (Task 5); validation (Task 6). The `list` behavior in the spec needs no code (the borrower owns no instance), so it has no task.
- **Type consistency:** `VmStanza.shared_from: tuple[str, str] | None`; `Borrow(org, repo, stanza)`; `resolve_borrow(...) -> Borrow | None`; `_read_repo_vm(...) -> VmStanza | None`; `_resolve_target(args, *, borrow_allowed=False)`. Names are used identically across tasks.
- **No placeholders:** every code and test block is complete.
