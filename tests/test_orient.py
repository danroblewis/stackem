"""stackem.orient -- working out what the stack IS (SPEC.md sec 5.1).

Everything here runs against real git repositories built by tests/harness.py.
Only the forge is faked, and it is faked over HTTP by the shared mock server, so
the provider these tests drive speaks the same protocol the real one will.

The cases that earn their keep, each naming what it protects:

* trunk detection when ``origin/HEAD`` is unset (SPEC.md sec 6.6) -- and that the
  read-only path never writes the ref (CLAUDE.md invariant 5)
* the stack boundary: the trunk is never a member, so an unrelated branch rooted
  on the trunk is excluded while a sibling *within* the stack is included
  (CLAUDE.md invariant 8)
* pull request indexing: two PRs on one branch (invariant 16), a fork PR whose
  head ref collides with a local branch (invariant 17), and never a single
  "list every pull request" call (invariant 18)
* a branch with no PR falling back to topology, for this run only (invariant 1)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from stackem.forge import ForgeError, Provider, select_pull_request
from stackem.model import BranchState, ParentSource, PullRequest, PullRequestState, RepoSettings
from stackem.orient import (
    Orientation,
    OrientationError,
    TrunkSource,
    index_pull_requests,
    infer_parent,
    is_contained_in_trunk,
    orient,
    resolve_trunk,
)


# --------------------------------------------------------------------------
# a provider that speaks HTTP to the mock server
#
# The real GitHub provider belongs to another agent and does not exist yet, so
# these tests drive their own client rather than stubbing the Provider protocol.
# It is deliberately ordinary: page through OPEN pull requests (never
# `--state all --limit 100`, invariant 18), and query per branch, filtered by
# head ref, for the merged/closed ones.
# --------------------------------------------------------------------------

class HttpProvider:
    """A minimal, honest Provider implementation against the mock server.

    ``strict=False`` makes :meth:`get_pull_request_for_branch` sloppy -- it drops
    the ``head=owner:branch`` filter and returns the first pull request with a
    matching head ref, fork pull requests included.  Orientation must still
    exclude them (invariant 17); it does not get to trust the provider.
    """

    def __init__(self, base_url: str, repo: str = "acme/app", *, strict: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.repo = repo
        self.owner = repo.partition("/")[0]
        self.strict = strict

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, url: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, method=method, data=data)
        request.add_header("Authorization", "Bearer test-token")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None), dict(response.headers)
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, (json.loads(raw) if raw else None), dict(err.headers)

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _get(self, path: str, **params):
        status, payload, _ = self._request("GET", self._url(path, params))
        if status >= 400:
            raise ForgeError(f"GET {path} -> {status}: {payload}")
        return payload

    def _get_optional(self, path: str, **params):
        status, payload, _ = self._request("GET", self._url(path, params))
        if status == 404:
            return None
        if status >= 400:
            raise ForgeError(f"GET {path} -> {status}: {payload}")
        return payload

    def _paged(self, path: str, **params) -> list[dict]:
        url = self._url(path, {**params, "per_page": 100})
        items: list[dict] = []
        while url:
            status, payload, headers = self._request("GET", url)
            if status >= 400:
                raise ForgeError(f"GET {url} -> {status}: {payload}")
            items.extend(payload or [])
            url = _next_link(headers.get("Link"))
        return items

    # -- Provider ----------------------------------------------------------

    def default_branch(self) -> str:
        return self._get(f"/repos/{self.repo}")["default_branch"]

    def repo_settings(self) -> RepoSettings:
        payload = self._get(f"/repos/{self.repo}")
        return RepoSettings(
            default_branch=payload["default_branch"],
            delete_branch_on_merge=payload["delete_branch_on_merge"],
        )

    def list_open_pull_requests(self) -> list[PullRequest]:
        payload = self._paged(f"/repos/{self.repo}/pulls", state="open")
        return [_parse_pull_request(item, self.repo) for item in payload]

    def get_pull_request_for_branch(self, branch: str) -> PullRequest | None:
        if self.strict:
            payload = self._paged(
                f"/repos/{self.repo}/pulls", state="all", head=f"{self.owner}:{branch}"
            )
            return select_pull_request(
                [_parse_pull_request(item, self.repo) for item in payload], branch
            )
        payload = self._paged(f"/repos/{self.repo}/pulls", state="all")
        for item in payload:
            if item["head"]["ref"] == branch:
                return _parse_pull_request(item, self.repo)
        return None

    def retarget(self, pr_number: int, new_base: str) -> None:
        status, payload, _ = self._request(
            "PATCH", self._url(f"/repos/{self.repo}/pulls/{pr_number}"), {"base": new_base}
        )
        if status >= 400:
            raise ForgeError(f"PATCH pulls/{pr_number} -> {status}: {payload}")

    def branch_exists_on_remote(self, branch: str) -> bool:
        return self._get_optional(f"/repos/{self.repo}/branches/{branch}") is not None

    def repo_slug(self) -> str:
        return self.repo


def _next_link(header: str | None) -> str | None:
    for part in (header or "").split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1]:
            return section[0].strip().strip("<>")
    return None


def _timestamp(value: str | None):
    if not value:
        return None
    from datetime import datetime, timezone

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _parse_pull_request(payload: dict, repo: str) -> PullRequest:
    head, base = payload["head"], payload["base"]
    head_repo = (head.get("repo") or {}).get("full_name")
    merged_at = _timestamp(payload.get("merged_at"))
    if merged_at is not None:
        # A merged PR is REST state "closed" with merged_at set.
        state = PullRequestState.MERGED
    elif payload["state"] == "open":
        state = PullRequestState.OPEN
    else:
        state = PullRequestState.CLOSED
    return PullRequest(
        number=payload["number"],
        head=head["ref"],
        base=base["ref"],
        state=state,
        title=payload.get("title", ""),
        url=payload.get("html_url", ""),
        is_cross_repository=head_repo != repo,
        head_repository_owner=(head.get("user") or {}).get("login"),
        head_sha=head.get("sha"),
        merge_commit_sha=payload.get("merge_commit_sha"),
        merged_at=merged_at,
        closed_at=_timestamp(payload.get("closed_at")),
        updated_at=_timestamp(payload.get("updated_at")),
        draft=payload.get("draft", False),
    )


def provider_for(server, **kwargs) -> HttpProvider:
    return HttpProvider(server.url, repo=server.api.repo_full_name, **kwargs)


def test_the_test_provider_satisfies_the_protocol(mock_github_memory):
    assert isinstance(provider_for(mock_github_memory), Provider)


# --------------------------------------------------------------------------
# trunk detection -- SPEC.md sec 6.6
# --------------------------------------------------------------------------

def test_trunk_comes_from_origin_head(sandbox):
    resolved = resolve_trunk(sandbox.git)
    assert resolved.name == "main"
    assert resolved.source is TrunkSource.ORIGIN_HEAD
    assert resolved.ref == "origin/main"


def test_trunk_detection_falls_back_to_remote_set_head(sandbox):
    # SPEC.md sec 6.6: **Verified** that refs/remotes/origin/HEAD can be unset.
    sandbox.unset_origin_head()
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD") is None

    resolved = resolve_trunk(sandbox.git)

    assert resolved.name == "main"
    assert resolved.source is TrunkSource.REMOTE_SET_HEAD
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD") == "refs/remotes/origin/main"


def test_read_only_trunk_detection_never_writes_the_ref(sandbox, mock_github):
    # CLAUDE.md invariant 5: `stackem` with no arguments is read-only.  It must
    # not run `git remote set-head`, which WRITES a ref.
    sandbox.unset_origin_head()

    resolved = resolve_trunk(sandbox.git, provider_for(mock_github), allow_ref_write=False)

    assert resolved.name == "main"
    assert resolved.source is TrunkSource.FORGE
    assert sandbox.git.symbolic_ref("refs/remotes/origin/HEAD") is None


def test_trunk_falls_back_to_the_forge_when_set_head_fails(sandbox, mock_github):
    # An unreachable remote is the ordinary way step 2 fails: no network, or the
    # remote url is wrong.  The API is the last word (SPEC.md sec 6.6).
    sandbox.unset_origin_head()
    sandbox.git.run("remote", "set-url", "origin", str(sandbox.root / "missing.git"))

    resolved = resolve_trunk(sandbox.git, provider_for(mock_github))

    assert resolved.name == "main"
    assert resolved.source is TrunkSource.FORGE


def test_trunk_resolution_raises_when_nothing_can_answer(sandbox):
    sandbox.unset_origin_head()
    sandbox.git.run("remote", "set-url", "origin", str(sandbox.root / "missing.git"))

    with pytest.raises(OrientationError):
        resolve_trunk(sandbox.git, provider=None)


def test_an_explicit_trunk_is_used_as_given(sandbox):
    resolved = resolve_trunk(sandbox.git, trunk="develop")
    assert resolved.name == "develop"
    assert resolved.source is TrunkSource.EXPLICIT


# --------------------------------------------------------------------------
# the stack boundary -- CLAUDE.md invariant 8, SPEC.md sec 5.1
# --------------------------------------------------------------------------

def test_unrelated_branches_rooted_on_trunk_are_excluded(stacked_sandbox, mock_github):
    # The correction in SPEC.md sec 5.1: walking "up" from the trunk collects
    # every branch rooted on it -- scratch branches, WIP, unrelated features --
    # and sync would rebase and force-push all of them.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")

    sb.checkout(sb.trunk)
    sb.create_branch("scratch")  # no PR, rooted on the trunk
    sb.commit("scratch: wip")
    sb.checkout(sb.trunk)
    sb.create_branch("other-feature")
    sb.commit("other: work")
    sb.push("other-feature")
    api.open_pull_request("other-feature", "main")  # its own stack, off the trunk
    sb.checkout("feat-c")

    result = orient(sb.git, provider_for(mock_github))

    assert result.member_names == ["feat-a", "feat-b", "feat-c"]
    assert "scratch" not in result.member_names
    assert "other-feature" not in result.member_names


def test_the_trunk_is_never_a_member(stacked_sandbox, mock_github):
    api = mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")

    result = orient(stacked_sandbox.git, provider_for(mock_github))

    assert result.stack.trunk == "main"
    assert "main" not in result.member_names
    assert result.stack.branches[0].parent == "main"


def test_a_sibling_within_the_stack_is_included(stacked_sandbox, mock_github):
    # SPEC.md sec 5.1: "A sibling *within* the stack -- two branches off the same
    # parent -- is correctly included."
    sb, api = stacked_sandbox, mock_github.api
    sb.checkout("feat-a")
    sb.create_branch("feat-b2")
    sb.commit("feat-b2: c1")
    sb.push("feat-b2")
    sb.checkout("feat-c")
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")
    api.open_pull_request("feat-b2", "feat-a")

    result = orient(sb.git, provider_for(mock_github))

    assert set(result.member_names) == {"feat-a", "feat-b", "feat-b2", "feat-c"}
    assert result.branch("feat-b2").parent == "feat-a"
    assert sorted(result.branch("feat-a").children) == ["feat-b", "feat-b2"]


def test_a_sibling_without_a_pull_request_is_included_by_topology(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    sb.checkout("feat-b")
    sb.create_branch("feat-c2")
    sb.commit("feat-c2: c1")
    sb.checkout("feat-c")
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("feat-c2")
    assert "feat-c2" in result.member_names
    assert branch.parent == "feat-b"
    assert branch.parent_source is ParentSource.TOPOLOGY


def test_members_are_ordered_bottom_up(stacked_sandbox, mock_github):
    # model.Stack: branches are ordered BOTTOM-UP, the order the cascade walks.
    sb, api = stacked_sandbox, mock_github.api
    sb.checkout("feat-a")
    sb.create_branch("feat-b2")
    sb.commit("feat-b2: c1")
    sb.checkout("feat-c")
    for head, base in (("feat-a", "main"), ("feat-b", "feat-a"), ("feat-c", "feat-b")):
        api.open_pull_request(head, base)

    result = orient(sb.git, provider_for(mock_github))

    order = result.member_names
    assert set(order) == {"feat-a", "feat-b", "feat-b2", "feat-c"}
    for index, branch in enumerate(result.stack.branches):
        if branch.parent in order:
            assert order.index(branch.parent) < index, (
                f"{branch.parent} must precede {branch.name} in {order}"
            )
    assert order[0] == "feat-a", "the root of the stack comes first"


def test_head_on_the_trunk_yields_an_empty_stack(stacked_sandbox, mock_github):
    # Spine = HEAD down to the trunk; members = spine minus trunk.  Standing on
    # the trunk there is no spine, and collecting the trunk's children would be
    # exactly the bug invariant 8 exists to prevent.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    sb.checkout(sb.trunk)

    result = orient(sb.git, provider_for(mock_github))

    assert result.member_names == []
    assert result.stack.head == "main"


def test_a_parent_cycle_does_not_hang(stacked_sandbox, mock_github):
    # SPEC.md sec 12: there is no cycle detection on `stackem parent`, so two
    # PRs can point at each other.  Orientation must still terminate.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "feat-b")
    api.open_pull_request("feat-b", "feat-a")
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    assert set(result.member_names) <= {"feat-a", "feat-b", "feat-c"}
    assert "feat-b" in result.member_names


def test_head_is_recovered_from_an_in_progress_rebase(conflicted):
    # HEAD is detached mid-rebase; sync's phase 0 still has to derive the stack
    # to decide whether the rebase is its own (CLAUDE.md invariant 22).
    sb = conflicted.sb
    assert sb.git.current_branch() is None

    result = orient(sb.git)

    assert result.stack.head == "feat"
    assert result.member_names == ["feat"]


# --------------------------------------------------------------------------
# pull request indexing -- CLAUDE.md invariants 16, 17, 18
# --------------------------------------------------------------------------

def test_a_branch_with_two_pull_requests_prefers_the_open_one(stacked_sandbox, mock_github):
    # SPEC.md sec 5.1: **Verified** that one branch had two pull requests.
    # Precedence is open > most recent merged > closed.
    sb, api = stacked_sandbox, mock_github.api
    stale = api.open_pull_request("feat-b", "main")  # opened before feat-a existed
    api.close_pull_request(stale.number)
    current = api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-a", "main")
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("feat-b")
    assert branch.pull_request.number == current.number
    assert branch.parent == "feat-a"  # the closed PR's base=main must not win
    assert branch.parent_source is ParentSource.PULL_REQUEST


def test_a_merged_pull_request_beats_a_closed_one(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    merged = api.open_pull_request("feat-a", "main")
    api.squash_merge(merged.number)
    abandoned = api.open_pull_request("feat-a", "main")
    api.close_pull_request(abandoned.number)
    sb.fetch()
    sb.checkout("feat-a")

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("feat-a")
    assert branch.pull_request.number == merged.number
    assert branch.pull_request.state is PullRequestState.MERGED
    assert branch.state is BranchState.MERGED


def test_a_fork_pull_request_with_a_colliding_head_ref_is_ignored(stacked_sandbox, mock_github):
    # CLAUDE.md invariant 17.  A fork PR whose head ref collides with a local
    # branch name would otherwise be indexed as that branch's PR -- and
    # retargeted, on somebody else's repository.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    ours = api.open_pull_request("feat-b", "feat-a")
    theirs = api.open_pull_request("feat-b", "main", head_repo="stranger/app")
    assert theirs.is_cross_repository
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("feat-b")
    assert branch.pull_request.number == ours.number
    assert branch.parent == "feat-a"


def test_a_branch_whose_only_pull_request_is_a_fork_has_no_pull_request(
    stacked_sandbox, mock_github
):
    sb, api = stacked_sandbox, mock_github.api
    sb.checkout("feat-a")
    sb.create_branch("patch-1")
    sb.commit("patch-1: c1")
    sb.push("patch-1")
    sb.checkout("feat-c")
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")
    api.open_pull_request("patch-1", "main", head_repo="stranger/app")

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("patch-1")
    assert branch.pull_request is None
    assert branch.parent == "feat-a", "the fork PR's base=main must not become the parent"
    assert branch.parent_source is ParentSource.TOPOLOGY
    assert "patch-1" in result.member_names


def test_a_fork_pull_request_handed_over_by_the_provider_is_still_excluded(
    stacked_sandbox, mock_github
):
    # Orientation does not get to trust the provider on invariant 17: this one
    # drops the head-ref filter and returns fork pull requests.
    sb, api = stacked_sandbox, mock_github.api
    sb.checkout("feat-a")
    sb.create_branch("patch-1")
    sb.commit("patch-1: c1")
    # Pushed only so the mock can mint refs/pull/N/head for the fork PR below:
    # bound to a repository, MockGitHub.open_pull_request falls back to a
    # synthetic sha for a head ref the repository does not have, and writing
    # that ref then fails.  Nothing in this test depends on the push.
    sb.push("patch-1")
    sb.checkout("feat-c")
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")
    fork = api.open_pull_request("patch-1", "main", head_repo="stranger/app")
    api.close_pull_request(fork.number)  # keep it out of the open listing entirely

    lenient = provider_for(mock_github, strict=False)
    assert lenient.get_pull_request_for_branch("patch-1").is_cross_repository

    result = orient(sb.git, lenient)

    assert result.branch("patch-1").pull_request is None
    assert result.branch("patch-1").parent == "feat-a"


def test_pull_requests_are_never_read_from_one_list_them_all_call(stacked_sandbox, mock_github):
    # CLAUDE.md invariant 18: in an active repo a `--state all --limit 100`
    # window fills with closed PRs and the stack's open PR falls outside it.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")

    orient(sb.git, provider_for(mock_github))

    listings = [r for r in mock_github.requests if r.path.endswith("/pulls")]
    assert listings, "orientation must ask the forge about pull requests"
    for request in listings:
        state = request.query.get("state", ["open"])[0]
        head = request.query.get("head")
        assert state == "open" or head, f"unfiltered listing: {request.query}"


def test_an_open_pull_request_outside_the_first_page_is_still_indexed(mock_github_memory):
    # The same invariant from the other side: the open PR is the oldest of 121,
    # so a single page of 100 does not contain it.
    api = mock_github_memory.api
    wanted = api.open_pull_request("feat-x", "main")
    for index in range(120):
        api.open_pull_request(f"noise-{index}", "main")

    index = index_pull_requests(provider_for(mock_github_memory), ["feat-x"])

    assert index.get("feat-x").number == wanted.number


def test_only_branches_without_an_open_pull_request_are_queried_one_by_one(
    stacked_sandbox, mock_github
):
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")

    index = index_pull_requests(provider_for(mock_github), ["feat-a", "feat-b", "feat-c"])

    assert index.get("feat-a").base == "main"
    assert index.get("feat-c") is None
    assert index.queried == ("feat-c",)


# --------------------------------------------------------------------------
# parents -- CLAUDE.md invariant 1, SPEC.md sec 5.1
# --------------------------------------------------------------------------

def test_the_parent_is_the_pull_requests_base_branch(stacked_sandbox, mock_github):
    # Invariant 1.  Note feat-b's PR says its base is the TRUNK even though
    # topologically it sits on feat-a: the PR wins, because the PR base is the
    # parent record and retargeting it is the whole of reparenting.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "main")
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-b").parent == "main"
    assert result.branch("feat-b").parent_source is ParentSource.PULL_REQUEST
    assert "feat-a" not in result.member_names, "feat-a is not on feat-b's spine"


def test_a_branch_with_no_pull_request_falls_back_to_topology(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    # feat-c has no PR at all -- the common "just branched, not pushed" case.

    result = orient(sb.git, provider_for(mock_github))

    branch = result.branch("feat-c")
    assert branch.pull_request is None
    assert branch.parent == "feat-b"
    assert branch.parent_source is ParentSource.TOPOLOGY
    assert branch.state is BranchState.LIVE


def test_topology_inference_picks_the_nearest_ancestor(stacked_sandbox):
    sb = stacked_sandbox
    candidates = ["feat-a", "feat-b", "feat-c", "main"]
    assert infer_parent(sb.git, "feat-c", candidates, trunk="main") == "feat-b"
    assert infer_parent(sb.git, "feat-a", candidates, trunk="main") == "main"
    # A branch whose only ancestor is the trunk lands on the trunk.
    sb.checkout(sb.trunk)
    sb.create_branch("scratch")
    sb.commit("scratch: c1")
    assert infer_parent(sb.git, "scratch", candidates + ["scratch"], trunk="main") == "main"


def test_inferred_parents_are_marked_as_derived_for_this_run_only(stacked_sandbox, mock_github):
    # SPEC.md sec 5.1: inference is "used for that run only.  Nothing is
    # recorded."  The proof a test can hold: no refs and no config appear.
    sb = stacked_sandbox
    before_refs = sb.git.lines("for-each-ref", "--format=%(refname)")
    before_config = sb.git.lines("config", "--local", "--list")

    orient(sb.git, provider_for(mock_github))

    assert sb.git.lines("for-each-ref", "--format=%(refname)") == before_refs
    assert sb.git.lines("config", "--local", "--list") == before_config


def test_without_a_forge_every_parent_comes_from_topology(stacked_sandbox):
    # SPEC.md sec 12: offline, sync falls back to topology inference.
    result = orient(stacked_sandbox.git, provider=None)

    assert result.member_names == ["feat-a", "feat-b", "feat-c"]
    assert [b.parent for b in result.stack.branches] == ["main", "feat-a", "feat-b"]
    assert result.offline is True


def test_a_forge_failure_degrades_to_topology_rather_than_crashing(stacked_sandbox, mock_github):
    broken = provider_for(mock_github)
    broken.repo = "acme/missing"  # every request 404s

    result = orient(stacked_sandbox.git, broken)

    assert result.member_names == ["feat-a", "feat-b", "feat-c"]
    assert result.forge_error is not None
    assert result.branch("feat-b").parent_source is ParentSource.TOPOLOGY


def test_the_fork_point_is_the_parents_last_synced_state(stacked_sandbox, mock_github):
    # SPEC.md sec 2: merge-base(origin/<parent>, <branch>) -- never the parent's
    # local tip, which fails after an amend.
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")
    sb.amend("feat-a", content="late fix\n")

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-b").fork_point == sb.sha("origin/feat-a")
    assert result.branch("feat-b").fork_point == sb.git.merge_base("origin/feat-a", "feat-b")
    assert result.branch("feat-b").fork_point != sb.git.merge_base("feat-a", "feat-b")
    assert result.branch("feat-a").fork_point == sb.git.merge_base("origin/main", "feat-a")
    assert result.branch("feat-b").remote_sha == sb.sha("origin/feat-b")


# --------------------------------------------------------------------------
# classification -- SPEC.md sec 5.1
# --------------------------------------------------------------------------

def test_open_and_missing_pull_requests_are_both_live(stacked_sandbox, mock_github):
    api = mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")

    result = orient(stacked_sandbox.git, provider_for(mock_github))

    assert result.branch("feat-a").state is BranchState.LIVE
    assert result.branch("feat-c").state is BranchState.LIVE


def test_a_squash_merged_branch_is_classified_merged(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.squash_merge(pr.number)
    sb.fetch()

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-a").state is BranchState.MERGED
    assert result.branch("feat-b").state is BranchState.LIVE


def test_a_closed_pull_request_whose_branch_was_deleted_is_orphaned(stacked_sandbox, mock_github):
    # SPEC.md sec 5.1: orphaned == closed, not merged, head branch gone from the
    # remote.  sync reports it and never rescues it (invariant 14).
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.delete_branch("feat-b")  # closes the PR, GitHub-style (invariant 13)
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-b").state is BranchState.ORPHANED
    assert result.branch("feat-b").pull_request.state is PullRequestState.CLOSED


def test_a_closed_pull_request_whose_branch_survives_is_live(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    closed = api.open_pull_request("feat-b", "feat-a")
    api.close_pull_request(closed.number)
    sb.checkout("feat-b")

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-b").state is BranchState.LIVE


def test_offline_merge_detection_needs_the_unique_commit_guard(stacked_sandbox):
    # CLAUDE.md invariant 19 / SPEC.md sec 6.4: merge-tree alone reports ANY
    # branch with no unique commits as merged, including a fresh WIP branch.
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    sb.checkout(sb.trunk)
    sb.create_branch("behind")  # branched off the stale local trunk, no commits
    sb.fetch()

    assert is_contained_in_trunk(sb.git, "feat-a", trunk_ref="origin/main") is True
    assert is_contained_in_trunk(sb.git, "behind", trunk_ref="origin/main") is False
    assert is_contained_in_trunk(sb.git, "feat-b", trunk_ref="origin/main") is False


def test_offline_orientation_classifies_a_squash_merged_branch_as_merged(stacked_sandbox):
    # With no forge there is no PR state, so classification falls back to the
    # guarded merge-tree test (SPEC.md sec 3, "is B merged").
    sb = stacked_sandbox
    sb.squash_merge("feat-a")
    sb.fetch()

    result = orient(sb.git, provider=None)

    assert result.branch("feat-a").state is BranchState.MERGED
    assert result.branch("feat-b").state is BranchState.LIVE


def test_a_forge_answer_is_not_second_guessed_by_merge_tree(stacked_sandbox, mock_github):
    # The PR state is the authority when there is one (SPEC.md sec 3).  A branch
    # with an open PR whose content happens to be in the trunk stays live --
    # sync must not silently skip it in the cascade (invariant 6).
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    sb.squash_merge("feat-a")  # landed by hand, PR still open
    sb.fetch()

    result = orient(sb.git, provider_for(mock_github))

    assert result.branch("feat-a").state is BranchState.LIVE


# --------------------------------------------------------------------------
# shape of what orientation returns
# --------------------------------------------------------------------------

def test_orientation_reports_the_whole_derived_world(stacked_sandbox, mock_github):
    sb, api = stacked_sandbox, mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    api.open_pull_request("feat-c", "feat-b")

    result = orient(sb.git, provider_for(mock_github))

    assert isinstance(result, Orientation)
    assert result.stack.trunk == "main"
    assert result.stack.remote == "origin"
    assert result.stack.head == "feat-c"
    assert result.stack.trunk_remote_sha == sb.sha("origin/main")
    assert [b.name for b in result.stack.branches] == ["feat-a", "feat-b", "feat-c"]
    assert [b.sha for b in result.stack.branches] == [
        sb.sha("feat-a"), sb.sha("feat-b"), sb.sha("feat-c")
    ]
    assert result.branch("feat-a").children == ["feat-b"]
    assert result.branch("feat-c").children == []
    assert set(result.branches) >= {"main", "feat-a", "feat-b", "feat-c"}
