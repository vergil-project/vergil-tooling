"""The release state block in the tracking-issue body (spec §4.2, epic vergil-project/.github#1440).

The tracking issue's body carries one machine-read block, a fenced ``json``
document between two markers, each alone on its line::

    <!-- vrg-release:state:begin -->
    ```json
    { "version": "2.1.340", "cut": "<sha>", ... }
    ```
    <!-- vrg-release:state:end -->

Only ``vrg-release`` writes it, by a read-modify-write of the body. Its JSON
keys are :class:`StateBlock`'s field names, and tuples render as lists. A
block that is missing, duplicated, not JSON, or has an unknown key, a missing
key or a value of the wrong type is a :class:`StateBlockError`, never a
fallback to defaults.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Literal, cast

BEGIN = "<!-- vrg-release:state:begin -->"
END = "<!-- vrg-release:state:end -->"

_BEGIN_RE = re.compile(rf"^{re.escape(BEGIN)}$", re.MULTILINE)
_END_RE = re.compile(rf"^{re.escape(END)}$", re.MULTILINE)
_FENCE_RE = re.compile(r"\A\s*```json\n(?P<json>.*)\n```\s*\Z", re.DOTALL)

_BRANCHES = ("main", "develop")


class StateBlockError(Exception):
    """The state block is missing, duplicated or malformed."""


@dataclass(frozen=True)
class IssueEntry:
    """One issue the release contains: its ``<owner>/<repo>#<N>`` ref and closing PRs."""

    ref: str
    prs: tuple[int, ...]


@dataclass(frozen=True)
class FailedJob:
    """A CD publish job that did not succeed: the branch's run and the job's name."""

    branch: Literal["main", "develop"]
    run_id: str
    job: str


@dataclass(frozen=True)
class StateBlock:
    """Everything a release records about itself on its tracking issue."""

    version: str
    cut: str
    issues: tuple[IssueEntry, ...]
    release_pr: str | None
    bump_pr: str | None
    tag: str | None
    # The commit the tag peels to, written by confirm-main.
    tag_commit: str | None
    main_cd_run: str | None
    develop_cd_run: str | None
    failed_jobs: tuple[FailedJob, ...]
    index_listed: bool
    without_deps: bool
    done: tuple[str, ...]
    install_done: bool
    records_posted: tuple[str, ...]
    supersedes: str | None
    superseded_main_commit: str | None
    # The CD release run confirm-main saw fail.
    failed_release_run: str | None


def initial_state(
    *, version: str, cut: str, issues: tuple[IssueEntry, ...], without_deps: bool
) -> StateBlock:
    """The state a tracking issue is created with: nothing done yet."""
    return StateBlock(
        version=version,
        cut=cut,
        issues=issues,
        release_pr=None,
        bump_pr=None,
        tag=None,
        tag_commit=None,
        main_cd_run=None,
        develop_cd_run=None,
        failed_jobs=(),
        index_listed=False,
        without_deps=without_deps,
        done=(),
        install_done=False,
        records_posted=(),
        supersedes=None,
        superseded_main_commit=None,
        failed_release_run=None,
    )


def render_state(state: StateBlock) -> str:
    """The delimited state block for *state*, markers included, no trailing newline."""
    document = json.dumps(asdict(state), indent=2)
    return f"{BEGIN}\n```json\n{document}\n```\n{END}"


def _span(body: str) -> tuple[int, int]:
    """The start of the begin marker and the end of the end marker in *body*.

    Raises :class:`StateBlockError` unless *body* has exactly one of each, in order.
    """
    begins = list(_BEGIN_RE.finditer(body))
    ends = list(_END_RE.finditer(body))
    if not begins and not ends:
        msg = f"no release state block ({BEGIN} ... {END}) in the tracking-issue body"
        raise StateBlockError(msg)
    if len(begins) > 1 or len(ends) > 1:
        msg = (
            f"the tracking-issue body has {len(begins)} {BEGIN} and {len(ends)} {END} "
            "markers; a release state block must appear exactly once"
        )
        raise StateBlockError(msg)
    if len(begins) != 1 or len(ends) != 1 or ends[0].start() < begins[0].end():
        msg = f"the release state block's {BEGIN} and {END} markers do not pair up"
        raise StateBlockError(msg)
    return begins[0].start(), ends[0].end()


def _fail(key: str, expected: str, value: object) -> StateBlockError:
    return StateBlockError(
        f"release state block key {key!r} must be {expected}, not {json.dumps(value)}"
    )


def _string(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise _fail(key, "a string", value)
    return value


def _optional_string(key: str, value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _fail(key, "a string or null", value)
    return value


def _boolean(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _fail(key, "true or false", value)
    return value


def _list(key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise _fail(key, "a list", value)
    return cast("list[object]", value)


def _strings(key: str, value: object) -> tuple[str, ...]:
    return tuple(_string(f"{key}[{i}]", item) for i, item in enumerate(_list(key, value)))


def _object(key: str, value: object, names: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _fail(key, "an object", value)
    obj = cast("dict[str, object]", value)
    _check_keys(key, obj, names)
    return obj


def _check_keys(where: str, obj: dict[str, object], names: tuple[str, ...]) -> None:
    """Refuse *obj* unless its keys are exactly *names*; *where* names the object."""
    what = f"release state block {where}" if where else "release state block"
    unknown = sorted(set(obj) - set(names))
    if unknown:
        msg = f"{what} has unknown key(s): {', '.join(unknown)}"
        raise StateBlockError(msg)
    missing = [name for name in names if name not in obj]
    if missing:
        msg = f"{what} is missing key(s): {', '.join(missing)}"
        raise StateBlockError(msg)


def _issue(key: str, value: object) -> IssueEntry:
    obj = _object(key, value, ("ref", "prs"))
    prs: list[int] = []
    for i, pr in enumerate(_list(f"{key}.prs", obj["prs"])):
        if not isinstance(pr, int) or isinstance(pr, bool):
            raise _fail(f"{key}.prs[{i}]", "an integer", pr)
        prs.append(pr)
    return IssueEntry(ref=_string(f"{key}.ref", obj["ref"]), prs=tuple(prs))


def _failed_job(key: str, value: object) -> FailedJob:
    obj = _object(key, value, ("branch", "run_id", "job"))
    branch = _string(f"{key}.branch", obj["branch"])
    if branch not in _BRANCHES:
        raise _fail(f"{key}.branch", '"main" or "develop"', branch)
    return FailedJob(
        branch=cast("Literal['main', 'develop']", branch),
        run_id=_string(f"{key}.run_id", obj["run_id"]),
        job=_string(f"{key}.job", obj["job"]),
    )


_FIELDS = tuple(f.name for f in fields(StateBlock))


def parse_state(body: str) -> StateBlock:
    """The :class:`StateBlock` in tracking-issue *body*.

    Raises :class:`StateBlockError` naming the block when it is missing,
    duplicated, not fenced JSON, or has an unknown key, a missing key or a
    value of the wrong type.
    """
    start, end = _span(body)
    inner = body[start + len(BEGIN) : end - len(END)]
    fenced = _FENCE_RE.match(inner)
    if fenced is None:
        msg = "the release state block does not hold one fenced ```json block"
        raise StateBlockError(msg)
    try:
        document = json.loads(fenced.group("json"))
    except json.JSONDecodeError as exc:
        msg = f"the release state block is not valid JSON: {exc}"
        raise StateBlockError(msg) from exc
    if not isinstance(document, dict):
        msg = "the release state block's JSON is not an object"
        raise StateBlockError(msg)
    data = cast("dict[str, object]", document)
    _check_keys("", data, _FIELDS)
    return StateBlock(
        version=_string("version", data["version"]),
        cut=_string("cut", data["cut"]),
        issues=tuple(
            _issue(f"issues[{i}]", item) for i, item in enumerate(_list("issues", data["issues"]))
        ),
        release_pr=_optional_string("release_pr", data["release_pr"]),
        bump_pr=_optional_string("bump_pr", data["bump_pr"]),
        tag=_optional_string("tag", data["tag"]),
        tag_commit=_optional_string("tag_commit", data["tag_commit"]),
        main_cd_run=_optional_string("main_cd_run", data["main_cd_run"]),
        develop_cd_run=_optional_string("develop_cd_run", data["develop_cd_run"]),
        failed_jobs=tuple(
            _failed_job(f"failed_jobs[{i}]", item)
            for i, item in enumerate(_list("failed_jobs", data["failed_jobs"]))
        ),
        index_listed=_boolean("index_listed", data["index_listed"]),
        without_deps=_boolean("without_deps", data["without_deps"]),
        done=_strings("done", data["done"]),
        install_done=_boolean("install_done", data["install_done"]),
        records_posted=_strings("records_posted", data["records_posted"]),
        supersedes=_optional_string("supersedes", data["supersedes"]),
        superseded_main_commit=_optional_string(
            "superseded_main_commit", data["superseded_main_commit"]
        ),
        failed_release_run=_optional_string("failed_release_run", data["failed_release_run"]),
    )


def upsert_state(body: str, state: StateBlock) -> str:
    """*body* with its state block replaced by *state*'s, or appended when it has none.

    Everything outside the block, the stage checklist included, is kept as it
    is. A body whose markers are duplicated or do not pair up raises
    :class:`StateBlockError` rather than gaining a second block.
    """
    block = render_state(state)
    if not _BEGIN_RE.search(body) and not _END_RE.search(body):
        return body.rstrip() + "\n\n" + block + "\n"
    start, end = _span(body)
    return body[:start] + block + body[end:]
