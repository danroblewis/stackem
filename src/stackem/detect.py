"""Is this branch merged, and what happened to its closed pull request?

SPEC.md sec 6.4 (merge detection) and sec 5.1 (classifying branches).

Merge detection decides whether the cascade skips a branch (CLAUDE.md invariant
6: replaying a merged branch either drops all its commits -- so sync mistakes it
for an emptied branch -- or conflicts against the squash commit and halts
forever).  Getting it wrong in the other direction is worse: sync reports a
perfectly good branch for deletion.

Two rules, in order:

1. **Prefer the pull request.**  The forge knows a merge happened before the
   next fetch does, and it knows an *open* pull request has not merged however
   the trees look.
2. **Offline, compare trees.**  ``merge-tree --write-tree <trunk> <branch>``
   equal to the trunk's own tree means the branch's content is already in the
   trunk -- but only after the unique-commit guard.

**The guard is not optional (CLAUDE.md invariant 19).**  ``merge-tree`` reports
*any* branch with no unique commits as contained, a fresh branch merely behind
the trunk included, so ``rev-list --count <trunk>..<branch>`` must be greater
than zero before the tree test is allowed to speak.

Two techniques that look right and are not, so neither is implemented here:

* ``git cherry`` -- squashing rewrites the patch, so the patch-ids of the
  branch's commits match nothing in the trunk and every commit still reports
  ``+`` ("not upstream") long after the branch landed.  Verified in
  ``tests/test_detect.py::test_git_cherry_cannot_see_a_squash_merge``.
* a dry-run rebase -- against a squash commit it conflicts rather than emptying,
  which wedges the repository instead of answering the question (SPEC.md sec
  6.4).

Nothing in this module acts.  A closed pull request is labelled orphaned or
deliberate and that is the end of it: sync reports and prints the commands, and
never closes, deletes or rescues anything (CLAUDE.md invariant 14).  No ref, no
config key and no file is written, so the read-only ``stackem`` (invariant 5)
can call any of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stackem.gitx import Git
from stackem.model import BranchState, PullRequest, PullRequestState

__all__ = [
    "Classification",
    "ClosedPullRequestKind",
    "MergeCheck",
    "MergeEvidence",
    "classify",
    "classify_closed_pull_request",
    "detect_merged",
    "has_unique_commits",
    "is_contained_in_trunk",
]


class MergeEvidence(Enum):
    """What decided a :class:`MergeCheck`.

    PULL_REQUEST   -- the forge said so, either way (SPEC.md sec 6.4).
    UNIQUE_COMMITS -- the branch has no commits of its own, so the tree test was
                      never run (CLAUDE.md invariant 19).
    TREE           -- ``merge-tree --write-tree`` against the trunk's own tree.
    """

    PULL_REQUEST = "pull_request"
    UNIQUE_COMMITS = "unique_commits"
    TREE = "tree"


class ClosedPullRequestKind(Enum):
    """Why a pull request is closed (SPEC.md sec 5.1).

    ORPHANED   -- closed, not merged, and its head branch is gone from the
                  remote: wreckage from a branch deletion (invariant 13).  sync
                  prints the sec 6.3 rescue commands and runs none of them.
    DELIBERATE -- closed, not merged, head branch still there: a person closed
                  it.  GitHub offers a "Delete branch" button on closed pull
                  requests, so a person can produce the ORPHANED shape too --
                  which is exactly why auto-rescue was cut (invariant 14).
    """

    ORPHANED = "orphaned"
    DELIBERATE = "deliberate"


@dataclass(frozen=True)
class MergeCheck:
    """Whether a branch's work is already in the trunk, and how we know.

    ``unique_commits`` and ``tree_contained`` are ``None`` when the pull request
    answered and git was never asked; ``tree_contained`` is also ``None`` when
    the unique-commit guard stopped the check before the tree test.
    """

    merged: bool
    evidence: MergeEvidence
    reason: str
    unique_commits: int | None = None
    tree_contained: bool | None = None


@dataclass(frozen=True)
class Classification:
    """One branch, classified for this run (SPEC.md sec 5.1).

    Derived fresh and thrown away; nothing here is ever recorded.
    """

    branch: str
    state: BranchState
    reason: str
    merge: MergeCheck
    pull_request: PullRequest | None = None
    closed_kind: ClosedPullRequestKind | None = None


def has_unique_commits(git: Git, trunk_ref: str, branch: str) -> bool:
    """``rev-list --count <trunk_ref>..<branch> > 0`` -- invariant 19's guard.

    ``trunk_ref`` is a *ref*, and it should be ``origin/<trunk>``: the local
    trunk is not fast-forwarded by sync (invariant 4), so a merge that landed on
    the remote is invisible from it.
    """
    return git.rev_list_count(f"{trunk_ref}..{branch}") > 0


def is_contained_in_trunk(git: Git, trunk_ref: str, branch: str) -> bool:
    """Does merging ``branch`` into ``trunk_ref`` yield the trunk's own tree?

    SPEC.md sec 6.4.  True means the trunk already has every change the branch
    makes -- however the branch's commits were rewritten on the way in, which is
    what makes this survive a squash merge.

    On its own this answers "yes" for a branch that is merely *behind* the
    trunk; :func:`detect_merged` is what puts the guard in front of it.

    A conflicted merge is reported as not contained: a branch that cannot even
    merge cleanly is certainly not already in the trunk.
    """
    result = git.merge_tree_write_tree(trunk_ref, branch)
    if result.conflicted or result.tree is None:
        return False
    return result.tree == git.tree_id(trunk_ref)


def detect_merged(
    git: Git,
    trunk_ref: str,
    branch: str,
    *,
    pull_request: PullRequest | None = None,
) -> MergeCheck:
    """Is ``branch``'s work already in ``trunk_ref``? (SPEC.md sec 6.4)

    The pull request decides when there is one and it is open or merged.  A
    *closed, unmerged* pull request says nothing about containment -- the work
    may well have landed under another number -- so those fall through to the
    offline test, as does a branch with no pull request at all.

    Raises :class:`~stackem.gitx.GitError` when ``branch`` or ``trunk_ref`` does
    not resolve.
    """
    if pull_request is not None:
        if pull_request.state is PullRequestState.MERGED:
            return MergeCheck(
                merged=True,
                evidence=MergeEvidence.PULL_REQUEST,
                reason=f"pull request #{pull_request.number} is merged",
            )
        if pull_request.state is PullRequestState.OPEN:
            return MergeCheck(
                merged=False,
                evidence=MergeEvidence.PULL_REQUEST,
                reason=f"pull request #{pull_request.number} is open",
            )

    # CLAUDE.md invariant 19: the guard comes FIRST.  Skip it and every branch
    # with no commits of its own -- a fresh branch behind the trunk -- comes
    # back "merged", and sync reports it for deletion.
    unique = git.rev_list_count(f"{trunk_ref}..{branch}")
    if unique == 0:
        return MergeCheck(
            merged=False,
            evidence=MergeEvidence.UNIQUE_COMMITS,
            reason=f"{branch} has no commits of its own above {trunk_ref}",
            unique_commits=0,
        )

    contained = is_contained_in_trunk(git, trunk_ref, branch)
    if contained:
        reason = (
            f"{branch}'s {unique} commit{'' if unique == 1 else 's'} are already "
            f"in {trunk_ref} (merging it changes nothing)"
        )
    else:
        reason = (
            f"{branch} has {unique} commit{'' if unique == 1 else 's'} "
            f"not in {trunk_ref}"
        )
    return MergeCheck(
        merged=contained,
        evidence=MergeEvidence.TREE,
        reason=reason,
        unique_commits=unique,
        tree_contained=contained,
    )


def classify_closed_pull_request(
    pull_request: PullRequest | None,
    *,
    branch_exists_on_remote: bool,
) -> ClosedPullRequestKind | None:
    """Orphaned, deliberate, or neither (SPEC.md sec 5.1).

    ``None`` for an open or merged pull request, and for no pull request at all:
    only a closed, unmerged one is being classified here.  Note that a merged
    pull request has REST state ``closed`` with ``merged_at`` set -- providers
    normalize that to :attr:`~stackem.model.PullRequestState.MERGED`, so the
    distinction is already made by the time it reaches this function.

    The rule is the spec's: the *head* branch decides.  A pull request closed
    because its **base** branch was deleted therefore classifies as DELIBERATE
    even though it is wreckage -- invariant 13 closes pull requests referencing
    the deleted branch as head *or* base.  Either way stackem does not act
    (invariant 14), and the sec 6.3 rescue restores both branches.
    """
    if pull_request is None or pull_request.state is not PullRequestState.CLOSED:
        return None
    if branch_exists_on_remote:
        return ClosedPullRequestKind.DELIBERATE
    return ClosedPullRequestKind.ORPHANED


def classify(
    git: Git,
    trunk_ref: str,
    branch: str,
    *,
    pull_request: PullRequest | None = None,
    branch_exists_on_remote: bool | None = None,
    remote: str = "origin",
) -> Classification:
    """Classify one branch: live, merged or orphaned (SPEC.md sec 5.1).

    ``branch_exists_on_remote`` is the forge's answer
    (:meth:`~stackem.forge.Provider.branch_exists_on_remote`).  Left ``None``,
    the remote-tracking ref ``refs/remotes/<remote>/<branch>`` answers instead --
    honest only just after ``git fetch --prune`` (SPEC.md sec 5.2, phase 1 step
    1), which is why the forge's answer is preferred when there is one.

    A deliberately closed pull request leaves the branch **live**: its work is
    not in the trunk and its branch is still there, so the cascade keeps
    restacking it.  sync reports the closed pull request; it never reopens or
    deletes anything (invariant 14).
    """
    merge = detect_merged(git, trunk_ref, branch, pull_request=pull_request)
    if merge.merged:
        return Classification(
            branch=branch,
            state=BranchState.MERGED,
            reason=merge.reason,
            merge=merge,
            pull_request=pull_request,
        )

    if pull_request is not None and pull_request.state is PullRequestState.CLOSED:
        exists = branch_exists_on_remote
        if exists is None:
            exists = git.try_rev_parse(f"refs/remotes/{remote}/{branch}") is not None
        closed_kind = classify_closed_pull_request(
            pull_request, branch_exists_on_remote=exists
        )
    else:
        closed_kind = None

    if closed_kind is ClosedPullRequestKind.ORPHANED:
        reason = (
            f"pull request #{pull_request.number} is closed and {branch} is gone "
            f"from {remote} -- a branch deletion closed it"
        )
        return Classification(
            branch=branch,
            state=BranchState.ORPHANED,
            reason=reason,
            merge=merge,
            pull_request=pull_request,
            closed_kind=closed_kind,
        )

    if closed_kind is ClosedPullRequestKind.DELIBERATE:
        reason = (
            f"pull request #{pull_request.number} was closed but {branch} is still "
            f"on {remote} -- someone closed it on purpose"
        )
    else:
        reason = merge.reason

    return Classification(
        branch=branch,
        state=BranchState.LIVE,
        reason=reason,
        merge=merge,
        pull_request=pull_request,
        closed_kind=closed_kind,
    )
