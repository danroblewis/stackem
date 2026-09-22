"""The forge interface: what sync needs from GitHub, and nothing else.

GitHub is first.  Gitea and Codeberg speak substantially the same API shape;
GitLab's merge-request model differs enough to need real adaptation (SPEC.md
sec 11).  So this protocol is written in forge-neutral terms.

What is deliberately NOT here
-----------------------------
**Pull request creation.**  SPEC.md sec 6.5: the team's PR template needs an
agent to write the body, and ``gh pr create --fill`` ignores the upstream ref
anyway.  sync prints ``gh pr create --base <parent> --head <branch>`` instead.

**Closing, deleting, reopening.**  CLAUDE.md invariant 14: sync never closes a
pull request, deletes a branch, or rescues an orphan.  Auto-rescue has a false
positive (a person can close a PR *and* delete its branch) and forms an infinite
flip-flop with empty-branch removal.  It reports and prints the command.

Their absence is the design, not an omission.  Adding them to this protocol is
how the invariants get violated, so do not.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from stackem.model import PullRequest, PullRequestState, RepoSettings

__all__ = ["ForgeError", "Provider", "select_pull_request"]


class ForgeError(RuntimeError):
    """The forge could not answer: auth, network, rate limit, or a 4xx."""


@runtime_checkable
class Provider(Protocol):
    """Everything sync is allowed to ask a forge for."""

    def default_branch(self) -> str:
        """The repository's default branch -- the last trunk fallback (sec 6.6)."""

    def repo_settings(self) -> RepoSettings:
        """Repository settings, including ``delete_branch_on_merge`` (sec 6.3)."""

    def list_open_pull_requests(self) -> list[PullRequest]:
        """Every OPEN pull request in the repository.

        CLAUDE.md invariant 18: do not implement this as
        ``--state all --limit 100`` -- in an active repo the window fills with
        closed PRs and the stack's open PRs fall outside it.  Page through the
        open ones.

        Invariant 17: fork pull requests must come back marked
        ``is_cross_repository`` so they can be excluded.
        """

    def get_pull_request_for_branch(self, branch: str) -> PullRequest | None:
        """The pull request that represents ``branch``, or None.

        Branch to PR is not one-to-one (invariant 16): index by head ref with
        precedence **open > most recent merged > closed**, and exclude fork PRs
        (invariant 17).  :func:`select_pull_request` implements exactly that;
        implementations should use it so every forge agrees.
        """

    def retarget(self, pr_number: int, new_base: str) -> None:
        """Point a pull request's base at ``new_base``.

        The PR base IS the parent record (invariant 1), so this is the whole of
        reparenting.  It is idempotent, and it must happen before anything is
        deleted (invariant 15).
        """

    def branch_exists_on_remote(self, branch: str) -> bool:
        """Whether the branch still exists on the forge.

        Needed to classify a closed PR as orphaned (SPEC.md sec 5.1): closed, not
        merged, and its head branch gone.
        """

    def repo_slug(self) -> str:
        """``owner/name``, for printing the rescue commands of SPEC.md sec 6.3."""


def select_pull_request(
    pull_requests: Iterable[PullRequest], branch: str
) -> PullRequest | None:
    """Pick the pull request that represents ``branch``.

    CLAUDE.md invariant 16: precedence is open > most recent merged > closed.
    CLAUDE.md invariant 17: fork pull requests are excluded outright -- a fork PR
    whose head ref collides with a local branch name would otherwise be indexed
    as that branch's PR and retargeted.

    Ties (no ``merged_at``, several closed PRs) fall back to the highest number,
    which is the most recently created.
    """
    candidates = [
        pr
        for pr in pull_requests
        if pr.head == branch and not pr.is_cross_repository
    ]
    if not candidates:
        return None

    def recency(pr: PullRequest) -> tuple[float, int]:
        stamp = pr.merged_at or pr.closed_at or pr.updated_at
        return (stamp.timestamp() if stamp else float("-inf"), pr.number)

    for state in (PullRequestState.OPEN, PullRequestState.MERGED, PullRequestState.CLOSED):
        matching = [pr for pr in candidates if pr.state is state]
        if matching:
            return max(matching, key=recency)
    return None
