"""stackem.restack -- the cascade (SPEC.md sec 2, sec 5.2 PHASE 1, sec 7).

Every test here runs against a REAL git repository built by tests/harness.py: real
commits, real branches, real rebases, a real bare repo acting as ``origin``.
Nothing about git is mocked, and the forge is not involved at all -- the restack
engine takes a derived Stack and moves branches; classifying them and talking to
GitHub belong to sync.

The matrix these cover (SPEC.md sec 10):

fork-point derivation
    amend a lower branch . new commit mid-stack . trunk moves . parent
    force-pushed outside sync -> the guard fires before any rebase
timing (sec 7)
    a late fix on a lower branch: independent . conflicting . duplicating higher
    work (the higher commit is dropped) . emptying the higher branch completely
squash merge
    the walk skips a merged branch (invariant 6) . two merged in one run hoist
    transitively (invariant 7)
re-entrancy (sec 8)
    resume our own rebase . REFUSE a rebase we did not start (invariant 22) .
    a half-finished cascade self-heals on the next run (invariant 24)
"""

from __future__ import annotations

import pytest

from stackem.gitx import RebaseOutcome
from stackem.model import Branch, BranchState, ParentSource, Stack
from stackem.restack import (
    BranchAction,
    RestackStatus,
    _rebase_skip,
    hoist_over_merged,
    read_resume,
    restack,
)
from tests.harness import Sandbox


# --------------------------------------------------------------------------
# building the derived Stack the engine is handed
# --------------------------------------------------------------------------

def stack_model(
    sb: Sandbox,
    *names: str,
    merged: tuple[str, ...] = (),
    parents: dict[str, str] | None = None,
) -> Stack:
    """A Stack over ``names``, bottom-up, each parented on the one below it.

    This is what sync derives (SPEC.md sec 5.1) and hands to the engine; the
    engine never asks the forge anything itself.
    """
    overrides = dict(parents or {})
    branches: list[Branch] = []
    below = sb.trunk
    for name in names:
        parent = overrides.get(name, below)
        branches.append(
            Branch(
                name=name,
                sha=sb.sha(name),
                parent=parent,
                parent_source=(
                    ParentSource.TRUNK if parent == sb.trunk else ParentSource.PULL_REQUEST
                ),
                state=BranchState.MERGED if name in merged else BranchState.LIVE,
            )
        )
        below = name
    return Stack(trunk=sb.trunk, remote="origin", head=names[-1] if names else None,
                 branches=branches)


def actions(report) -> dict[str, BranchAction]:
    return {result.branch: result.action for result in report.results}


# --------------------------------------------------------------------------
# the no-op case: sync on a clean stack is fast and writes nothing
# --------------------------------------------------------------------------

def test_a_current_stack_is_skipped_entirely(stacked_sandbox):
    sb = stacked_sandbox
    before = sb.tips()

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert actions(report) == {
        "feat-a": BranchAction.SKIPPED_UP_TO_DATE,
        "feat-b": BranchAction.SKIPPED_UP_TO_DATE,
        "feat-c": BranchAction.SKIPPED_UP_TO_DATE,
    }
    assert sb.tips() == before
    assert report.changed_branches == ()
    assert report.ready_for_push is True


def test_the_local_phase_never_touches_the_remote(stacked_sandbox):
    """CLAUDE.md invariant 9: all local work completes before anything is pushed."""
    sb = stacked_sandbox
    sb.advance_trunk(1)
    before = sb.remote_tips()

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert sb.remote_tips() == before, "phase 1 must not push"


# --------------------------------------------------------------------------
# fork-point derivation (SPEC.md sec 2)
# --------------------------------------------------------------------------

def test_trunk_moves_restacks_the_whole_stack_onto_origin_trunk(stacked_sandbox):
    """SPEC.md sec 2 STEP 3, CLAUDE.md invariant 4."""
    sb = stacked_sandbox
    sb.advance_trunk(2)
    local_trunk_before = sb.sha(sb.trunk)

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert set(report.changed_branches) == {"feat-a", "feat-b", "feat-c"}
    sb.assert_stacked("feat-a", "feat-b", "feat-c", base="origin/main")
    assert sb.git.is_ancestor("origin/main", "feat-a")
    assert sb.shape("feat-a", "feat-b", "feat-c", base="origin/main") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    assert sb.sha(sb.trunk) == local_trunk_before, (
        "invariant 4: roots rebase onto origin/<trunk>; the local trunk is left alone"
    )


def test_amending_a_lower_branch_restacks_its_children(stacked_sandbox):
    """SPEC.md sec 2 STEP 1: the fork point survives an amend, because it is
    derived against origin/<parent>, not the parent's local tip."""
    sb = stacked_sandbox
    sb.amend("feat-a", subject="feat-a: c1 (amended)")

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert actions(report)["feat-a"] is BranchAction.SKIPPED_UP_TO_DATE
    assert set(report.changed_branches) == {"feat-b", "feat-c"}
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1 (amended)"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    assert report.dropped == ()


def test_a_new_commit_mid_stack_replays_only_the_branch_own_commits(stacked_sandbox):
    """SPEC.md sec 2 STEP 2: each branch owns exactly A=1 B=2 C=1, and the
    parent's unpushed commit is not reported as a dropped commit."""
    sb = stacked_sandbox
    sb.commit("feat-b: c2", branch="feat-b", files={"feat-b-2.txt": "b2\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1", "feat-b: c2"],
        "feat-c": ["feat-c: c1"],
    }
    assert report.dropped == (), "feat-b's own commit is not feat-c's to lose"


def test_the_guard_fires_before_any_rebase_when_a_parent_was_force_pushed(stacked_sandbox):
    """SPEC.md sec 2 STEP 4, CLAUDE.md invariant 3b."""
    sb = stacked_sandbox
    sb.amend("feat-a", subject="feat-a: rewritten")
    sb.push("feat-a", force=True)
    sb.fetch()
    before = sb.tips()

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.GUARD_VIOLATION
    assert report.guard is not None
    assert report.guard.branch == "feat-b"
    assert report.guard.parent == "feat-a"
    assert sb.tips() == before, "nothing may be rebased once the guard fails"
    assert report.queued == ("feat-b", "feat-c")
    assert report.ready_for_push is False


def test_the_skip_check_runs_before_the_guard(stacked_sandbox):
    """CLAUDE.md invariant 3: a branch left correctly restacked by an earlier
    cascade has a stale origin/<parent>; running the guard first would report it
    as a violation."""
    sb = stacked_sandbox
    sb.amend("feat-a", subject="feat-a: c1 (amended)")
    # what an earlier, half-finished cascade leaves behind: feat-b already sits on
    # the new feat-a, while origin/feat-a still points at the old tip.
    fork = sb.git.merge_base("origin/feat-a", "feat-b")
    assert sb.git.rebase_onto(sb.sha("feat-a"), fork, "feat-b").ok
    assert not sb.git.is_ancestor("origin/feat-a", "feat-b")

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert actions(report)["feat-b"] is BranchAction.SKIPPED_UP_TO_DATE
    assert actions(report)["feat-c"] is BranchAction.RESTACKED
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1 (amended)"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }


def test_a_branch_never_pushed_derives_its_fork_from_the_run_snapshot(stacked_sandbox):
    """origin/<parent> is the parent's last-synced state -- but a brand new branch
    has none, so the fork point falls back to the parent's tip at the START of the
    run, never its tip after the cascade already moved it."""
    sb = stacked_sandbox
    sb.checkout("feat-c")
    sb.create_branch("feat-d")
    sb.commit("feat-d: c1", files={"feat-d-1.txt": "d1\n"})
    assert sb.git.try_rev_parse("origin/feat-d") is None
    sb.advance_trunk(1)

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c", "feat-d"))

    assert report.status is RestackStatus.OK
    assert sb.shape("feat-a", "feat-b", "feat-c", "feat-d", base="origin/main") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
        "feat-d": ["feat-d: c1"],
    }


# --------------------------------------------------------------------------
# squash merge: skipping and transitive hoisting
# --------------------------------------------------------------------------

def test_a_merged_branch_is_skipped_and_its_child_hoists_to_the_trunk(stacked_sandbox):
    """CLAUDE.md invariant 6: replaying a merged branch either drops all its
    commits or conflicts against the squash commit."""
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    merged_tip = sb.sha("feat-a")

    stack = stack_model(sb, "feat-a", "feat-b", "feat-c", merged=("feat-a",))
    report = restack(sb.git, stack)

    assert report.status is RestackStatus.OK
    assert actions(report)["feat-a"] is BranchAction.SKIPPED_MERGED
    assert sb.sha("feat-a") == merged_tip, "a merged branch is never rebased"
    assert [(r.branch, r.old_parent, r.new_parent) for r in report.reparented] == [
        ("feat-b", "feat-a", "main")
    ]
    assert sb.shape("feat-b", "feat-c", base="origin/main") == {
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    assert report.dropped == ()


def test_two_branches_merged_in_one_run_hoist_transitively(stacked_sandbox):
    """CLAUDE.md invariant 7: hoist to the nearest UNMERGED ancestor, to a
    fixpoint -- one level would leave feat-c parented to a doomed branch."""
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    sb.squash_merge("feat-b")

    stack = stack_model(sb, "feat-a", "feat-b", "feat-c", merged=("feat-a", "feat-b"))
    report = restack(sb.git, stack)

    assert report.status is RestackStatus.OK
    assert actions(report) == {
        "feat-a": BranchAction.SKIPPED_MERGED,
        "feat-b": BranchAction.SKIPPED_MERGED,
        "feat-c": BranchAction.RESTACKED,
    }
    assert [(r.branch, r.new_parent) for r in report.reparented] == [("feat-c", "main")]
    assert sb.shape("feat-c", base="origin/main") == {"feat-c": ["feat-c: c1"]}


def test_hoist_over_merged_is_a_fixpoint_and_reports_what_it_changed(stacked_sandbox):
    sb = stacked_sandbox
    stack = stack_model(sb, "feat-a", "feat-b", "feat-c", merged=("feat-a", "feat-b"))

    changes = hoist_over_merged(stack)

    assert [(c.branch, c.old_parent, c.new_parent) for c in changes] == [
        ("feat-c", "feat-b", "main")
    ]
    assert {b.name: b.parent for b in stack.branches}["feat-c"] == "main"
    assert hoist_over_merged(stack) == [], "hoisting twice changes nothing"


def test_a_merged_branch_whose_remote_ref_is_gone_still_yields_a_fork_point(stacked_sandbox):
    """`delete_branch_on_merge` removes origin/feat-a; the child's fork point
    falls back to the parent's tip at the start of the run."""
    sb = stacked_sandbox
    sb.squash_merge("feat-a", delete_branch=True)
    assert sb.git.try_rev_parse("origin/feat-a") is None

    stack = stack_model(sb, "feat-a", "feat-b", "feat-c", merged=("feat-a",))
    report = restack(sb.git, stack)

    assert report.status is RestackStatus.OK
    assert sb.shape("feat-b", "feat-c", base="origin/main") == {
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }


# --------------------------------------------------------------------------
# the timing problem (SPEC.md sec 7)
# --------------------------------------------------------------------------

def test_timing_an_independent_late_fix_on_a_lower_branch(stacked_sandbox):
    sb = stacked_sandbox
    sb.commit("feat-a: address review", branch="feat-a", files={"a-review.txt": "fix\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert report.dropped == ()
    assert [e.status for e in report.verification.entries] == ["=", "="]
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1", "feat-a: address review"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }


def test_timing_a_conflicting_late_fix_stops_the_cascade_with_a_report(stacked_sandbox):
    sb = stacked_sandbox
    sb.commit("feat-b: touch shared", branch="feat-b", files={"shared.txt": "from b\n"})
    sb.commit("feat-a: touch shared", branch="feat-a", files={"shared.txt": "from a\n"})
    feat_c_before = sb.sha("feat-c")

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.CONFLICT
    conflict = report.conflict
    assert conflict.branch == "feat-b"
    assert conflict.parent == "feat-a"
    assert conflict.stopped_subject == "feat-b: touch shared"
    assert conflict.files == ("shared.txt",)
    assert conflict.queued == ("feat-c",)
    assert report.queued == ("feat-c",)
    assert sb.sha("feat-c") == feat_c_before, "the cascade stops where it stopped"
    assert sb.git.rebase_in_progress(), "git's normal rebase state is left in place"
    assert report.ready_for_push is False


def test_timing_a_duplicate_commit_is_dropped_and_detected_structurally(stacked_sandbox):
    """SPEC.md sec 7.2 / CLAUDE.md invariant 10: the drop is found by comparing
    ranges.  git's own "dropping ..." line is never parsed."""
    sb = stacked_sandbox
    sb.commit("feat-c: hotfix", branch="feat-c", files={"hot.txt": "fixed\n"})
    # the same fix, asked for by a reviewer three branches down
    sb.commit("feat-a: hotfix", branch="feat-a", files={"hot.txt": "fixed\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert [(d.branch, d.subject) for d in report.dropped] == [("feat-c", "feat-c: hotfix")]
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1", "feat-a: hotfix"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    assert report.by_branch["feat-c"].emptied is False
    assert report.verification.ok, "a clean drop is reported, not a reason to stop"


def test_timing_a_branch_that_loses_only_some_commits_stays_in_the_stack(stacked_sandbox):
    """SPEC.md sec 7.2: "A branch that loses only *some* commits stays in the
    stack, with the loss reported"."""
    sb = stacked_sandbox
    sb.commit("feat-b: the fix", branch="feat-b", files={"fix.txt": "fixed\n"})
    sb.commit("feat-a: the same fix", branch="feat-a", files={"fix.txt": "fixed\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b"))

    assert report.status is RestackStatus.OK
    assert [(d.branch, d.subject) for d in report.dropped] == [("feat-b", "feat-b: the fix")]
    assert report.by_branch["feat-b"].emptied is False
    assert sb.subjects("feat-a..feat-b") == ["feat-b: c1"]
    assert report.emptied_branches == ()


def test_a_branch_whose_every_commit_is_dropped_is_reported_as_emptied(sandbox):
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.checkout("feat-a")
    sb.create_branch("feat-b")
    sb.commit("feat-b: the fix", files={"fix.txt": "fixed\n"})
    sb.push("feat-b")
    sb.create_branch("feat-c")
    sb.commit("feat-c: c1", files={"feat-c-1.txt": "c1\n"})
    sb.push("feat-c")
    # the reviewer asks for feat-b's fix on feat-a instead
    sb.commit("feat-a: the same fix", branch="feat-a", files={"fix.txt": "fixed\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    empty = report.by_branch["feat-b"]
    assert empty.emptied is True
    assert sb.git.rev_list_count("feat-a..feat-b") == 0
    assert report.emptied_branches == ("feat-b",)
    assert [(d.branch, d.subject) for d in report.dropped] == [
        ("feat-b", "feat-b: the fix")
    ]
    # the child rides over the emptied branch untouched
    assert sb.subjects("feat-b..feat-c") == ["feat-c: c1"]


# --------------------------------------------------------------------------
# conflicts and re-entrancy (SPEC.md sec 8)
# --------------------------------------------------------------------------

def _stop_mid_cascade(sb: Sandbox):
    """main <- feat-a <- feat-b <- feat-c with a conflict on feat-b."""
    sb.commit("feat-b: touch shared", branch="feat-b", files={"shared.txt": "from b\n"})
    sb.commit("feat-a: touch shared", branch="feat-a", files={"shared.txt": "from a\n"})
    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))
    assert report.status is RestackStatus.CONFLICT
    return report


def test_sync_resumes_its_own_rebase_and_finishes_the_cascade(stacked_sandbox):
    sb = stacked_sandbox
    _stop_mid_cascade(sb)

    sb.write("shared.txt", "from a and b\n")
    sb.git.run("add", "--", "shared.txt")

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert actions(report)["feat-b"] is BranchAction.RESUMED
    assert actions(report)["feat-c"] is BranchAction.RESTACKED
    assert not sb.git.rebase_in_progress()
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1", "feat-a: touch shared"],
        "feat-b": ["feat-b: c1", "feat-b: touch shared"],
        "feat-c": ["feat-c: c1"],
    }
    assert report.dropped == (), "the resolved commit survived; it was not dropped"
    assert any(e.rewritten for e in report.verification.entries), (
        "a resolution range-diff cannot pair with, and must not be read as a loss"
    )
    assert report.verification.ok is True
    assert report.ready_for_push is True


def test_an_unresolved_conflict_is_reprinted_not_continued(stacked_sandbox):
    sb = stacked_sandbox
    first = _stop_mid_cascade(sb)

    again = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert again.status is RestackStatus.CONFLICT
    assert again.conflict.branch == "feat-b"
    assert again.conflict.files == ("shared.txt",)
    assert again.conflict.queued == ("feat-c",)
    assert again.conflict.stopped_subject == first.conflict.stopped_subject
    assert sb.git.rebase_in_progress()


def test_refuses_to_continue_a_rebase_on_a_branch_outside_the_stack(stacked_sandbox):
    """CLAUDE.md invariant 22: continuing a user's own `git rebase -i` is the
    failure mode this prevents."""
    sb = stacked_sandbox
    sb.commit("trunk: shared", branch=sb.trunk, files={"shared.txt": "from trunk\n"})
    sb.checkout(sb.trunk)
    sb.git.run("checkout", "-b", "wip", f"{sb.trunk}~1")
    sb.commit("wip: shared", files={"shared.txt": "from wip\n"})
    result = sb.git.rebase_onto(sb.sha(sb.trunk), sb.sha(f"{sb.trunk}~1"), "wip")
    assert result.outcome is RebaseOutcome.CONFLICT
    state_before = sb.git.read_rebase_state()

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.FOREIGN_REBASE
    assert report.foreign is not None
    assert report.foreign.state.branch == "wip"
    assert "wip" in report.foreign.reason
    assert report.results == []
    after = sb.git.read_rebase_state()
    assert after.onto == state_before.onto
    assert sb.git.rebase_in_progress(), "the user's rebase is left exactly as it was"


def test_refuses_a_rebase_of_a_member_onto_something_that_is_not_its_parent(stacked_sandbox):
    """head-name IS a stack member, but `onto` is not the tip of its derived
    parent -- so it is not ours (CLAUDE.md invariant 22)."""
    sb = stacked_sandbox
    sb.commit("feat-b: touch shared", branch="feat-b", files={"shared.txt": "from b\n"})
    sb.advance_trunk(1, subjects=["trunk: touch shared"])
    sb.teammate_commit(sb.trunk, subject="trunk: shared", files={"shared.txt": "from trunk\n"})
    sb.fetch()
    # the user rebases feat-b straight onto origin/main, skipping its parent
    fork = sb.git.merge_base("origin/feat-a", "feat-b")
    result = sb.git.rebase_onto(sb.sha("origin/main"), fork, "feat-b")
    assert result.outcome is RebaseOutcome.CONFLICT

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.FOREIGN_REBASE
    assert report.foreign.state.branch == "feat-b"
    assert "onto" in report.foreign.reason
    assert sb.git.rebase_in_progress()


def test_read_resume_reports_no_rebase_in_progress(stacked_sandbox):
    sb = stacked_sandbox
    decision = read_resume(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))
    assert decision.in_progress is False
    assert decision.ours is False
    assert decision.branch is None


def test_read_resume_recognises_our_own_conflicted_rebase(stacked_sandbox):
    sb = stacked_sandbox
    _stop_mid_cascade(sb)

    decision = read_resume(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert decision.in_progress is True
    assert decision.ours is True
    assert decision.branch == "feat-b"
    assert decision.resolved is False


def test_a_half_finished_cascade_self_heals_on_the_next_run(sandbox):
    """CLAUDE.md invariant 24 / SPEC.md sec 8: after `git rebase --abort` the next
    run skips the branches below the conflict and retries only the failed one."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b", "feat-c"])
    # feat-b will conflict with feat-a; feat-a itself needs a restack onto the trunk
    sb.advance_trunk(1)
    sb.commit("feat-b: touch shared", branch="feat-b", files={"shared.txt": "from b\n"})
    sb.commit("feat-a: touch shared", branch="feat-a", files={"shared.txt": "from a\n"})

    first = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))
    assert first.status is RestackStatus.CONFLICT
    assert actions(first)["feat-a"] is BranchAction.RESTACKED
    feat_a_after = sb.sha("feat-a")

    sb.git.rebase_abort()

    second = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert second.status is RestackStatus.CONFLICT
    assert second.conflict.branch == "feat-b"
    assert actions(second)["feat-a"] is BranchAction.SKIPPED_UP_TO_DATE, (
        "the branch below the conflict is already correct; it must not be rebased again"
    )
    assert sb.sha("feat-a") == feat_a_after


def test_a_conflict_resolved_to_an_empty_diff_still_completes(stacked_sandbox):
    """SPEC.md sec 7.2: "after a conflict resolved to an empty diff it says
    nothing at all" -- git needs a skip, and the drop must still be reported."""
    sb = stacked_sandbox
    sb.commit("feat-b: touch shared", branch="feat-b", files={"shared.txt": "from b\n"})
    sb.commit("feat-a: touch shared", branch="feat-a", files={"shared.txt": "from a\n"})
    assert restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c")).status is (
        RestackStatus.CONFLICT
    )

    # the reviewer's version wins: the resolution is exactly what feat-a already has
    sb.write("shared.txt", "from a\n")
    sb.git.run("add", "--", "shared.txt")

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert not sb.git.rebase_in_progress()
    assert sb.subjects("feat-a..feat-b") == ["feat-b: c1"]
    assert [(d.branch, d.subject) for d in report.dropped] == [
        ("feat-b", "feat-b: touch shared")
    ]


# --------------------------------------------------------------------------
# verification (SPEC.md sec 6.1, CLAUDE.md invariants 9 and 10)
# --------------------------------------------------------------------------

def test_verification_compares_each_branch_against_its_own_new_fork_point(stacked_sandbox):
    sb = stacked_sandbox
    sb.advance_trunk(1)

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.verification is not None
    assert report.verification.ok is True
    assert {e.branch for e in report.verification.entries} == {"feat-a", "feat-b", "feat-c"}
    assert all(e.status == "=" for e in report.verification.entries)
    assert report.ready_for_push is True


def test_verification_marks_a_dropped_commit_and_still_allows_the_push(stacked_sandbox):
    sb = stacked_sandbox
    sb.commit("feat-c: hotfix", branch="feat-c", files={"hot.txt": "fixed\n"})
    sb.commit("feat-a: hotfix", branch="feat-a", files={"hot.txt": "fixed\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    dropped = [e for e in report.verification.entries if e.dropped]
    assert [(e.branch, e.subject) for e in dropped] == [("feat-c", "feat-c: hotfix")]
    assert report.verification.unexpected == ()
    assert report.verification.ok is True


def test_the_snapshot_records_every_member_tip_before_the_walk(stacked_sandbox):
    sb = stacked_sandbox
    before = {name: sb.sha(name) for name in ("feat-a", "feat-b", "feat-c")}
    sb.advance_trunk(1)

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.snapshot == before
    assert all(sb.sha(name) != before[name] for name in before)


def test_a_commit_the_parent_owns_is_not_reported_as_dropped(stacked_sandbox):
    """The fork point is origin/<parent>, so a commit the parent has not pushed
    yet gets replayed along with the branch's own and is dropped as already
    upstream.  It was never this branch's commit, so it is not a loss."""
    sb = stacked_sandbox
    sb.commit("feat-b: c2", branch="feat-b", files={"feat-b-2.txt": "b2\n"})
    # an earlier, unpushed cascade already moved feat-c onto feat-b's new tip
    fork = sb.git.merge_base("origin/feat-b", "feat-c")
    assert sb.git.rebase_onto(sb.sha("feat-b"), fork, "feat-c").ok
    sb.commit("feat-b: c3", branch="feat-b", files={"feat-b-3.txt": "b3\n"})

    report = restack(sb.git, stack_model(sb, "feat-a", "feat-b", "feat-c"))

    assert report.status is RestackStatus.OK
    assert report.dropped == (), "feat-b: c2 is feat-b's commit, not feat-c's"
    carried = [e for e in report.verification.entries if e.carried]
    assert [e.subject for e in carried] == ["feat-b: c2"]
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1", "feat-b: c2", "feat-b: c3"],
        "feat-c": ["feat-c: c1"],
    }


def test_rebase_skip_gets_past_a_patch_that_resolved_to_nothing(conflicted):
    """The fallback for a git that stops on an emptied patch instead of dropping
    it: `git rebase --continue` then wants `--skip`, and nothing else gets past
    it.  git 2.39.5 drops it during --continue, so this exercises the helper
    directly."""
    sb = conflicted.sb

    outcome = _rebase_skip(sb.git, conflicted.branch, conflicted.onto)

    assert outcome.ok
    assert not sb.git.rebase_in_progress()
    assert sb.sha("feat") == sb.sha(sb.trunk), "the only commit was skipped"
