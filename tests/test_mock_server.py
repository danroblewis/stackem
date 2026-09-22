"""stackem.forge.mock_server -- the in-process mock GitHub.

SPEC.md sec 10: "The mock must reproduce the verified behaviors ... or the suite
passes while reality breaks."  Every verified behavior gets a test here, and each
test names the section that verified it.

The real GitHub provider will be pointed at this server, so these tests speak HTTP
rather than calling the Python API where the HTTP shape is the thing under test.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from stackem.forge import select_pull_request
from stackem.forge.mock_server import MockGitHub, MockGitHubError, MockGitHubServer
from stackem.model import PullRequest, PullRequestState


# --------------------------------------------------------------------------
# a tiny HTTP client, so the tests speak the same protocol the provider will
# --------------------------------------------------------------------------

def call(server, method, path, *, params=None, body=None):
    url = server.url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, method=method, data=data)
    request.add_header("Authorization", "Bearer test-token")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            parsed = json.loads(payload) if payload else None
            return response.status, parsed, dict(response.headers)
    except urllib.error.HTTPError as err:
        payload = err.read()
        parsed = json.loads(payload) if payload else None
        return err.code, parsed, dict(err.headers)


def get(server, path, **params):
    return call(server, "GET", path, params=params or None)


# --------------------------------------------------------------------------
# repository metadata
# --------------------------------------------------------------------------

def test_repo_endpoint_exposes_default_branch_and_delete_branch_on_merge(mock_github):
    # SPEC.md sec 6.3: warn when delete_branch_on_merge is true; sec 6.6: trunk.
    status, repo, _ = get(mock_github, "/repos/acme/app")
    assert status == 200
    assert repo["default_branch"] == "main"
    assert repo["delete_branch_on_merge"] is False
    mock_github.api.delete_branch_on_merge = True
    _, repo, _ = get(mock_github, "/repos/acme/app")
    assert repo["delete_branch_on_merge"] is True


def test_the_api_v3_prefix_is_accepted_too(mock_github):
    status, repo, _ = get(mock_github, "/api/v3/repos/acme/app")
    assert status == 200 and repo["full_name"] == "acme/app"


def test_an_unknown_path_is_a_github_shaped_404(mock_github):
    status, body, _ = get(mock_github, "/repos/acme/app/nonsense")
    assert status == 404
    assert body["message"] == "Not Found"


def test_requests_are_recorded_for_assertions(mock_github):
    get(mock_github, "/repos/acme/app/pulls", state="open", per_page="100")
    recorded = mock_github.requests[-1]
    assert recorded.method == "GET"
    assert recorded.path == "/repos/acme/app/pulls"
    assert recorded.query["state"] == ["open"]
    assert recorded.headers.get("Authorization") == "Bearer test-token"


# --------------------------------------------------------------------------
# listing pull requests
# --------------------------------------------------------------------------

def test_open_pull_requests_are_listed_over_http(mock_github, stacked_sandbox):
    api = mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    status, prs, _ = get(mock_github, "/repos/acme/app/pulls", state="open")
    assert status == 200
    assert [(p["number"], p["head"]["ref"], p["base"]["ref"]) for p in prs] == [
        (2, "feat-b", "feat-a"),
        (1, "feat-a", "main"),
    ]
    assert prs[0]["head"]["sha"] == stacked_sandbox.sha("feat-b")
    assert prs[0]["state"] == "open"
    assert prs[0]["merged_at"] is None


def test_listing_can_be_filtered_by_head_and_base(mock_github):
    api = mock_github.api
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-b", "feat-a")
    _, prs, _ = get(mock_github, "/repos/acme/app/pulls", head="acme:feat-b")
    assert [p["number"] for p in prs] == [2]
    _, prs, _ = get(mock_github, "/repos/acme/app/pulls", base="main")
    assert [p["number"] for p in prs] == [1]


def test_a_merged_pull_request_reports_state_closed_with_merged_at(mock_github):
    # GitHub reality the provider must not get wrong: "merged" is not a REST state.
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    api.squash_merge(pr.number)
    status, body, _ = get(mock_github, f"/repos/acme/app/pulls/{pr.number}")
    assert status == 200
    assert body["state"] == "closed"
    assert body["merged"] is True
    assert body["merged_at"] is not None
    assert body["merge_commit_sha"]
    _, open_prs, _ = get(mock_github, "/repos/acme/app/pulls", state="open")
    assert open_prs == []


def test_state_all_with_a_full_window_hides_an_open_pull_request(mock_github_memory):
    # CLAUDE.md invariant 18 / SPEC.md sec 5.1: do not rely on --state all --limit 100.
    api = mock_github_memory.api
    stack_pr = api.open_pull_request("feat-a", "main")
    for i in range(120):
        noise = api.open_pull_request(f"noise-{i}", "main")
        api.close_pull_request(noise.number)
    _, window, _ = get(mock_github_memory, "/repos/acme/app/pulls", state="all", per_page="100", page="1")
    assert len(window) == 100
    assert stack_pr.number not in [p["number"] for p in window]
    # ...while the query sync actually uses still finds it.
    _, open_prs, _ = get(mock_github_memory, "/repos/acme/app/pulls", state="open")
    assert [p["number"] for p in open_prs] == [stack_pr.number]


def test_listing_is_paginated_with_link_headers(mock_github_memory):
    api = mock_github_memory.api
    for i in range(5):
        api.open_pull_request(f"b{i}", "main")
    status, page_one, headers = get(mock_github_memory, "/repos/acme/app/pulls", per_page="2", page="1")
    assert status == 200 and len(page_one) == 2
    assert 'rel="next"' in headers["Link"]
    _, page_three, headers = get(mock_github_memory, "/repos/acme/app/pulls", per_page="2", page="3")
    assert len(page_three) == 1
    assert "Link" not in headers or 'rel="next"' not in headers.get("Link", "")


def test_per_page_is_capped_at_a_hundred_like_github(mock_github_memory):
    api = mock_github_memory.api
    for i in range(150):
        api.open_pull_request(f"b{i}", "main")
    _, page, _ = get(mock_github_memory, "/repos/acme/app/pulls", per_page="500")
    assert len(page) == 100


# --------------------------------------------------------------------------
# one branch, several pull requests (SPEC.md sec 5.1)
# --------------------------------------------------------------------------

def test_one_branch_may_have_several_pull_requests(mock_github):
    api = mock_github.api
    first = api.open_pull_request("feat-a", "main")
    api.close_pull_request(first.number)
    second = api.open_pull_request("feat-a", "main")
    api.squash_merge(second.number)
    third = api.open_pull_request("feat-a", "main")
    _, prs, _ = get(mock_github, "/repos/acme/app/pulls", state="all", head="acme:feat-a")
    assert sorted(p["number"] for p in prs) == [first.number, second.number, third.number]


def test_select_pull_request_prefers_open_then_most_recent_merged():
    # CLAUDE.md invariant 16: open > most recent merged > closed.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closed = PullRequest(number=1, head="feat-a", base="main", state=PullRequestState.CLOSED)
    old_merge = PullRequest(
        number=2, head="feat-a", base="main", state=PullRequestState.MERGED, merged_at=now
    )
    new_merge = PullRequest(
        number=3,
        head="feat-a",
        base="main",
        state=PullRequestState.MERGED,
        merged_at=now + timedelta(days=1),
    )
    open_pr = PullRequest(number=4, head="feat-a", base="main", state=PullRequestState.OPEN)
    other = PullRequest(number=5, head="feat-b", base="main", state=PullRequestState.OPEN)

    pool = [closed, old_merge, new_merge, open_pr, other]
    assert select_pull_request(pool, "feat-a") is open_pr
    assert select_pull_request([closed, old_merge, new_merge], "feat-a") is new_merge
    assert select_pull_request([closed], "feat-a") is closed
    assert select_pull_request(pool, "feat-z") is None


def test_select_pull_request_excludes_fork_pull_requests():
    # CLAUDE.md invariant 17: a fork PR with a colliding head ref must not be indexed.
    fork = PullRequest(
        number=1,
        head="feat-a",
        base="main",
        state=PullRequestState.OPEN,
        is_cross_repository=True,
        head_repository_owner="stranger",
    )
    assert select_pull_request([fork], "feat-a") is None
    ours = PullRequest(number=2, head="feat-a", base="main", state=PullRequestState.CLOSED)
    assert select_pull_request([fork, ours], "feat-a") is ours


def test_cross_repository_pull_requests_are_distinguishable_over_http(mock_github):
    # SPEC.md sec 5.1: head.repo tells a fork PR from one of ours.
    api = mock_github.api
    ours = api.open_pull_request("feat-a", "main")
    theirs = api.open_pull_request("feat-a", "main", head_repo="stranger/app")
    _, prs, _ = get(mock_github, "/repos/acme/app/pulls", state="open")
    by_number = {p["number"]: p for p in prs}
    assert by_number[ours.number]["head"]["repo"]["full_name"] == "acme/app"
    assert by_number[ours.number]["head"]["label"] == "acme:feat-a"
    assert by_number[theirs.number]["head"]["repo"]["full_name"] == "stranger/app"
    assert by_number[theirs.number]["head"]["label"] == "stranger:feat-a"
    assert by_number[theirs.number]["base"]["repo"]["full_name"] == "acme/app"


def test_a_fork_pull_request_whose_head_exists_nowhere_can_still_be_opened(mock_github):
    """CLAUDE.md invariant 17, in its realistic shape.

    A stranger's branch is not in this repository at all, so the mock has no
    object to point ``refs/pull/N/head`` at -- and neither does GitHub, which is
    part of why fork pull requests are excluded rather than supported.  Opening
    one must not blow up the repository the mock is bound to.
    """
    api = mock_github.api
    fork = api.open_pull_request("their-branch", "main", head_repo="stranger/app")

    assert fork.is_cross_repository is True
    assert api.branch_exists("their-branch") is False
    assert api.pull_ref(fork.number) == fork.head_sha  # bookkeeping only
    _, payload, _ = get(mock_github, f"/repos/acme/app/pulls/{fork.number}")
    assert payload["head"]["repo"]["full_name"] == "stranger/app"


# --------------------------------------------------------------------------
# branch deletion closes pull requests (SPEC.md sec 6.3, CLAUDE.md invariant 13)
# --------------------------------------------------------------------------

def test_deleting_a_branch_closes_every_pr_referencing_it_as_head_or_base(mock_github):
    api = mock_github.api
    parent = api.open_pull_request("feat-a", "main")
    child = api.open_pull_request("feat-b", "feat-a")
    grandchild = api.open_pull_request("feat-c", "feat-b")

    closed = api.delete_branch("feat-a")
    assert sorted(closed) == sorted([parent.number, child.number])

    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{parent.number}")
    assert body["state"] == "closed" and body["merged"] is False
    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{child.number}")
    assert body["state"] == "closed"
    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{grandchild.number}")
    assert body["state"] == "open"


def test_deleting_a_branch_over_http_closes_the_same_pull_requests(mock_github):
    api = mock_github.api
    parent = api.open_pull_request("feat-a", "main")
    child = api.open_pull_request("feat-b", "feat-a")
    status, _, _ = call(mock_github, "DELETE", "/repos/acme/app/git/refs/heads/feat-a")
    assert status == 204
    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{child.number}")
    assert body["state"] == "closed"
    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{parent.number}")
    assert body["state"] == "closed"


def test_delete_branch_on_merge_orphans_the_child_pull_request(mock_github):
    # SPEC.md sec 12: with the setting on, every merge orphans a child PR.
    api = mock_github.api
    api.delete_branch_on_merge = True
    parent = api.open_pull_request("feat-a", "main")
    child = api.open_pull_request("feat-b", "feat-a")
    api.squash_merge(parent.number)
    assert api.branch_exists("feat-a") is False
    _, body, _ = get(mock_github, f"/repos/acme/app/pulls/{child.number}")
    assert body["state"] == "closed"


def test_reopening_fails_with_422_while_the_head_branch_is_missing(mock_github):
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    api.delete_branch("feat-b")
    status, body, _ = call(
        mock_github, "PATCH", f"/repos/acme/app/pulls/{pr.number}", body={"state": "open"}
    )
    assert status == 422
    assert "feat-b branch has been deleted" in body["message"]
    with pytest.raises(MockGitHubError):
        api.reopen_pull_request(pr.number)


def test_reopening_fails_with_422_while_the_base_branch_is_missing(mock_github):
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    api.delete_branch("feat-a")
    status, body, _ = call(
        mock_github, "PATCH", f"/repos/acme/app/pulls/{pr.number}", body={"state": "open"}
    )
    assert status == 422
    assert "feat-a branch has been deleted" in body["message"]


def test_reopening_succeeds_once_both_branches_are_restored(mock_github, stacked_sandbox):
    # SPEC.md sec 6.3: the rescue, verified end to end.
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    api.delete_branch("feat-a")
    api.delete_branch("feat-b")
    api.create_branch("feat-a", stacked_sandbox.sha("feat-a"))
    api.create_branch("feat-b", stacked_sandbox.sha("feat-b"))
    status, body, _ = call(
        mock_github, "PATCH", f"/repos/acme/app/pulls/{pr.number}", body={"state": "open"}
    )
    assert status == 200 and body["state"] == "open"


def test_refs_pull_n_head_persists_after_the_branch_is_deleted(mock_github, stacked_sandbox):
    # SPEC.md sec 6.3: GitHub retains the commits under refs/pull/N/head forever.
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    tip = stacked_sandbox.sha("feat-b")
    api.delete_branch("feat-b")
    assert api.branch_exists("feat-b") is False
    assert api.pull_ref(pr.number) == tip

    status, body, _ = get(mock_github, f"/repos/acme/app/git/ref/pull/{pr.number}/head")
    assert status == 200 and body["object"]["sha"] == tip
    status, _, _ = get(mock_github, "/repos/acme/app/git/ref/heads/feat-b")
    assert status == 404

    # and the rescue fetch works against the real bare repo behind the mock
    stacked_sandbox.git.fetch("origin", refspecs=[f"refs/pull/{pr.number}/head:rescue-head"])
    assert stacked_sandbox.sha("rescue-head") == tip


# --------------------------------------------------------------------------
# squash merge (SPEC.md sec 6.4)
# --------------------------------------------------------------------------

def test_squash_merge_mints_a_new_commit_with_a_different_patch_id(mock_github, sandbox):
    sandbox.make_stack(["feat-a"], commits=2)
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    originals = {sandbox.patch_id(sha) for sha in sandbox.git.rev_list("main..feat-a")}

    merge_sha = api.squash_merge(pr.number)

    sandbox.fetch()
    assert merge_sha == sandbox.sha("origin/main")
    assert merge_sha not in [sandbox.sha("feat-a"), sandbox.sha("main")]
    assert sandbox.patch_id(merge_sha) not in originals
    assert sandbox.git.is_ancestor("feat-a", "origin/main") is False
    assert api.pull_request(pr.number).merge_commit_sha == merge_sha


def test_squash_merge_over_http_marks_the_pull_request_merged(mock_github, stacked_sandbox):
    api = mock_github.api
    pr = api.open_pull_request("feat-a", "main")
    status, body, _ = call(
        mock_github,
        "PUT",
        f"/repos/acme/app/pulls/{pr.number}/merge",
        body={"merge_method": "squash"},
    )
    assert status == 200 and body["merged"] is True
    assert body["sha"] == api.pull_request(pr.number).merge_commit_sha


# --------------------------------------------------------------------------
# retargeting and branch existence (what Provider.retarget / branch_exists use)
# --------------------------------------------------------------------------

def test_patching_the_base_retargets_the_pull_request(mock_github):
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    status, body, _ = call(
        mock_github, "PATCH", f"/repos/acme/app/pulls/{pr.number}", body={"base": "main"}
    )
    assert status == 200
    assert body["base"]["ref"] == "main"
    assert api.pull_request(pr.number).base == "main"


def test_retargeting_onto_a_missing_branch_is_rejected(mock_github):
    api = mock_github.api
    pr = api.open_pull_request("feat-b", "feat-a")
    status, body, _ = call(
        mock_github, "PATCH", f"/repos/acme/app/pulls/{pr.number}", body={"base": "ghost"}
    )
    assert status == 422
    assert "ghost" in body["message"]
    assert api.pull_request(pr.number).base == "feat-a"


def test_the_branch_endpoint_answers_branch_exists_on_remote(mock_github, stacked_sandbox):
    # SPEC.md sec 5.1: needed to classify a closed PR as orphaned.
    status, body, _ = get(mock_github, "/repos/acme/app/branches/feat-a")
    assert status == 200 and body["commit"]["sha"] == stacked_sandbox.sha("feat-a")
    status, _, _ = get(mock_github, "/repos/acme/app/branches/ghost")
    assert status == 404


def test_the_mock_tracks_the_real_bare_repository_it_is_bound_to(mock_github, stacked_sandbox):
    api = mock_github.api
    assert api.branch_exists("feat-c") is True
    stacked_sandbox.delete_remote_branch("feat-c")
    assert api.branch_exists("feat-c") is False
    api.create_branch("straight-from-the-mock", stacked_sandbox.sha("feat-b"))
    assert stacked_sandbox.origin_sha("straight-from-the-mock") == stacked_sandbox.sha("feat-b")


def test_the_mock_works_without_a_repository_behind_it(tmp_path):
    # Unit tests that only care about PR bookkeeping should not need git at all.
    api = MockGitHub(repo="acme/app", default_branch="main")
    api.create_branch("feat-a")
    pr = api.open_pull_request("feat-a", "main")
    assert api.pull_ref(pr.number)
    assert api.squash_merge(pr.number)
    assert api.pull_request(pr.number).state == "closed"


def test_the_server_can_be_used_as_a_context_manager():
    api = MockGitHub(repo="acme/app", default_branch="trunk")
    with MockGitHubServer(api) as server:
        status, repo, _ = get(server, "/repos/acme/app")
        assert status == 200 and repo["default_branch"] == "trunk"
        url = server.url
    with pytest.raises(OSError):
        urllib.request.urlopen(url + "/repos/acme/app", timeout=1)
