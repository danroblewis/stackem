"""stackem.detect -- merge detection and closed-pull-request classification.

SPEC.md sec 5.1 (classifying branches) and sec 6.4 (merge detection).

Everything here runs against real git repositories built by tests/harness.py:
real commits, a real bare repo acting as origin, and real squash merges minted
with plumbing exactly the way GitHub mints them (a NEW commit on the trunk that
shares no history with the branch).  Only the forge is faked, through the shared
in-process mock (tests/conftest.py).

The two things these tests exist to hold down:

* CLAUDE.md invariant 19 -- the unique-commit guard.  ``merge-tree`` alone
  reports *any* branch with no unique commits as merged, including a fresh
  branch that is merely behind the trunk, and sync would then report it for
  deletion.  ``test_a_branch_merely_behind_the_trunk_is_not_merged`` asserts
  both halves: that the raw tree test does say "merged", and that detect does
  not.
* CLAUDE.md invariant 14 -- stackem never closes, deletes, or rescues.  This
  module only *classifies*; a closed pull request is labelled orphaned or
  deliberate and nothing else happens.
"""

from __future__ import annotations

import pytest

from stackem.detect import (
    Classification,
    ClosedPullRequestKind,
    MergeEvidence,
    classify,
    classify_closed_pull_request,
    detect_merged,
    has_unique_commits,
    is_contained_in_trunk,
)
from stackem.forge.mock_server import MockPullRequest
from stackem.model import BranchState, PullRequest, PullRequestState


# --------------------------------------------------------------------------
# translating the mock's pull requests into the shared model
#
# This is the one line of provider behaviour the tests need and do not own: a
# merged pull request has REST state "closed" with merged_at set -- "merged" is
# not a REST state (SPEC.md sec 6.4, mock_server's module docstring).
# --------------------------------------------------------------------------

def model_pr(pr: MockPullRequest) -> PullRequest:
    if pr.merged_at is not None:
        state = PullRequestState.MERGED
    elif pr.state == "open":
        state = PullRequestState.OPEN
    else:
        state = PullRequestState.CLOSED
    return PullRequest(
        number=pr.number,
        head=pr.head,
        base=pr.base,
        state=state,
        title=pr.title,
        is_cross_repository=pr.is_cross_repository,
        head_sha=pr.head_sha,
        merge_commit_sha=pr.merge_commit_sha,
        merged_at=pr.merged_at,
        closed_at=pr.closed_at,
        updated_at=pr.updated_at,
    )


# --------------------------------------------------------------------------
# merge detection, offline (SPEC.md sec 6.4)
# --------------------------------------------------------------------------

def test_a_squash_merged_branch_is_detected_as_merged(sandbox):
    """The headline case: the company squash-merges, so no commit survives."""
    sb = sandbox
    sb.make_stack(["feat-a"], commits=2)
    sb.squash_merge("feat-a")  # a real new commit on origin/main, then fetch

    # The branch's own commits are nowhere in the trunk's history ...
    assert sb.git.rev_list_count("origin/main..feat-a") == 2
    # ... but its content is.
    check = detect_merged(sb.git, "origin/main", "feat-a")

    assert check.merged
    assert check.evidence is MergeEvidence.TREE
    assert check.unique_commits == 2
    assert check.tree_contained is True


def test_a_branch_merely_behind_the_trunk_is_not_merged(sandbox):
    """CLAUDE.md invariant 19 -- the case the guard exists for.

    A fresh branch cut from the trunk and left behind has no unique content, so
    merging it into the trunk yields the trunk's own tree.  Without the guard
    sync classifies it as merged and reports it for deletion.
    """
    sb = sandbox
    sb.create_branch("fresh")
    sb.advance_trunk(2)

    # The raw tree test -- the trap.  This assertion is the bug, written down.
    assert (
        sb.git.merge_tree_write_tree("origin/main", "fresh").tree
        == sb.git.tree_id("origin/main")
    )
    assert sb.git.rev_list_count("origin/main..fresh") == 0
    assert has_unique_commits(sb.git, "origin/main", "fresh") is False

    check = detect_merged(sb.git, "origin/main", "fresh")

    assert check.merged is False
    assert check.evidence is MergeEvidence.UNIQUE_COMMITS
    assert check.unique_commits == 0
    # The tree test must not even run: nothing it could say is trustworthy here.
    assert check.tree_contained is None


def test_a_branch_with_real_unique_work_is_not_merged(stacked_sandbox):
    sb = stacked_sandbox
    sb.advance_trunk(1)

    check = detect_merged(sb.git, "origin/main", "feat-a")

    assert check.merged is False
    assert check.evidence is MergeEvidence.TREE
    assert check.unique_commits == 1
    assert check.tree_contained is False


def test_a_branch_whose_parent_merged_is_still_not_merged_itself(sandbox):
    """The child of a squash-merged branch keeps its own work, so it is live."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    sb.squash_merge("feat-a")

    assert detect_merged(sb.git, "origin/main", "feat-a").merged is True
    assert detect_merged(sb.git, "origin/main", "feat-b").merged is False


def test_git_cherry_cannot_see_a_squash_merge(sandbox):
    """Why detect.py does not use ``git cherry`` (SPEC.md sec 6.4).

    Squashing rewrites the patch, so every commit still reports ``+`` ("not
    upstream") long after the branch has landed.  A dry-run rebase is no better
    -- it conflicts against the squash commit rather than emptying, and wedges
    the repository -- so neither is implemented.
    """
    sb = sandbox
    sb.make_stack(["feat-a"], commits=2)
    sb.squash_merge("feat-a")

    cherry = sb.git.lines("cherry", "origin/main", "feat-a")
    assert [line[0] for line in cherry] == ["+", "+"], cherry

    assert detect_merged(sb.git, "origin/main", "feat-a").merged is True


def test_containment_is_measured_against_the_remote_trunk(sandbox):
    """Invariant 4's other face: the local trunk is stale, so it cannot decide."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.squash_merge("feat-a")  # lands on ORIGIN; the local trunk stays behind

    assert is_contained_in_trunk(sb.git, "origin/main", "feat-a") is True
    assert is_contained_in_trunk(sb.git, "main", "feat-a") is False


# --------------------------------------------------------------------------
# merge detection, with a pull request (SPEC.md sec 6.4: "prefer the PR state")
# --------------------------------------------------------------------------

def test_a_merged_pull_request_beats_a_stale_local_view(sandbox, mock_github):
    """The forge knows before the next fetch does."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.squash_merge(pr.number)  # real commit on origin/main; we have NOT fetched

    assert is_contained_in_trunk(sb.git, "origin/main", "feat-a") is False

    check = detect_merged(sb.git, "origin/main", "feat-a", pull_request=model_pr(pr))

    assert check.merged is True
    assert check.evidence is MergeEvidence.PULL_REQUEST
    assert check.tree_contained is None  # git was never asked


def test_an_open_pull_request_is_never_reported_merged(sandbox, mock_github):
    """An open PR is authoritative in the other direction.

    Someone landed the same content on the trunk by another route, so the tree
    test says "contained".  The branch still has an open pull request, so the
    cascade must restack it rather than skipping it (invariant 6 skips *merged*
    branches only).
    """
    sb = sandbox
    sb.make_stack(["feat-a"])
    pr = mock_github.api.open_pull_request("feat-a", "main")
    sb.squash_merge("feat-a")  # the content lands; the pull request stays open

    assert is_contained_in_trunk(sb.git, "origin/main", "feat-a") is True

    check = detect_merged(sb.git, "origin/main", "feat-a", pull_request=model_pr(pr))

    assert check.merged is False
    assert check.evidence is MergeEvidence.PULL_REQUEST


def test_a_closed_pull_request_falls_back_to_the_tree_test(sandbox, mock_github):
    """"Closed, not merged" says nothing about containment, so git decides."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    landed = api.open_pull_request("feat-a", "main")
    abandoned = api.open_pull_request("feat-b", "feat-a")
    api.close_pull_request(landed.number)
    api.close_pull_request(abandoned.number)
    sb.squash_merge("feat-a")  # feat-a's work went in under some other number

    contained = detect_merged(
        sb.git, "origin/main", "feat-a", pull_request=model_pr(landed)
    )
    assert contained.merged is True
    assert contained.evidence is MergeEvidence.TREE

    outstanding = detect_merged(
        sb.git, "origin/main", "feat-b", pull_request=model_pr(abandoned)
    )
    assert outstanding.merged is False
    assert outstanding.evidence is MergeEvidence.TREE


# --------------------------------------------------------------------------
# closed pull requests: orphaned or deliberate (SPEC.md sec 5.1)
# --------------------------------------------------------------------------

def test_a_closed_pull_request_whose_head_branch_is_gone_is_orphaned(sandbox, mock_github):
    """CLAUDE.md invariant 13: deleting a branch closes its pull requests."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    child = api.open_pull_request("feat-b", "feat-a")

    assert api.delete_branch("feat-b") == [child.number]

    pr = model_pr(api.pull_request(child.number))
    assert pr.state is PullRequestState.CLOSED

    kind = classify_closed_pull_request(
        pr, branch_exists_on_remote=api.branch_exists("feat-b")
    )
    assert kind is ClosedPullRequestKind.ORPHANED


def test_a_closed_pull_request_whose_branch_survives_was_closed_on_purpose(
    sandbox, mock_github
):
    """The false positive that killed auto-rescue (SPEC.md sec 5.1).

    A person closing a pull request looks exactly like wreckage except for this:
    the head branch is still on the remote.
    """
    sb = sandbox
    sb.make_stack(["feat-a"])
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.close_pull_request(pr.number)

    assert api.branch_exists("feat-a") is True

    kind = classify_closed_pull_request(
        model_pr(api.pull_request(pr.number)), branch_exists_on_remote=True
    )
    assert kind is ClosedPullRequestKind.DELIBERATE


def test_a_pull_request_closed_by_its_base_branch_deletion_looks_deliberate(
    sandbox, mock_github
):
    """Characterization, not endorsement.

    Deleting a branch closes PRs referencing it as head *or* base (invariant
    13), and reopening needs BOTH branches (sec 6.3).  SPEC.md sec 5.1 defines
    orphaned by the *head* branch alone, so a pull request closed by its base
    branch's deletion classifies as deliberate.  Implemented as specified; see
    the report.
    """
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    child = api.open_pull_request("feat-b", "feat-a")

    assert api.delete_branch("feat-a") == [child.number]

    pr = model_pr(api.pull_request(child.number))
    assert api.branch_exists("feat-b") is True
    assert (
        classify_closed_pull_request(pr, branch_exists_on_remote=True)
        is ClosedPullRequestKind.DELIBERATE
    )


def test_merged_and_open_pull_requests_are_not_closed_pull_requests(mock_github_memory):
    """A merged PR is REST-closed with merged_at set; it is not "closed" here."""
    api = mock_github_memory.api
    merged = api.open_pull_request("feat-a", "main")
    api.squash_merge(merged.number)
    still_open = api.open_pull_request("feat-b", "main")

    assert model_pr(api.pull_request(merged.number)).state is PullRequestState.MERGED
    assert (
        classify_closed_pull_request(
            model_pr(api.pull_request(merged.number)), branch_exists_on_remote=False
        )
        is None
    )
    assert (
        classify_closed_pull_request(model_pr(still_open), branch_exists_on_remote=True)
        is None
    )


# --------------------------------------------------------------------------
# the whole classification (SPEC.md sec 5.1: live / merged / orphaned)
# --------------------------------------------------------------------------

def test_classify_reports_a_merged_branch_as_merged(sandbox, mock_github):
    sb = sandbox
    sb.make_stack(["feat-a"])
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.squash_merge(pr.number)
    sb.fetch()

    result = classify(
        sb.git,
        "origin/main",
        "feat-a",
        pull_request=model_pr(api.pull_request(pr.number)),
        branch_exists_on_remote=api.branch_exists("feat-a"),
    )

    assert isinstance(result, Classification)
    assert result.state is BranchState.MERGED
    assert result.closed_kind is None
    assert result.merge.merged is True


def test_classify_reports_an_orphaned_branch_as_orphaned(sandbox, mock_github):
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    child = api.open_pull_request("feat-b", "feat-a")
    api.delete_branch("feat-b")
    sb.fetch()

    result = classify(
        sb.git,
        "origin/main",
        "feat-b",
        pull_request=model_pr(api.pull_request(child.number)),
        branch_exists_on_remote=api.branch_exists("feat-b"),
    )

    assert result.state is BranchState.ORPHANED
    assert result.closed_kind is ClosedPullRequestKind.ORPHANED
    assert result.merge.merged is False  # the guard: it still has its own work


def test_classify_keeps_a_deliberately_closed_branch_live(sandbox, mock_github):
    """Invariant 14: this is reported, never acted on -- so it stays live."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.close_pull_request(pr.number)

    result = classify(
        sb.git,
        "origin/main",
        "feat-a",
        pull_request=model_pr(api.pull_request(pr.number)),
        branch_exists_on_remote=True,
    )

    assert result.state is BranchState.LIVE
    assert result.closed_kind is ClosedPullRequestKind.DELIBERATE


def test_classify_treats_a_branch_with_no_pull_request_as_live(stacked_sandbox):
    sb = stacked_sandbox

    result = classify(sb.git, "origin/main", "feat-b")

    assert result.state is BranchState.LIVE
    assert result.closed_kind is None
    assert result.pull_request is None


def test_classify_falls_back_to_the_remote_tracking_ref(sandbox, mock_github):
    """With no answer from the forge, ``refs/remotes/origin/<branch>`` decides.

    ``git fetch --prune`` is what makes that ref honest, which is why sync
    fetches first (SPEC.md sec 5.2, phase 1 step 1).
    """
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    gone = api.open_pull_request("feat-a", "main")
    kept = api.open_pull_request("feat-b", "feat-a")
    api.close_pull_request(kept.number)
    api.delete_branch("feat-a")
    sb.fetch()  # --prune drops refs/remotes/origin/feat-a

    assert sb.git.try_rev_parse("refs/remotes/origin/feat-a") is None
    assert sb.git.try_rev_parse("refs/remotes/origin/feat-b") is not None

    orphan = classify(
        sb.git, "origin/main", "feat-a", pull_request=model_pr(api.pull_request(gone.number))
    )
    assert orphan.state is BranchState.ORPHANED

    deliberate = classify(
        sb.git, "origin/main", "feat-b", pull_request=model_pr(api.pull_request(kept.number))
    )
    assert deliberate.state is BranchState.LIVE
    assert deliberate.closed_kind is ClosedPullRequestKind.DELIBERATE


def test_classify_explains_itself(stacked_sandbox):
    """Every classification carries the sentence sync prints (invariant 23)."""
    sb = stacked_sandbox
    sb.squash_merge("feat-a")

    merged = classify(sb.git, "origin/main", "feat-a")
    assert merged.reason
    assert "origin/main" in merged.reason

    live = classify(sb.git, "origin/main", "feat-c")
    assert live.reason


def test_detection_writes_no_refs(stacked_sandbox):
    """CLAUDE.md invariant 5: bare ``stackem`` must not write.

    ``merge-tree --write-tree`` writes one unreferenced tree object that gc
    collects; no ref, no config, no file is touched, so nothing here can be
    called from the read-only path by mistake.
    """
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    sb.git.trace.clear()

    classify(sb.git, "origin/main", "feat-a")

    verbs = {invocation.argv[1] for invocation in sb.git.trace}
    assert verbs <= {"rev-list", "merge-tree", "rev-parse", "merge-base", "log"}, verbs


def test_detect_merged_refuses_an_unknown_branch(stacked_sandbox):
    from stackem.gitx import GitError

    with pytest.raises(GitError):
        detect_merged(stacked_sandbox.git, "origin/main", "no-such-branch")
