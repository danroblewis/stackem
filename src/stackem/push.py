"""PHASE 2 -- the remote, ordered, irreversible half of sync.

SPEC.md sec 5.2 steps 8-10 and sec 6.2.  Three things happen here, in this order
and no other:

    8.  retarget pull request bases to match the derived parents, children of
        merged and of emptied branches first (CLAUDE.md invariant 15);
    9.  ``git push --atomic --force-with-lease --force-if-includes`` every
        changed branch in one go (invariants 11 and 12), restoring local tips
        from the in-memory snapshot if it is rejected;
    10. report -- including, for everything stackem refuses to do itself, the
        exact command the user should run (invariant 14).

Why the input type is shaped the way it is
------------------------------------------
CLAUDE.md invariant 9: *all local work completes and verifies before anything
touches the remote.*  So there is no way to call this module with unverified
work:

* :class:`VerifiedLocalWork` refuses to exist unless the local phase ran to
  completion and every restacked branch carries a :class:`Verification` whose
  verdict is **computed from git's own range-diff signs** -- ``ok`` is not a
  field anybody can set;
* :func:`run_remote_phase` then re-checks the repository itself -- no rebase in
  progress, a clean worktree, and every branch still sitting exactly where it
  was verified -- because a caller could always build a plausible-looking object
  out of stale data.

What this module never does
---------------------------
It never closes a pull request, deletes a branch, or reopens a closed one
(invariant 14).  Auto-rescue has a verified false positive -- a person can close
a PR *and* delete its branch on purpose -- and it forms an infinite flip-flop
with empty-branch removal.  Every such action comes back as a
:class:`FollowUp` carrying the literal commands for the CLI to print.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from stackem.forge import ForgeError, Provider
from stackem.gitx import Git, RangeDiffEntry
from stackem.model import BranchState, PullRequest

__all__ = [
    "PUSH_FLAGS",
    "BranchChange",
    "FollowUp",
    "FollowUpKind",
    "LocalWorkNotVerified",
    "PushOutcome",
    "RemoteResult",
    "Retarget",
    "RetargetFailure",
    "RetargetReason",
    "Verification",
    "VerifiedLocalWork",
    "close_and_delete_commands",
    "create_pull_request_command",
    "delete_merged_branch_commands",
    "order_retargets",
    "push_branches",
    "rescue_commands",
    "restore_tips",
    "run_remote_phase",
]

#: SPEC.md sec 6.2, verified both ways: these bare flags block a teammate
#: clobber and permit a legitimate post-rebase push, with no stored push-point.
#: ``--atomic`` is not optional either -- a sequential push leaves a window where
#: a child's pull request displays the whole stack (invariant 12).
PUSH_FLAGS = ("--atomic", "--force-with-lease", "--force-if-includes")

#: Range-diff signs that mean "this branch replayed correctly": a byte-identical
#: patch, or a commit that is simply gone because its contents were already
#: upstream (SPEC.md sec 6.1 and sec 7.2).  ``!`` and ``>`` are not acceptable.
_ACCEPTED_RANGE_DIFF = frozenset("=<")

_REJECTED_LINE = re.compile(r"^\s*!\s*\[(?:rejected|remote rejected)\]\s+(?P<local>\S+)\s*->")


class LocalWorkNotVerified(RuntimeError):
    """Phase 2 was asked to run on work phase 1 did not finish and verify.

    CLAUDE.md invariant 9.  Raised both when the plan itself is inconsistent and
    when the repository disagrees with it.
    """


class PushOutcome(Enum):
    PUSHED = "pushed"
    NOTHING_TO_PUSH = "nothing-to-push"
    REJECTED = "rejected"
    RETARGET_FAILED = "retarget-failed"


class RetargetReason(Enum):
    """Why a pull request's base has to move.

    The first two are urgent: their old base is a branch that is about to be
    deleted, and deleting it would close this pull request (invariant 13).  They
    go first (invariant 15).
    """

    PARENT_MERGED = "parent-merged"
    PARENT_EMPTIED = "parent-emptied"
    REPARENTED = "reparented"


class FollowUpKind(Enum):
    MERGED = "merged"
    EMPTIED = "emptied"
    ORPHANED = "orphaned"
    NO_PULL_REQUEST = "no-pull-request"
    DELETE_BRANCH_ON_MERGE = "delete-branch-on-merge"


_URGENT_REASONS = (RetargetReason.PARENT_MERGED, RetargetReason.PARENT_EMPTIED)


@dataclass(frozen=True)
class Verification:
    """The range-diff of one branch's old range against its new one.

    SPEC.md sec 6.1: each branch is compared against **its own** new fork point.
    There is deliberately no ``ok`` field -- the verdict is read off git's signs,
    so no caller can assert a branch is fine.
    """

    branch: str
    old_range: str
    new_range: str
    entries: tuple[RangeDiffEntry, ...] = ()
    #: This branch's rebase stopped on a conflict the user resolved by hand.
    #: SPEC.md sec 8 makes that flow mandatory ("resolve, `git add`, run sync
    #: again"), and a resolution necessarily changes the patch -- so "!" and ">"
    #: are expected on THIS branch and nowhere else.  Without the carve-out the
    #: resolution could never be pushed, and phase 1 (stackem.restack.verify)
    #: already applies exactly the same rule to exactly the same branches.
    resolved_conflict: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))

    @property
    def dropped(self) -> tuple[RangeDiffEntry, ...]:
        """Commits that exist only in the old range -- SPEC.md sec 7.2.

        Detected structurally, never from rebase output (invariant 10).
        """
        return tuple(e for e in self.entries if e.status == "<")

    @property
    def unexpected(self) -> tuple[RangeDiffEntry, ...]:
        if self.resolved_conflict:
            return ()
        return tuple(e for e in self.entries if e.status not in _ACCEPTED_RANGE_DIFF)

    @property
    def ok(self) -> bool:
        return not self.unexpected

    def describe(self) -> str:
        return ", ".join(f"{e.status} {e.subject}" for e in self.unexpected)


@dataclass(frozen=True)
class BranchChange:
    """What phase 1 did to one branch, and where the remote stands.

    ``before`` is the tip from the in-memory snapshot (SPEC.md sec 5.2 step 5) --
    the value a rejected push restores.  ``after`` is the local tip now.
    ``remote`` is ``origin/<branch>`` as of the phase 1 fetch, or ``None`` when
    the branch is not on the remote at all.
    """

    branch: str
    before: str
    after: str
    remote: str | None = None
    state: BranchState = BranchState.LIVE
    parent: str | None = None
    pull_request: PullRequest | None = None
    verification: Verification | None = None
    emptied: bool = False

    @property
    def restacked(self) -> bool:
        return self.before != self.after

    @property
    def needs_push(self) -> bool:
        """Whether this branch goes in the push.

        A merged or orphaned branch never does: its remote branch may be gone,
        and pushing it back is a rescue, which stackem never performs by itself
        (invariant 14).
        """
        if self.state is not BranchState.LIVE:
            return False
        if self.emptied:
            # SPEC.md sec 7.2 / docs/sessions/06: an emptied branch is leaving
            # the chain and the user is being handed the commands to close its
            # pull request and delete it.  Force-pushing it first would replace
            # the remote branch with zero commits -- destroying, on the remote,
            # exactly the work the report says is recoverable.
            return False
        return self.remote is None or self.remote != self.after

    @property
    def on_remote(self) -> bool:
        return self.remote is not None


@dataclass(frozen=True)
class Retarget:
    """One pull request base to move.  The base IS the parent (invariant 1)."""

    pr_number: int
    branch: str
    old_base: str
    new_base: str
    reason: RetargetReason = RetargetReason.REPARENTED

    @property
    def needed(self) -> bool:
        return self.old_base != self.new_base

    @property
    def urgent(self) -> bool:
        return self.reason in _URGENT_REASONS


@dataclass(frozen=True)
class RetargetFailure:
    retarget: Retarget
    error: str


@dataclass(frozen=True)
class FollowUp:
    """Something only a human should do, with the exact commands to do it."""

    kind: FollowUpKind
    note: str
    commands: tuple[str, ...] = ()
    branch: str | None = None
    pr_number: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", tuple(self.commands))


@dataclass(frozen=True)
class VerifiedLocalWork:
    """A finished, verified phase 1 -- the only key that opens phase 2.

    Constructing one asserts invariant 9.  It cannot be built from a branch that
    was restacked into something other than "=" or a clean drop, nor from a
    cascade that stopped on a conflict.
    """

    changes: tuple[BranchChange, ...] = ()
    retargets: tuple[Retarget, ...] = ()
    local_complete: bool = False
    head_branch: str | None = None
    delete_branch_on_merge: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "retargets", tuple(self.retargets))
        if not self.local_complete:
            raise LocalWorkNotVerified(
                "the local phase did not run to completion; nothing may touch the "
                "remote until it has (CLAUDE.md invariant 9)"
            )
        for change in self.changes:
            if not change.restacked:
                continue
            verification = change.verification
            if verification is None:
                raise LocalWorkNotVerified(
                    f"{change.branch} was restacked but never range-diffed "
                    "(SPEC.md sec 5.2 step 7)"
                )
            if verification.branch != change.branch:
                raise LocalWorkNotVerified(
                    f"{change.branch} carries a verification for "
                    f"{verification.branch}"
                )
            if not verification.ok:
                raise LocalWorkNotVerified(
                    f"{change.branch} did not replay cleanly: "
                    f"{verification.describe()} -- stopping before the remote "
                    "(SPEC.md sec 6.1)"
                )

    # -- convenience -------------------------------------------------------

    def change(self, branch: str) -> BranchChange | None:
        for candidate in self.changes:
            if candidate.branch == branch:
                return candidate
        return None

    @property
    def pushable(self) -> tuple[BranchChange, ...]:
        return tuple(change for change in self.changes if change.needs_push)


@dataclass(frozen=True)
class RemoteResult:
    """What phase 2 did, for the CLI to print (invariant 23 is the CLI's job)."""

    outcome: PushOutcome
    retargeted: tuple[Retarget, ...] = ()
    retarget_failures: tuple[RetargetFailure, ...] = ()
    pushed: tuple[str, ...] = ()
    attempted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()
    push_command: str = ""
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (PushOutcome.PUSHED, PushOutcome.NOTHING_TO_PUSH)


# ---------------------------------------------------------------------------
# step 8 -- retargeting, urgent cases first (invariant 15)
# ---------------------------------------------------------------------------


def order_retargets(retargets: Iterable[Retarget]) -> list[Retarget]:
    """Children of merged and emptied branches first, order otherwise kept.

    Those are the pull requests whose base is about to disappear; every other
    retarget can wait behind them.
    """
    ordered = [r for r in retargets if r.needed]
    return sorted(ordered, key=lambda r: 0 if r.urgent else 1)


def _apply_retargets(
    provider: Provider | None, retargets: Sequence[Retarget]
) -> tuple[tuple[Retarget, ...], tuple[RetargetFailure, ...]]:
    planned = order_retargets(retargets)
    if not planned:
        return (), ()
    if provider is None:
        raise ValueError("retargeting a pull request needs a forge provider")
    done: list[Retarget] = []
    failed: list[RetargetFailure] = []
    for retarget in planned:
        try:
            provider.retarget(retarget.pr_number, retarget.new_base)
        except ForgeError as error:
            failed.append(RetargetFailure(retarget=retarget, error=str(error)))
        else:
            done.append(retarget)
    return tuple(done), tuple(failed)


# ---------------------------------------------------------------------------
# step 9 -- the push, and the snapshot restore behind it
# ---------------------------------------------------------------------------


def push_branches(git: Git, branches: Sequence[str], *, remote: str = "origin"):
    """``git push --atomic --force-with-lease --force-if-includes <remote> ...``

    One call for every changed branch (invariant 12).  Returns the
    CompletedProcess: a rejection is an outcome to report, not an exception.
    """
    return git.push(
        remote,
        list(branches),
        atomic=True,
        force_with_lease=True,
        force_if_includes=True,
        check=False,
    )


def push_command_line(branches: Sequence[str], *, remote: str = "origin") -> str:
    return " ".join(["git", "push", *PUSH_FLAGS, remote, *branches])


def _rejected_branches(stderr: str, attempted: Sequence[str]) -> tuple[str, ...]:
    """Which refs git named in its rejection.

    Display only -- ``--atomic`` already guarantees that *nothing* landed, so
    the set of unpushed branches is always everything attempted.
    """
    named = []
    for line in stderr.splitlines():
        match = _REJECTED_LINE.match(line)
        if match:
            name = match["local"].split(":")[0].removeprefix("refs/heads/")
            if name not in named:
                named.append(name)
    return tuple(named) or tuple(attempted)


def restore_tips(git: Git, changes: Iterable[BranchChange]) -> tuple[str, ...]:
    """Put every restacked branch back where the snapshot found it.

    SPEC.md sec 5.2 step 9.  The checked-out branch is restored with
    ``reset --hard`` so the working tree follows it -- phase 1 required a clean
    worktree, so there is nothing of the user's to lose; every other branch moves
    with a compare-and-swap ``update-ref``, which refuses if it is not where we
    left it.
    """
    current = git.current_branch()
    restored: list[str] = []
    for change in changes:
        if not change.restacked:
            continue
        now = git.try_rev_parse(f"refs/heads/{change.branch}")
        if now is None or now == change.before:
            continue
        if change.branch == current:
            git.run("reset", "--hard", change.before)
        else:
            git.run(
                "update-ref",
                "-m",
                "stackem: restore after a rejected push",
                f"refs/heads/{change.branch}",
                change.before,
                now,
            )
        restored.append(change.branch)
    return tuple(restored)


# ---------------------------------------------------------------------------
# step 10 -- the commands stackem refuses to run itself (invariant 14)
# ---------------------------------------------------------------------------


def create_pull_request_command(branch: str, base: str) -> str:
    """SPEC.md sec 6.5 / invariant 20: print it, never run it."""
    return f"gh pr create --base {base} --head {branch}"


def close_and_delete_commands(
    branch: str,
    pr_number: int | None = None,
    *,
    remote: str = "origin",
    remote_exists: bool = True,
) -> tuple[str, ...]:
    """Retire an emptied branch (SPEC.md sec 7.2).

    A pull request with zero commits cannot be merged, so the branch leaves the
    chain -- but only a human closes and deletes it, because doing it
    automatically created a verified infinite flip-flop with orphan rescue
    (SPEC.md sec 5.1).
    """
    commands = []
    if pr_number is not None:
        commands.append(f"gh pr close {pr_number}")
    if remote_exists:
        commands.append(f"git push {remote} --delete {branch}")
    commands.append(f"git branch -D {branch}")
    return tuple(commands)


def delete_merged_branch_commands(
    branch: str, *, remote: str = "origin", remote_exists: bool = True
) -> tuple[str, ...]:
    commands = []
    if remote_exists:
        commands.append(f"git push {remote} --delete {branch}")
    commands.append(f"git branch -D {branch}")
    return tuple(commands)


def rescue_commands(
    *,
    slug: str,
    pr_number: int,
    head: str,
    base: str,
    head_missing: bool = True,
    base_missing: bool = False,
    base_pr_number: int | None = None,
    retarget_to: str | None = None,
    remote: str = "origin",
) -> tuple[str, ...]:
    """The SPEC.md sec 6.3 rescue, verified end to end against GitHub.

    GitHub keeps a pull request's commits under ``refs/pull/N/head`` forever, so
    a closed-by-deletion pull request can be brought back -- but only once
    **both** its head and its base branch exist again (invariant 13).
    """
    fetch_refspecs: list[str] = []
    push_refspecs: list[str] = []
    if base_missing and base_pr_number is not None:
        fetch_refspecs.append(f"refs/pull/{base_pr_number}/head:rescue-base")
        push_refspecs.append(f"rescue-base:refs/heads/{base}")
    if head_missing:
        fetch_refspecs.append(f"refs/pull/{pr_number}/head:rescue-head")
        push_refspecs.append(f"rescue-head:refs/heads/{head}")
    commands: list[str] = []
    if fetch_refspecs:
        commands.append(f"git fetch {remote} " + " ".join(fetch_refspecs))
        commands.append(f"git push {remote} " + " ".join(push_refspecs))
    commands.append(f"gh api -X PATCH /repos/{slug}/pulls/{pr_number} -f state=open")
    if retarget_to and retarget_to != base:
        commands.append(f"gh pr edit {pr_number} --base {retarget_to}")
    return tuple(commands)


_DELETE_BRANCH_ON_MERGE_NOTE = (
    "this repository has delete_branch_on_merge = true, which closes a child "
    "pull request on every merge (SPEC.md sec 6.3); never merge with "
    "--delete-branch, and expect to rescue orphans by hand"
)


# ---------------------------------------------------------------------------
# the phase itself
# ---------------------------------------------------------------------------


def run_remote_phase(
    git: Git,
    provider: Provider | None,
    work: VerifiedLocalWork,
    *,
    remote: str = "origin",
    slug: str | None = None,
) -> RemoteResult:
    """Steps 8, 9 and 10 of SPEC.md sec 5.2, in that order.

    ``work`` can only exist if phase 1 finished and verified (invariant 9); this
    function additionally checks the repository agrees before touching anything.
    """
    _assert_repository_matches(git, work)

    retargeted, failures = _apply_retargets(provider, work.retargets)
    if slug is None and provider is not None:
        slug = provider.repo_slug()

    if failures:
        # A pull request still based on a branch that is about to vanish would
        # display the whole stack the moment its content landed.  Nothing is
        # pushed; rerunning sync retries -- retargeting is idempotent.
        return RemoteResult(
            outcome=PushOutcome.RETARGET_FAILED,
            retargeted=retargeted,
            retarget_failures=failures,
            follow_ups=_follow_ups(work, failures, pushed=(), slug=slug, remote=remote),
            stderr="\n".join(f.error for f in failures),
        )

    branches = tuple(change.branch for change in work.pushable)
    if not branches:
        return RemoteResult(
            outcome=PushOutcome.NOTHING_TO_PUSH,
            retargeted=retargeted,
            follow_ups=_follow_ups(work, (), pushed=(), slug=slug, remote=remote),
        )

    proc = push_branches(git, branches, remote=remote)
    command = push_command_line(branches, remote=remote)
    if proc.returncode == 0:
        return RemoteResult(
            outcome=PushOutcome.PUSHED,
            retargeted=retargeted,
            pushed=branches,
            attempted=branches,
            follow_ups=_follow_ups(work, (), pushed=branches, slug=slug, remote=remote),
            push_command=command,
            returncode=0,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # Rejected.  --atomic means not one ref moved, so the local branches go back
    # to the snapshot and the run ends saying nothing was pushed.
    restored = restore_tips(git, work.changes)
    return RemoteResult(
        outcome=PushOutcome.REJECTED,
        retargeted=retargeted,
        pushed=(),
        attempted=branches,
        rejected=_rejected_branches(proc.stderr, branches),
        restored=restored,
        follow_ups=_follow_ups(work, (), pushed=(), slug=slug, remote=remote),
        push_command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _assert_repository_matches(git: Git, work: VerifiedLocalWork) -> None:
    """Invariant 9, checked against git rather than against a flag."""
    if git.rebase_in_progress():
        raise LocalWorkNotVerified(
            "a rebase is still in progress; the local phase has not finished "
            "(SPEC.md sec 5.2 PHASE 0)"
        )
    if not git.is_clean(untracked=False):
        # Tracked modifications only: a rejected push restores tips with
        # `git reset --hard`, which would overwrite those and leaves untracked
        # files alone.  Refusing over a scratch file would make sync unusable.
        raise LocalWorkNotVerified(
            "the worktree has uncommitted changes; phase 2 restores tips on a "
            "rejected push and must not overwrite them"
        )
    for change in work.changes:
        if not (change.restacked or change.needs_push):
            continue  # nothing of this branch reaches the remote
        actual = git.try_rev_parse(f"refs/heads/{change.branch}")
        if actual != change.after:
            raise LocalWorkNotVerified(
                f"{change.branch} is at {actual or 'nothing'} but was verified at "
                f"{change.after}; rerun stackem sync"
            )


def _follow_ups(
    work: VerifiedLocalWork,
    failures: Sequence[RetargetFailure],
    *,
    pushed: Sequence[str],
    slug: str | None,
    remote: str,
) -> tuple[FollowUp, ...]:
    """Everything the user has to do by hand, with the commands to do it."""
    slug = slug or "OWNER/REPO"
    follow_ups: list[FollowUp] = []
    if work.delete_branch_on_merge:
        follow_ups.append(
            FollowUp(kind=FollowUpKind.DELETE_BRANCH_ON_MERGE, note=_DELETE_BRANCH_ON_MERGE_NOTE)
        )

    for change in work.changes:
        branch = change.branch
        pr = change.pull_request
        number = pr.number if pr else None
        blocked = _blocking_failure(branch, failures)

        if change.state is BranchState.MERGED:
            note = f"{branch} is merged" + (f" (#{number})" if number else "")
            commands = delete_merged_branch_commands(
                branch, remote=remote, remote_exists=change.on_remote and blocked is None
            )
            if blocked is not None:
                note += (
                    "; NOT printing the branch deletion: retargeting "
                    f"{blocked.retarget.branch}'s pull request "
                    f"(#{blocked.retarget.pr_number}) failed, and deleting {branch} "
                    "would close it (invariant 13)"
                )
            follow_ups.append(
                FollowUp(
                    kind=FollowUpKind.MERGED,
                    note=note,
                    commands=commands,
                    branch=branch,
                    pr_number=number,
                )
            )
            continue

        if change.state is BranchState.ORPHANED:
            follow_ups.append(_orphan_follow_up(work, change, slug=slug, remote=remote))
            continue

        if change.emptied:
            note = f"{branch} has no commits of its own; its pull request cannot be merged"
            if blocked is not None:
                note += (
                    "; NOT printing the branch deletion: retargeting "
                    f"{blocked.retarget.branch}'s pull request "
                    f"(#{blocked.retarget.pr_number}) failed"
                )
            follow_ups.append(
                FollowUp(
                    kind=FollowUpKind.EMPTIED,
                    note=note,
                    commands=close_and_delete_commands(
                        branch,
                        number,
                        remote=remote,
                        remote_exists=change.on_remote and blocked is None,
                    ),
                    branch=branch,
                    pr_number=number,
                )
            )
            continue

        if pr is None and change.parent and (change.on_remote or branch in pushed):
            follow_ups.append(
                FollowUp(
                    kind=FollowUpKind.NO_PULL_REQUEST,
                    note=f"{branch} has no pull request",
                    commands=(create_pull_request_command(branch, change.parent),),
                    branch=branch,
                )
            )
    return tuple(follow_ups)


def _blocking_failure(
    branch: str, failures: Sequence[RetargetFailure]
) -> RetargetFailure | None:
    """A failed retarget whose pull request still points at ``branch``.

    Invariant 15: while that is true, printing "delete this branch" would be
    printing "close that pull request".
    """
    for failure in failures:
        if failure.retarget.old_base == branch:
            return failure
    return None


def _orphan_follow_up(
    work: VerifiedLocalWork, change: BranchChange, *, slug: str, remote: str
) -> FollowUp:
    pr = change.pull_request
    branch = change.branch
    if pr is None:  # pragma: no cover - an orphan is defined by its closed PR
        return FollowUp(
            kind=FollowUpKind.ORPHANED,
            note=f"{branch} is missing from {remote} and has no pull request to rescue",
            branch=branch,
        )
    base_change = work.change(pr.base)
    base_missing = base_change is not None and not base_change.on_remote
    base_pr = base_change.pull_request if base_change else None
    note = (
        f"{branch}'s pull request (#{pr.number}) was closed and its branch is gone "
        f"from {remote}; stackem never reopens one (invariant 14)"
    )
    if base_missing and base_pr is None:
        note += (
            f"; restore its base branch {pr.base} first -- no pull request of its "
            "own is known, so refs/pull/N/head cannot be guessed"
        )
    return FollowUp(
        kind=FollowUpKind.ORPHANED,
        note=note,
        commands=rescue_commands(
            slug=slug,
            pr_number=pr.number,
            head=pr.head,
            base=pr.base,
            head_missing=not change.on_remote,
            base_missing=base_missing,
            base_pr_number=base_pr.number if base_pr else None,
            retarget_to=change.parent,
            remote=remote,
        ),
        branch=branch,
        pr_number=pr.number,
    )
