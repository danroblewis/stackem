"""The cascade: replay every branch in the stack onto its parent's new position.

SPEC.md sec 2, sec 5.2 PHASE 1, sec 7.  Everything here is local and reversible
-- nothing in this module touches the remote (CLAUDE.md invariant 9), and
nothing in it is ever written down (the stack is derived fresh every run).

One primitive
-------------
::

    git rebase --onto <where the parent is NOW> <where the parent WAS> <branch>

*Where the parent is now* is the target: ``origin/<trunk>`` for a root branch
(invariant 4), otherwise the parent's local tip.  *Where the parent was* is the
fork point, ``merge-base(origin/<parent>, <branch>)`` -- the parent's
**last-synced** state, never its local tip (invariant 2).

The walk, bottom-up over the members::

    merged branch                  -> SKIP entirely           (invariant 6)
    is_ancestor(target, branch)    -> SKIP, already based on it (invariant 3)
    guard: is_ancestor(origin/<parent>, branch) else STOP     (invariant 3b)
    fork = merge-base(origin/<parent>, branch)
    git rebase --onto <target> <fork> <branch>

The skip-check comes **before** the guard on purpose: a cascade that stopped on a
conflict leaves the branches below it correctly restacked while their ``origin/``
refs still point at the old tips, and the guard would call those a violation.

Two places this reads sec 5.2's pseudo-code more narrowly than it is written
---------------------------------------------------------------------------
**The guard does not apply to a branch whose parent is the trunk.**
``origin/<trunk>`` is never an ancestor of a branch that needs restacking onto a
moved trunk, so guarding there would call SPEC.md sec 2 STEP 3 -- "trunk moves
-> whole stack rebased", the most ordinary run there is -- a violation, and the
skip-check has already ruled out the case where it would pass.  The guard is for
a parent that was *rewritten* (sec 2 STEP 4); a trunk only fast-forwards, and
``merge-base(origin/<trunk>, branch)`` is the true fork point either way.

**The fork point is derived from the parent BEFORE hoisting.**  Step 4 moves a
child of a merged branch onto the trunk, but where that child forked is still
the merged parent's last-synced tip.  Against the trunk the derivation reaches
back past the merged parent's own commits and replays them on top of the squash
commit -- the conflict invariant 6 exists to avoid.  Only the *target* moves to
the trunk; the fork point stays with the parent the branch actually grew from.

Who calls what
--------------
``restack(git, stack)`` is the whole of PHASE 1: it resumes an in-progress rebase
of its own (or refuses one that is not), hoists children of merged branches to
their nearest unmerged ancestor, snapshots every tip, walks, and -- when the walk
finished -- verifies with ``git range-diff`` before returning.  sync reads
``report.ready_for_push``, then does PHASE 2 (retarget, push).  Retargeting a
pull request is sync's job; the parents this module derived are in
``report.reparented`` and on the Stack it was handed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from stackem.gitx import Git, RangeDiffEntry, RebaseOutcome, RebaseResult, RebaseState
from stackem.model import Branch, BranchState, Stack

__all__ = [
    "BranchAction",
    "BranchResult",
    "ConflictReport",
    "DroppedCommit",
    "ForeignRebase",
    "GuardViolation",
    "Reparent",
    "RestackReport",
    "RestackStatus",
    "ResumeDecision",
    "VerificationEntry",
    "VerificationReport",
    "hoist_over_merged",
    "read_resume",
    "restack",
    "target_ref",
    "verify",
]


class RestackStatus(Enum):
    """How PHASE 1 ended.

    Only ``OK`` may be followed by PHASE 2, and only once the verification
    attached to the report also passes (invariant 9).
    """

    OK = "ok"
    CONFLICT = "conflict"
    GUARD_VIOLATION = "guard_violation"
    FOREIGN_REBASE = "foreign_rebase"


class BranchAction(Enum):
    """What the walk did with one branch."""

    RESTACKED = "restacked"  # rebased onto its parent's new tip
    RESUMED = "resumed"  # a conflicted rebase of ours, continued to completion
    SKIPPED_UP_TO_DATE = "skipped-up-to-date"  # already based on the target
    SKIPPED_MERGED = "skipped-merged"  # invariant 6
    BLOCKED = "blocked"  # the guard fired (invariant 3b)
    CONFLICTED = "conflicted"  # stopped here, git's rebase state is live


@dataclass(frozen=True)
class Reparent:
    """A child hoisted off a merged parent (invariant 7).

    sync retargets the pull request's base to ``new_parent`` in PHASE 2; that
    retarget *is* the reparenting, because the PR base is the parent record.
    """

    branch: str
    old_parent: str | None
    new_parent: str


@dataclass(frozen=True)
class DroppedCommit:
    """A commit that did not survive the replay (SPEC.md sec 7.2).

    Found structurally, by comparing the old range with the new one -- never by
    parsing rebase output, which says nothing at all when a conflict is resolved
    to an empty diff (invariant 10).
    """

    branch: str
    sha: str | None
    subject: str


@dataclass
class BranchResult:
    """What happened to one branch in the walk."""

    branch: str
    action: BranchAction
    parent: str | None = None  # the parent the TARGET came from (after hoisting)
    fork_parent: str | None = None  # the parent the FORK POINT came from (before)
    target: str | None = None
    fork: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    emptied: bool = False
    dropped: tuple[DroppedCommit, ...] = ()
    range_diff: tuple[RangeDiffEntry, ...] = ()
    resolved_conflict: bool = False
    #: the parent's tip at the start of the run; a replayed commit reachable from
    #: it belonged to the parent, not to this branch (see :func:`verify`).
    carry_base: str | None = field(default=None, repr=False)

    @property
    def changed(self) -> bool:
        return bool(self.new_sha) and self.new_sha != self.old_sha


@dataclass(frozen=True)
class GuardViolation:
    """``origin/<parent>`` is not an ancestor of the branch (invariant 3b).

    The parent was force-pushed without restacking its children, so the fork
    point cannot be derived.  Rebasing anyway would conflict on the parent's own
    commit, so the walk stops before touching anything.
    """

    branch: str
    parent: str | None
    parent_ref: str | None
    reason: str


@dataclass(frozen=True)
class ConflictReport:
    """Everything the CLI needs to print SPEC.md sec 8's conflict block."""

    branch: str
    parent: str | None
    target: str | None
    fork: str | None
    stopped_sha: str | None
    stopped_subject: str | None
    files: tuple[str, ...]
    queued: tuple[str, ...]
    result: RebaseResult | None = None


@dataclass(frozen=True)
class ForeignRebase:
    """A rebase that is not ours (invariant 22).

    sync refuses: continuing a user's own ``git rebase -i`` and cascading on top
    of it is the failure mode the check exists to prevent.
    """

    state: RebaseState
    reason: str


@dataclass(frozen=True)
class VerificationEntry:
    """One ``git range-diff`` line, classified (SPEC.md sec 6.1)."""

    branch: str
    status: str  # "=" identical, "!" changed, "<" only in the old range, ">" only in the new
    subject: str
    old_sha: str | None
    new_sha: str | None
    dropped: bool = False  # a commit of this branch's own that did not survive
    carried: bool = False  # a "<" that belonged to the parent, not to this branch
    rewritten: bool = False  # a "<" whose commit came back as a ">": changed, not lost
    expected: bool = True


@dataclass(frozen=True)
class VerificationReport:
    """Step 7: every restacked branch's old range against its new one.

    ``ok`` is the gate on PHASE 2.  ``=`` is a byte-identical patch and a clean
    drop is reported rather than fatal; anything else is an unexpected change and
    stops sync before the remote.

    The one exception: a branch whose conflict the user resolved by hand *must*
    come out with a different patch, so ``!`` and ``>`` on a resumed branch are
    expected.  Without that carve-out SPEC.md sec 8's "resolve, then run
    ``stackem sync`` again" could never reach PHASE 2.
    """

    entries: tuple[VerificationEntry, ...] = ()
    unexpected: tuple[VerificationEntry, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.unexpected

    @property
    def dropped(self) -> tuple[VerificationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.dropped)


@dataclass
class RestackReport:
    """The result of PHASE 1."""

    status: RestackStatus
    stack: Stack
    results: list[BranchResult] = field(default_factory=list)
    snapshot: dict[str, str] = field(default_factory=dict)
    reparented: list[Reparent] = field(default_factory=list)
    queued: tuple[str, ...] = ()
    conflict: ConflictReport | None = None
    guard: GuardViolation | None = None
    foreign: ForeignRebase | None = None
    verification: VerificationReport | None = None

    @property
    def by_branch(self) -> dict[str, BranchResult]:
        return {result.branch: result for result in self.results}

    @property
    def changed_branches(self) -> tuple[str, ...]:
        """Branches whose tip moved -- exactly what PHASE 2 pushes."""
        return tuple(result.branch for result in self.results if result.changed)

    @property
    def restacked(self) -> tuple[str, ...]:
        return tuple(
            result.branch
            for result in self.results
            if result.action in (BranchAction.RESTACKED, BranchAction.RESUMED)
        )

    @property
    def emptied_branches(self) -> tuple[str, ...]:
        """Branches with no commits left (SPEC.md sec 7.2)."""
        return tuple(result.branch for result in self.results if result.emptied)

    @property
    def dropped(self) -> tuple[DroppedCommit, ...]:
        return tuple(
            commit for result in self.results for commit in result.dropped
        )

    @property
    def ready_for_push(self) -> bool:
        """Invariant 9: local work finished AND verified, or the remote is off limits."""
        return (
            self.status is RestackStatus.OK
            and self.verification is not None
            and self.verification.ok
        )


@dataclass(frozen=True)
class ResumeDecision:
    """PHASE 0: is there a rebase in progress, and is it ours? (invariant 22)"""

    in_progress: bool
    ours: bool
    branch: str | None = None
    state: RebaseState | None = None
    resolved: bool = False
    reason: str | None = None


# --------------------------------------------------------------------------
# deriving the target and the fork point
# --------------------------------------------------------------------------

def target_ref(stack: Stack, parent: str | None) -> str:
    """Where the parent is NOW.

    ``origin/<trunk>`` when the parent is the trunk -- never the local trunk,
    which sync does not fast-forward (CLAUDE.md invariant 4) -- otherwise the
    parent branch itself, whose ref *is* its tip.
    """
    if parent is None or parent == stack.trunk:
        return f"{stack.remote}/{stack.trunk}"
    return parent


def _fork_ref(
    git: Git, stack: Stack, parent: str | None, snapshot: dict[str, str]
) -> tuple[str | None, bool]:
    """Where the parent WAS, as a rev, plus whether the guard applies to it.

    ``origin/<parent>`` is the parent's last-synced state and the spec's answer
    (invariant 2).  Two fallbacks, for parents that have no remote ref: a merged
    branch deleted on merge, and a branch never pushed at all.  Both fall back to
    the parent's tip at the START of this run -- never its tip after the cascade
    has already moved it, which would replay the parent's own commits.
    """
    if parent is None:
        parent = stack.trunk
    remote = f"{stack.remote}/{parent}"
    if git.try_rev_parse(remote):
        return remote, parent != stack.trunk
    if parent in snapshot:
        return snapshot[parent], parent != stack.trunk
    if git.try_rev_parse(parent):
        return parent, False
    return None, False


def hoist_over_merged(stack: Stack) -> list[Reparent]:
    """Reparent every child of a merged branch onto its nearest UNMERGED ancestor.

    CLAUDE.md invariant 7: transitive, to a fixpoint.  Hoisting one level when two
    pull requests land the same morning leaves a branch parented to a branch that
    is about to be deleted -- and deleting it closes that child's pull request.

    Mutates ``Branch.parent`` on the stack it is handed (that is the derived
    parent for the rest of the run) and returns what it changed, so sync can
    retarget those pull requests in PHASE 2.
    """
    by_name = {branch.name: branch for branch in stack.branches}
    merged = {
        branch.name
        for branch in stack.branches
        if branch.state is BranchState.MERGED
    }
    changes: list[Reparent] = []
    for branch in stack.branches:
        if branch.state is BranchState.MERGED:
            continue
        parent = branch.parent
        seen: set[str] = set()
        while parent is not None and parent in merged and parent not in seen:
            seen.add(parent)
            above = by_name.get(parent)
            parent = above.parent if above is not None else stack.trunk
        if parent is None or parent in merged:
            parent = stack.trunk
        if parent != branch.parent:
            changes.append(
                Reparent(branch=branch.name, old_parent=branch.parent, new_parent=parent)
            )
            branch.parent = parent
    return changes


# --------------------------------------------------------------------------
# PHASE 0 -- resume
# --------------------------------------------------------------------------

def read_resume(git: Git, stack: Stack) -> ResumeDecision:
    """Decide whether an in-progress rebase is stackem's (CLAUDE.md invariant 22).

    It is ours **iff** ``head-name`` is a member of the derived stack AND ``onto``
    equals the tip of that branch's derived parent.  Anything else is the user's
    own rebase; sync refuses rather than continuing it.

    Call this after :func:`hoist_over_merged`, so the derived parent is the one
    the walk would use.
    """
    state = git.read_rebase_state()
    if state is None:
        return ResumeDecision(in_progress=False, ours=False)

    members = {branch.name: branch for branch in stack.branches}
    branch = state.branch
    resolved = not git.conflicted_files()
    if branch is None:
        return ResumeDecision(
            in_progress=True,
            ours=False,
            branch=None,
            state=state,
            resolved=resolved,
            reason=(
                f"a rebase of {state.head_name or 'a detached HEAD'} is in progress, "
                "which is not a branch stackem restacks"
            ),
        )
    if branch not in members:
        return ResumeDecision(
            in_progress=True,
            ours=False,
            branch=branch,
            state=state,
            resolved=resolved,
            reason=f"a rebase of {branch} is in progress, and {branch} is not in the stack",
        )

    parent = members[branch].parent
    ref = target_ref(stack, parent)
    tip = git.try_rev_parse(ref)
    onto = state.onto
    if tip is None or onto is None or not _same_commit(tip, onto):
        return ResumeDecision(
            in_progress=True,
            ours=False,
            branch=branch,
            state=state,
            resolved=resolved,
            reason=(
                f"a rebase of {branch} is in progress, but it is being rebased onto "
                f"{(onto or '?')[:7]}, which is not the tip of {parent or stack.trunk} "
                f"({(tip or '?')[:7]})"
            ),
        )
    return ResumeDecision(
        in_progress=True,
        ours=True,
        branch=branch,
        state=state,
        resolved=resolved,
    )


def _same_commit(a: str, b: str) -> bool:
    """Compare two object ids, either of which may be abbreviated."""
    shortest = min(len(a), len(b))
    return shortest >= 4 and a[:shortest] == b[:shortest]


# --------------------------------------------------------------------------
# PHASE 1 -- the walk
# --------------------------------------------------------------------------

def restack(git: Git, stack: Stack) -> RestackReport:
    """Run PHASE 1 over ``stack`` and report what happened.

    Steps 4 through 7 of SPEC.md sec 5.2, plus the PHASE 0 resume check.  The
    stack's branches must be ordered bottom-up (parents before children), which
    is what :class:`~stackem.model.Stack` promises.

    Nothing here pushes, retargets, closes or deletes anything (invariants 9 and
    14).  On a conflict it leaves git's ordinary rebase state exactly as git left
    it: ``git rebase --abort`` backs out, and the next ``stackem sync`` resumes.
    """
    members = [branch for branch in stack.branches if branch.name != stack.trunk]
    reparented = hoist_over_merged(stack)
    # Step 5: every member tip as it was before the walk.  sync restores from
    # this if the push in PHASE 2 is rejected, and the fork point of a branch
    # whose parent has no remote ref is derived from it.
    snapshot = {
        name: sha
        for name, sha in ((b.name, git.try_rev_parse(b.name)) for b in members)
        if sha
    }
    report = RestackReport(
        status=RestackStatus.OK,
        stack=stack,
        snapshot=snapshot,
        reparented=reparented,
    )

    resume = read_resume(git, stack)
    if resume.in_progress and not resume.ours:
        report.status = RestackStatus.FOREIGN_REBASE
        report.foreign = ForeignRebase(state=resume.state, reason=resume.reason or "")
        report.queued = tuple(branch.name for branch in members)
        return report

    done: dict[str, BranchResult] = {}
    if resume.in_progress and resume.ours:
        finished = _continue_ours(git, stack, members, resume, report)
        if finished is None:
            return report
        done[finished.branch] = finished

    for branch in members:
        if branch.name in done:
            report.results.append(done[branch.name])
            continue
        if branch.state is BranchState.MERGED:
            # Invariant 6.  Replaying a merged branch either drops all its commits
            # -- so sync mistakes it for an emptied branch -- or conflicts against
            # the squash commit and halts the cascade forever.
            report.results.append(
                BranchResult(
                    branch=branch.name,
                    action=BranchAction.SKIPPED_MERGED,
                    parent=branch.parent,
                    old_sha=snapshot.get(branch.name),
                    new_sha=snapshot.get(branch.name),
                )
            )
            continue

        result = _restack_one(git, stack, branch, snapshot, report, members)
        report.results.append(result)
        if result.action in (BranchAction.BLOCKED, BranchAction.CONFLICTED):
            return report

    report.verification = verify(git, report)
    return report


def _remaining(members: list[Branch], after: str, *, inclusive: bool = False) -> tuple[str, ...]:
    names = [branch.name for branch in members]
    index = names.index(after)
    return tuple(names[index if inclusive else index + 1:])


def _restack_one(
    git: Git,
    stack: Stack,
    branch: Branch,
    snapshot: dict[str, str],
    report: RestackReport,
    members: list[Branch],
) -> BranchResult:
    name = branch.name
    parent = branch.parent
    ref = target_ref(stack, parent)
    target = git.rev_parse(ref)
    old_sha = snapshot.get(name) or git.rev_parse(name)

    # Invariant 3: "already based on target" comes BEFORE the guard, and needs no
    # fork point.  This is what makes a half-finished cascade self-heal.
    if git.is_ancestor(target, name):
        return BranchResult(
            branch=name,
            action=BranchAction.SKIPPED_UP_TO_DATE,
            parent=parent,
            target=target,
            old_sha=old_sha,
            new_sha=old_sha,
            emptied=git.rev_list_count(f"{target}..{name}") == 0,
        )

    fork_parent = _original_parent(report, name, parent)
    fork_from, guarded = _fork_ref(git, stack, fork_parent, snapshot)
    carry_base = _carry_base(git, fork_parent, fork_from, snapshot)

    if guarded and fork_from is not None and not git.is_ancestor(fork_from, name):
        # Invariant 3b: the parent was force-pushed without restacking its
        # children.  Stop before rebasing anything, rather than conflicting on
        # the parent's own commit.
        report.status = RestackStatus.GUARD_VIOLATION
        report.guard = GuardViolation(
            branch=name,
            parent=fork_parent,
            parent_ref=fork_from,
            reason=(
                f"{fork_from} is not an ancestor of {name}: {fork_parent} was "
                "force-pushed without restacking its children, so the fork point "
                "cannot be derived"
            ),
        )
        report.queued = _remaining(members, name, inclusive=True)
        return BranchResult(
            branch=name,
            action=BranchAction.BLOCKED,
            parent=parent,
            fork_parent=fork_parent,
            target=target,
            old_sha=old_sha,
            new_sha=old_sha,
        )

    fork = git.merge_base(fork_from, name) if fork_from else None
    if fork is None:
        report.status = RestackStatus.GUARD_VIOLATION
        report.guard = GuardViolation(
            branch=name,
            parent=fork_parent,
            parent_ref=fork_from,
            reason=f"no merge base between {fork_from or fork_parent} and {name}",
        )
        report.queued = _remaining(members, name, inclusive=True)
        return BranchResult(
            branch=name,
            action=BranchAction.BLOCKED,
            parent=parent,
            fork_parent=fork_parent,
            target=target,
            old_sha=old_sha,
            new_sha=old_sha,
        )

    outcome = git.rebase_onto(target, fork, name)
    if outcome.outcome is RebaseOutcome.CONFLICT:
        report.status = RestackStatus.CONFLICT
        queued = _remaining(members, name)
        report.queued = queued
        report.conflict = ConflictReport(
            branch=name,
            parent=parent,
            target=target,
            fork=fork,
            stopped_sha=outcome.stopped_sha,
            stopped_subject=outcome.stopped_subject,
            files=outcome.conflicted_files,
            queued=queued,
            result=outcome,
        )
        return BranchResult(
            branch=name,
            action=BranchAction.CONFLICTED,
            parent=parent,
            fork_parent=fork_parent,
            target=target,
            fork=fork,
            old_sha=old_sha,
            new_sha=None,
            carry_base=carry_base,
        )

    new_sha = git.rev_parse(name)
    return BranchResult(
        branch=name,
        action=BranchAction.RESTACKED,
        parent=parent,
        fork_parent=fork_parent,
        target=target,
        fork=fork,
        old_sha=old_sha,
        new_sha=new_sha,
        emptied=git.rev_list_count(f"{target}..{name}") == 0,
        carry_base=carry_base,
    )


def _original_parent(report: RestackReport, name: str, parent: str | None) -> str | None:
    """The parent the FORK POINT is derived from: the one before hoisting.

    After a squash merge the target moves to the trunk, but "where the branch
    forked" is still the merged parent's last-synced tip -- deriving the fork
    against the trunk would replay the merged parent's commits on top of the
    squash commit, which is the conflict invariant 6 exists to avoid.
    """
    for change in report.reparented:
        if change.branch == name:
            return change.old_parent
    return parent


def _carry_base(
    git: Git, parent: str | None, fork_from: str | None, snapshot: dict[str, str]
) -> str | None:
    """The parent's tip at the START of this run.

    A commit in the branch's replayed range that is reachable from here belonged
    to the parent, not to the branch -- see :func:`verify`.  It is the snapshot,
    not ``origin/<parent>``: a commit the parent has not pushed yet is exactly the
    case this distinguishes, and it is never an ancestor of ``origin/<parent>``.
    """
    if parent is not None and parent in snapshot:
        return snapshot[parent]
    return git.try_rev_parse(fork_from) if fork_from else None


def _continue_ours(
    git: Git,
    stack: Stack,
    members: list[Branch],
    resume: ResumeDecision,
    report: RestackReport,
) -> BranchResult | None:
    """Finish the rebase PHASE 0 recognised as ours, or report it still conflicted."""
    name = resume.branch or ""
    state = resume.state
    branch = next(b for b in members if b.name == name)
    parent = branch.parent
    target = state.onto if state else None
    old_sha = report.snapshot.get(name) or (state.orig_head if state else None)
    fork_parent = _original_parent(report, name, parent)
    fork_from, _ = _fork_ref(git, stack, fork_parent, report.snapshot)
    carry_base = _carry_base(git, fork_parent, fork_from, report.snapshot)
    fork = git.merge_base(fork_from, old_sha) if fork_from and old_sha else None

    if not resume.resolved:
        report.status = RestackStatus.CONFLICT
        queued = _remaining(members, name)
        report.queued = queued
        report.conflict = ConflictReport(
            branch=name,
            parent=parent,
            target=target,
            fork=fork,
            stopped_sha=state.stopped_sha if state else None,
            stopped_subject=_stopped_subject(git, state),
            files=git.conflicted_files(),
            queued=queued,
        )
        report.results.append(
            BranchResult(
                branch=name,
                action=BranchAction.CONFLICTED,
                parent=parent,
                fork_parent=fork_parent,
                target=target,
                fork=fork,
                old_sha=old_sha,
            )
        )
        return None

    outcome = git.rebase_continue()
    if outcome.outcome is RebaseOutcome.CONFLICT and not git.conflicted_files():
        # SPEC.md sec 7.2: the resolution left an empty diff, so there is nothing
        # to commit and git wants the patch skipped.  The drop is still detected
        # structurally, by the range-diff in verify() -- git says nothing at all
        # about it (invariant 10).
        outcome = _rebase_skip(git, name, target)
    if outcome.outcome is RebaseOutcome.CONFLICT:
        report.status = RestackStatus.CONFLICT
        queued = _remaining(members, name)
        report.queued = queued
        report.conflict = ConflictReport(
            branch=name,
            parent=parent,
            target=target,
            fork=fork,
            stopped_sha=outcome.stopped_sha,
            stopped_subject=outcome.stopped_subject,
            files=outcome.conflicted_files,
            queued=queued,
            result=outcome,
        )
        report.results.append(
            BranchResult(
                branch=name,
                action=BranchAction.CONFLICTED,
                parent=parent,
                fork_parent=fork_parent,
                target=target,
                fork=fork,
                old_sha=old_sha,
                resolved_conflict=True,
            )
        )
        return None

    new_sha = git.rev_parse(name)
    return BranchResult(
        branch=name,
        action=BranchAction.RESUMED,
        parent=parent,
        fork_parent=fork_parent,
        target=target,
        fork=fork,
        old_sha=old_sha,
        new_sha=new_sha,
        emptied=bool(target) and git.rev_list_count(f"{target}..{name}") == 0,
        resolved_conflict=True,
        carry_base=carry_base,
    )


def _rebase_skip(git: Git, branch: str, onto: str | None) -> RebaseResult:
    """``git rebase --skip``: the only way past a patch that resolved to nothing."""
    proc = git.run("rebase", "--skip", check=False)
    if proc.returncode == 0:
        return RebaseResult(
            outcome=RebaseOutcome.OK,
            branch=branch,
            onto=onto,
            upstream=None,
            returncode=0,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    state = git.read_rebase_state()
    return RebaseResult(
        outcome=RebaseOutcome.CONFLICT,
        branch=branch,
        onto=onto,
        upstream=None,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        conflicted_files=git.conflicted_files(),
        stopped_sha=state.stopped_sha if state else None,
        stopped_subject=_stopped_subject(git, state),
    )


def _stopped_subject(git: Git, state: RebaseState | None) -> str | None:
    if state is None:
        return None
    if state.stopped_sha:
        return git.commit_subject(state.stopped_sha)
    if state.message:
        return state.message.splitlines()[0]
    return None


# --------------------------------------------------------------------------
# step 7 -- verification (SPEC.md sec 6.1)
# --------------------------------------------------------------------------

def verify(git: Git, report: RestackReport) -> VerificationReport:
    """Range-diff every branch the walk moved, before anything reaches the remote.

    ``git range-diff <old fork>..<old tip> <new fork>..<new tip>`` -- each branch
    against **its own** new fork point (SPEC.md sec 6.1).  ``=`` is a
    byte-identical patch.  A ``<`` is a commit that did not survive the replay:
    that is SPEC.md sec 7.2's real hazard and CLAUDE.md invariant 10's structural
    detection, and it is reported, not fatal.

    One ``<`` is *not* a loss: a commit the parent owned and had not pushed yet
    gets replayed along with the branch's own commits (the fork point is
    ``origin/<parent>``, which predates it) and is dropped as already upstream.
    Such a commit is reachable from the parent's tip at the start of the run, so
    it is marked ``carried`` rather than dropped.

    Nor is a ``<`` a loss when the same commit comes back as a ``>``: a conflict
    the user resolved by hand can change a patch past the point where range-diff
    will pair the two halves.  Those are marked ``rewritten``.
    """
    entries: list[VerificationEntry] = []
    unexpected: list[VerificationEntry] = []
    for result in report.results:
        if result.action not in (BranchAction.RESTACKED, BranchAction.RESUMED):
            continue
        if not (result.fork and result.target and result.new_sha and result.old_sha):
            continue
        raw = _compare_ranges(
            git, result.fork, result.old_sha, result.target, result.new_sha
        )
        result.range_diff = tuple(raw)
        dropped: list[DroppedCommit] = []
        # A conflict resolved by hand can change a patch so much that range-diff
        # stops pairing it and prints "<" then ">" for the same commit.  The
        # commit survived; pairing them back up by subject keeps it out of the
        # dropped list, which is reserved for commits that really are gone.
        returning: dict[str, int] = {}
        for line in raw:
            if line.status == ">":
                returning[line.subject] = returning.get(line.subject, 0) + 1
        for line in raw:
            carried = bool(
                line.status == "<"
                and line.old_sha
                and result.carry_base
                and git.is_ancestor(line.old_sha, result.carry_base)
            )
            rewritten = False
            if line.status == "<" and not carried and returning.get(line.subject):
                returning[line.subject] -= 1
                rewritten = True
            is_drop = line.status == "<" and not carried and not rewritten
            expected = line.status in ("=", "<") or result.resolved_conflict
            entry = VerificationEntry(
                branch=result.branch,
                status=line.status,
                subject=line.subject,
                old_sha=line.old_sha,
                new_sha=line.new_sha,
                dropped=is_drop,
                carried=carried,
                rewritten=rewritten,
                expected=expected,
            )
            entries.append(entry)
            if not expected:
                unexpected.append(entry)
            if is_drop:
                dropped.append(
                    DroppedCommit(
                        branch=result.branch, sha=line.old_sha, subject=line.subject
                    )
                )
        result.dropped = tuple(dropped)
    return VerificationReport(entries=tuple(entries), unexpected=tuple(unexpected))


def _compare_ranges(
    git: Git, old_base: str, old_tip: str, new_base: str, new_tip: str
) -> list[RangeDiffEntry]:
    """``git range-diff``, with the degenerate ranges git refuses handled here.

    ``git range-diff X..X ...`` is "fatal: need two commit ranges", and an emptied
    branch (SPEC.md sec 7.2) has exactly that on the new side -- which is the one
    case where every commit was dropped and reporting it matters most.
    """
    old_empty = _same_commit(old_base, old_tip)
    new_empty = _same_commit(new_base, new_tip)
    if old_empty and new_empty:
        return []
    if new_empty:
        return _one_sided(git, f"{old_base}..{old_tip}", status="<", old=True)
    if old_empty:
        return _one_sided(git, f"{new_base}..{new_tip}", status=">", old=False)
    return git.range_diff(f"{old_base}..{old_tip}", f"{new_base}..{new_tip}")


def _one_sided(git: Git, rev_range: str, *, status: str, old: bool) -> list[RangeDiffEntry]:
    """Every commit in one range, as range-diff entries with no counterpart."""
    shas = list(reversed(git.rev_list(rev_range)))  # rev-list is newest-first
    entries = []
    for index, sha in enumerate(shas, start=1):
        entries.append(
            RangeDiffEntry(
                old_index=index if old else None,
                old_sha=sha if old else None,
                status=status,
                new_index=None if old else index,
                new_sha=None if old else sha,
                subject=git.commit_subject(sha),
            )
        )
    return entries
