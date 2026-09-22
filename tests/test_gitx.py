"""stackem.gitx -- the only module allowed to run git.

Everything here runs against REAL git repositories built by tests/harness.py.
Nothing is mocked.
"""

from __future__ import annotations

import subprocess

import pytest

from stackem.gitx import (
    MINIMUM_GIT_VERSION,
    Git,
    GitError,
    GitInvocation,
    MergeTreeResult,
    RangeDiffEntry,
    RebaseOutcome,
    RebaseResult,
    RebaseState,
)


# --------------------------------------------------------------------------
# run(): the single choke point
# --------------------------------------------------------------------------

def test_run_returns_completed_process_with_captured_output(sandbox):
    proc = sandbox.git.run("rev-parse", "--is-inside-work-tree")
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "true"
    assert proc.stderr == ""


def test_run_check_true_raises_git_error_carrying_context(sandbox):
    with pytest.raises(GitError) as excinfo:
        sandbox.git.run("rev-parse", "--verify", "refs/heads/nope")
    err = excinfo.value
    assert err.returncode != 0
    assert err.argv[0] == "git"
    assert "rev-parse" in err.argv
    assert str(sandbox.path) == err.cwd
    assert "nope" in str(err)


def test_run_check_false_returns_the_failure(sandbox):
    proc = sandbox.git.run("rev-parse", "--verify", "refs/heads/nope", check=False)
    assert proc.returncode != 0


def test_run_accepts_a_per_call_cwd(sandbox):
    proc = sandbox.git.run("rev-parse", "--is-bare-repository", cwd=sandbox.origin)
    assert proc.stdout.strip() == "true"


def test_every_invocation_is_traced_and_logged(sandbox):
    seen: list[GitInvocation] = []
    git = Git(sandbox.path, env=sandbox.env, logger=seen.append)
    git.run("rev-parse", "HEAD")
    git.run("status", "--porcelain")
    assert [inv.argv[1] for inv in seen] == ["rev-parse", "status"]
    assert seen == git.trace
    assert seen[0].argv[0] == "git"
    assert seen[0].returncode == 0
    assert seen[0].duration_s >= 0
    assert seen[0].command.startswith("git rev-parse")


def test_a_failing_invocation_is_still_traced(sandbox):
    git = Git(sandbox.path, env=sandbox.env)
    git.run("rev-parse", "--verify", "refs/heads/nope", check=False)
    assert git.trace[-1].returncode != 0


def test_version_is_at_least_the_documented_minimum(sandbox):
    # SPEC.md sec 11: merge-tree --write-tree needs 2.38, --force-if-includes 2.30.
    assert sandbox.git.version() >= MINIMUM_GIT_VERSION
    sandbox.git.check_version()


# --------------------------------------------------------------------------
# reading the repository
# --------------------------------------------------------------------------

def test_rev_parse_resolves_and_try_rev_parse_reports_absence(sandbox):
    head = sandbox.git.rev_parse("HEAD")
    assert len(head) == 40
    assert sandbox.git.rev_parse(sandbox.trunk) == head
    assert sandbox.git.try_rev_parse("refs/heads/nope") is None
    with pytest.raises(GitError):
        sandbox.git.rev_parse("refs/heads/nope")


def test_tree_id_differs_from_commit_id(sandbox):
    assert sandbox.git.tree_id("HEAD") != sandbox.git.rev_parse("HEAD")


def test_merge_base_and_is_ancestor(stacked_sandbox):
    sb = stacked_sandbox
    git = sb.git
    root = sb.sha(sb.trunk)
    assert git.merge_base(sb.trunk, "feat-c") == root
    assert git.is_ancestor(sb.trunk, "feat-c") is True
    assert git.is_ancestor("feat-c", sb.trunk) is False
    # SPEC.md sec 2: the fork point is merge-base(origin/<parent>, <branch>).
    assert git.merge_base("origin/feat-a", "feat-b") == sb.sha("feat-a")


def test_merge_base_returns_none_for_unrelated_histories(sandbox):
    sandbox.git.run("checkout", "--orphan", "island")
    sandbox.commit("island root", files={"island.txt": "x\n"})
    assert sandbox.git.merge_base(sandbox.trunk, "island") is None


def test_for_each_ref_and_branches(stacked_sandbox):
    sb = stacked_sandbox
    # SPEC.md sec 5.1: every parent candidate in one git call.
    lines = sb.git.for_each_ref("refs/heads", "%(refname:short) %(objectname)")
    names = sorted(line.split()[0] for line in lines)
    assert names == ["feat-a", "feat-b", "feat-c", sb.trunk]
    branches = sb.git.branches()
    assert branches["feat-b"] == sb.sha("feat-b")
    remote = sb.git.branches(namespace="refs/remotes/origin")
    assert remote["feat-b"] == sb.sha("feat-b")


def test_rev_list_count_is_the_unique_commit_guard(stacked_sandbox):
    # CLAUDE.md invariant 19 / SPEC.md sec 6.4: merge detection needs this guard.
    sb = stacked_sandbox
    assert sb.git.rev_list_count(f"{sb.trunk}..feat-a") == 1
    sb.create_branch("wip-behind", start=sb.sha(sb.trunk))
    sb.advance_trunk(1)
    sb.git.run("branch", "-f", sb.trunk, f"origin/{sb.trunk}")
    assert sb.git.rev_list_count(f"{sb.trunk}..wip-behind") == 0


def test_rev_list_returns_shas_newest_first(stacked_sandbox):
    sb = stacked_sandbox
    shas = sb.git.rev_list(f"{sb.trunk}..feat-c")
    assert shas == [sb.sha("feat-c"), sb.sha("feat-b"), sb.sha("feat-a")]


def test_commit_subject(stacked_sandbox):
    assert stacked_sandbox.git.commit_subject("feat-b") == "feat-b: c1"


def test_current_branch_and_symbolic_ref(sandbox):
    assert sandbox.git.current_branch() == sandbox.trunk
    # SPEC.md sec 6.6: trunk detection starts at refs/remotes/origin/HEAD.
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD", short=True) == f"origin/{sandbox.trunk}"
    sandbox.unset_origin_head()
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD") is None
    sandbox.git.remote_set_head("origin")
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD", short=True) == f"origin/{sandbox.trunk}"


def test_current_branch_is_none_when_detached(sandbox):
    sandbox.git.run("checkout", "--detach", "HEAD")
    assert sandbox.git.current_branch() is None


def test_worktree_cleanliness(sandbox):
    # SPEC.md sec 11: require a clean worktree for any restack.
    assert sandbox.git.is_clean() is True
    sandbox.write("dirty.txt", "x\n")
    sandbox.git.run("add", "dirty.txt")
    assert sandbox.git.is_clean() is False
    assert "dirty.txt" in sandbox.git.status_porcelain()


# --------------------------------------------------------------------------
# rebase: the one primitive (SPEC.md sec 2)
# --------------------------------------------------------------------------

def test_rebase_onto_replays_only_the_branch_commits(stacked_sandbox):
    sb = stacked_sandbox
    sb.advance_trunk(1, subjects=["trunk moves"])
    onto = sb.sha(f"origin/{sb.trunk}")
    fork = sb.git.merge_base(f"origin/{sb.trunk}", "feat-a")
    result = sb.git.rebase_onto(onto, fork, "feat-a")
    assert isinstance(result, RebaseResult)
    assert result.outcome is RebaseOutcome.OK
    assert result.conflicted_files == ()
    assert sb.git.is_ancestor(onto, "feat-a")
    assert sb.subjects(f"{onto}..feat-a") == ["feat-a: c1"]


def test_rebase_onto_surfaces_conflict_as_a_typed_outcome(conflicted):
    # SPEC.md sec 8: sync stops and leaves an ordinary git rebase state.
    result = conflicted.result
    assert result.outcome is RebaseOutcome.CONFLICT
    assert result.returncode != 0
    assert result.branch == "feat"
    assert "f.txt" in result.conflicted_files
    assert result.stopped_subject == "feat edits f"
    assert conflicted.sb.git.rebase_in_progress() is True


def test_rebase_onto_raises_for_a_non_conflict_failure(sandbox):
    with pytest.raises(GitError):
        sandbox.git.rebase_onto("HEAD", "HEAD", "refs/heads/not-a-branch")
    assert sandbox.git.rebase_in_progress() is False


def test_read_rebase_state_exposes_what_invariant_22_needs(conflicted):
    # CLAUDE.md invariant 22: ours iff head-name is a member and onto == tip(parent).
    sb = conflicted.sb
    state = sb.git.read_rebase_state()
    assert isinstance(state, RebaseState)
    assert state.kind == "merge"
    assert state.head_name == "refs/heads/feat"
    assert state.branch == "feat"
    assert state.onto == conflicted.onto
    assert state.orig_head == conflicted.orig_head
    assert state.stopped_sha == conflicted.orig_head


def test_read_rebase_state_returns_none_when_no_rebase_is_running(sandbox):
    assert sandbox.git.read_rebase_state() is None
    assert sandbox.git.rebase_in_progress() is False


def test_read_rebase_state_handles_the_apply_backend(sandbox):
    sb = sandbox
    sb.commit("base f", files={"f.txt": "base\n"})
    sb.push(sb.trunk)
    sb.create_branch("feat")
    sb.commit("feat edits f", files={"f.txt": "feat\n"})
    sb.checkout(sb.trunk)
    sb.commit("trunk edits f", files={"f.txt": "trunk\n"})
    fork = sb.git.merge_base(sb.trunk, "feat")
    onto = sb.sha(sb.trunk)
    sb.git.run("rebase", "--apply", "--onto", onto, fork, "feat", check=False)
    state = sb.git.read_rebase_state()
    assert state is not None and state.kind == "apply"
    assert state.branch == "feat"
    assert state.onto == onto
    assert sb.git.rebase_in_progress() is True
    sb.git.rebase_abort()
    assert sb.git.read_rebase_state() is None


def test_rebase_continue_finishes_the_restack(conflicted):
    sb = conflicted.sb
    sb.write("f.txt", "resolved\n")
    sb.git.run("add", "f.txt")
    result = sb.git.rebase_continue()
    assert result.outcome is RebaseOutcome.OK
    assert sb.git.rebase_in_progress() is False
    assert sb.subjects(f"{conflicted.onto}..feat") == ["feat edits f"]


def test_rebase_continue_without_a_rebase_is_an_error(sandbox):
    with pytest.raises(GitError):
        sandbox.git.rebase_continue()


def test_rebase_abort_restores_the_branch(conflicted):
    # SPEC.md sec 8 / CLAUDE.md invariant 24: backing out is git rebase --abort.
    sb = conflicted.sb
    sb.git.rebase_abort()
    assert sb.git.rebase_in_progress() is False
    assert sb.sha("feat") == conflicted.orig_head


# --------------------------------------------------------------------------
# verification primitives
# --------------------------------------------------------------------------

def test_range_diff_reports_every_commit_equal_after_a_clean_restack(stacked_sandbox):
    # SPEC.md sec 6.1: "=" means a byte-identical patch.
    sb = stacked_sandbox
    old_fork = sb.git.merge_base(sb.trunk, "feat-a")
    old_tip = sb.sha("feat-a")
    sb.advance_trunk(1, subjects=["trunk moves"])
    onto = sb.sha(f"origin/{sb.trunk}")
    sb.git.rebase_onto(onto, old_fork, "feat-a")
    entries = sb.git.range_diff(f"{old_fork}..{old_tip}", f"{onto}..feat-a")
    assert [e.status for e in entries] == ["="]
    entry = entries[0]
    assert isinstance(entry, RangeDiffEntry)
    assert entry.old_index == 1 and entry.new_index == 1
    assert entry.subject == "feat-a: c1"
    assert sb.git.range_diff_raw(f"{old_fork}..{old_tip}", f"{onto}..feat-a").strip()


def test_range_diff_reports_a_dropped_commit_structurally(stacked_sandbox):
    # SPEC.md sec 7.2 / CLAUDE.md invariant 10: never parse rebase output.
    sb = stacked_sandbox
    fork = sb.git.merge_base(sb.trunk, "feat-c")
    old_tip = sb.sha("feat-c")
    entries = sb.git.range_diff(f"{fork}..{old_tip}", f"{fork}..{old_tip}~1")
    assert [e.status for e in entries] == ["=", "=", "<"]
    dropped = entries[-1]
    assert dropped.status == "<"
    assert dropped.new_index is None
    assert dropped.subject == "feat-c: c1"


def test_merge_tree_write_tree_identifies_a_contained_branch(stacked_sandbox):
    # SPEC.md sec 6.4: merging the branch into trunk yields the trunk's own tree.
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    result = sb.git.merge_tree_write_tree(f"origin/{sb.trunk}", "feat-a")
    assert isinstance(result, MergeTreeResult)
    assert result.conflicted is False
    assert result.tree == sb.git.tree_id(f"origin/{sb.trunk}")
    # ...and the guard that keeps a merely-behind branch from matching.
    assert sb.git.rev_list_count(f"origin/{sb.trunk}..feat-a") > 0


def test_merge_tree_write_tree_reports_conflicts(sandbox):
    sb = sandbox
    sb.commit("base f", files={"f.txt": "base\n"})
    sb.create_branch("feat")
    sb.commit("feat edits f", files={"f.txt": "feat\n"})
    sb.checkout(sb.trunk)
    sb.commit("trunk edits f", files={"f.txt": "trunk\n"})
    result = sb.git.merge_tree_write_tree(sb.trunk, "feat")
    assert result.conflicted is True
    assert result.tree is not None


# --------------------------------------------------------------------------
# remote
# --------------------------------------------------------------------------

def test_push_uses_the_lease_flags_and_never_bare_force(stacked_sandbox):
    # CLAUDE.md invariants 11 and 12.
    sb = stacked_sandbox
    sb.commit("feat-c: c2", branch="feat-c")
    proc = sb.git.push("origin", ["feat-c"])
    assert proc.returncode == 0
    argv = sb.git.trace[-1].argv
    assert "--atomic" in argv
    assert "--force-with-lease" in argv
    assert "--force-if-includes" in argv
    assert "--force" not in argv
    assert "-f" not in argv
    assert sb.origin_sha("feat-c") == sb.sha("feat-c")


def test_push_refuses_a_forced_refspec(stacked_sandbox):
    with pytest.raises(ValueError):
        stacked_sandbox.git.push("origin", ["+feat-c:feat-c"])


def test_push_is_atomic_across_branches(stacked_sandbox):
    sb = stacked_sandbox
    sb.commit("feat-a: c2", branch="feat-a")
    sb.commit("feat-b: c2", branch="feat-b")
    sb.git.push("origin", ["feat-a", "feat-b"])
    assert sb.origin_sha("feat-a") == sb.sha("feat-a")
    assert sb.origin_sha("feat-b") == sb.sha("feat-b")


def test_push_rejected_by_the_lease_returns_instead_of_raising(stacked_sandbox):
    # CLAUDE.md invariant 11: a teammate's unseen commit survives.
    sb = stacked_sandbox
    mate = sb.teammate_commit("feat-a", subject="mate work")
    sb.commit("feat-a: c2", branch="feat-a")
    proc = sb.git.push("origin", ["feat-a"])
    assert proc.returncode != 0
    assert sb.origin_sha("feat-a") == mate


def test_push_delete_is_available_but_explicit(stacked_sandbox):
    # CLAUDE.md invariant 14: stackem never deletes on its own -- the helper exists
    # for tests and for printing, and deleting requires asking for it.
    sb = stacked_sandbox
    proc = sb.git.push("origin", ["feat-c"], delete=True)
    assert proc.returncode == 0
    assert "--delete" in sb.git.trace[-1].argv
    assert sb.origin_sha("feat-c") is None


def test_fetch_prunes_deleted_remote_branches(stacked_sandbox):
    sb = stacked_sandbox
    sb.delete_remote_branch("feat-c")
    assert sb.git.try_rev_parse("refs/remotes/origin/feat-c") is not None
    sb.git.fetch("origin", prune=True)
    assert "--prune" in sb.git.trace[-1].argv
    assert sb.git.try_rev_parse("refs/remotes/origin/feat-c") is None


def test_fetch_accepts_explicit_refspecs(stacked_sandbox):
    # SPEC.md sec 6.3: rescue fetches refs/pull/N/head.
    sb = stacked_sandbox
    sb.set_pull_ref(7, sb.sha("feat-c"))
    sb.git.fetch("origin", refspecs=["refs/pull/7/head:rescue-head"])
    assert sb.sha("rescue-head") == sb.sha("feat-c")
