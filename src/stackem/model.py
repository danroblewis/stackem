"""Plain data.  No logic lives here.

stackem stores no state (CLAUDE.md, "Two constraints"): every object below is
derived fresh on each run from git topology and the forge, then thrown away.
Nothing here is ever serialized to a ref, a config key or a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

__all__ = [
    "Branch",
    "BranchState",
    "ParentSource",
    "PullRequest",
    "PullRequestState",
    "RepoSettings",
    "Stack",
]


class PullRequestState(Enum):
    """A pull request's state, forge-neutral.

    GitHub's REST API has only ``open`` and ``closed`` and marks a merge with
    ``merged_at``; providers normalize that into ``MERGED`` here, because the
    difference decides whether a branch is skipped in the cascade (CLAUDE.md
    invariant 6) or reported as orphaned (SPEC.md sec 5.1).
    """

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class BranchState(Enum):
    """Branch classification, SPEC.md sec 5.1.

    live      -- has an open pull request, or no pull request at all
    merged    -- its pull request is merged; the cascade SKIPS it (invariant 6)
    orphaned  -- its pull request is closed, not merged, and its head branch is
                 missing from the remote.  sync reports it and prints the rescue
                 commands; it never reopens anything (invariant 14).
    """

    LIVE = "live"
    MERGED = "merged"
    ORPHANED = "orphaned"


class ParentSource(Enum):
    """Where a branch's parent came from on this run.

    PULL_REQUEST -- the pull request's base branch.  This IS the parent record
                    (CLAUDE.md invariant 1); there is no other.
    TOPOLOGY     -- inferred by nearest ancestor for a branch with no PR, used
                    for this run only and never recorded (SPEC.md sec 5.1).
    TRUNK        -- the branch sits directly on the trunk.
    """

    PULL_REQUEST = "pull_request"
    TOPOLOGY = "topology"
    TRUNK = "trunk"


@dataclass(frozen=True)
class PullRequest:
    """One pull request, as much of it as sync uses.

    ``base`` is the parent branch (invariant 1).  ``is_cross_repository`` marks a
    fork PR, which is excluded from indexing (invariant 17).  ``merged_at`` breaks
    ties when one branch has several merged PRs (invariant 16).
    """

    number: int
    head: str
    base: str
    state: PullRequestState
    title: str = ""
    url: str = ""
    is_cross_repository: bool = False
    head_repository_owner: str | None = None
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    updated_at: datetime | None = None
    draft: bool = False


@dataclass
class Branch:
    """One local branch as this run sees it.

    ``parent`` and ``fork_point`` are derived, never stored: the parent is the
    pull request's base branch, and the fork point is
    ``merge-base(origin/<parent>, <branch>)`` -- the parent's last-synced state
    (SPEC.md sec 2 and 3).
    """

    name: str
    sha: str
    remote_sha: str | None = None
    pull_request: PullRequest | None = None
    state: BranchState = BranchState.LIVE
    parent: str | None = None
    parent_source: ParentSource | None = None
    fork_point: str | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class Stack:
    """The stack for one run.

    ``branches`` is ordered BOTTOM-UP -- a parent always precedes its children,
    which is the order the cascade walks (SPEC.md sec 5.2 step 6).

    The trunk is NOT a member (CLAUDE.md invariant 8): walking up from the trunk
    would collect every branch in the repository.
    """

    trunk: str
    remote: str = "origin"
    head: str | None = None
    branches: list[Branch] = field(default_factory=list)
    trunk_remote_sha: str | None = None


@dataclass(frozen=True)
class RepoSettings:
    """Forge-side repository settings sync needs to warn about.

    ``delete_branch_on_merge`` orphans a child pull request on every merge
    (SPEC.md sec 6.3 and sec 12), so sync warns when it is on.
    """

    default_branch: str
    delete_branch_on_merge: bool = False
