"""tests/harness.py -- real git repositories in temp directories.

Every other agent's tests are built on this, so its contract is pinned here.
SPEC.md sec 10: unit and integration tests use real repositories; only the forge
is faked.
"""

from __future__ import annotations

import pytest

from stackem.gitx import Git
from tests.harness import Sandbox


# --------------------------------------------------------------------------
# shape of the sandbox
# --------------------------------------------------------------------------

def test_sandbox_is_a_bare_origin_plus_a_real_clone(sandbox):
    assert isinstance(sandbox, Sandbox)
    assert sandbox.origin.is_dir()
    assert (sandbox.path / ".git").is_dir()
    assert sandbox.git.run("rev-parse", "--is-bare-repository", cwd=sandbox.origin).stdout.strip() == "true"
    assert sandbox.git.run("rev-parse", "--is-bare-repository").stdout.strip() == "false"
    assert isinstance(sandbox.git, Git)
    assert isinstance(sandbox.origin_git, Git)
    assert sandbox.trunk == "main"
    assert sandbox.git.current_branch() == "main"
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD", short=True) == "origin/main"


def test_the_trunk_starts_pushed_and_identical_on_both_sides(sandbox):
    assert sandbox.origin_sha("main") == sandbox.sha("main")
    assert sandbox.sha("origin/main") == sandbox.sha("main")


def test_the_sandbox_is_isolated_from_the_developers_git_config(sandbox):
    author = sandbox.git.run("log", "-1", "--format=%an <%ae>").stdout.strip()
    assert author == "stackem tests <tests@stackem.invalid>"
    assert sandbox.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert sandbox.env["GIT_CONFIG_SYSTEM"] == "/dev/null"


def test_commits_are_deterministic_across_sandboxes(tmp_path_factory, git_env):
    from tests.harness import make_sandbox

    one = make_sandbox(tmp_path_factory.mktemp("one"))
    two = make_sandbox(tmp_path_factory.mktemp("two"))
    one.make_stack(["feat-a"], commits=2)
    two.make_stack(["feat-a"], commits=2)
    assert one.sha("feat-a") == two.sha("feat-a")


# --------------------------------------------------------------------------
# building history
# --------------------------------------------------------------------------

def test_commit_writes_a_real_commit_with_a_default_file(sandbox):
    sha = sandbox.commit("add a thing")
    assert sha == sandbox.sha("HEAD")
    assert sandbox.git.commit_subject("HEAD") == "add a thing"
    assert (sandbox.path / "add-a-thing.txt").exists()


def test_commit_accepts_explicit_files_and_a_branch(sandbox):
    sandbox.create_branch("feat")
    sandbox.checkout(sandbox.trunk)
    sandbox.commit("on feat", files={"a/b.txt": "hello\n"}, branch="feat")
    assert sandbox.git.current_branch() == sandbox.trunk  # branch= restores HEAD
    assert sandbox.subjects(f"{sandbox.trunk}..feat") == ["on feat"]
    assert (sandbox.path / "a" / "b.txt").exists() is False  # it lives on feat only


def test_write_leaves_the_worktree_dirty_until_committed(sandbox):
    sandbox.write("scratch.txt", "x\n")
    assert sandbox.git.is_clean() is False
    sandbox.commit("scratch", files={"scratch.txt": "x\n"})
    assert sandbox.git.is_clean() is True


def test_make_stack_builds_a_pushed_chain_and_leaves_head_on_top(sandbox):
    names = sandbox.make_stack(["feat-a", "feat-b", "feat-c"], commits=2)
    assert names == ["feat-a", "feat-b", "feat-c"]
    assert sandbox.git.current_branch() == "feat-c"
    sandbox.assert_stacked("feat-a", "feat-b", "feat-c")
    assert sandbox.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1", "feat-a: c2"],
        "feat-b": ["feat-b: c1", "feat-b: c2"],
        "feat-c": ["feat-c: c1", "feat-c: c2"],
    }
    for name in names:
        assert sandbox.origin_sha(name) == sandbox.sha(name)


def test_stacked_sandbox_fixture_is_the_common_case(stacked_sandbox):
    sb = stacked_sandbox
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    assert sb.tips().keys() >= {"main", "feat-a", "feat-b", "feat-c"}
    assert sb.remote_tips() == sb.tips()


def test_assert_stacked_fails_loudly_when_the_chain_is_broken(stacked_sandbox):
    sb = stacked_sandbox
    sb.amend("feat-a", subject="feat-a: c1 (amended)")
    with pytest.raises(AssertionError) as excinfo:
        sb.assert_stacked("feat-a", "feat-b", "feat-c")
    assert "feat-b" in str(excinfo.value)


def test_amend_rewrites_the_tip_and_strands_the_children(stacked_sandbox):
    # SPEC.md sec 2 step 1: amending the bottom branch is a restack trigger.
    sb = stacked_sandbox
    before = sb.sha("feat-a")
    after = sb.amend("feat-a", subject="feat-a: c1 (amended)")
    assert after != before
    assert sb.git.commit_subject("feat-a") == "feat-a: c1 (amended)"
    assert sb.git.is_ancestor("feat-a", "feat-b") is False
    # the fork point still derives, because origin/feat-a is the LAST SYNCED state
    assert sb.git.merge_base("origin/feat-a", "feat-b") == before
    assert sb.git.current_branch() == "feat-c"  # HEAD restored


def test_advance_trunk_moves_origin_ahead_of_the_local_trunk(stacked_sandbox):
    # CLAUDE.md invariant 4: sync rebases roots onto origin/<trunk>.
    sb = stacked_sandbox
    local_before = sb.sha(sb.trunk)
    sb.advance_trunk(2, subjects=["trunk one", "trunk two"])
    assert sb.sha(sb.trunk) == local_before          # local trunk is NOT moved
    assert sb.sha(f"origin/{sb.trunk}") != local_before
    assert sb.subjects(f"{sb.trunk}..origin/{sb.trunk}") == ["trunk one", "trunk two"]


def test_squash_merge_mints_a_new_commit_that_shares_no_history(stacked_sandbox):
    # SPEC.md sec 6.4 and the whole reason stackem exists.
    sb = stacked_sandbox
    squashed = sb.squash_merge("feat-a")
    assert squashed == sb.sha(f"origin/{sb.trunk}")
    assert sb.git.is_ancestor("feat-a", f"origin/{sb.trunk}") is False
    assert sb.git.tree_id(squashed) == sb.git.tree_id("feat-a")
    assert sb.git.rev_list_count(f"{sb.trunk}..origin/{sb.trunk}") == 1
    # the squash contains the work without containing the commits
    merged = sb.git.merge_tree_write_tree(f"origin/{sb.trunk}", "feat-a")
    assert merged.tree == sb.git.tree_id(f"origin/{sb.trunk}")


def test_squash_merge_can_delete_the_branch_on_origin(stacked_sandbox):
    sb = stacked_sandbox
    sb.squash_merge("feat-a", delete_branch=True)
    assert sb.origin_sha("feat-a") is None
    assert sb.sha("feat-a")  # the local branch is untouched


def test_delete_remote_branch_leaves_the_local_branch_alone(stacked_sandbox):
    sb = stacked_sandbox
    sb.delete_remote_branch("feat-c")
    assert sb.origin_sha("feat-c") is None
    assert sb.sha("feat-c")


def test_set_pull_ref_recreates_githubs_refs_pull_n_head(stacked_sandbox):
    # SPEC.md sec 6.3: refs/pull/N/head survives branch deletion.
    sb = stacked_sandbox
    tip = sb.sha("feat-c")
    sb.set_pull_ref(12, "feat-c")
    sb.delete_remote_branch("feat-c")
    assert sb.origin_git.rev_parse("refs/pull/12/head") == tip


def test_teammate_commit_pushes_behind_our_back(stacked_sandbox):
    # CLAUDE.md invariant 11 / SPEC.md sec 3 CASE 2.
    sb = stacked_sandbox
    mate = sb.teammate_commit("feat-a", subject="mate work")
    assert sb.origin_sha("feat-a") == mate
    assert sb.sha("origin/feat-a") != mate  # we have not fetched it
    sb.fetch()
    assert sb.sha("origin/feat-a") == mate


def test_unset_origin_head_reproduces_the_environment_case(sandbox):
    # SPEC.md sec 6.6: refs/remotes/origin/HEAD can be unset.
    sandbox.unset_origin_head()
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD") is None


# --------------------------------------------------------------------------
# inspection helpers
# --------------------------------------------------------------------------

def test_subjects_accepts_a_branch_or_a_range(stacked_sandbox):
    sb = stacked_sandbox
    assert sb.subjects("main..feat-b") == ["feat-a: c1", "feat-b: c1"]
    assert sb.subjects("feat-b")[0] == "root"
    assert sb.unique_subjects("feat-b", "feat-a") == ["feat-b: c1"]


def test_shape_reports_each_branchs_own_commits(stacked_sandbox):
    sb = stacked_sandbox
    sb.commit("feat-b: c2", branch="feat-b")
    assert sb.shape("feat-a", "feat-b") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1", "feat-b: c2"],
    }


def test_describe_renders_the_stack_for_failure_messages(stacked_sandbox):
    text = stacked_sandbox.describe("feat-a", "feat-b", "feat-c")
    assert "feat-b" in text and "feat-b: c1" in text


def test_patch_id_distinguishes_a_squash_from_the_commits_it_replaced(sandbox):
    # SPEC.md sec 6.4: squashing changes the patch-id, so git cherry cannot see it.
    sandbox.make_stack(["feat-a"], commits=2)
    originals = {sandbox.patch_id(sha) for sha in sandbox.git.rev_list("main..feat-a")}
    squashed = sandbox.squash_merge("feat-a")
    assert sandbox.patch_id(squashed) not in originals
