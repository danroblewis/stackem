"""The remote phase: SPEC.md sec 5.2 PHASE 2 (steps 8-10) and sec 6.2.

Every git repository here is real -- a bare repo acting as ``origin``, a clone to
work in, and (where a lease has to be provoked) a second clone acting as a
teammate.  Only the forge is faked.

What these tests are actually about:

* **Invariant 9** -- phase 2 is unreachable with unverified local work.  The
  interface refuses it, and ``run_remote_phase`` re-checks the repository itself
  rather than believing a flag it was handed.
* **Invariant 11** -- exactly ``--atomic --force-with-lease --force-if-includes``,
  proven both ways: a legitimate post-rebase push lands, and a teammate's commit
  survives whether or not we fetched it first.
* **Invariant 12** -- atomic across every changed branch: all of them land, or
  none does.
* **Invariant 15** -- retargets happen before anything could be deleted, children
  of merged and emptied branches first.
* **Invariant 14** -- stackem never closes, deletes or reopens.  It prints the
  commands.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request

import pytest

from stackem.forge import ForgeError
from stackem.gitx import RangeDiffEntry
from stackem.model import BranchState, PullRequest, PullRequestState, RepoSettings
from stackem.push import (
    PUSH_FLAGS,
    BranchChange,
    FollowUpKind,
    LocalWorkNotVerified,
    PushOutcome,
    Retarget,
    RetargetReason,
    Verification,
    VerifiedLocalWork,
    create_pull_request_command,
    rescue_commands,
    run_remote_phase,
)

ZERO = "0" * 40
ONE = "1" * 40


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def entry(status: str, subject: str = "a commit", index: int = 1) -> RangeDiffEntry:
    """One ``git range-diff`` line, the shape gitx parses it into."""
    old_index = None if status == ">" else index
    new_index = None if status == "<" else index
    return RangeDiffEntry(
        old_index=old_index,
        old_sha=None if old_index is None else "aaaaaaa",
        status=status,
        new_index=new_index,
        new_sha=None if new_index is None else "bbbbbbb",
        subject=subject,
    )


def verification(branch: str, *statuses: str) -> Verification:
    return Verification(
        branch=branch,
        old_range=f"old..{branch}",
        new_range=f"new..{branch}",
        entries=tuple(entry(s, f"{branch}: c{i + 1}", i + 1) for i, s in enumerate(statuses)),
    )


def change(branch: str, **kwargs) -> BranchChange:
    """A BranchChange with harmless defaults: unchanged, already pushed."""
    before = kwargs.pop("before", ZERO)
    after = kwargs.pop("after", before)
    kwargs.setdefault("remote", after)
    kwargs.setdefault("parent", "main")
    if before != after and "verification" not in kwargs:
        kwargs["verification"] = verification(branch, "=")
    return BranchChange(branch=branch, before=before, after=after, **kwargs)


def restack(sb, branch: str, *, onto: str, fork: str) -> BranchChange:
    """Really rebase ``branch``, really range-diff it, and describe the change.

    This is what phase 1 hands phase 2 -- built from git's own output, never
    from a boolean a caller made up.
    """
    before = sb.sha(branch)
    result = sb.git.rebase_onto(onto, fork, branch)
    assert result.ok, result.stderr
    after = sb.sha(branch)
    old_range, new_range = f"{fork}..{before}", f"{onto}..{after}"
    entries = tuple(sb.git.range_diff(old_range, new_range))
    return BranchChange(
        branch=branch,
        before=before,
        after=after,
        remote=sb.git.try_rev_parse(f"refs/remotes/origin/{branch}"),
        parent=sb.trunk,
        verification=Verification(
            branch=branch, old_range=old_range, new_range=new_range, entries=entries
        ),
    )


def restack_onto_trunk(sb, branch: str) -> BranchChange:
    onto = sb.sha(f"origin/{sb.trunk}")
    fork = sb.git.merge_base(f"origin/{sb.trunk}", branch)
    return restack(sb, branch, onto=onto, fork=fork)


def pull_request(number: int, head: str, base: str, state=PullRequestState.OPEN) -> PullRequest:
    return PullRequest(number=number, head=head, base=base, state=state)


def mark(git) -> int:
    """How much git this test has run so far, so a push can be attributed."""
    return len(git.trace)


def push_invocations(git, since: int = 0) -> list[tuple[str, ...]]:
    """Every ``git push`` the module ran after ``since`` -- the harness pushes
    too, and "stackem pushed nothing" has to mean *nothing*, not "nothing with
    the flags I was looking for"."""
    return [inv.argv for inv in git.trace[since:] if inv.argv[1:2] == ("push",)]


class RecordingProvider:
    """A forge that records retargets.  It has no close/delete/reopen, because
    the Provider protocol deliberately has none (invariant 14)."""

    def __init__(self, *, slug: str = "acme/app", fail: set[int] | None = None) -> None:
        self.slug = slug
        self.fail = fail or set()
        self.retargets: list[tuple[int, str]] = []

    def default_branch(self) -> str:
        return "main"

    def repo_settings(self) -> RepoSettings:
        return RepoSettings(default_branch="main")

    def list_open_pull_requests(self) -> list[PullRequest]:
        return []

    def get_pull_request_for_branch(self, branch: str) -> PullRequest | None:
        return None

    def retarget(self, pr_number: int, new_base: str) -> None:
        if pr_number in self.fail:
            raise ForgeError(f"422 base branch '{new_base}' does not exist")
        self.retargets.append((pr_number, new_base))

    def branch_exists_on_remote(self, branch: str) -> bool:
        return True

    def repo_slug(self) -> str:
        return self.slug


class HttpProvider(RecordingProvider):
    """The thinnest possible real client, so one test drives the remote phase
    over actual HTTP against the mock GitHub instead of a Python stub.

    (The shipped GitHub provider belongs to another agent; this exists only to
    prove the retarget really reaches a forge.)"""

    def __init__(self, base_url: str, slug: str = "acme/app") -> None:
        super().__init__(slug=slug)
        self.base_url = base_url.rstrip("/")

    def retarget(self, pr_number: int, new_base: str) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/repos/{self.slug}/pulls/{pr_number}",
            data=json.dumps({"base": new_base}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as error:  # pragma: no cover - failure path
            raise ForgeError(error.read().decode()) from error
        self.retargets.append((pr_number, new_base))


# ---------------------------------------------------------------------------
# invariant 9: unverified local work cannot reach the remote
# ---------------------------------------------------------------------------


def test_a_restacked_branch_whose_patches_changed_cannot_enter_the_remote_phase():
    """SPEC.md sec 6.1: anything beyond "=" or a clean drop stops sync."""
    with pytest.raises(LocalWorkNotVerified) as caught:
        VerifiedLocalWork(
            changes=(change("feat-a", before=ZERO, after=ONE, verification=verification("feat-a", "!")),),
            local_complete=True,
        )
    assert "feat-a" in str(caught.value)


def test_a_restacked_branch_with_no_verification_at_all_is_refused():
    with pytest.raises(LocalWorkNotVerified):
        VerifiedLocalWork(
            changes=(BranchChange(branch="feat-a", before=ZERO, after=ONE, remote=ZERO),),
            local_complete=True,
        )


def test_a_cascade_that_stopped_partway_is_refused():
    """A conflict leaves local work incomplete; phase 2 must not run at all."""
    with pytest.raises(LocalWorkNotVerified):
        VerifiedLocalWork(changes=(change("feat-a"),), local_complete=False)


def test_a_clean_drop_is_verified_work():
    """SPEC.md sec 7.2: a dropped commit is expected, not a failure."""
    work = VerifiedLocalWork(
        changes=(
            change("feat-a", before=ZERO, after=ONE, verification=verification("feat-a", "=", "<")),
        ),
        local_complete=True,
    )
    dropped = work.changes[0].verification.dropped
    assert [e.subject for e in dropped] == ["feat-a: c2"]


def test_verification_cannot_be_handed_a_verdict():
    """``ok`` is computed from git's own range-diff signs, never passed in."""
    with pytest.raises(TypeError):
        Verification(branch="feat-a", old_range="a..b", new_range="c..d", entries=(), ok=True)
    assert verification("feat-a", "=", "<").ok is True
    assert verification("feat-a", "!").ok is False
    assert verification("feat-a", ">").ok is False


def test_the_remote_phase_refuses_while_a_rebase_is_in_progress(conflicted):
    """Invariant 9, checked against the repository rather than a flag."""
    sb = conflicted.sb
    work = VerifiedLocalWork(changes=(change("feat"),), local_complete=True)
    started = mark(sb.git)
    with pytest.raises(LocalWorkNotVerified) as caught:
        run_remote_phase(sb.git, None, work)
    assert "rebase" in str(caught.value).lower()
    assert push_invocations(sb.git, started) == []


def test_the_remote_phase_refuses_when_a_branch_moved_since_it_was_verified(sandbox):
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    sb.commit("a late commit nobody verified", branch="feat-a")
    started = mark(sb.git)
    with pytest.raises(LocalWorkNotVerified) as caught:
        run_remote_phase(sb.git, None, work)
    assert "feat-a" in str(caught.value)
    assert push_invocations(sb.git, started) == []


def test_a_resolved_conflict_may_change_the_patch_and_still_reach_the_remote():
    """SPEC.md sec 8, which is impossible without this carve-out.

    Resolving a conflict by hand necessarily changes the patch, so its
    range-diff comes back "!" (or "<" then ">").  Read strictly, invariant 9
    would then block phase 2 forever and the resolution could never be pushed --
    "resolve, `git add`, run stackem sync again" would be a lie.  The exemption
    is per branch and is set from the branch's own rebase having been resumed;
    stackem.restack.verify applies exactly the same rule in phase 1.
    """
    changed = dataclasses.replace(verification("feat-a", "!", ">"), resolved_conflict=True)
    assert changed.ok is True
    assert changed.unexpected == ()

    # The same range-diff on a branch nobody resolved still stops the run.
    with pytest.raises(LocalWorkNotVerified):
        VerifiedLocalWork(
            changes=(
                change("feat-a", before=ZERO, after=ONE, verification=verification("feat-a", "!")),
            ),
            local_complete=True,
        )

    work = VerifiedLocalWork(
        changes=(
            change("feat-a", before=ZERO, after=ONE, remote=ZERO, verification=changed),
        ),
        local_complete=True,
    )
    assert [c.branch for c in work.pushable] == ["feat-a"]


def test_an_emptied_branch_is_never_pushed(sandbox):
    """SPEC.md sec 7.2 and docs/sessions/06.

    An emptied branch is leaving the chain, and the report hands the user the
    commands to close its pull request and delete it (invariant 14).  Pushing it
    first would replace the remote branch with zero commits -- destroying, on
    the remote, the very work the report calls recoverable.
    """
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    before = sb.sha("feat-b")
    # What an empty replay leaves behind: the branch sitting on its parent's tip.
    sb.checkout("feat-a")
    sb.git.run("update-ref", "refs/heads/feat-b", sb.sha("feat-a"), before)
    emptied = BranchChange(
        branch="feat-b",
        before=before,
        after=sb.sha("feat-a"),
        remote=sb.origin_sha("feat-b"),
        parent="feat-a",
        emptied=True,
        verification=verification("feat-b", "<"),
    )
    assert emptied.restacked is True
    assert emptied.needs_push is False

    work = VerifiedLocalWork(changes=(emptied,), local_complete=True)
    started = mark(sb.git)
    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.NOTHING_TO_PUSH
    assert push_invocations(sb.git, started) == []
    assert sb.origin_sha("feat-b") != sb.sha("feat-a")
    assert [f.kind for f in result.follow_ups] == [FollowUpKind.EMPTIED]


def test_the_remote_phase_refuses_a_dirty_worktree(sandbox):
    sb = sandbox
    sb.make_stack(["feat-a"])
    work = VerifiedLocalWork(
        changes=(change("feat-a", before=sb.sha("feat-a"), after=sb.sha("feat-a")),),
        local_complete=True,
    )
    sb.write("scratch.txt", "uncommitted\n")
    sb.git.run("add", "--", "scratch.txt")
    with pytest.raises(LocalWorkNotVerified):
        run_remote_phase(sb.git, None, work)


def test_an_untracked_file_does_not_stop_the_remote_phase(sandbox):
    """The restore is `git reset --hard`, which cannot touch an untracked file.

    Refusing to sync over a scratch file in the worktree would make the tool
    unusable in a real checkout, and nothing it does can lose that file.
    """
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    sb.write("scratch.txt", "never added\n")

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    assert (sb.path / "scratch.txt").read_text() == "never added\n"


# ---------------------------------------------------------------------------
# invariant 11: the push itself
# ---------------------------------------------------------------------------


def test_a_legitimate_post_rebase_force_push_succeeds(sandbox):
    """SPEC.md sec 3, CASE 1."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    assert result.pushed == ("feat-a",)
    assert sb.origin_sha("feat-a") == sb.sha("feat-a")
    assert sb.subjects(f"origin/{sb.trunk}..feat-a") == ["feat-a: c1"]


def test_the_push_uses_exactly_the_flags_the_spec_verified(sandbox):
    """Invariant 11: bare ``--force`` never appears, and no stored push-point."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    started = mark(sb.git)

    result = run_remote_phase(sb.git, None, work)

    assert push_invocations(sb.git, started) == [
        ("git", "push", "--atomic", "--force-with-lease", "--force-if-includes", "origin", "feat-a")
    ]
    assert PUSH_FLAGS == ("--atomic", "--force-with-lease", "--force-if-includes")
    assert "--force " not in result.push_command + " "
    assert "--force-with-lease=" not in result.push_command


def test_the_force_flags_are_load_bearing(sandbox):
    """The push phase 2 makes really is a force-push.

    Without it git refuses the very same refspec, so "the push succeeded" on its
    own would still pass if stackem had quietly stopped forcing.
    """
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)

    plain = sb.git.run("push", "--dry-run", "origin", "feat-a", check=False)
    assert plain.returncode != 0, "the post-rebase push fast-forwarded; nothing was forced"
    assert "rejected" in plain.stderr

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    assert sb.origin_sha("feat-a") == sb.sha("feat-a")


def test_a_teammates_commit_is_not_clobbered_when_we_never_fetched_it(sandbox):
    """SPEC.md sec 3, CASE 2: the lease sees a stale remote-tracking ref."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    mate = sb.teammate_commit("feat-a", subject="mate: important fix", fetch=False)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.REJECTED
    assert result.pushed == ()
    assert sb.origin_sha("feat-a") == mate, "the teammate's work was overwritten"
    assert "feat-a" in result.rejected


def test_a_teammates_commit_survives_even_after_we_fetched_it(sandbox):
    """--force-with-lease alone would pass here; --force-if-includes refuses.

    sync fetches in phase 1 step 1, which refreshes the very remote-tracking ref
    the lease compares against.  That is exactly why the spec pairs the flags.
    """
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    mate = sb.teammate_commit("feat-a", subject="mate: important fix", fetch=True)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.REJECTED
    assert sb.origin_sha("feat-a") == mate
    assert sb.subjects("origin/feat-a") [-1] == "mate: important fix"


def test_a_rejected_push_restores_local_tips_from_the_snapshot(sandbox):
    """SPEC.md sec 5.2 step 9: restore, and report that nothing was pushed."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    snapshot = sb.sha("feat-a")
    sb.advance_trunk(1)
    mate = sb.teammate_commit("feat-a", subject="mate: important fix", fetch=False)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    assert sb.sha("feat-a") != snapshot, "the rebase must have moved the branch"

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.REJECTED
    assert result.restored == ("feat-a",)
    assert sb.sha("feat-a") == snapshot
    assert sb.git.is_clean(), "restoring the checked-out branch left the worktree dirty"
    assert sb.origin_sha("feat-a") == mate


def test_a_rejected_push_leaves_the_retargets_applied_and_says_so(sandbox, mock_github):
    """SPEC.md sec 5.2: "a rejection at 9 leaves PR bases updated but content
    unpushed.  That is recoverable -- rerun sync."

    Phase 2 reports exactly that instead of pretending the run did nothing: the
    base moves are already on the forge, and re-doing them next run is a no-op
    because retargeting is idempotent.
    """
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    api.open_pull_request("feat-a", sb.trunk, number=11)
    api.open_pull_request("feat-b", "feat-a", number=12)
    provider = HttpProvider(mock_github.url)
    sb.advance_trunk(1)
    feat_a = restack_onto_trunk(sb, "feat-a")
    mate = sb.teammate_commit("feat-a", subject="mate: important fix", fetch=False)
    work = VerifiedLocalWork(
        changes=(feat_a,),
        retargets=(
            Retarget(
                pr_number=12, branch="feat-b", old_base="feat-a", new_base=sb.trunk,
                reason=RetargetReason.PARENT_EMPTIED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    assert result.outcome is PushOutcome.REJECTED
    assert result.pushed == ()
    assert [r.pr_number for r in result.retargeted] == [12]
    assert api.pull_request(12).base == sb.trunk
    assert sb.origin_sha("feat-a") == mate, "the teammate's work was overwritten"
    assert sb.sha("feat-a") == feat_a.before


def test_the_same_verified_work_cannot_be_replayed_after_a_rejection(sandbox):
    """Invariants 9 and 21: restoring the tips makes the plan stale, so the
    answer is "rerun stackem sync", never a silent second attempt at the push."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    sb.teammate_commit("feat-a", subject="mate: important fix", fetch=False)
    work = VerifiedLocalWork(changes=(restack_onto_trunk(sb, "feat-a"),), local_complete=True)
    assert run_remote_phase(sb.git, None, work).outcome is PushOutcome.REJECTED
    started = mark(sb.git)

    with pytest.raises(LocalWorkNotVerified) as caught:
        run_remote_phase(sb.git, None, work)

    assert "feat-a" in str(caught.value)
    assert push_invocations(sb.git, started) == []


def test_the_push_is_atomic_across_every_changed_branch(sandbox):
    """Invariant 12: feat-a alone would have landed; with feat-b leased against
    a teammate, neither does."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    remote_before = {name: sb.origin_sha(name) for name in ("feat-a", "feat-b")}
    sb.advance_trunk(1)
    feat_a = restack_onto_trunk(sb, "feat-a")
    feat_b = restack(
        sb, "feat-b", onto=sb.sha("feat-a"), fork=sb.git.merge_base("origin/feat-a", "feat-b")
    )
    mate = sb.teammate_commit("feat-b", subject="mate: on the child", fetch=False)
    work = VerifiedLocalWork(changes=(feat_a, feat_b), local_complete=True)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.REJECTED
    assert result.pushed == ()
    assert sb.origin_sha("feat-a") == remote_before["feat-a"], "a partial push landed"
    assert sb.origin_sha("feat-b") == mate
    assert sb.sha("feat-a") == feat_a.before and sb.sha("feat-b") == feat_b.before


def test_an_atomic_push_lands_every_changed_branch(sandbox):
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    sb.advance_trunk(1)
    feat_a = restack_onto_trunk(sb, "feat-a")
    feat_b = restack(
        sb, "feat-b", onto=sb.sha("feat-a"), fork=sb.git.merge_base("origin/feat-a", "feat-b")
    )
    work = VerifiedLocalWork(changes=(feat_a, feat_b), local_complete=True)
    started = mark(sb.git)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    assert result.pushed == ("feat-a", "feat-b")
    assert sb.origin_sha("feat-a") == sb.sha("feat-a")
    assert sb.origin_sha("feat-b") == sb.sha("feat-b")
    assert push_invocations(sb.git, started)[0][-2:] == ("feat-a", "feat-b")


def test_a_branch_that_was_never_pushed_is_created(sandbox):
    sb = sandbox
    sb.create_branch("feat-new")
    sb.commit("feat-new: c1")
    work = VerifiedLocalWork(
        changes=(change("feat-new", before=sb.sha("feat-new"), remote=None, pull_request=None),),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    assert sb.origin_sha("feat-new") == sb.sha("feat-new")


def test_an_unchanged_stack_pushes_nothing(sandbox):
    """SPEC.md sec 5.1: running sync on a clean stack is a fast no-op."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    work = VerifiedLocalWork(
        changes=(change("feat-a", before=sb.sha("feat-a"), remote=sb.origin_sha("feat-a")),),
        local_complete=True,
    )
    started = mark(sb.git)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.NOTHING_TO_PUSH
    assert push_invocations(sb.git, started) == []


def test_merged_and_orphaned_branches_are_never_pushed(sandbox):
    """Invariant 14: pushing a branch the forge no longer has is a rescue, and
    stackem never rescues."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    sb.delete_remote_branch("feat-a")
    sb.delete_remote_branch("feat-b")
    sb.fetch()
    work = VerifiedLocalWork(
        changes=(
            change("feat-a", before=sb.sha("feat-a"), remote=None, state=BranchState.MERGED),
            change(
                "feat-b",
                before=sb.sha("feat-b"),
                remote=None,
                state=BranchState.ORPHANED,
                pull_request=pull_request(2, "feat-b", "feat-a", PullRequestState.CLOSED),
            ),
        ),
        local_complete=True,
    )
    started = mark(sb.git)

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.NOTHING_TO_PUSH
    assert push_invocations(sb.git, started) == []
    assert sb.origin_sha("feat-a") is None
    assert sb.origin_sha("feat-b") is None


# ---------------------------------------------------------------------------
# invariant 15: retargeting comes first, and in the right order
# ---------------------------------------------------------------------------


def test_children_of_merged_and_emptied_branches_are_retargeted_first(sandbox):
    """SPEC.md sec 5.2 step 8."""
    sb = sandbox
    provider = RecordingProvider()
    work = VerifiedLocalWork(
        changes=(change("feat-a"), change("feat-b"), change("feat-c")),
        retargets=(
            Retarget(
                pr_number=30, branch="feat-c", old_base="feat-b", new_base="feat-a",
                reason=RetargetReason.REPARENTED,
            ),
            Retarget(
                pr_number=20, branch="feat-b", old_base="feat-a", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
            Retarget(
                pr_number=40, branch="feat-d", old_base="feat-c", new_base="feat-b",
                reason=RetargetReason.PARENT_EMPTIED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    assert [number for number, _ in provider.retargets] == [20, 40, 30]
    assert provider.retargets[0] == (20, "main")
    assert [r.pr_number for r in result.retargeted] == [20, 40, 30]


def test_a_pull_request_already_pointing_at_its_parent_is_left_alone(sandbox):
    sb = sandbox
    provider = RecordingProvider()
    work = VerifiedLocalWork(
        changes=(change("feat-b"),),
        retargets=(
            Retarget(
                pr_number=20, branch="feat-b", old_base="main", new_base="main",
                reason=RetargetReason.REPARENTED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    assert provider.retargets == []
    assert result.retargeted == ()


def test_retargeting_happens_before_the_push(sandbox):
    """Step 8 precedes step 9 so a child PR never displays the whole stack."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    order: list[str] = []
    provider = RecordingProvider()
    original = provider.retarget

    def spy(pr_number: int, new_base: str) -> None:
        order.append("retarget")
        original(pr_number, new_base)

    provider.retarget = spy  # type: ignore[method-assign]
    sb.git.logger = lambda invocation: (
        order.append("push") if invocation.argv[1:2] == ("push",) else None
    )
    work = VerifiedLocalWork(
        changes=(restack_onto_trunk(sb, "feat-a"),),
        retargets=(
            Retarget(
                pr_number=10, branch="feat-a", old_base="old-parent", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
        ),
        local_complete=True,
    )

    run_remote_phase(sb.git, provider, work)

    assert order == ["retarget", "push"]


def test_a_failed_retarget_stops_the_push(sandbox):
    """A PR left pointing at a doomed base would display the whole stack the
    moment its content landed, so nothing is pushed."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    remote_before = sb.origin_sha("feat-a")
    sb.advance_trunk(1)
    provider = RecordingProvider(fail={20})
    work = VerifiedLocalWork(
        changes=(restack_onto_trunk(sb, "feat-a"),),
        retargets=(
            Retarget(
                pr_number=10, branch="feat-a", old_base="old", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
            Retarget(
                pr_number=20, branch="feat-b", old_base="feat-a", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
        ),
        local_complete=True,
    )
    started = mark(sb.git)

    result = run_remote_phase(sb.git, provider, work)

    assert result.outcome is PushOutcome.RETARGET_FAILED
    assert push_invocations(sb.git, started) == []
    assert sb.origin_sha("feat-a") == remote_before
    assert [r.pr_number for r in result.retargeted] == [10]
    assert [f.retarget.pr_number for f in result.retarget_failures] == [20]


def test_a_failed_retarget_withholds_the_delete_command_for_that_parent(sandbox):
    """Invariant 15: never print a deletion that would close a PR still pointing
    at the branch being deleted."""
    sb = sandbox
    provider = RecordingProvider(fail={20})
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                state=BranchState.MERGED,
                pull_request=pull_request(10, "feat-a", "main", PullRequestState.MERGED),
            ),
            change("feat-b", pull_request=pull_request(20, "feat-b", "feat-a")),
        ),
        retargets=(
            Retarget(
                pr_number=20, branch="feat-b", old_base="feat-a", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    merged = [f for f in result.follow_ups if f.kind is FollowUpKind.MERGED]
    assert [f.branch for f in merged] == ["feat-a"]
    assert not any("--delete" in command for command in merged[0].commands)
    assert "feat-b" in merged[0].note


def test_a_retarget_reaches_a_real_forge_over_http(sandbox, mock_github_memory):
    """The same code path, driven against the mock GitHub server."""
    sb = sandbox
    api = mock_github_memory.api
    api.open_pull_request("feat-a", "main", number=10)
    api.open_pull_request("feat-b", "feat-a", number=20)
    provider = HttpProvider(mock_github_memory.url)
    work = VerifiedLocalWork(
        changes=(change("feat-b"),),
        retargets=(
            Retarget(
                pr_number=20, branch="feat-b", old_base="feat-a", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    assert result.outcome is PushOutcome.NOTHING_TO_PUSH
    assert api.pull_request(20).base == "main"
    assert [(r.method, r.path) for r in mock_github_memory.requests] == [
        ("PATCH", "/repos/acme/app/pulls/20")
    ]


# ---------------------------------------------------------------------------
# invariant 14: report the command, never run it
# ---------------------------------------------------------------------------


def test_an_emptied_branch_is_reported_never_closed_or_deleted(mock_github, sandbox):
    """SPEC.md sec 7.2 and sec 5.1: reporting is the whole behaviour."""
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b"])
    api = mock_github.api
    api.open_pull_request("feat-a", sb.trunk, number=11)
    api.open_pull_request("feat-b", "feat-a", number=12)
    provider = HttpProvider(mock_github.url)
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                before=sb.sha("feat-a"),
                remote=sb.origin_sha("feat-a"),
                emptied=True,
                pull_request=pull_request(11, "feat-a", sb.trunk),
            ),
            change(
                "feat-b",
                before=sb.sha("feat-b"),
                remote=sb.origin_sha("feat-b"),
                parent="feat-a",
                pull_request=pull_request(12, "feat-b", "feat-a"),
            ),
        ),
        retargets=(
            Retarget(
                pr_number=12, branch="feat-b", old_base="feat-a", new_base=sb.trunk,
                reason=RetargetReason.PARENT_EMPTIED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    emptied = [f for f in result.follow_ups if f.kind is FollowUpKind.EMPTIED]
    assert [f.branch for f in emptied] == ["feat-a"]
    assert emptied[0].commands == (
        "gh pr close 11",
        "git push origin --delete feat-a",
        "git branch -D feat-a",
    )
    # nothing was actually closed or deleted
    assert api.pull_request(11).state == "open"
    assert api.branch_exists("feat-a")
    assert sb.origin_sha("feat-a") is not None
    assert api.pull_request(12).base == sb.trunk, "the child was retargeted first"


def test_an_orphaned_pull_request_gets_the_rescue_sequence(sandbox):
    """SPEC.md sec 6.3, verified end to end against GitHub."""
    sb = sandbox
    provider = RecordingProvider(slug="acme/app")
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                state=BranchState.ORPHANED,
                remote=None,
                pull_request=pull_request(1, "feat-a", "main", PullRequestState.CLOSED),
            ),
            change(
                "feat-b",
                parent="feat-a",
                state=BranchState.ORPHANED,
                remote=None,
                pull_request=pull_request(2, "feat-b", "feat-a", PullRequestState.CLOSED),
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    orphans = {f.branch: f for f in result.follow_ups if f.kind is FollowUpKind.ORPHANED}
    assert set(orphans) == {"feat-a", "feat-b"}
    assert orphans["feat-b"].commands == (
        "git fetch origin refs/pull/1/head:rescue-base refs/pull/2/head:rescue-head",
        "git push origin rescue-base:refs/heads/feat-a rescue-head:refs/heads/feat-b",
        "gh api -X PATCH /repos/acme/app/pulls/2 -f state=open",
    )
    assert orphans["feat-a"].commands == (
        "git fetch origin refs/pull/1/head:rescue-head",
        "git push origin rescue-head:refs/heads/feat-a",
        "gh api -X PATCH /repos/acme/app/pulls/1 -f state=open",
    )


def test_the_rescue_retargets_when_the_derived_parent_moved():
    """The last line of SPEC.md sec 6.3: ``gh pr edit 2 --base main``."""
    commands = rescue_commands(
        slug="acme/app",
        pr_number=2,
        head="feat-b",
        base="feat-a",
        head_missing=True,
        base_missing=True,
        base_pr_number=1,
        retarget_to="main",
    )
    assert commands[-2:] == (
        "gh api -X PATCH /repos/acme/app/pulls/2 -f state=open",
        "gh pr edit 2 --base main",
    )


def test_a_branch_with_no_pull_request_is_reported_with_gh_pr_create(sandbox):
    """SPEC.md sec 6.5 / invariant 20: print the command, never create the PR."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.create_branch("feat-docs")
    sb.commit("feat-docs: c1")
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                before=sb.sha("feat-a"),
                remote=sb.origin_sha("feat-a"),
                pull_request=pull_request(10, "feat-a", sb.trunk),
            ),
            change("feat-docs", before=sb.sha("feat-docs"), remote=None, parent="feat-a"),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.PUSHED
    missing = [f for f in result.follow_ups if f.kind is FollowUpKind.NO_PULL_REQUEST]
    assert [f.branch for f in missing] == ["feat-docs"]
    assert missing[0].commands == ("gh pr create --base feat-a --head feat-docs",)
    assert create_pull_request_command("feat-docs", "feat-a") == (
        "gh pr create --base feat-a --head feat-docs"
    )


def test_an_unpushed_branch_is_not_told_to_open_a_pull_request(sandbox):
    """A rejected push means the branch is not on the forge; ``gh pr create``
    would fail, so it is not printed."""
    sb = sandbox
    sb.make_stack(["feat-a"])
    sb.advance_trunk(1)
    sb.teammate_commit("feat-a", subject="mate: important fix", fetch=False)
    feat_a = restack_onto_trunk(sb, "feat-a")
    feat_a = dataclasses.replace(feat_a, pull_request=pull_request(10, "feat-a", sb.trunk))
    sb.create_branch("feat-docs")
    sb.commit("feat-docs: c1")
    work = VerifiedLocalWork(
        changes=(feat_a, change("feat-docs", before=sb.sha("feat-docs"), remote=None, parent="feat-a")),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, None, work)

    assert result.outcome is PushOutcome.REJECTED
    assert [f.kind for f in result.follow_ups if f.kind is FollowUpKind.NO_PULL_REQUEST] == []


def test_delete_branch_on_merge_is_warned_about(sandbox):
    """SPEC.md sec 6.3: it orphans a child PR on every merge."""
    sb = sandbox
    work = VerifiedLocalWork(
        changes=(change("feat-a"),),
        delete_branch_on_merge=True,
        local_complete=True,
    )

    result = run_remote_phase(sb.git, None, work)

    warnings = [f for f in result.follow_ups if f.kind is FollowUpKind.DELETE_BRANCH_ON_MERGE]
    assert len(warnings) == 1
    assert "--delete-branch" in warnings[0].note


def test_a_merged_branch_is_reported_with_its_cleanup_commands(sandbox):
    sb = sandbox
    provider = RecordingProvider()
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                state=BranchState.MERGED,
                remote=None,
                pull_request=pull_request(10, "feat-a", "main", PullRequestState.MERGED),
            ),
            change("feat-b", pull_request=pull_request(20, "feat-b", "feat-a")),
        ),
        retargets=(
            Retarget(
                pr_number=20, branch="feat-b", old_base="feat-a", new_base="main",
                reason=RetargetReason.PARENT_MERGED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    merged = [f for f in result.follow_ups if f.kind is FollowUpKind.MERGED]
    assert [f.branch for f in merged] == ["feat-a"]
    # the remote branch is already gone, so only the local cleanup is printed
    assert merged[0].commands == ("git branch -D feat-a",)
    assert provider.retargets == [(20, "main")]


def test_phase_two_never_asks_the_forge_to_close_delete_or_reopen(sandbox, mock_github):
    """Invariant 14, checked at the wire.

    A merged branch, an emptied branch and the child of both: every one of them
    has a cleanup only a human should perform.  The only thing that leaves the
    process is a new base -- the rest comes back as command strings.
    """
    sb = sandbox
    sb.make_stack(["feat-a", "feat-b", "feat-c"])
    api = mock_github.api
    api.open_pull_request("feat-a", sb.trunk, number=11)
    api.open_pull_request("feat-b", "feat-a", number=12)
    api.open_pull_request("feat-c", "feat-b", number=13)
    api.squash_merge(11)
    provider = HttpProvider(mock_github.url)
    work = VerifiedLocalWork(
        changes=(
            change(
                "feat-a",
                before=sb.sha("feat-a"),
                remote=sb.origin_sha("feat-a"),
                state=BranchState.MERGED,
                pull_request=pull_request(11, "feat-a", sb.trunk, PullRequestState.MERGED),
            ),
            change(
                "feat-b",
                before=sb.sha("feat-b"),
                remote=sb.origin_sha("feat-b"),
                parent=sb.trunk,
                emptied=True,
                pull_request=pull_request(12, "feat-b", "feat-a"),
            ),
            change(
                "feat-c",
                before=sb.sha("feat-c"),
                remote=sb.origin_sha("feat-c"),
                parent=sb.trunk,
                pull_request=pull_request(13, "feat-c", "feat-b"),
            ),
        ),
        retargets=(
            Retarget(
                pr_number=12, branch="feat-b", old_base="feat-a", new_base=sb.trunk,
                reason=RetargetReason.PARENT_MERGED,
            ),
            Retarget(
                pr_number=13, branch="feat-c", old_base="feat-b", new_base=sb.trunk,
                reason=RetargetReason.PARENT_EMPTIED,
            ),
        ),
        local_complete=True,
    )

    result = run_remote_phase(sb.git, provider, work)

    assert result.outcome is PushOutcome.NOTHING_TO_PUSH
    assert {(r.method, r.path) for r in mock_github.requests} == {
        ("PATCH", "/repos/acme/app/pulls/12"),
        ("PATCH", "/repos/acme/app/pulls/13"),
    }
    assert all(set(r.body) == {"base"} for r in mock_github.requests), (
        "phase 2 sent the forge something other than a new base"
    )
    assert api.pull_request(12).state == "open"
    assert api.pull_request(13).state == "open"
    assert api.branch_exists("feat-a") and api.branch_exists("feat-b")
    assert sb.origin_sha("feat-a") is not None and sb.origin_sha("feat-b") is not None

    kinds = {f.kind for f in result.follow_ups}
    assert FollowUpKind.MERGED in kinds and FollowUpKind.EMPTIED in kinds
