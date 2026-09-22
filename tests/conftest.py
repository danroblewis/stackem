"""Fixtures every stackem test can use.

``sandbox``            a bare origin + a clone, one commit on main, pushed
``stacked_sandbox``    the same sandbox with feat-a/feat-b/feat-c pushed
``mock_github``        a running mock GitHub bound to that sandbox's origin
``mock_github_memory`` a running mock GitHub with no repository behind it
``conflicted``         a sandbox stopped mid-rebase, with the CONFLICT result
``git_env``            the isolated git environment the sandbox uses

``mock_github`` and ``stacked_sandbox`` both build on ``sandbox``, so a test can
ask for any combination of them and get one consistent world: branches the mock
creates appear in the bare repo, and branches the sandbox pushes are visible to
the mock.

See tests/harness.py for what a Sandbox can do.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from stackem.forge.mock_server import MockGitHub, MockGitHubServer
from stackem.gitx import RebaseOutcome, RebaseResult
from tests.harness import GIT_ENV, Sandbox, make_sandbox


@pytest.fixture
def git_env() -> dict[str, str]:
    return dict(GIT_ENV)


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    """A real bare origin plus a real clone, with one commit on the trunk."""
    return make_sandbox(tmp_path / "world")


@pytest.fixture
def mock_github_memory():
    """A mock GitHub with no repository behind it.

    Use it when the test only cares about pull request bookkeeping -- it needs
    no git at all, so it is much faster than ``mock_github``.
    """
    with MockGitHubServer(MockGitHub(repo="acme/app", default_branch="main")) as server:
        yield server


@pytest.fixture
def stacked_sandbox(sandbox: Sandbox) -> Sandbox:
    """The common case: main <- feat-a <- feat-b <- feat-c, all pushed."""
    sandbox.make_stack(["feat-a", "feat-b", "feat-c"])
    return sandbox


@pytest.fixture
def mock_github(sandbox: Sandbox):
    """A mock GitHub for ``acme/app``, backed by the sandbox's bare origin."""
    api = MockGitHub(
        repo="acme/app",
        default_branch=sandbox.trunk,
        repo_path=sandbox.origin,
        env=sandbox.env,
    )
    with MockGitHubServer(api) as server:
        yield server


@dataclass
class ConflictedRebase:
    """A sandbox parked mid-rebase, exactly where sync leaves one (SPEC.md sec 8).

    ``feat`` was branched off the trunk and both sides edited ``f.txt``; the
    rebase of ``feat`` onto the moved trunk stopped on the conflict.  git is in
    its ordinary rebase state, so ``git rebase --abort`` or a staged resolution
    plus ``rebase_continue()`` both work.
    """

    sb: Sandbox
    result: RebaseResult
    branch: str
    onto: str
    fork: str
    orig_head: str


@pytest.fixture
def conflicted(sandbox: Sandbox) -> ConflictedRebase:
    sb = sandbox
    sb.commit("base f", files={"f.txt": "base\n"})
    sb.push(sb.trunk)
    sb.create_branch("feat")
    sb.commit("feat edits f", files={"f.txt": "feat\n"})
    sb.checkout(sb.trunk)
    sb.commit("trunk edits f", files={"f.txt": "trunk\n"})
    orig_head = sb.sha("feat")
    fork = sb.git.merge_base(sb.trunk, "feat")
    onto = sb.sha(sb.trunk)
    result = sb.git.rebase_onto(onto, fork, "feat")
    assert result.outcome is RebaseOutcome.CONFLICT, "the fixture must actually conflict"
    return ConflictedRebase(
        sb=sb, result=result, branch="feat", onto=onto, fork=fork, orig_head=orig_head
    )
