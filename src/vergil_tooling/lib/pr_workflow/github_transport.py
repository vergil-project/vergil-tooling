"""The GitHub-ref transport: relay pr-workflow.json over a reserved git ref.

Carries the workflow state across filesystems (a cloud VM and the Mac never
share a disk) via the reserved ref ``refs/vergil/pr-workflow/<branch>``. Nested
slashes are valid, so ``feature/123-x`` maps to
``refs/vergil/pr-workflow/feature/123-x``; the namespace is invisible to the
branch/PR UI and to default fetch refspecs, so it never pollutes history.

The ref points at a **single-file commit** holding ``pr-workflow.json`` — GitHub
rejects a ref that does not resolve to a commit. ``write`` builds that commit
entirely **out-of-band** with git plumbing (``hash-object`` -> ``mktree`` ->
``commit-tree``) and force-pushes the resulting SHA straight to the ref. It
never runs ``git commit`` and never touches HEAD, the index, or the working
tree, so the push is a pure ref write. That freeze-neutrality is load-bearing:
``report-ready`` arms the post-report freeze (epic #146), and a normal commit
would advance the feature branch out from under it (design
2026-06-24-cloud-pr-handoff, Deliverable B).
"""

from __future__ import annotations

from vergil_tooling.lib import git
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

_FILE = "pr-workflow.json"
_REF_PREFIX = "refs/vergil/pr-workflow"
_COMMIT_MESSAGE = "vergil: pr-workflow relay"
# The regular-file mode git uses for a blob entry in a tree.
_BLOB_MODE = "100644"


class GitHubTransport(Transport):
    """Relay the workflow state over ``refs/vergil/pr-workflow/<branch>``."""

    def __init__(
        self, branch: str, *, base: str = "origin/develop", remote: str = "origin"
    ) -> None:
        self.branch = branch
        self.base = base
        self.remote = remote

    @property
    def ref(self) -> str:
        """The reserved ref carrying this branch's workflow payload."""
        return f"{_REF_PREFIX}/{self.branch}"

    def _remote_ref_exists(self) -> bool:
        """Return True when the relay ref is present on the remote."""
        return bool(git.read_output("ls-remote", self.remote, self.ref))

    def read(self) -> WorkflowState | None:
        """Fetch the relay ref and parse its payload; None if the ref is absent."""
        if not self._remote_ref_exists():
            return None
        # Fetch into FETCH_HEAD only — a read that never writes a local ref and
        # never touches HEAD/index/worktree.
        git.read_output("fetch", self.remote, self.ref)
        return WorkflowState.from_json(git.read_output("show", f"FETCH_HEAD:{_FILE}"))

    def write(self, state: WorkflowState) -> None:
        """Build the single-file commit out-of-band and force-push it to the ref.

        INVARIANT: never runs ``git commit``; never mutates HEAD, the index, or
        the working tree. The blob/tree/commit are built with plumbing and the
        push targets only the reserved ref, keeping the write freeze-neutral.
        """
        blob = git.read_output_stdin(state.to_json(), "hash-object", "-w", "--stdin")
        tree = git.read_output_stdin(f"{_BLOB_MODE} blob {blob}\t{_FILE}\n", "mktree")
        commit = git.read_output("commit-tree", tree, "-m", _COMMIT_MESSAGE)
        git.run("push", "--force", self.remote, f"{commit}:{self.ref}")

    def delete(self) -> None:
        """Remove the relay ref from the remote; a no-op when it is absent."""
        if not self._remote_ref_exists():
            return
        git.run("push", self.remote, f":{self.ref}")

    def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        """Return the merge-base SHA of the base ref and HEAD."""
        return git.read_output("merge-base", self.base, "HEAD")
