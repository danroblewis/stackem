"""End to end: the real CLI, real git repositories, a mock GitHub over HTTP.

Everything below drives ``stackem.cli.main`` -- argument parsing, the engine in
``stackem.sync``, orientation, the cascade, the push, and every byte of the
report.  The repositories are real: a bare repo acting as origin plus a clone,
built by tests/harness.py, with real commits, real rebases and a real
``git push``.  The only thing faked is GitHub, and it is faked at the wire: the
production :class:`~stackem.forge.github.GitHubProvider` speaks real HTTP to the
in-process mock server (SPEC.md sec 10).

The engine is built through ``stackem.sync.build_engine`` from the CLI's own
``engine_factory`` seam, with an ``env`` that points the provider at the mock
(``STACKEM_GITHUB_API_URL`` / ``STACKEM_GITHUB_REPO``) and has an empty ``PATH``
so the provider takes its ``urllib`` transport rather than looking for ``gh``.
That env reaches the provider only -- git runs with the process environment, as
it does in production.

What each test is for
---------------------
The task list in SPEC.md sec 10, driven through the command line instead of the
module APIs: a healthy stack is a no-op, an amend cascades, a moved trunk
cascades, a squash merge skips and retargets, two merges hoist transitively, an
emptied branch leaves the chain and *stays* left (the flip-flop regression),
a conflict stops and resumes, and a rebase stackem did not start is refused.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytest

from stackem.cli import EXIT_INCOMPLETE, EXIT_OK, main
from stackem.forge import ForgeError
from stackem.gitx import Git

# ==========================================================================
# driving the command line
# ==========================================================================


@dataclass
class Run:
    """One ``stackem`` invocation: its exit code, its output, its git."""

    code: int
    out: str
    err: str
    git: Git | None = None

    @property
    def text(self) -> str:
        return self.out + self.err

    @property
    def pushes(self) -> list[tuple[str, ...]]:
        """Every ``git push`` this run made.

        The harness pushes too, and "nothing was pushed" has to mean *nothing*,
        so this reads the invocations of the git object the CLI itself built.
        """
        if self.git is None:
            return []
        return [inv.argv for inv in self.git.trace if inv.argv[1:2] == ("push",)]

    @property
    def pushed_branches(self) -> list[str]:
        """The branch names of the one push this run made."""
        assert len(self.pushes) == 1, self.pushes
        return [
            arg
            for arg in self.pushes[0][2:]
            if not arg.startswith("-") and arg != "origin"
        ]

    def assert_ends_with(self, line: str) -> None:
        # CLAUDE.md invariant 23: every output ends with the next command.
        assert self.text.rstrip().splitlines()[-1] == line, self.text


class World:
    """A sandbox, a mock GitHub bound to its origin, and a way to run stackem."""

    def __init__(self, sandbox, server) -> None:
        self.sb = sandbox
        self.server = server
        self.api = server.api

    @property
    def env(self) -> dict[str, str]:
        return {
            # An empty PATH is what makes the provider choose urllib over `gh`;
            # it never reaches git, which the CLI runs with its own environment.
            "PATH": "",
            "GITHUB_TOKEN": "e2e-token",
            "STACKEM_GITHUB_REPO": self.api.repo_full_name,
            "STACKEM_GITHUB_API_URL": self.server.url,
        }

    def run(self, *argv: str, provider=None) -> Run:
        from stackem.sync import build_engine

        out, err = io.StringIO(), io.StringIO()
        seen: list[Git] = []

        def factory(context):
            seen.append(context.git)
            engine = build_engine(git=context.git, env=self.env)
            if provider is not None:
                engine = build_engine(
                    git=context.git, env=self.env, provider=provider(engine.provider)
                )
            return engine

        code = main(
            list(argv), cwd=str(self.sb.path), out=out, err=err, engine_factory=factory
        )
        return Run(code, out.getvalue(), err.getvalue(), seen[0] if seen else None)

    # -- setting the forge up ---------------------------------------------

    def open_prs(self, chain: list[str], *, base: str | None = None) -> list[int]:
        """One pull request per branch, each based on the branch below it."""
        below = base or self.sb.trunk
        numbers = []
        for index, name in enumerate(chain):
            pr = self.api.open_pull_request(name, below, number=101 + index)
            numbers.append(pr.number)
            below = name
        return numbers

    def bases(self) -> dict[int, str]:
        return {pr.number: pr.base for pr in self.api.pull_requests}

    def retargets(self) -> list[tuple[str, str]]:
        """Every base change that reached the forge, as (path, new base)."""
        return [
            (r.path, r.body.get("base", ""))
            for r in self.server.requests
            if r.method == "PATCH"
        ]


@pytest.fixture
def world(sandbox, mock_github) -> World:
    return World(sandbox, mock_github)


@pytest.fixture
def stack(world) -> World:
    """main <- feat-a <- feat-b <- feat-c, pushed, with #101 #102 #103 open."""
    world.sb.make_stack(["feat-a", "feat-b", "feat-c"])
    world.open_prs(["feat-a", "feat-b", "feat-c"])
    return world


# ==========================================================================
# the display command
# ==========================================================================


def test_stackem_shows_the_stack_and_writes_nothing(stack):
    """SPEC.md sec 9, CLAUDE.md invariant 5: read-only, and it says so."""
    sb = stack.sb
    before_refs = dict(sb.git.branches("refs/remotes/origin"))
    before_tips = sb.tips()

    run = stack.run()

    assert run.code == EXIT_OK, run.text
    assert "feat-a" in run.out and "#101" in run.out
    assert "feat-b" in run.out and "#102" in run.out
    assert "feat-c" in run.out and "#103" in run.out
    assert "synced" in run.out
    run.assert_ends_with("next: nothing — the stack is current.")
    assert sb.git.branches("refs/remotes/origin") == before_refs
    assert sb.tips() == before_tips
    assert run.pushes == []


def test_stackem_reports_a_branch_with_no_pull_request(stack):
    """SPEC.md sec 6.5 / invariant 20: print the command, never create the PR."""
    sb = stack.sb
    sb.create_branch("feat-d")
    sb.commit("feat-d: c1")
    sb.push("feat-d")

    run = stack.run()

    assert run.code == EXIT_OK, run.text
    assert "gh pr create --base feat-c --head feat-d" in run.out
    run.assert_ends_with("next: gh pr create --base feat-c --head feat-d")
    assert len(stack.api.pull_requests) == 3, "stackem created a pull request"


def test_a_branch_rooted_on_the_trunk_is_told_to_base_its_pull_request_on_it(stack):
    """The base of a new pull request is a branch name, never origin/<trunk>.

    Also CLAUDE.md invariant 8: HEAD is on a branch of its own, so the feat-*
    stack -- the trunk's other children -- is not collected.
    """
    sb = stack.sb
    sb.checkout(sb.trunk)
    sb.create_branch("solo")
    sb.commit("solo: c1")
    sb.push("solo")

    run = stack.run()

    assert run.code == EXIT_OK, run.text
    assert "gh pr create --base main --head solo" in run.out
    assert "feat-a" not in run.out, "a sibling stack was collected"
    run.assert_ends_with("next: gh pr create --base main --head solo")


# ==========================================================================
# the no-op and the ordinary cascade
# ==========================================================================


def test_a_healthy_three_branch_stack_syncs_to_a_no_op(stack):
    """CLAUDE.md invariant 21: sync on a current stack changes nothing."""
    sb = stack.sb
    before_tips = sb.tips()
    before_remote = sb.remote_tips()

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    assert sb.tips() == before_tips
    assert sb.remote_tips() == before_remote
    assert run.pushes == [], "a current stack was pushed anyway"
    assert stack.retargets() == [], "a current stack was retargeted anyway"
    assert "done. 0 branches restacked." in run.out
    run.assert_ends_with("next: nothing — the stack is current.")


def test_amending_the_bottom_branch_restacks_and_pushes_atomically(stack):
    """SPEC.md sec 2 STEP 1, and invariants 11 and 12 at the wire."""
    sb = stack.sb
    sb.amend("feat-a", subject="feat-a: c1 (amended)")

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    sb.assert_stacked("feat-a", "feat-b", "feat-c")
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1 (amended)"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }, sb.describe("feat-a", "feat-b", "feat-c")

    # Invariant 12: one atomic push, not three sequential ones.
    assert len(run.pushes) == 1, run.pushes
    argv = run.pushes[0]
    assert "--atomic" in argv
    assert "--force-with-lease" in argv and "--force-if-includes" in argv
    assert "--force" not in argv, "invariant 11: never bare --force"
    assert argv[-3:] == ("feat-a", "feat-b", "feat-c")

    sb.fetch()
    for name in ("feat-a", "feat-b", "feat-c"):
        assert sb.origin_sha(name) == sb.sha(name), f"{name} was not pushed"
    assert "restacking feat-b onto feat-a... ok (1 commit)" in run.out
    assert "pushing feat-a feat-b feat-c... ok (atomic)" in run.out
    run.assert_ends_with("next: nothing — the stack is current.")


def test_a_moved_trunk_rebases_the_whole_stack_onto_origin_trunk(stack):
    """SPEC.md sec 2 STEP 3 and CLAUDE.md invariant 4."""
    sb = stack.sb
    local_trunk_before = sb.sha(sb.trunk)
    sb.advance_trunk(2)

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    assert sb.git.is_ancestor(f"origin/{sb.trunk}", "feat-a"), sb.describe(
        "feat-a", "feat-b", "feat-c"
    )
    sb.assert_stacked("feat-a", "feat-b", "feat-c", base=f"origin/{sb.trunk}")
    assert sb.shape("feat-a", "feat-b", "feat-c", base=f"origin/{sb.trunk}") == {
        "feat-a": ["feat-a: c1"],
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }
    # Invariant 4: sync rebases onto origin/<trunk> and never moves local trunk.
    assert sb.sha(sb.trunk) == local_trunk_before
    assert "trunk origin/main moved: 2 new commits" in run.out
    assert "restacking feat-a onto origin/main... ok (1 commit)" in run.out
    assert len(run.pushes) == 1, run.pushes
    run.assert_ends_with("next: nothing — the stack is current.")


# ==========================================================================
# squash merges
# ==========================================================================


def test_a_squash_merged_bottom_pull_request_is_skipped_and_its_child_retargeted(stack):
    """SPEC.md sec 5.2 step 4 and step 6; invariants 6, 7 and 15."""
    sb, api = stack.sb, stack.api
    feat_a_tip = sb.sha("feat-a")
    api.squash_merge(101)

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    # Invariant 6: the merged branch was not replayed, and not pushed.
    assert sb.sha("feat-a") == feat_a_tip
    assert "restacking feat-a" not in run.out
    assert run.pushed_branches == ["feat-b", "feat-c"], run.pushes

    # Invariant 15: #102's base moved to the trunk before anything else.
    assert stack.bases()[102] == sb.trunk
    assert stack.retargets() == [("/repos/acme/app/pulls/102", sb.trunk)]

    # The history is linear: feat-b owns only its own commit, on top of the
    # squash commit -- feat-a's originals were not replayed onto it.
    sb.fetch()
    sb.assert_stacked("feat-b", "feat-c", base=f"origin/{sb.trunk}")
    assert sb.shape("feat-b", "feat-c", base=f"origin/{sb.trunk}") == {
        "feat-b": ["feat-b: c1"],
        "feat-c": ["feat-c: c1"],
    }, sb.describe("feat-b", "feat-c", base=f"origin/{sb.trunk}")

    assert "feat-a: merged" in run.out
    assert "feat-b: parent feat-a -> main" in run.out
    assert "git push origin --delete feat-a && git branch -D feat-a" in run.out
    # Invariant 14: printed, not run.
    assert api.branch_exists("feat-a")
    run.assert_ends_with("next: nothing — the stack is current.")


def test_two_pull_requests_merged_in_one_run_hoist_transitively(stack):
    """CLAUDE.md invariant 7: hoist to the nearest UNMERGED ancestor."""
    sb, api = stack.sb, stack.api
    api.squash_merge(101)
    # GitHub leaves #102 pointing at the merged branch; it is merged into the
    # trunk next, which is the "two PRs land the same morning" case.
    api.retarget(102, sb.trunk)
    api.squash_merge(102)

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    # feat-c must land on the trunk, not on either doomed branch.
    assert stack.bases()[103] == sb.trunk
    sb.fetch()
    sb.assert_stacked("feat-c", base=f"origin/{sb.trunk}")
    assert sb.shape("feat-c", base=f"origin/{sb.trunk}") == {"feat-c": ["feat-c: c1"]}
    assert "feat-c: parent feat-b -> main" in run.out
    assert run.pushed_branches == ["feat-c"]
    run.assert_ends_with("next: nothing — the stack is current.")


# ==========================================================================
# the emptied branch, and the flip-flop it used to cause
# ==========================================================================


@pytest.fixture
def emptied(world) -> World:
    """feat-b's only commit is made redundant by a later fix on feat-a.

    SPEC.md sec 7.2: replaying feat-b produces nothing, so the branch empties
    and leaves the chain.
    """
    sb = world.sb
    sb.make_stack(["feat-a"])
    sb.create_branch("feat-b")
    sb.commit("fix: expire sessions on logout", files={"expiry.txt": "expire\n"})
    sb.create_branch("feat-c")
    sb.commit("feat-c: c1")
    sb.push("feat-b", "feat-c")
    world.open_prs(["feat-a", "feat-b", "feat-c"])
    # The same fix, moved down to where it belongs.  Deliberately NOT pushed:
    # sync is what pushes a parent, and a parent pushed by hand ahead of its
    # children is the fork-point guard's business (invariant 3b), not this
    # test's.
    sb.commit(
        "fix: expire sessions on logout",
        files={"expiry.txt": "expire\n"},
        branch="feat-a",
    )
    return world


def test_an_emptied_branch_leaves_the_chain_and_is_never_closed(emptied):
    """SPEC.md sec 7.2 and CLAUDE.md invariants 14 and 15."""
    sb, api = emptied.sb, emptied.api
    feat_b_on_origin = sb.origin_sha("feat-b")

    run = emptied.run("sync")

    assert run.code == EXIT_OK, run.text
    assert "restacking feat-b onto feat-a... EMPTY" in run.out
    assert "removing feat-b from the chain:" in run.out
    assert "feat-c: parent feat-b -> feat-a" in run.out

    # The child was retargeted (invariant 15) and the branch was left alone
    # (invariant 14): no close, no delete, just the commands.
    assert emptied.bases()[103] == "feat-a"
    assert api.pull_request(102).state == "open"
    assert api.branch_exists("feat-b")
    assert sb.origin_sha("feat-b") == feat_b_on_origin, "the emptied branch was pushed"
    assert 'gh pr close 102 -c "emptied' in run.out
    assert "git push origin --delete feat-b && git branch -D feat-b" in run.out

    # feat-c is stitched past it: its own commit, directly on feat-a.
    assert sb.shape("feat-a", "feat-c") == {
        "feat-a": ["feat-a: c1", "fix: expire sessions on logout"],
        "feat-c": ["feat-c: c1"],
    }, sb.describe("feat-a", "feat-b", "feat-c")
    run.assert_ends_with("next: nothing — the stack is current.")


def test_rerunning_sync_after_a_branch_emptied_is_a_clean_no_op(emptied):
    """The flip-flop regression (SPEC.md sec 5.1, invariant 21).

    An earlier design closed the emptied branch's pull request and deleted the
    branch; the next run classified that as an orphan and restored it, forever.
    Nothing may change on the second run.
    """
    first = emptied.run("sync")
    assert first.code == EXIT_OK, first.text
    sb = emptied.sb
    tips, remote_tips = sb.tips(), sb.remote_tips()
    bases = emptied.bases()
    requests_before = len(emptied.server.requests)

    second = emptied.run("sync")

    assert second.code == EXIT_OK, second.text
    assert sb.tips() == tips, "the second sync moved a branch"
    assert sb.remote_tips() == remote_tips, "the second sync pushed"
    assert second.pushes == []
    assert emptied.bases() == bases
    assert [
        r for r in emptied.server.requests[requests_before:] if r.method != "GET"
    ] == [], "the second sync wrote to the forge"
    assert "restacking" not in second.out
    assert "done. 0 branches restacked." in second.out
    # It keeps reminding, and still refuses to act (invariant 14).
    assert "gh pr close 102" in second.out
    assert emptied.api.pull_request(102).state == "open"
    second.assert_ends_with("next: nothing — the stack is current.")


def test_a_pull_request_closed_by_a_branch_deletion_is_reported_not_reopened(stack):
    """CLAUDE.md invariants 13 and 14, and the gap between them.

    Deleting feat-a closes #102, whose head branch is still there -- so SPEC.md
    sec 5.1 does not call feat-b orphaned, and without a word from sync the run
    would restack and push into a pull request nobody can merge.  stackem says
    so and still refuses to reopen it.
    """
    sb, api = stack.sb, stack.api
    api.squash_merge(101)
    api.delete_branch("feat-a")  # "Delete branch" on the merge, invariant 13
    assert api.pull_request(102).state == "closed"
    sb.fetch()

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    assert "#102 (feat-b) is closed and not merged" in run.out
    assert "state=open" in run.out, "the rescue was not even mentioned"
    # Invariant 14: mentioned, never performed.
    assert api.pull_request(102).state == "closed"
    assert [
        r for r in stack.server.requests if r.method == "PATCH" and "state" in r.body
    ] == []


# ==========================================================================
# conflicts
# ==========================================================================


@pytest.fixture
def conflicting(world) -> World:
    """feat-b edits the same file feat-a is about to amend."""
    sb = world.sb
    sb.make_stack(["feat-a"], push=False)
    sb.commit("feat-a: shared file", files={"shared.txt": "a\n"})
    sb.create_branch("feat-b")
    sb.commit("feat-b: edit the shared file", files={"shared.txt": "b\n"})
    sb.create_branch("feat-c")
    sb.commit("feat-c: c1")
    sb.push("feat-a", "feat-b", "feat-c")
    world.open_prs(["feat-a", "feat-b", "feat-c"])
    sb.commit("feat-a: shared file, revised", files={"shared.txt": "a2\n"}, branch="feat-a")
    return world


def test_a_conflict_stops_the_cascade_and_pushes_nothing(conflicting):
    """SPEC.md sec 8: stop in git's ordinary rebase state, say what to do."""
    sb = conflicting.sb
    remote_before = sb.remote_tips()

    run = conflicting.run("sync")

    assert run.code == EXIT_INCOMPLETE, run.text
    assert "CONFLICT in feat-b" in run.out
    assert "feat-b: edit the shared file" in run.out
    assert "shared.txt" in run.out
    assert "still queued after this: feat-c" in run.out
    assert "To back out instead: git rebase --abort" in run.out
    run.assert_ends_with(
        "next: resolve the conflicts and git add them, then: stackem sync"
    )

    # Invariant 9: nothing local is verified, so nothing reaches the remote.
    assert run.pushes == []
    assert sb.remote_tips() == remote_before
    assert conflicting.retargets() == []
    assert sb.git.rebase_in_progress(), "git's rebase state was cleaned up"


def test_a_resolved_conflict_resumes_and_finishes_the_cascade(conflicting):
    """SPEC.md sec 8 and invariant 21: one verb, re-entrant."""
    sb = conflicting.sb
    first = conflicting.run("sync")
    assert first.code == EXIT_INCOMPLETE, first.text

    sb.write("shared.txt", "resolved\n")
    sb.git.run("add", "shared.txt")

    second = conflicting.run("sync")

    assert second.code == EXIT_OK, second.text
    assert "continuing rebase of feat-b... ok" in second.out
    assert "restacking feat-c onto feat-b... ok (1 commit)" in second.out
    assert not sb.git.rebase_in_progress()
    sb.assert_stacked("feat-a", "feat-b", "feat-c")
    assert sb.shape("feat-a", "feat-b", "feat-c") == {
        "feat-a": ["feat-a: c1", "feat-a: shared file", "feat-a: shared file, revised"],
        "feat-b": ["feat-b: edit the shared file"],
        "feat-c": ["feat-c: c1"],
    }, sb.describe("feat-a", "feat-b", "feat-c")
    assert (sb.path / "shared.txt").read_text() == "resolved\n"

    assert len(second.pushes) == 1, second.pushes
    sb.fetch()
    for name in ("feat-a", "feat-b", "feat-c"):
        assert sb.origin_sha(name) == sb.sha(name), f"{name} was not pushed"
    second.assert_ends_with("next: nothing — the stack is current.")


def test_sync_refuses_to_continue_a_rebase_it_did_not_start(stack):
    """CLAUDE.md invariant 22, the reason there is no `stackem continue`."""
    sb = stack.sb
    sb.teammate_commit(sb.trunk, subject="trunk: edits c1", files={"feat-a-1.txt": "trunk\n"})
    sb.fetch()
    tips_before = sb.tips()
    # The user's own rebase: feat-c onto the trunk, skipping its parent.
    result = sb.git.run("rebase", f"origin/{sb.trunk}", "feat-c", check=False)
    assert result.returncode != 0, "the fixture must actually conflict"
    assert sb.git.rebase_in_progress()

    run = stack.run("sync")

    assert run.code == EXIT_INCOMPLETE, run.text
    assert "a rebase is already in progress and stackem did not start it" in run.out
    assert "feat-c" in run.out
    assert run.pushes == []
    assert stack.retargets() == []
    assert sb.tips() == tips_before, "sync rewrote a branch anyway"
    assert sb.git.rebase_in_progress(), "sync continued or aborted the user's rebase"
    run.assert_ends_with("next: finish your own rebase, then: stackem sync")


def test_a_deleted_branch_orphans_its_pull_request_and_blocks_the_branches_above(stack):
    """SPEC.md sec 5.1 and sec 6.3; CLAUDE.md invariants 13 and 14.

    Deleting feat-b closes #102 (its head) and #103 (its base).  #102 cannot be
    reopened until the branch is back, so sync prints the rescue instead of
    performing it -- and leaves the branches above it alone rather than
    restacking them onto wreckage.
    """
    sb, api = stack.sb, stack.api
    api.delete_branch("feat-b")
    sb.fetch()
    tips = sb.tips()

    run = stack.run("sync")

    assert run.code == EXIT_OK, run.text
    assert "PR #102 (feat-b) is closed and its head branch is gone from origin." in run.out
    assert "git fetch origin refs/pull/102/head:rescue-head" in run.out
    assert "git push origin rescue-head:refs/heads/feat-b" in run.out
    assert "gh api -X PATCH /repos/acme/app/pulls/102 -f state=open" in run.out
    assert "gh pr edit 102 --base feat-a" in run.out
    assert "feat-c: parent chain reaches a closed PR — skipped this run." in run.out

    # Invariant 14: reported, never performed.
    assert api.pull_request(102).state == "closed"
    assert api.branch_exists("feat-b") is False
    assert run.pushes == []
    assert stack.retargets() == []
    assert sb.tips() == tips
    run.assert_ends_with("next: run the commands above, then: stackem sync")


class RefusingForge:
    """A forge that answers every question and refuses to move a base."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def retarget(self, pr_number: int, new_base: str) -> None:
        raise ForgeError(
            f"422 Validation Failed: base branch '{new_base}' is protected"
        )


def test_a_failed_retarget_stops_the_run_before_the_push(stack):
    """SPEC.md sec 5.2 step 8, and invariants 13 and 15.

    A pull request still based on a branch that is about to be deleted must not
    have the content pushed under it, and the report must not print the command
    that deletes that branch.
    """
    sb, api = stack.sb, stack.api
    api.squash_merge(101)

    run = stack.run("sync", provider=RefusingForge)

    assert run.code == EXIT_INCOMPLETE, run.text
    assert "FAILED" in run.out
    assert "is protected" in run.out
    assert run.pushes == [], "content was pushed under a mis-based pull request"
    assert stack.bases()[102] == "feat-a", "the base moved after all"
    # Invariant 15: the delete command is withheld while #102 still points at it.
    assert "--delete feat-a" not in run.out
    # Invariant 24: the local work stands; rerunning sync is the recovery.
    assert sb.git.is_ancestor(f"origin/{sb.trunk}", "feat-b")
    run.assert_ends_with("next: fix the error above, then: stackem sync")


# ==========================================================================
# stackem parent
# ==========================================================================


def test_stackem_parent_retargets_the_pull_request(stack):
    """CLAUDE.md invariant 1: the base IS the parent record."""
    run = stack.run("parent", "feat-c", "--onto", "feat-a")

    assert run.code == EXIT_OK, run.text
    assert "retargeting PR #103 base feat-b -> feat-a... ok" in run.out
    assert stack.bases()[103] == "feat-a"
    run.assert_ends_with("next: stackem sync")


def test_stackem_parent_is_idempotent(stack):
    run = stack.run("parent", "feat-c", "--onto", "feat-b")

    assert run.code == EXIT_OK, run.text
    assert "already targets feat-b" in run.out
    assert stack.retargets() == [], "an unnecessary retarget reached the forge"


def test_stackem_parent_on_a_branch_with_no_pull_request_prints_the_create(stack):
    sb = stack.sb
    sb.create_branch("feat-d")
    sb.commit("feat-d: c1")
    sb.push("feat-d")

    run = stack.run("parent", "feat-d", "--onto", "feat-c")

    assert run.code == EXIT_OK, run.text
    assert "gh pr create --base feat-c --head feat-d" in run.out
    assert len(stack.api.pull_requests) == 3


# ==========================================================================
# --dry-run changes nothing
# ==========================================================================


def test_a_dry_run_prints_the_plan_and_changes_nothing(stack):
    sb = stack.sb
    sb.amend("feat-a", subject="feat-a: c1 (amended)")
    tips, remote_tips = sb.tips(), sb.remote_tips()

    run = stack.run("sync", "--dry-run")

    assert run.code == EXIT_OK, run.text
    assert "dry run — nothing will be changed" in run.out
    assert "would restack feat-b onto feat-a" in run.out
    assert "would push" in run.out
    for word in ("restacking", "pushing", "retargeting", "done."):
        assert word not in run.out, f"a dry run reported {word!r} as done"
    assert sb.tips() == tips
    assert sb.remote_tips() == remote_tips
    assert run.pushes == []
    assert stack.retargets() == []
