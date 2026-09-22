"""Orientation: working out what the stack IS (SPEC.md sec 5.1).

Nothing here writes anything down.  Every answer is derived fresh from git
topology and the pull requests themselves, exactly as SPEC.md sec 3 requires,
and thrown away when the run ends.

The four questions, and where their answers come from:

**Which branch is the trunk?**  ``origin/HEAD``, else ``git remote set-head
origin -a``, else the forge's default branch (SPEC.md sec 6.6).  **Verified**
that ``refs/remotes/origin/HEAD`` is often unset.  The middle step WRITES a ref,
so ``stackem`` with no arguments passes ``allow_ref_write=False`` and falls
straight through to the forge (CLAUDE.md invariant 5).

**Who is a branch's parent?**  The pull request's base branch -- that IS the
parent record (CLAUDE.md invariant 1).  Only a branch with no pull request gets
a parent inferred by nearest ancestor, and that inference lives for this run
only: nothing records it, because a display command that mutated state was a
verified mistake (SPEC.md sec 4).

**Which branches are in the stack?**  Spine = HEAD down through parents to the
trunk; members = spine MINUS the trunk; then, transitively, branches whose
parent is a member (CLAUDE.md invariant 8).  The trunk is never a member.  An
earlier draft left it in, and "walk up from the set" then collected every branch
rooted on trunk -- scratch branches, WIP, unrelated features -- all of which sync
would have rebased and force-pushed.

**What state is a branch in?**  live (open pull request, or none), merged (its
pull request is merged), or orphaned (closed, not merged, and its head branch is
gone from the remote).  Offline, with no pull request to ask, merge detection
falls back to ``merge-tree`` guarded by a unique-commit count (CLAUDE.md
invariant 19).

Pull requests are read the way invariant 18 demands: one paged listing of the
OPEN ones, then a per-branch query for the branches that listing did not cover.
Never a single ``--state all --limit 100`` window, which in an active repo fills
with closed pull requests and hides the open one you were looking for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence

from stackem.forge import ForgeError, Provider, select_pull_request
from stackem.gitx import Git, GitError
from stackem.model import (
    Branch,
    BranchState,
    ParentSource,
    PullRequest,
    PullRequestState,
    Stack,
)

__all__ = [
    "Orientation",
    "OrientationError",
    "PullRequestIndex",
    "TrunkResolution",
    "TrunkSource",
    "index_pull_requests",
    "infer_parent",
    "is_contained_in_trunk",
    "orient",
    "resolve_trunk",
]


class OrientationError(RuntimeError):
    """The stack could not be derived -- most often: no trunk could be found."""


class TrunkSource(Enum):
    """Which of SPEC.md sec 6.6's steps answered."""

    EXPLICIT = "explicit"  # the caller said so (--trunk, or a test)
    ORIGIN_HEAD = "origin/HEAD"
    REMOTE_SET_HEAD = "git remote set-head"
    FORGE = "forge"


@dataclass(frozen=True)
class TrunkResolution:
    """The trunk, and how it was found."""

    name: str
    source: TrunkSource
    remote: str = "origin"

    @property
    def ref(self) -> str:
        """``origin/<trunk>``.

        CLAUDE.md invariant 4: roots rebase onto this, never onto the local
        trunk -- sync does not fast-forward the local trunk, so using it would
        silently rebase the stack onto a stale base.
        """
        return f"{self.remote}/{self.name}"


@dataclass(frozen=True)
class PullRequestIndex:
    """Branch name -> the pull request that represents it.

    ``queried`` names the branches that needed a per-branch lookup because the
    open listing did not cover them; it exists so a test can prove the open
    listing is not being asked to do a job it cannot do (invariant 18).
    """

    by_branch: Mapping[str, PullRequest] = field(default_factory=dict)
    open_pull_requests: tuple[PullRequest, ...] = ()
    queried: tuple[str, ...] = ()

    def get(self, branch: str) -> PullRequest | None:
        return self.by_branch.get(branch)

    def __contains__(self, branch: object) -> bool:
        return branch in self.by_branch

    def __len__(self) -> int:
        return len(self.by_branch)


@dataclass
class Orientation:
    """Everything one run derived, before anything is changed.

    ``stack`` is what sync acts on: members only, ordered bottom-up.
    ``branches`` holds a record for EVERY local branch, members and non-members
    alike, because working out the boundary needs every branch's parent.  Only
    members are classified and only members carry a fork point and children --
    a non-member's ``state`` is the default and means nothing.
    """

    stack: Stack
    trunk: TrunkResolution
    branches: dict[str, Branch] = field(default_factory=dict)
    pull_requests: PullRequestIndex = field(default_factory=PullRequestIndex)
    offline: bool = False
    forge_error: str | None = None

    @property
    def members(self) -> list[Branch]:
        """Stack members, bottom-up (parents before children)."""
        return list(self.stack.branches)

    @property
    def member_names(self) -> list[str]:
        return [branch.name for branch in self.stack.branches]

    def branch(self, name: str) -> Branch | None:
        return self.branches.get(name)

    def is_member(self, name: str) -> bool:
        return any(branch.name == name for branch in self.stack.branches)


# --------------------------------------------------------------------------
# the trunk -- SPEC.md sec 6.6
# --------------------------------------------------------------------------

def resolve_trunk(
    git: Git,
    provider: Provider | None = None,
    *,
    trunk: str | None = None,
    remote: str = "origin",
    allow_ref_write: bool = True,
) -> TrunkResolution:
    """Work out the trunk, in SPEC.md sec 6.6's order.

    ``allow_ref_write=False`` skips ``git remote set-head``, which writes
    ``refs/remotes/<remote>/HEAD``.  ``stackem`` with no arguments must pass it
    (CLAUDE.md invariant 5: the display command never writes).
    """
    if trunk:
        return TrunkResolution(trunk, TrunkSource.EXPLICIT, remote)

    name = _origin_head(git, remote)
    if name:
        return TrunkResolution(name, TrunkSource.ORIGIN_HEAD, remote)

    if allow_ref_write:
        try:
            git.remote_set_head(remote)
        except GitError:
            pass  # no network, a bad url, no such remote -- the forge is next
        else:
            name = _origin_head(git, remote)
            if name:
                return TrunkResolution(name, TrunkSource.REMOTE_SET_HEAD, remote)

    if provider is not None:
        try:
            name = provider.default_branch()
        except (ForgeError, OSError) as err:
            raise OrientationError(
                f"cannot determine the trunk: {remote}/HEAD is unset and the forge "
                f"could not be asked ({err})"
            ) from err
        if name:
            return TrunkResolution(name, TrunkSource.FORGE, remote)

    raise OrientationError(
        f"cannot determine the trunk: {remote}/HEAD is unset, "
        f"`git remote set-head {remote} -a` did not set it, and no forge is available"
    )


def _origin_head(git: Git, remote: str) -> str | None:
    value = git.symbolic_ref(f"refs/remotes/{remote}/HEAD", short=True)
    if not value:
        return None
    prefix = f"{remote}/"
    return value[len(prefix):] if value.startswith(prefix) else value


# --------------------------------------------------------------------------
# pull requests -- CLAUDE.md invariants 16, 17, 18
# --------------------------------------------------------------------------

def index_pull_requests(
    provider: Provider | None, branches: Iterable[str]
) -> PullRequestIndex:
    """Index ``branches`` by the pull request that represents each one.

    Two calls' worth of data, never one big one (CLAUDE.md invariant 18):

    1. every OPEN pull request, paged -- one listing covers the whole stack;
    2. for each branch that listing did not cover, a per-branch query, which is
       the only way to see a merged or closed pull request without relying on a
       100-row window that fills up with closed ones.

    Fork pull requests are dropped on both paths (CLAUDE.md invariant 17).  The
    second drop is not redundant: a provider that answers a per-branch query
    without the head-ref filter would otherwise hand back a stranger's pull
    request whose head ref happens to match a local branch name, and sync would
    retarget it.
    """
    names = list(dict.fromkeys(branches))
    if provider is None:
        return PullRequestIndex()

    open_pull_requests = tuple(provider.list_open_pull_requests())
    found: dict[str, PullRequest] = {}
    queried: list[str] = []
    for name in names:
        pull_request = select_pull_request(open_pull_requests, name)
        if pull_request is None:
            queried.append(name)
            pull_request = provider.get_pull_request_for_branch(name)
            if pull_request is not None and (
                pull_request.is_cross_repository or pull_request.head != name
            ):
                pull_request = None
        if pull_request is not None:
            found[name] = pull_request
    return PullRequestIndex(found, open_pull_requests, tuple(queried))


# --------------------------------------------------------------------------
# parents -- CLAUDE.md invariant 1, SPEC.md sec 5.1
# --------------------------------------------------------------------------

def infer_parent(
    git: Git, branch: str, candidates: Iterable[str], *, trunk: str
) -> str:
    """The nearest ancestor of ``branch`` among ``candidates``, else the trunk.

    Only for a branch with no pull request, and only for this run: SPEC.md
    sec 5.1 says the inference is never recorded.  "Nearest" is the fewest
    commits between the candidate's tip and the branch; a candidate with none
    (its tip is the branch's tip, or ahead of it) is not a parent.
    """
    reachable = _branches_merged_into(git, branch)
    best: str | None = None
    best_distance: int | None = None
    for name in sorted(set(candidates)):
        if name == branch or name not in reachable:
            continue
        distance = git.rev_list_count(f"{name}..{branch}")
        if distance == 0:
            continue
        if best_distance is None or distance < best_distance:
            best, best_distance = name, distance
    return best or trunk


def _branches_merged_into(git: Git, rev: str) -> set[str]:
    """Local branches whose tip is reachable from ``rev`` -- one git call."""
    return set(
        git.lines(
            "for-each-ref",
            "refs/heads",
            "--merged",
            rev,
            "--format=%(refname:short)",
        )
    )


class _Parents:
    """Memoized parent lookup: the pull request's base, else nearest ancestor."""

    def __init__(self, git: Git, index: PullRequestIndex, trunk: str, local: Sequence[str]):
        self._git = git
        self._index = index
        self._trunk = trunk
        self._local = list(local)
        self._cache: dict[str, tuple[str, ParentSource]] = {}

    def of(self, name: str) -> tuple[str, ParentSource]:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        pull_request = self._index.get(name)
        if pull_request is not None and pull_request.base and pull_request.base != name:
            # CLAUDE.md invariant 1: the pull request's base branch IS the parent.
            result = (pull_request.base, ParentSource.PULL_REQUEST)
        else:
            inferred = infer_parent(
                self._git, name, self._local, trunk=self._trunk
            )
            source = (
                ParentSource.TRUNK if inferred == self._trunk else ParentSource.TOPOLOGY
            )
            result = (inferred, source)
        self._cache[name] = result
        return result

    def name_of(self, branch: str) -> str:
        return self.of(branch)[0]


# --------------------------------------------------------------------------
# classification -- SPEC.md sec 5.1, sec 6.4
# --------------------------------------------------------------------------

def is_contained_in_trunk(git: Git, branch: str, *, trunk_ref: str) -> bool:
    """Offline merge detection (SPEC.md sec 6.4), with invariant 19's guard.

    ``merge-tree --write-tree`` reports ANY branch with no unique content as
    merged, a brand-new branch that is merely behind trunk included -- so the
    unique-commit count comes first.  ``git cherry`` cannot do this job at all:
    squashing changes the patch-id.

    ``stackem.detect`` carries the same check under the same name with a
    different signature (``git, trunk_ref, branch``); the two agree, and one of
    them should go.  See this module's report: orientation uses the tree test
    only when there is no forge to ask, while ``detect`` uses it whenever the
    pull request cannot answer.
    """
    if git.rev_list_count(f"{trunk_ref}..{branch}") == 0:
        return False
    merged = git.merge_tree_write_tree(trunk_ref, branch)
    if merged.conflicted or merged.tree is None:
        return False
    return merged.tree == git.tree_id(trunk_ref)


def _classify(
    git: Git,
    name: str,
    pull_request: PullRequest | None,
    *,
    trunk_ref: str,
    remote_branches: Mapping[str, str],
    provider: Provider | None,
) -> BranchState:
    if pull_request is not None:
        # The pull request's state is the authority whenever there is one
        # (SPEC.md sec 3).  merge-tree does not get to overrule it: a branch
        # whose content someone landed by hand still has an open pull request
        # and must stay in the cascade.
        if pull_request.state is PullRequestState.MERGED:
            return BranchState.MERGED
        if pull_request.state is PullRequestState.OPEN:
            return BranchState.LIVE
        # Closed and not merged: orphaned only if the head branch is gone
        # (SPEC.md sec 5.1).  sync reports it; it never rescues (invariant 14).
        return (
            BranchState.LIVE
            if _exists_on_remote(name, provider, remote_branches)
            else BranchState.ORPHANED
        )
    if provider is None and is_contained_in_trunk(git, name, trunk_ref=trunk_ref):
        # No forge to ask, so fall back to the guarded tree comparison.
        return BranchState.MERGED
    return BranchState.LIVE


def _exists_on_remote(
    name: str, provider: Provider | None, remote_branches: Mapping[str, str]
) -> bool:
    """Whether the forge still has the branch.

    The forge is asked first: a remote-tracking ref only tells the truth after a
    ``fetch --prune``, and ``stackem`` with no arguments does not fetch.  When
    the forge cannot answer, a surviving tracking ref is taken as "still there",
    which errs towards NOT reporting an orphan -- the report prints rescue
    commands, and printing them for a branch that is fine is the worse mistake.
    """
    if provider is not None:
        try:
            return provider.branch_exists_on_remote(name)
        except (ForgeError, OSError):
            pass
    return name in remote_branches


# --------------------------------------------------------------------------
# the whole derivation
# --------------------------------------------------------------------------

def orient(
    git: Git,
    provider: Provider | None = None,
    *,
    trunk: str | None = None,
    head: str | None = None,
    remote: str = "origin",
    allow_ref_write: bool = True,
) -> Orientation:
    """Derive the stack: trunk, parents, membership, classification.

    Read-only except for the one ref write SPEC.md sec 6.6 sanctions, which
    ``allow_ref_write=False`` turns off (CLAUDE.md invariant 5).  It does not
    fetch -- sync fetches first, in phase 1 step 1, and ``stackem`` deliberately
    does not.
    """
    resolution = resolve_trunk(
        git, provider, trunk=trunk, remote=remote, allow_ref_write=allow_ref_write
    )
    trunk_name = resolution.name
    trunk_ref = resolution.ref

    local = git.branches("refs/heads")
    remote_branches = git.branches(f"refs/remotes/{remote}")
    head_name = _head_branch(git, head)
    candidates = [name for name in local if name != trunk_name]

    forge_error: str | None = None
    try:
        index = index_pull_requests(provider, candidates)
    except (ForgeError, OSError) as err:
        # SPEC.md sec 12: offline, parent resolution falls back to topology.
        # That is correct for a healthy stack, and it is better than refusing to
        # run at all -- but it cannot tell that a parent was merged, so the
        # failure is reported rather than swallowed.
        forge_error = str(err)
        provider = None
        index = PullRequestIndex()

    parents = _Parents(git, index, trunk_name, list(local))
    spine = _spine(head_name, trunk_name, local, parents)
    members = _members(spine, candidates, parents)
    order = _order(members, spine, parents)

    branches: dict[str, Branch] = {}
    for name, sha in local.items():
        parent, source = (None, None)
        if name != trunk_name:
            parent, source = parents.of(name)
        branches[name] = Branch(
            name=name,
            sha=sha,
            remote_sha=remote_branches.get(name),
            pull_request=index.get(name),
            parent=parent,
            parent_source=source,
        )

    stack = Stack(
        trunk=trunk_name,
        remote=remote,
        head=head_name,
        trunk_remote_sha=remote_branches.get(trunk_name),
    )
    for name in order:
        branch = branches[name]
        branch.children = [
            other for other in order if parents.name_of(other) == name and other != name
        ]
        branch.fork_point = _fork_point(git, branch.parent, name, remote)
        branch.state = _classify(
            git,
            name,
            branch.pull_request,
            trunk_ref=trunk_ref,
            remote_branches=remote_branches,
            provider=provider,
        )
        stack.branches.append(branch)

    return Orientation(
        stack=stack,
        trunk=resolution,
        branches=branches,
        pull_requests=index,
        offline=provider is None,
        forge_error=forge_error,
    )


def _head_branch(git: Git, head: str | None) -> str | None:
    """The branch the user is on.

    HEAD is detached mid-rebase, and sync still has to derive the stack there --
    phase 0 decides whether a rebase in progress is its own by checking the
    rebase's head-name against the members (CLAUDE.md invariant 22).
    """
    if head:
        return head
    current = git.current_branch()
    if current:
        return current
    state = git.read_rebase_state()
    return state.branch if state is not None else None


def _spine(
    head: str | None, trunk: str, local: Mapping[str, str], parents: _Parents
) -> list[str]:
    """HEAD down through parents to the trunk, bottom-up, trunk excluded."""
    spine: list[str] = []
    seen: set[str] = set()
    node = head
    while node is not None and node != trunk and node in local and node not in seen:
        spine.append(node)
        seen.add(node)
        node = parents.name_of(node)
    spine.reverse()
    return spine


def _members(spine: Sequence[str], candidates: Sequence[str], parents: _Parents) -> set[str]:
    """The spine, plus transitively every branch whose parent is a member.

    The trunk is not in ``spine`` and never enters this set, which is the whole
    point of CLAUDE.md invariant 8.
    """
    members = set(spine)
    growing = True
    while growing:
        growing = False
        for name in candidates:
            if name in members:
                continue
            if parents.name_of(name) in members:
                members.add(name)
                growing = True
    return members


def _order(members: set[str], spine: Sequence[str], parents: _Parents) -> list[str]:
    """Members bottom-up: a parent always precedes its children.

    Ties go to the spine, in spine order, so the branch the user is standing on
    reads as one chain; everything else is alphabetical, so a run is repeatable.
    """
    rank = {name: index for index, name in enumerate(spine)}

    def key(name: str) -> tuple[int, str]:
        return (rank.get(name, len(spine)), name)

    pending = set(members)
    order: list[str] = []
    while pending:
        ready = sorted(
            (name for name in pending if parents.name_of(name) not in pending), key=key
        )
        if not ready:
            # A parent cycle (SPEC.md sec 12: `stackem parent` has no cycle
            # detection).  Break it deterministically rather than spin.
            ready = [min(pending, key=key)]
        for name in ready:
            order.append(name)
            pending.discard(name)
    return order


def _fork_point(git: Git, parent: str | None, branch: str, remote: str) -> str | None:
    """``merge-base(origin/<parent>, <branch>)`` -- SPEC.md sec 2.

    The parent's LAST-SYNCED state, never its local tip: against the local tip
    the derivation fails after an amend.  ``None`` when the parent has never
    been pushed, and sync recomputes it during the walk anyway, because
    invariant 3 puts the "already based on target" skip-check ahead of anything
    that needs a fork point.
    """
    if not parent:
        return None
    ref = f"{remote}/{parent}"
    if git.try_rev_parse(ref) is None:
        return None
    return git.merge_base(ref, branch)
