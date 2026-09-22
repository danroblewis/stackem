"""The engine: what ``stackem``, ``stackem sync`` and ``stackem parent`` do.

This is the wiring, and only the wiring.  Every mechanism it drives lives
somewhere else and is tested there:

    stackem.orient    derive the trunk, the parents, the stack, the states
    stackem.restack   PHASE 0 (resume) and PHASE 1 (the local cascade)
    stackem.push      PHASE 2 (retarget, push, report)
    stackem.forge     the Provider protocol; stackem.forge.github implements it
    stackem.cli       every byte that reaches the user

What this module adds is the order of SPEC.md sec 5.2 and the translation
between those modules' vocabularies and the CLI's view model.  It stores
nothing, writes no config and creates no refs of its own -- the only things it
writes are the ones the spec sanctions: a ``git fetch``, the rebases
``stackem.restack`` runs, and the push ``stackem.push`` runs.

The shape of a sync
-------------------
::

    PHASE 0  a rebase in progress that is not ours  -> refuse (invariant 22)
    PHASE 1  fetch, orient, restack, verify         -- local, reversible
    PHASE 2  retarget pull request bases, push      -- only if PHASE 1 verified

Three decisions this module owns, because no other module could
----------------------------------------------------------------
**A branch below an orphaned pull request is skipped, not restacked.**  Its
branch is gone from the remote and its pull request is closed; restacking and
pushing its children would build on wreckage the user is being asked to rescue
by hand (SPEC.md sec 6.3, docs/sessions/05).  The walk itself only knows to skip
*merged* branches, so the skipping is done by handing ``restack`` a stack those
branches are not in.

**The display parent of a branch above an emptied one is the branch the chain
now has** (SPEC.md sec 7.2).  Locally the child is still based on the emptied
branch -- whose tip now equals its parent's, so this is the same commit -- but
its pull request is retargeted past it, and the report says so.

**A dirty worktree stops sync before it starts.**  SPEC.md sec 11 requires a
clean worktree for a restack; git would fail mid-cascade otherwise.  Untracked
files do not count (see :meth:`~stackem.gitx.Git.is_clean`), and a rebase already
in progress is exempt -- resolving a conflict *means* having a staged change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from stackem import cli
from stackem import push as push_phase
from stackem.forge import ForgeError, Provider
from stackem.forge.github import GitHubProvider
from stackem.gitx import Git, GitError
from stackem.model import Branch, BranchState, PullRequestState, Stack
from stackem.orient import Orientation, OrientationError, orient
from stackem.restack import (
    BranchAction,
    ForeignRebase,
    ResumeDecision,
    RestackReport,
    RestackStatus,
    hoist_over_merged,
    read_resume,
    restack,
    target_ref,
)

__all__ = ["SyncEngine", "build_engine"]

_SHORT = 7


def _short(sha: str | None) -> str | None:
    return sha[:_SHORT] if sha else None


def _count(git: Git, base: str | None, tip: str | None) -> int:
    """Commits in ``base..tip``, or 0 when either end is unknown."""
    if not base or not tip:
        return 0
    try:
        return git.rev_list_count(f"{base}..{tip}")
    except GitError:
        return 0


@dataclass
class _Retarget:
    """One pull request base to move, shared by the report and the phase.

    The same object is rendered (as a "retargeting PR #N base a -> b" line) and
    handed to :mod:`stackem.push`, so the line cannot claim something the phase
    did not do: ``ok`` is set from the phase's own result.
    """

    pr: int
    branch: str
    old_base: str
    new_base: str
    reason: push_phase.RetargetReason
    ok: bool = True

    def to_push(self) -> push_phase.Retarget:
        return push_phase.Retarget(
            pr_number=self.pr,
            branch=self.branch,
            old_base=self.old_base,
            new_base=self.new_base,
            reason=self.reason,
        )


class SyncEngine:
    """What the CLI calls.  One instance per command, nothing cached across runs."""

    def __init__(
        self,
        git: Git | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        provider: Provider | None = None,
        env: dict[str, str] | None = None,
        remote: str = "origin",
        trunk: str | None = None,
    ) -> None:
        self.git = git if git is not None else Git(cwd or os.getcwd())
        self.remote = remote
        self.trunk = trunk
        #: Only ever read, and only to build the provider: which repository this
        #: is and where its API lives.  stackem writes no configuration.
        self.env = dict(os.environ if env is None else env)
        self._provider = provider
        self._resolved = provider is not None
        self.forge_error: str | None = None

    # ------------------------------------------------------------------
    # the forge
    # ------------------------------------------------------------------

    @property
    def provider(self) -> Provider | None:
        """The forge, or None when there is no way to reach it.

        SPEC.md sec 12: offline, parent resolution falls back to topology
        inference, which is correct for a healthy stack but cannot tell that a
        parent was merged.  That is a degraded run, not a failed one, so the
        reason is reported rather than raised.
        """
        if not self._resolved:
            self._resolved = True
            try:
                self._provider = self._build_provider()
            except (ForgeError, ValueError, GitError, OSError) as error:
                self._provider = None
                self.forge_error = str(error)
        return self._provider

    def _build_provider(self) -> Provider:
        # `git config --get` rather than `git remote get-url`: it is on the
        # read-only allowlist `stackem` (no arguments) runs behind (invariant 5).
        proc = self.git.run(
            "config", "--get", f"remote.{self.remote}.url", check=False
        )
        url = proc.stdout.strip()
        if not url:
            raise ForgeError(
                f"no url for the {self.remote} remote, so there is no forge to ask"
            )
        return GitHubProvider.from_remote_url(url, env=self.env)

    def _slug(self) -> str:
        provider = self.provider
        if provider is None:
            return "OWNER/REPO"
        try:
            return provider.repo_slug()
        except ForgeError:
            return "OWNER/REPO"

    def _delete_branch_on_merge(self) -> bool:
        provider = self.provider
        if provider is None:
            return False
        try:
            return provider.repo_settings().delete_branch_on_merge
        except ForgeError:
            return False

    # ------------------------------------------------------------------
    # orientation
    # ------------------------------------------------------------------

    def _orient(self, *, allow_ref_write: bool) -> Orientation:
        try:
            return orient(
                self.git,
                self.provider,
                trunk=self.trunk,
                remote=self.remote,
                allow_ref_write=allow_ref_write,
            )
        except OrientationError as error:
            raise cli.CliError(
                str(error),
                detail=(
                    "  stackem derives the trunk from origin/HEAD, then "
                    "`git remote set-head`,",
                    "  then the forge (SPEC.md sec 6.6). None of them answered.",
                ),
                next_command=f"git remote set-head {self.remote} -a",
            ) from error

    def _warnings(self, orientation: Orientation | None = None) -> tuple[str, ...]:
        warnings: list[str] = []
        if self.forge_error:
            warnings.append(
                "could not reach the forge, so pull request state is unknown and "
                f"parents come from branch topology only: {self.forge_error}"
            )
        warnings.extend(_closed_pull_requests(orientation, self._slug()))
        return tuple(warnings)

    def _trunk_behind(self, orientation: Orientation) -> int:
        """Commits on ``origin/<trunk>`` the stack is not built on."""
        ref = orientation.trunk.ref
        if self.git.try_rev_parse(ref) is None:
            return 0
        members = orientation.members
        anchor = members[0].name if members else (orientation.stack.head or "HEAD")
        if self.git.try_rev_parse(anchor) is None:
            return 0
        base = self.git.merge_base(ref, anchor)
        return _count(self.git, base, ref)

    def _branch_name(self, orientation: Orientation, parent: str | None) -> str:
        """The parent as a BRANCH name.

        ``gh pr create --base`` and a sentence about where work moved to both
        want ``main``; only a rebase target wants ``origin/main``.
        """
        return parent or orientation.trunk.name

    def _parent_display(self, orientation: Orientation, parent: str | None) -> str:
        """How a parent is named in the report.

        A root branch rebases onto ``origin/<trunk>`` and never the local trunk
        (invariant 4), and the report says which one it was.
        """
        trunk = orientation.trunk.name
        if parent is None or parent == trunk:
            return f"{self.remote}/{trunk}"
        return parent

    # ------------------------------------------------------------------
    # stackem -- the display (READ-ONLY: invariant 5)
    # ------------------------------------------------------------------

    def status(self) -> cli.StatusView:
        orientation = self._orient(allow_ref_write=False)
        git = self.git
        members = orientation.members
        remote_branches = git.branches(f"refs/remotes/{self.remote}")

        # Display only: the object is derived fresh and thrown away, and the
        # hoist tells us whose pull request base is a merged branch.
        originals = {branch.name: branch.parent for branch in members}
        hoisted = {rep.branch: rep for rep in hoist_over_merged(orientation.stack)}

        # A branch whose parent has to move has to move too, even though it is
        # sitting correctly on the parent's *current* tip (docs/sessions/03 and
        # 05 mark the whole stack, not just the branch the change landed under).
        stale: set[str] = set()

        rows: list[cli.BranchRow] = []
        for branch in members:
            pull_request = branch.pull_request
            row = cli.BranchRow(
                name=branch.name,
                pr=pull_request.number if pull_request else None,
                parent=originals.get(branch.name) or orientation.trunk.name,
                state=branch.state.value,
            )
            if branch.state is BranchState.MERGED:
                row.merged_as = _short(
                    pull_request.merge_commit_sha if pull_request else None
                )
                row.branch_deleted = branch.name not in remote_branches
                rows.append(row)
                continue
            if branch.state is BranchState.ORPHANED:
                rows.append(row)
                continue

            target = target_ref(orientation.stack, branch.parent)
            tip = git.try_rev_parse(target)
            moved = tip is not None and not git.is_ancestor(target, branch.name)
            if moved or branch.parent in stale:
                row.needs_restack = True
                stale.add(branch.name)
                # "(trunk moved)" belongs to a branch that really sits on the
                # trunk -- not to one the hoist just put there because its
                # parent merged; that one's reason is the note below.
                if moved and originals.get(branch.name) in (
                    None,
                    orientation.trunk.name,
                ):
                    row.restack_reason = "trunk moved"
            elif branch.name in remote_branches and remote_branches[
                branch.name
            ] != branch.sha:
                row.restacked_not_pushed = True
            if branch.name in hoisted:
                row.notes = ("PR base is a merged branch",)
            rows.append(row)

        return cli.StatusView(
            trunk=orientation.trunk.name,
            remote=self.remote,
            trunk_behind=self._trunk_behind(orientation),
            branches=rows,
            warnings=self._warnings(orientation),
        )

    # ------------------------------------------------------------------
    # stackem sync
    # ------------------------------------------------------------------

    def sync(self, *, dry_run: bool = False) -> cli.SyncView:
        git = self.git
        resuming = git.rebase_in_progress()

        if resuming:
            # PHASE 0 comes before everything, the fetch included: a rebase that
            # is not ours stops sync where it stands (invariant 22).
            preview = self._orient(allow_ref_write=True)
            hoist_over_merged(preview.stack)
            decision = read_resume(git, preview.stack)
            if not decision.ours:
                return cli.SyncView(
                    trunk=preview.trunk.name,
                    remote=self.remote,
                    dry_run=dry_run,
                    fetched=False,
                    foreign=_foreign_view(decision),
                    warnings=self._warnings(),
                )
        elif not git.is_clean(untracked=False):
            raise cli.CliError(
                "the worktree has uncommitted changes, and a restack needs a "
                "clean one (SPEC.md sec 11).",
                detail=("  commit them, or set them aside with `git stash`.",),
                next_command="git stash, then: stackem sync",
            )

        git.fetch(self.remote, prune=True)
        orientation = self._orient(allow_ref_write=True)
        view = cli.SyncView(
            trunk=orientation.trunk.name,
            remote=self.remote,
            dry_run=dry_run,
            fetched=True,
            trunk_moved=self._trunk_behind(orientation),
            warnings=self._warnings(orientation),
        )
        if dry_run:
            return self._plan(orientation, view)
        return self._run(orientation, view)

    # -- the plan (--dry-run) ---------------------------------------------

    def _plan(self, orientation: Orientation, view: cli.SyncView) -> cli.SyncView:
        """What sync would do, computed without doing any of it."""
        git = self.git
        members = orientation.members
        blocked = _blocked_by_orphan(members)
        originals = {branch.name: branch.parent for branch in members}
        reparents = hoist_over_merged(orientation.stack)

        rows: list[cli.Restack] = []
        would_push: list[str] = []
        stale: set[str] = set()
        remote_branches = git.branches(f"refs/remotes/{self.remote}")
        for branch in members:
            if branch.name in blocked or branch.state is not BranchState.LIVE:
                continue
            target = target_ref(orientation.stack, branch.parent)
            tip = git.try_rev_parse(target)
            # A branch sitting correctly on its parent's CURRENT tip still has
            # to move once the parent does, so staleness runs up the chain.
            moved = (tip is not None and not git.is_ancestor(target, branch.name)) or (
                branch.parent in stale
            )
            if moved:
                stale.add(branch.name)
                rows.append(
                    cli.Restack(
                        branch=branch.name,
                        onto=self._parent_display(orientation, branch.parent),
                        pr=branch.pull_request.number if branch.pull_request else None,
                    )
                )
            if moved or remote_branches.get(branch.name) != branch.sha:
                would_push.append(branch.name)

        view.restacks = rows
        view.reparents = self._merged_reparents(
            orientation, reparents, originals, blocked
        )[0]
        if would_push:
            view.push = cli.PushResult(branches=tuple(would_push))
        view.merged = self._merged_summary(orientation)
        view.orphans = self._orphans(orientation, blocked)
        view.no_pull_request = self._missing_pull_requests(orientation, blocked)
        return view

    # -- the real thing ----------------------------------------------------

    def _run(self, orientation: Orientation, view: cli.SyncView) -> cli.SyncView:
        git = self.git
        members = orientation.members
        blocked = _blocked_by_orphan(members)
        originals = {branch.name: branch.parent for branch in members}
        head_before = git.current_branch()

        # SPEC.md sec 6.3: the branches above an orphaned pull request are left
        # alone this run; they are reported instead of rebuilt on wreckage.
        walk = Stack(
            trunk=orientation.stack.trunk,
            remote=orientation.stack.remote,
            head=orientation.stack.head,
            trunk_remote_sha=orientation.stack.trunk_remote_sha,
            branches=[b for b in members if b.name not in blocked],
        )
        report = restack(git, walk)

        view.orphans = self._orphans(orientation, blocked)
        if report.status is RestackStatus.FOREIGN_REBASE:
            view.foreign = _foreign_view(report.foreign)
            return view

        hoisted = {branch.name: branch.parent for branch in members}
        emptied = {result.branch for result in report.results if result.emptied}
        parent_of = _final_parents(hoisted, emptied)

        reparents, merged_retargets = self._merged_reparents(
            orientation, report.reparented, originals, blocked
        )
        view.reparents = reparents
        rows, emptied_retargets = self._rows(
            orientation, report, parent_of, emptied, blocked
        )
        view.restacks = rows
        view.merged = self._merged_summary(orientation)
        view.emptied = [
            cli.Emptied(
                name=name,
                pr=_pr_number(orientation, name),
                parent=self._branch_name(orientation, parent_of(name)),
            )
            for name in _member_order(members, emptied)
        ]
        view.no_pull_request = self._missing_pull_requests(
            orientation, blocked, parent_of
        )

        if report.status is RestackStatus.CONFLICT and report.conflict is not None:
            conflict = report.conflict
            view.conflict = cli.Conflict(
                branch=conflict.branch,
                sha=_short(conflict.stopped_sha) or "",
                subject=conflict.stopped_subject or "",
                onto=self._parent_display(orientation, conflict.parent),
                onto_sha=_short(conflict.target) or "",
                files=tuple(conflict.files),
                queued=tuple(conflict.queued),
            )
            return view

        # Everything below rewrote history; put the user back where they were
        # before anything else happens (git rebase leaves HEAD on the last
        # branch it touched).
        self._restore_head(head_before)

        if report.status is RestackStatus.GUARD_VIOLATION and report.guard is not None:
            view.guard = cli.GuardViolation(
                branch=report.guard.branch,
                parent=report.guard.parent or orientation.trunk.name,
                queued=tuple(report.queued),
            )
            return view

        verification = report.verification
        if verification is not None and not verification.ok:
            # Invariant 9: stop before the remote, and show what changed.
            view.verification = cli.Verification(
                ranges=len(verification.entries),
                ok=False,
                unexpected=tuple(verification.unexpected),
            )
            return view

        replayed = [
            result
            for result in report.results
            if result.action in (BranchAction.RESTACKED, BranchAction.RESUMED)
        ]
        if replayed:
            view.verification = cli.Verification(
                ranges=len(verification.entries) if verification else 0, ok=True
            )
        # "removed from the chain" is something this run DID: a branch that was
        # already empty when the run started is a standing reminder (SPEC.md
        # sec 7.2, docs/sessions/06), not a second removal.
        view.removed = sum(1 for result in replayed if result.emptied)
        retargets = list(merged_retargets) + list(emptied_retargets)
        return self._remote_phase(
            orientation, view, report, parent_of, blocked, retargets, head_before
        )

    # -- PHASE 2 -----------------------------------------------------------

    def _remote_phase(
        self,
        orientation: Orientation,
        view: cli.SyncView,
        report: RestackReport,
        parent_of,
        blocked: set[str],
        retargets: list[_Retarget],
        head_before: str | None,
    ) -> cli.SyncView:
        git = self.git
        provider = self.provider
        if retargets and provider is None:
            raise cli.CliError(
                "the local work is done, but retargeting a pull request needs "
                f"the forge and it could not be reached: {self.forge_error}",
                detail=(
                    "  nothing was pushed. The branches are restacked locally; "
                    "rerun sync once",
                    "  the forge is reachable.",
                ),
                next_command="stackem sync",
            )

        results = report.by_branch
        changes: list[push_phase.BranchChange] = []
        for branch in orientation.members:
            if branch.name in blocked:
                continue
            result = results.get(branch.name)
            before = report.snapshot.get(branch.name) or branch.sha
            after = git.try_rev_parse(branch.name)
            if after is None:
                continue
            verification = None
            if result is not None and before != after and result.range_diff:
                verification = push_phase.Verification(
                    branch=branch.name,
                    old_range=f"{result.fork}..{result.old_sha}",
                    new_range=f"{result.target}..{result.new_sha}",
                    entries=tuple(result.range_diff),
                    resolved_conflict=result.resolved_conflict,
                )
            changes.append(
                push_phase.BranchChange(
                    branch=branch.name,
                    before=before,
                    after=after,
                    remote=git.try_rev_parse(f"{self.remote}/{branch.name}"),
                    state=branch.state,
                    parent=parent_of(branch.name) or orientation.trunk.name,
                    pull_request=branch.pull_request,
                    verification=verification,
                    emptied=bool(result is not None and result.emptied),
                )
            )

        work = push_phase.VerifiedLocalWork(
            changes=tuple(changes),
            retargets=tuple(r.to_push() for r in retargets),
            local_complete=True,
            head_branch=head_before,
            delete_branch_on_merge=self._delete_branch_on_merge(),
        )
        outcome = push_phase.run_remote_phase(
            git, provider, work, remote=self.remote, slug=self._slug()
        )

        failed = {failure.retarget.pr_number for failure in outcome.retarget_failures}
        for retarget in retargets:
            retarget.ok = retarget.pr not in failed

        if outcome.outcome is push_phase.PushOutcome.RETARGET_FAILED:
            view.retarget_failed = tuple(
                cli.RetargetFailure(
                    pr=failure.retarget.pr_number,
                    branch=failure.retarget.branch,
                    new_base=failure.retarget.new_base,
                    error=failure.error,
                )
                for failure in outcome.retarget_failures
            )
            return view

        if outcome.outcome is push_phase.PushOutcome.REJECTED:
            view.push = cli.PushResult(
                branches=tuple(outcome.attempted),
                ok=False,
                rejected=tuple(outcome.rejected),
            )
            return view

        if outcome.pushed:
            view.push = cli.PushResult(branches=tuple(outcome.pushed), ok=True)
            view.pr_bases_ok = True
        return view

    # -- building the report's pieces --------------------------------------

    def _rows(
        self,
        orientation: Orientation,
        report: RestackReport,
        parent_of,
        emptied: set[str],
        blocked: set[str],
    ) -> tuple[list[cli.Restack], list[_Retarget]]:
        """One line per branch the walk actually touched, plus its retargets."""
        git = self.git
        rows: list[cli.Restack] = []
        retargets: list[_Retarget] = []
        for result in report.results:
            if result.action in (
                BranchAction.SKIPPED_MERGED,
                BranchAction.SKIPPED_UP_TO_DATE,
                BranchAction.BLOCKED,
            ):
                continue
            branch = orientation.branch(result.branch)
            pull_request = branch.pull_request if branch else None
            row = cli.Restack(
                branch=result.branch,
                onto=self._parent_display(orientation, parent_of(result.branch)),
                outcome=(
                    "conflict"
                    if result.action is BranchAction.CONFLICTED
                    else ("empty" if result.emptied else "ok")
                ),
                commits=_count(git, result.target, result.new_sha),
                commits_before=_count(git, result.fork, result.old_sha) or None,
                dropped=tuple(
                    cli.Dropped(sha=_short(commit.sha) or "", subject=commit.subject)
                    for commit in result.dropped
                ),
                pr=pull_request.number if pull_request else None,
                resumed=result.action is BranchAction.RESUMED,
                no_pull_request=pull_request is None,
            )
            if result.branch in emptied:
                # SPEC.md sec 7.2: the branch leaves the chain and its children
                # are stitched past it -- printed right under the EMPTY line.
                block, moves = self._emptied_reparent(
                    orientation, result.branch, parent_of, blocked
                )
                row.reparent = block
                retargets.extend(moves)
            rows.append(row)
        return rows, retargets

    def _emptied_reparent(
        self, orientation: Orientation, name: str, parent_of, blocked: set[str]
    ) -> tuple[cli.Reparent | None, list[_Retarget]]:
        new_parent = parent_of(name) or orientation.trunk.name
        children = [
            branch
            for branch in orientation.members
            if branch.name not in blocked
            and branch.name != name
            and parent_of(branch.name) == new_parent
            and _walk_parent(orientation, branch.name) == name
        ]
        if not children:
            return None, []
        moves = [
            _Retarget(
                pr=child.pull_request.number,
                branch=child.name,
                old_base=child.pull_request.base,
                new_base=new_parent,
                reason=push_phase.RetargetReason.PARENT_EMPTIED,
            )
            for child in children
            if child.pull_request is not None
            and child.pull_request.base != new_parent
        ]
        block = cli.Reparent(
            branch=name,
            cause="emptied",
            children=tuple((child.name, name, new_parent) for child in children),
            retargets=tuple(moves),
        )
        return block, moves

    def _merged_reparents(
        self,
        orientation: Orientation,
        reparented: Sequence[Any],
        originals: dict[str, str | None],
        blocked: set[str],
    ) -> tuple[list[cli.Reparent], list[_Retarget]]:
        """SPEC.md sec 5.2 step 4: a merged branch's children, hoisted off it."""
        groups: dict[str, list[Any]] = {}
        for change in reparented:
            if change.branch in blocked:
                continue
            groups.setdefault(change.old_parent or orientation.trunk.name, []).append(
                change
            )

        blocks: list[cli.Reparent] = []
        retargets: list[_Retarget] = []
        for merged_name, changes in groups.items():
            merged = orientation.branch(merged_name)
            pull_request = merged.pull_request if merged else None
            moves: list[_Retarget] = []
            for change in changes:
                child = orientation.branch(change.branch)
                child_pr = child.pull_request if child else None
                if child_pr is None or child_pr.base == change.new_parent:
                    continue
                moves.append(
                    _Retarget(
                        pr=child_pr.number,
                        branch=change.branch,
                        old_base=child_pr.base,
                        new_base=change.new_parent,
                        reason=push_phase.RetargetReason.PARENT_MERGED,
                    )
                )
            blocks.append(
                cli.Reparent(
                    branch=merged_name,
                    cause="merged",
                    squashed_as=_short(
                        pull_request.merge_commit_sha if pull_request else None
                    ),
                    children=tuple(
                        (change.branch, merged_name, change.new_parent)
                        for change in changes
                    ),
                    retargets=tuple(moves),
                )
            )
            retargets.extend(moves)
        return blocks, retargets

    def _merged_summary(self, orientation: Orientation) -> list[cli.MergedBranch]:
        return [
            cli.MergedBranch(
                name=branch.name,
                pr=branch.pull_request.number if branch.pull_request else None,
                squashed_as=_short(
                    branch.pull_request.merge_commit_sha
                    if branch.pull_request
                    else None
                ),
            )
            for branch in orientation.members
            if branch.state is BranchState.MERGED
        ]

    def _orphans(
        self, orientation: Orientation, blocked: set[str]
    ) -> list[cli.Orphan]:
        """SPEC.md sec 6.3: the rescue, printed and never run (invariant 14)."""
        remote_branches = self.git.branches(f"refs/remotes/{self.remote}")
        slug = self._slug()
        orphans: list[cli.Orphan] = []
        for branch in orientation.members:
            if branch.state is not BranchState.ORPHANED:
                continue
            pull_request = branch.pull_request
            if pull_request is None:  # pragma: no cover - orphan implies a PR
                continue
            base_missing = pull_request.base not in remote_branches
            base = orientation.branch(pull_request.base)
            base_pr = base.pull_request if base else None
            skipped = [
                name
                for name in (b.name for b in orientation.members)
                if name in blocked and name != branch.name
            ]
            orphans.append(
                cli.Orphan(
                    pr=pull_request.number,
                    branch=branch.name,
                    base_pr=base_pr.number if base_missing and base_pr else None,
                    base_branch=pull_request.base if base_missing else None,
                    new_base=_nearest_live_parent(orientation, branch),
                    repo=slug,
                    skipped=tuple(skipped),
                )
            )
        return orphans

    def _missing_pull_requests(
        self, orientation: Orientation, blocked: set[str], parent_of=None
    ) -> list[cli.NoPullRequest]:
        """SPEC.md sec 6.5 / invariant 20: print the command, never create one."""
        remote_branches = self.git.branches(f"refs/remotes/{self.remote}")
        resolve = parent_of or (lambda name: _walk_parent(orientation, name))
        return [
            cli.NoPullRequest(
                name=branch.name,
                parent=self._branch_name(orientation, resolve(branch.name)),
            )
            for branch in orientation.members
            if branch.pull_request is None
            and branch.state is BranchState.LIVE
            and branch.name not in blocked
            and branch.name in remote_branches
        ]

    def _restore_head(self, branch: str | None) -> None:
        """Put HEAD back on the branch the user was on.

        ``git rebase`` checks out the branch it rebased, so a cascade leaves
        HEAD wherever it finished.  Nothing else in stackem moves it.
        """
        if not branch or self.git.rebase_in_progress():
            return
        if self.git.current_branch() == branch:
            return
        if self.git.try_rev_parse(f"refs/heads/{branch}") is None:
            return
        self.git.run("checkout", "--quiet", branch, check=False)

    # ------------------------------------------------------------------
    # stackem parent <b> --onto <p>
    # ------------------------------------------------------------------

    def set_parent(
        self, branch: str, onto: str, *, dry_run: bool = False
    ) -> cli.ParentView:
        """Retarget a pull request.  The base IS the parent record (invariant 1)."""
        orientation = self._orient(allow_ref_write=True)
        record = orientation.branch(branch)
        if record is None and self.git.try_rev_parse(f"refs/heads/{branch}") is None:
            raise cli.CliError(
                f"there is no branch called {branch}.",
                next_command="stackem",
            )
        pull_request = record.pull_request if record else None
        if pull_request is None:
            return cli.ParentView(branch=branch, pr=None, new_base=onto)
        if pull_request.base == onto:
            return cli.ParentView(
                branch=branch,
                pr=pull_request.number,
                old_base=pull_request.base,
                new_base=onto,
                changed=False,
            )
        view = cli.ParentView(
            branch=branch,
            pr=pull_request.number,
            old_base=pull_request.base,
            new_base=onto,
            dry_run=dry_run,
        )
        if dry_run:
            return view
        provider = self.provider
        if provider is None:
            view.error = (
                f"the forge could not be reached: {self.forge_error}"
            )
            return view
        try:
            provider.retarget(pull_request.number, onto)
        except ForgeError as error:
            view.error = str(error)
        return view


# ==========================================================================
# helpers
# ==========================================================================


def _closed_pull_requests(
    orientation: Orientation | None, slug: str = "OWNER/REPO"
) -> list[str]:
    """Say when a member's pull request is closed but the branch is not orphaned.

    SPEC.md sec 5.1 calls a branch orphaned only when its head branch is *also*
    gone from the remote, so a pull request closed by its BASE branch's deletion
    (invariant 13 -- deleting a branch closes every pull request referencing it
    as head **or** base) classifies as live and would otherwise go by in
    silence, restacked and pushed into a pull request nobody can merge.

    stackem still will not reopen it (invariant 14): a person may have closed it
    on purpose, and telling the two apart is impossible.  So it says so.
    """
    if orientation is None:
        return []
    notes = []
    for branch in orientation.members:
        pull_request = branch.pull_request
        if (
            branch.state is BranchState.LIVE
            and pull_request is not None
            and pull_request.state is PullRequestState.CLOSED
        ):
            notes.append(
                f"#{pull_request.number} ({branch.name}) is closed and not merged. "
                "stackem never reopens a pull request (invariant 14); if a branch "
                "deletion closed it, SPEC.md sec 6.3 has the rescue -- restore "
                "both branches, then "
                f"`gh api -X PATCH /repos/{slug}/pulls/{pull_request.number} "
                "-f state=open`."
            )
    return notes


def _walk_parent(orientation: Orientation, name: str) -> str | None:
    """The parent the walk used.

    ``stackem.restack.hoist_over_merged`` mutates ``Branch.parent`` in place, so
    after the cascade this is the parent *after* hoisting over merged branches --
    which is exactly the one the report and the retargets are about.
    """
    branch = orientation.branch(name)
    return branch.parent if branch else None


def _final_parents(hoisted: dict[str, str | None], emptied: set[str]):
    """Resolve a parent past every branch that emptied this run (sec 7.2)."""

    def resolve(name: str | None) -> str | None:
        parent = hoisted.get(name) if name else None
        seen: set[str] = set()
        while parent is not None and parent in emptied and parent not in seen:
            seen.add(parent)
            parent = hoisted.get(parent)
        return parent

    return resolve


def _member_order(members: Sequence[Branch], names: Iterable[str]) -> list[str]:
    wanted = set(names)
    return [branch.name for branch in members if branch.name in wanted]


def _pr_number(orientation: Orientation, name: str) -> int | None:
    branch = orientation.branch(name)
    if branch is None or branch.pull_request is None:
        return None
    return branch.pull_request.number


def _nearest_live_parent(orientation: Orientation, branch: Branch) -> str:
    """Where an orphaned pull request should point once it is reopened."""
    trunk = orientation.trunk.name
    seen: set[str] = set()
    parent = branch.parent
    while parent is not None and parent != trunk and parent not in seen:
        seen.add(parent)
        record = orientation.branch(parent)
        if record is None or record.state is BranchState.LIVE:
            return parent
        parent = record.parent
    return trunk


def _blocked_by_orphan(members: Sequence[Branch]) -> set[str]:
    """Every member whose parent chain reaches an orphaned pull request.

    SPEC.md sec 6.3 and docs/sessions/05: the orphan's branch is gone from the
    remote and its pull request is closed.  Restacking the branches above it
    would build on wreckage the user is being told to rescue by hand, so they
    are skipped and reported instead.  Invariant 6's "skip merged" is the walk's
    own rule; this one has to be applied by the caller, because the walk is
    handed a stack and trusts it.
    """
    by_name = {branch.name: branch for branch in members}
    blocked: set[str] = set()
    for branch in members:
        chain: list[str] = []
        node: Branch | None = branch
        seen: set[str] = set()
        while node is not None and node.name not in seen:
            seen.add(node.name)
            if node.state is BranchState.ORPHANED or node.name in blocked:
                blocked.update(chain)
                blocked.add(node.name)
                break
            chain.append(node.name)
            node = by_name.get(node.parent) if node.parent else None
    return blocked


def _foreign_view(source: ResumeDecision | ForeignRebase | None) -> cli.ForeignRebase:
    """CLAUDE.md invariant 22: whose rebase it is, and why sync will not touch it."""
    state = getattr(source, "state", None)
    branch = getattr(source, "branch", None) or getattr(state, "branch", None)
    onto = _short(getattr(state, "onto", None))
    reason = getattr(source, "reason", "") or ""
    # The CLI already says "<branch> is being rebased onto <onto>", so the
    # reason only has to carry the part it cannot know.
    prefix = f"a rebase of {branch} is in progress, "
    if reason.startswith(prefix):
        reason = reason[len(prefix):]
    elif reason.startswith("a rebase of "):
        reason = reason.split(", ", 1)[-1]
    return cli.ForeignRebase(branch=branch, onto=onto, reason=reason)


def build_engine(
    git: Git | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
    provider: Provider | None = None,
    env: dict[str, str] | None = None,
    remote: str = "origin",
    trunk: str | None = None,
) -> SyncEngine:
    """What :mod:`stackem.cli` calls to get an engine."""
    return SyncEngine(
        git, cwd=cwd, provider=provider, env=env, remote=remote, trunk=trunk
    )


#: The CLI resolves ``build_engine`` first and falls back to ``Engine``.
Engine = SyncEngine
