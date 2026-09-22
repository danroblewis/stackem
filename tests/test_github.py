"""stackem.forge.github -- the GitHub provider, driven over real HTTP.

SPEC.md sec 10: "Driving the real provider against a mock server rather than
stubbing the provider keeps the HTTP layer under test."  So nearly every test
here talks to the in-process mock GitHub from ``tests/conftest.py`` through a
socket, with the provider's own urllib (or its own ``gh`` subprocess) in the
middle.

Two things the mock deliberately cannot produce -- a pull request whose head
repository GitHub has already deleted, and a server that answers without a
``Link`` header -- use an injected stub transport instead.  That is the same
provider code path with the socket removed, not a stubbed provider.

Invariants under test here:

14  no create / close / delete / reopen / merge anywhere on the provider
16  branch -> pull request precedence: open > most recent merged > closed
17  fork pull requests are never indexed as a local branch's pull request
18  never one ``--state all --limit 100`` window
"""

from __future__ import annotations

import json
import stat
import sys

import pytest

from stackem.forge import ForgeError, Provider
from stackem.forge.github import (
    DEFAULT_BASE_URL,
    GhTransport,
    GitHubApiError,
    GitHubProvider,
    Response,
    RestTransport,
    api_base_url,
    parse_remote_url,
)
from stackem.model import PullRequestState, RepoSettings

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def rest_provider(server, **kwargs) -> GitHubProvider:
    """A provider aimed at a running mock, over plain REST."""
    kwargs.setdefault("base_url", server.url)
    kwargs.setdefault("token", "test-token")
    kwargs.setdefault("transport", "rest")
    return GitHubProvider(server.api.repo_full_name, **kwargs)


def pull_listings(server) -> list:
    """Every ``GET .../pulls`` the provider made."""
    return [
        request
        for request in server.requests
        if request.method == "GET" and request.path.endswith("/pulls")
    ]


def header(request, name: str, default=None):
    """One request header, matched loosely.

    urllib capitalizes header names on the way out (``X-github-api-version``),
    and GitHub, like every HTTP server, does not care.  Neither does this.
    """
    lowered = {key.lower(): value for key, value in request.headers.items()}
    return lowered.get(name.lower(), default)


def param(request, key: str, default=None):
    values = request.query.get(key)
    return values[0] if values else default


def assert_never_a_blind_window(server) -> None:
    """CLAUDE.md invariant 18, as an assertion.

    A listing is acceptable only if it asks for OPEN pull requests, or if it is
    narrowed to one head ref.  An unfiltered ``state=all`` page is exactly the
    query whose 100-row window fills with closed pull requests and hides the
    stack's own open ones.
    """
    for request in pull_listings(server):
        state = param(request, "state", "open")
        head = param(request, "head")
        assert state == "open" or head, (
            f"unfiltered listing: state={state!r} head={head!r} -- invariant 18"
        )


@pytest.fixture
def provider(mock_github_memory) -> GitHubProvider:
    return rest_provider(mock_github_memory)


@pytest.fixture
def api(mock_github_memory):
    return mock_github_memory.api


class StubTransport:
    """Canned answers, for the two shapes the mock cannot serve."""

    def __init__(self, *responses: Response, repeat: Response | None = None) -> None:
        self.responses = list(responses)
        self.repeat = repeat
        self.calls: list[tuple[str, str, dict, object]] = []

    def request(self, method, path, *, params=None, body=None) -> Response:
        self.calls.append((method, path, dict(params or {}), body))
        if self.responses:
            return self.responses.pop(0)
        if self.repeat is not None:
            return self.repeat
        raise AssertionError(f"unexpected request: {method} {path}")


def pull_json(number: int, head: str, base: str = "main", **overrides) -> dict:
    payload = {
        "number": number,
        "state": "open",
        "title": f"{head} -> {base}",
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "merged_at": None,
        "closed_at": None,
        "updated_at": "2026-01-01T00:01:00Z",
        "head": {
            "ref": head,
            "sha": "a" * 40,
            "label": f"acme:{head}",
            "repo": {"full_name": "acme/app", "owner": {"login": "acme"}},
        },
        "base": {
            "ref": base,
            "sha": "b" * 40,
            "label": f"acme:{base}",
            "repo": {"full_name": "acme/app", "owner": {"login": "acme"}},
        },
    }
    payload.update(overrides)
    return payload


# --- a fake `gh`, so the subprocess path is exercised for real --------------

GH_STUB = '''#!{python}
"""A stand-in for the `gh` binary: speaks `gh api` to whatever url it is given.

It exists so the provider's subprocess path -- argv construction, `--include`
parsing, exit codes -- is exercised against the mock server, which real `gh`
cannot reach (it would insist on https and on being logged in).
"""
import json
import os
import sys
import urllib.error
import urllib.request

argv = sys.argv[1:]
log = os.environ.get("GH_STUB_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(argv) + "\\n")

if os.environ.get("GH_STUB_FAIL"):          # `gh` itself failing: not logged in
    sys.stderr.write("gh: You are not logged into any GitHub hosts\\n")
    sys.exit(4)

if not argv or argv[0] != "api":
    sys.stderr.write("stub gh: only `gh api` is implemented\\n")
    sys.exit(2)

argv = argv[1:]
method, endpoint, data, include = "GET", None, None, False
headers = {{}}
index = 0
while index < len(argv):
    token = argv[index]
    if token in ("--include", "-i"):
        include = True
    elif token in ("--method", "-X"):
        index += 1
        method = argv[index]
    elif token in ("--header", "-H"):
        index += 1
        key, _, value = argv[index].partition(":")
        headers[key.strip()] = value.strip()
    elif token == "--input":
        index += 1
        data = sys.stdin.buffer.read() if argv[index] == "-" else open(argv[index], "rb").read()
    elif token.startswith("-"):
        sys.stderr.write("stub gh: unsupported flag " + token + "\\n")
        sys.exit(2)
    else:
        endpoint = token
    index += 1

url = endpoint if "://" in endpoint else "https://api.github.com/" + endpoint.lstrip("/")
request = urllib.request.Request(url, method=method, data=data)
for key, value in headers.items():
    request.add_header(key, value)
if data is not None:
    request.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(request) as response:
        status, received, payload = response.status, response.headers, response.read()
except urllib.error.HTTPError as err:
    status, received, payload = err.code, err.headers, err.read()

if include:
    sys.stdout.write("HTTP/2.0 %d Status\\r\\n" % status)
    for key, value in received.items():
        sys.stdout.write("%s: %s\\r\\n" % (key, value))
    sys.stdout.write("\\r\\n")
sys.stdout.write(payload.decode())
sys.stdout.flush()
sys.exit(0 if status < 400 else 1)
'''


@pytest.fixture
def gh_stub(tmp_path) -> dict:
    """A directory containing a working fake ``gh``, plus the env that finds it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(GH_STUB.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "gh.log"
    return {
        "env": {"PATH": str(bindir), "GH_STUB_LOG": str(log), "HOME": str(tmp_path)},
        "log": log,
        "argv": lambda: [json.loads(line) for line in log.read_text().splitlines()],
    }


@pytest.fixture
def no_gh_env(tmp_path) -> dict:
    """An environment with nothing named ``gh`` anywhere on PATH."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    return {"PATH": str(empty), "HOME": str(tmp_path)}


# --------------------------------------------------------------------------
# repository settings (SPEC.md sec 6.3, sec 6.6)
# --------------------------------------------------------------------------


def test_repo_settings_reads_the_default_branch_and_delete_branch_on_merge(provider):
    settings = provider.repo_settings()
    assert isinstance(settings, RepoSettings)
    assert settings.default_branch == "main"
    assert settings.delete_branch_on_merge is False


def test_delete_branch_on_merge_is_surfaced_so_the_cli_can_warn(tmp_path):
    """SPEC.md sec 6.3: warn when it is on, because it orphans a child PR."""
    from stackem.forge.mock_server import MockGitHub, MockGitHubServer

    with MockGitHubServer(
        MockGitHub(repo="acme/app", default_branch="main", delete_branch_on_merge=True)
    ) as server:
        assert rest_provider(server).repo_settings().delete_branch_on_merge is True


def test_default_branch_is_the_last_trunk_fallback(provider):
    """SPEC.md sec 6.6: origin/HEAD, then remote set-head, then the API."""
    assert provider.default_branch() == "main"


def test_repo_settings_is_read_once_per_run(provider, mock_github_memory):
    provider.repo_settings()
    provider.default_branch()
    provider.repo_settings()
    repo_reads = [
        request
        for request in mock_github_memory.requests
        if request.path == "/repos/acme/app"
    ]
    assert len(repo_reads) == 1


def test_repo_slug_is_what_the_rescue_commands_print(provider):
    assert provider.repo_slug() == "acme/app"


# --------------------------------------------------------------------------
# the protocol, and what invariant 14 keeps out of it
# --------------------------------------------------------------------------


def test_the_provider_satisfies_the_forge_protocol(provider):
    assert isinstance(provider, Provider)


@pytest.mark.parametrize("verb", ["create", "close", "delete", "reopen", "merge"])
def test_the_provider_cannot_close_delete_or_create_anything(provider, verb):
    """CLAUDE.md invariant 14: sync reports and prints the command; it never acts."""
    public = [name for name in dir(provider) if not name.startswith("_")]
    assert [name for name in public if verb in name] == []


# --------------------------------------------------------------------------
# listing open pull requests (CLAUDE.md invariant 18)
# --------------------------------------------------------------------------


def test_list_open_pull_requests_returns_only_the_open_ones(provider, api):
    api.open_pull_request("feat-a", "main")
    merged = api.open_pull_request("feat-b", "feat-a")
    closed = api.open_pull_request("feat-c", "feat-b")
    api.squash_merge(merged.number)
    api.close_pull_request(closed.number)

    heads = {pr.head for pr in provider.list_open_pull_requests()}
    assert heads == {"feat-a"}


def test_list_open_pull_requests_pages_past_the_hundred_row_cap(provider, api, mock_github_memory):
    """GitHub caps per_page at 100; the answer is every page, not a bigger one."""
    for index in range(150):
        api.open_pull_request(f"feat-{index:03d}", "main")

    found = provider.list_open_pull_requests()

    assert len(found) == 150
    pages = [param(request, "page") for request in pull_listings(mock_github_memory)]
    assert pages == ["1", "2"]
    assert {param(request, "per_page") for request in pull_listings(mock_github_memory)} == {"100"}


def test_an_open_pull_request_outside_a_hundred_row_window_is_still_found(
    provider, api, mock_github_memory
):
    """CLAUDE.md invariant 18, the failure it exists to prevent.

    The stack's pull request is the oldest in the repository; 120 closed ones
    were opened after it.  A single ``state=all&per_page=100`` page -- GitHub
    sorts newest first -- contains none of it.
    """
    stack_pr = api.open_pull_request("feat-a", "main")
    for index in range(120):
        churn = api.open_pull_request(f"churn-{index:03d}", "main")
        api.close_pull_request(churn.number)

    assert [pr.number for pr in provider.list_open_pull_requests()] == [stack_pr.number]

    found = provider.get_pull_request_for_branch("feat-a")
    assert found is not None and found.number == stack_pr.number
    assert_never_a_blind_window(mock_github_memory)


def test_fork_pull_requests_come_back_from_the_listing_marked(provider, api):
    """Invariant 17 excludes them from *indexing*; the listing still reports them."""
    api.open_pull_request("feat-a", "main")
    api.open_pull_request("feat-a", "main", head_repo="stranger/app")

    by_fork = {pr.is_cross_repository for pr in provider.list_open_pull_requests()}
    assert by_fork == {True, False}


def test_pull_request_fields_survive_the_round_trip(provider, api):
    raw = api.open_pull_request("feat-a", "main", title="add the thing")

    (found,) = provider.list_open_pull_requests()

    assert found.number == raw.number
    assert found.head == "feat-a"
    assert found.base == "main"
    assert found.state is PullRequestState.OPEN
    assert found.title == "add the thing"
    assert found.url.endswith(f"/pull/{raw.number}")
    assert found.head_sha == raw.head_sha
    assert found.head_repository_owner == "acme"
    assert found.is_cross_repository is False
    assert found.draft is False
    assert found.updated_at is not None and found.updated_at.year == 2026
    assert found.merged_at is None


# --------------------------------------------------------------------------
# indexing one branch: precedence (CLAUDE.md invariant 16)
# --------------------------------------------------------------------------


def test_open_beats_merged_and_closed(provider, api):
    """SPEC.md sec 5.1: **Verified** one branch had two pull requests."""
    first = api.open_pull_request("feat-a", "main")
    second = api.open_pull_request("feat-a", "main")
    third = api.open_pull_request("feat-a", "main")
    api.close_pull_request(first.number)
    api.squash_merge(second.number)

    found = provider.get_pull_request_for_branch("feat-a")

    assert found is not None
    assert found.number == third.number
    assert found.state is PullRequestState.OPEN


def test_the_most_recent_merge_beats_an_older_merge_and_any_closed(provider, api):
    """Recency, not pull request number, decides between two merges."""
    older = api.open_pull_request("feat-a", "main")
    newer = api.open_pull_request("feat-a", "main")
    abandoned = api.open_pull_request("feat-a", "main")
    api.close_pull_request(abandoned.number)
    api.squash_merge(newer.number)
    api.squash_merge(older.number)  # merged LAST, though it is the lowest number

    found = provider.get_pull_request_for_branch("feat-a")

    assert found is not None
    assert found.state is PullRequestState.MERGED
    assert found.number == older.number


def test_a_merge_is_normalized_from_state_closed_plus_merged_at(provider, api):
    """REST has no "merged" state: it is ``closed`` with ``merged_at`` set."""
    raw = api.open_pull_request("feat-a", "main")
    merge_sha = api.squash_merge(raw.number)
    assert raw.state == "closed"  # the mock reports GitHub's own state

    found = provider.get_pull_request_for_branch("feat-a")

    assert found is not None
    assert found.state is PullRequestState.MERGED
    assert found.merged_at is not None
    assert found.closed_at is not None
    assert found.merge_commit_sha == merge_sha


def test_closed_is_used_only_when_there_is_nothing_else(provider, api):
    raw = api.open_pull_request("feat-a", "main")
    api.close_pull_request(raw.number)

    found = provider.get_pull_request_for_branch("feat-a")

    assert found is not None
    assert found.number == raw.number
    assert found.state is PullRequestState.CLOSED


def test_a_branch_with_no_pull_request_is_none(provider, api):
    api.open_pull_request("feat-a", "main")
    assert provider.get_pull_request_for_branch("feat-b") is None


def test_a_branch_whose_pull_request_is_on_another_head_ref_is_none(provider, api):
    """The base is not the index: a PR *targeting* feat-a is not feat-a's PR."""
    api.open_pull_request("feat-b", "feat-a")
    assert provider.get_pull_request_for_branch("feat-a") is None


# --------------------------------------------------------------------------
# fork exclusion (CLAUDE.md invariant 17)
# --------------------------------------------------------------------------


def test_a_fork_pull_request_with_a_colliding_head_ref_is_excluded(provider, api):
    """SPEC.md sec 5.1: it would otherwise be indexed and then *retargeted*.

    The fork here belongs to the same owner (``acme/app-fork``), so GitHub's own
    ``head=acme:feat-a`` filter does not remove it -- the provider has to.
    """
    fork_pr = api.open_pull_request("feat-a", "main", head_repo="acme/app-fork")

    assert provider.get_pull_request_for_branch("feat-a") is None
    # ...and it really was in the answer the server gave.
    assert fork_pr.number in {pr.number for pr in provider.list_open_pull_requests()}


def test_a_strangers_fork_pull_request_is_excluded_too(provider, api):
    api.open_pull_request("feat-a", "main", head_repo="stranger/app")
    assert provider.get_pull_request_for_branch("feat-a") is None


def test_a_local_pull_request_wins_over_a_colliding_fork(provider, api):
    fork_pr = api.open_pull_request("feat-a", "main", head_repo="acme/app-fork")
    mine = api.open_pull_request("feat-a", "main")

    found = provider.get_pull_request_for_branch("feat-a")

    assert found is not None
    assert found.number == mine.number != fork_pr.number


def test_a_pull_request_whose_head_repository_is_gone_is_treated_as_a_fork():
    """GitHub nulls ``head.repo`` once a fork is deleted.

    Unknown provenance is treated as a fork, because adopting a stranger's pull
    request -- and then retargeting it -- is the damaging mistake (invariant 17).
    """
    orphan = pull_json(7, "feat-a")
    orphan["head"] = {"ref": "feat-a", "sha": "c" * 40, "label": "stranger:feat-a", "repo": None}
    transport = StubTransport(repeat=Response(200, [orphan], {}))
    github = GitHubProvider("acme/app", transport=transport)

    assert github.get_pull_request_for_branch("feat-a") is None
    (found,) = github.list_open_pull_requests()
    assert found.is_cross_repository is True


# --------------------------------------------------------------------------
# retargeting (CLAUDE.md invariants 1 and 15)
# --------------------------------------------------------------------------


def test_retarget_moves_the_base_of_an_open_pull_request(provider, api, mock_github_memory):
    api.create_branch("feat-a")
    pr = api.open_pull_request("feat-b", "feat-a")

    provider.retarget(pr.number, "main")

    assert api.pull_request(pr.number).base == "main"
    assert api.pull_request(pr.number).state == "open"
    (patch,) = [r for r in mock_github_memory.requests if r.method == "PATCH"]
    assert patch.body == {"base": "main"}


def test_retarget_is_idempotent(provider, api):
    """Invariant 21: sync is re-entrant, so this runs again on the next run."""
    api.create_branch("feat-a")
    pr = api.open_pull_request("feat-b", "feat-a")

    provider.retarget(pr.number, "main")
    provider.retarget(pr.number, "main")

    assert api.pull_request(pr.number).base == "main"


def test_retarget_onto_a_missing_branch_is_a_forge_error_with_the_detail(provider, api):
    """GitHub's 422 message is just "Validation Failed"; the detail is in errors[]."""
    pr = api.open_pull_request("feat-b", "main")

    with pytest.raises(GitHubApiError) as caught:
        provider.retarget(pr.number, "no-such-branch")

    assert isinstance(caught.value, ForgeError)
    assert caught.value.status == 422
    assert "no-such-branch" in str(caught.value)
    assert api.pull_request(pr.number).base == "main"


def test_retarget_of_an_unknown_pull_request_is_a_forge_error(provider):
    with pytest.raises(GitHubApiError) as caught:
        provider.retarget(999, "main")
    assert caught.value.status == 404


# --------------------------------------------------------------------------
# does the branch still exist on the forge (SPEC.md sec 5.1)
# --------------------------------------------------------------------------


def test_branch_existence_is_an_answer_not_an_error(provider, api):
    api.create_branch("feat-a")
    assert provider.branch_exists_on_remote("feat-a") is True
    assert provider.branch_exists_on_remote("feat-gone") is False


def test_branch_existence_follows_real_git(sandbox, mock_github):
    """The mock reads its refs from the bare origin, so git and forge agree."""
    provider = rest_provider(mock_github)
    sandbox.make_stack(["feat-a"])

    assert provider.branch_exists_on_remote("feat-a") is True

    pr = mock_github.api.open_pull_request("feat-a", sandbox.trunk)
    found = provider.get_pull_request_for_branch("feat-a")
    assert found is not None and found.head_sha == sandbox.sha("feat-a")

    sandbox.delete_remote_branch("feat-a")
    assert provider.branch_exists_on_remote("feat-a") is False
    assert provider.get_pull_request_for_branch("feat-a").number == pr.number


def test_a_deleted_branch_closes_the_pull_requests_that_referenced_it(sandbox, mock_github):
    """CLAUDE.md invariant 13, as the provider sees it: closed, not merged."""
    provider = rest_provider(mock_github)
    sandbox.make_stack(["feat-a", "feat-b"])
    mock_github.api.open_pull_request("feat-a", sandbox.trunk)
    child = mock_github.api.open_pull_request("feat-b", "feat-a")

    mock_github.api.delete_branch("feat-a")

    found = provider.get_pull_request_for_branch("feat-b")
    assert found is not None and found.number == child.number
    assert found.state is PullRequestState.CLOSED
    assert found.merged_at is None  # closed, not merged: an orphan, sec 5.1


# --------------------------------------------------------------------------
# pagination mechanics
# --------------------------------------------------------------------------


def test_a_full_page_with_no_link_header_still_asks_for_the_next_one():
    rows = [pull_json(number, f"feat-{number}") for number in range(100)]
    transport = StubTransport(Response(200, rows, {}), Response(200, [], {}))
    github = GitHubProvider("acme/app", transport=transport)

    assert len(github.list_open_pull_requests()) == 100
    assert [call[2].get("page") for call in transport.calls] == [1, 2]


def test_a_server_that_never_stops_paginating_is_an_error_not_a_loop():
    page = Response(200, [pull_json(1, "feat-a")] * 100, {"link": '<next>; rel="next"'})
    github = GitHubProvider("acme/app", transport=StubTransport(repeat=page))

    with pytest.raises(ForgeError):
        github.list_open_pull_requests()


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------


def test_rest_requests_carry_the_token_and_the_api_version(provider, mock_github_memory):
    provider.repo_settings()

    request = mock_github_memory.requests[-1]
    assert header(request, "Authorization") == "Bearer test-token"
    assert header(request, "Accept") == "application/vnd.github+json"
    assert header(request, "X-GitHub-Api-Version")


def test_an_enterprise_style_base_url_is_spoken_too(mock_github_memory):
    """The mock serves ``/api/v3/...`` as well, which is the enterprise shape."""
    provider = rest_provider(mock_github_memory, base_url=f"{mock_github_memory.url}/api/v3")
    assert provider.default_branch() == "main"
    assert mock_github_memory.requests[-1].path == "/repos/acme/app"


def test_an_unknown_repository_is_a_forge_error(mock_github_memory):
    provider = GitHubProvider(
        "acme/other", base_url=mock_github_memory.url, token="t", transport="rest"
    )
    with pytest.raises(GitHubApiError) as caught:
        provider.repo_settings()
    assert caught.value.status == 404


def test_auto_prefers_gh_when_it_is_on_path(gh_stub, mock_github_memory):
    env = {**gh_stub["env"], "GITHUB_TOKEN": "unused"}
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=env)
    assert isinstance(provider.transport, GhTransport)


def test_gh_carries_reads_and_writes_all_the_way_through(gh_stub, mock_github_memory):
    api = mock_github_memory.api
    api.create_branch("feat-a")
    pr = api.open_pull_request("feat-b", "feat-a")
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=gh_stub["env"])

    assert provider.default_branch() == "main"
    assert [found.number for found in provider.list_open_pull_requests()] == [pr.number]
    provider.retarget(pr.number, "main")

    assert api.pull_request(pr.number).base == "main"
    calls = gh_stub["argv"]()
    assert all(call[0] == "api" and "--include" in call for call in calls)
    assert any("PATCH" in call for call in calls)
    assert all(mock_github_memory.url in call[-1] for call in calls)


def test_a_gh_api_error_is_a_forge_error(gh_stub, mock_github_memory):
    pr = mock_github_memory.api.open_pull_request("feat-b", "main")
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=gh_stub["env"])

    with pytest.raises(GitHubApiError) as caught:
        provider.retarget(pr.number, "no-such-branch")

    assert caught.value.status == 422
    assert "no-such-branch" in str(caught.value)


def test_gh_failing_to_answer_at_all_is_a_forge_error(gh_stub, mock_github_memory):
    """Not logged in: `gh` exits non-zero without ever making a request."""
    env = {**gh_stub["env"], "GH_STUB_FAIL": "1"}
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=env)

    with pytest.raises(ForgeError) as caught:
        provider.default_branch()

    assert "not logged" in str(caught.value).lower() or "failed" in str(caught.value).lower()


def test_without_gh_the_fallback_is_github_token_and_rest(no_gh_env, mock_github_memory):
    """`gh` is not universally installed, so the fallback is not optional."""
    env = {**no_gh_env, "GITHUB_TOKEN": "from-the-environment"}
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=env)

    assert isinstance(provider.transport, RestTransport)
    assert provider.default_branch() == "main"
    assert header(mock_github_memory.requests[-1], "Authorization") == "Bearer from-the-environment"


def test_gh_token_is_accepted_as_well(no_gh_env, mock_github_memory):
    env = {**no_gh_env, "GH_TOKEN": "gh-token"}
    provider = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=env)
    provider.default_branch()
    assert header(mock_github_memory.requests[-1], "Authorization") == "Bearer gh-token"


def test_asking_for_rest_ignores_gh_on_path(gh_stub, mock_github_memory):
    env = {**gh_stub["env"], "GITHUB_TOKEN": "t"}
    provider = GitHubProvider(
        "acme/app", base_url=mock_github_memory.url, env=env, transport="rest"
    )
    assert isinstance(provider.transport, RestTransport)
    assert provider.default_branch() == "main"
    assert not gh_stub["log"].exists()


def test_with_neither_gh_nor_a_token_it_says_how_to_authenticate(no_gh_env):
    with pytest.raises(ForgeError) as caught:
        GitHubProvider("acme/app", env=no_gh_env)
    message = str(caught.value)
    assert "gh" in message and "GITHUB_TOKEN" in message


def test_the_base_url_can_come_from_the_environment(no_gh_env, mock_github_memory):
    """So a test -- or an enterprise user -- can aim the provider without code."""
    env = {**no_gh_env, "GITHUB_TOKEN": "t", "STACKEM_GITHUB_API_URL": mock_github_memory.url}
    provider = GitHubProvider("acme/app", env=env)
    assert provider.default_branch() == "main"


# --------------------------------------------------------------------------
# remote urls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:acme/app.git", ("github.com", "acme/app")),
        ("git@github.com:acme/app", ("github.com", "acme/app")),
        ("https://github.com/acme/app.git", ("github.com", "acme/app")),
        ("https://github.com/acme/app", ("github.com", "acme/app")),
        ("ssh://git@github.com/acme/app.git", ("github.com", "acme/app")),
        ("https://user@github.example.com/acme/app.git", ("github.example.com", "acme/app")),
    ],
)
def test_parse_remote_url(url, expected):
    assert parse_remote_url(url) == expected


@pytest.mark.parametrize("url", ["", "not a url", "/local/path", "git@github.com:acme"])
def test_parse_remote_url_rejects_what_is_not_one(url):
    with pytest.raises(ValueError):
        parse_remote_url(url)


def test_api_base_url_knows_github_com_from_an_enterprise_host():
    assert api_base_url("github.com") == DEFAULT_BASE_URL
    assert api_base_url("github.example.com") == "https://github.example.com/api/v3"


def test_from_remote_url_builds_a_provider_for_that_host(no_gh_env):
    env = {**no_gh_env, "GITHUB_TOKEN": "t"}
    github = GitHubProvider.from_remote_url("git@github.example.com:acme/app.git", env=env)
    assert github.repo_slug() == "acme/app"
    assert github.base_url == "https://github.example.com/api/v3"


def test_from_remote_url_keeps_an_explicit_base_url(no_gh_env, mock_github_memory):
    env = {**no_gh_env, "GITHUB_TOKEN": "t"}
    github = GitHubProvider.from_remote_url(
        "git@github.com:acme/app.git", base_url=mock_github_memory.url, env=env
    )
    assert github.default_branch() == "main"


def test_a_repo_that_is_not_owner_slash_name_is_rejected(no_gh_env):
    env = {**no_gh_env, "GITHUB_TOKEN": "t"}
    with pytest.raises(ValueError):
        GitHubProvider("app", env=env)


def test_the_repo_can_come_from_the_environment_when_the_remote_is_not_github(
    no_gh_env, mock_github_memory, tmp_path
):
    """The seam an end-to-end test needs.

    A sandbox's ``origin`` is a bare repository on disk, so there is no
    ``owner/name`` to parse out of it.  ``STACKEM_GITHUB_REPO`` says what the
    repository is; ``STACKEM_GITHUB_API_URL`` says where it lives.
    """
    env = {
        **no_gh_env,
        "GITHUB_TOKEN": "t",
        "STACKEM_GITHUB_REPO": "acme/app",
        "STACKEM_GITHUB_API_URL": mock_github_memory.url,
    }
    github = GitHubProvider.from_remote_url(str(tmp_path / "origin.git"), env=env)

    assert github.repo_slug() == "acme/app"
    assert github.default_branch() == "main"


def test_the_environment_api_url_overrides_the_host_in_the_remote(no_gh_env, mock_github_memory):
    env = {
        **no_gh_env,
        "GITHUB_TOKEN": "t",
        "STACKEM_GITHUB_API_URL": mock_github_memory.url,
    }
    github = GitHubProvider.from_remote_url("git@github.example.com:acme/app.git", env=env)

    assert github.base_url == mock_github_memory.url
    assert github.default_branch() == "main"


def test_an_explicit_base_url_still_beats_the_environment(no_gh_env, mock_github_memory):
    env = {**no_gh_env, "GITHUB_TOKEN": "t", "STACKEM_GITHUB_API_URL": "https://wrong.example"}
    github = GitHubProvider("acme/app", base_url=mock_github_memory.url, env=env)
    assert github.default_branch() == "main"


def test_a_branch_name_with_a_slash_survives_both_queries(provider, api):
    """``dan/auth-ui`` is an ordinary branch name, and a url path segment."""
    api.create_branch("dan/auth-ui")
    pr = api.open_pull_request("dan/auth-ui", "main")

    assert provider.branch_exists_on_remote("dan/auth-ui") is True
    found = provider.get_pull_request_for_branch("dan/auth-ui")
    assert found is not None and found.number == pr.number


def test_the_only_write_that_ever_reaches_github_is_a_base_retarget(
    provider, api, mock_github_memory
):
    """CLAUDE.md invariant 14, asserted on the wire rather than on ``dir()``.

    Everything sync asks a forge for, in one run -- then the one write it is
    allowed to make.
    """
    api.create_branch("feat-a")
    pr = api.open_pull_request("feat-b", "feat-a")

    provider.repo_settings()
    provider.default_branch()
    provider.list_open_pull_requests()
    provider.get_pull_request_for_branch("feat-b")
    provider.branch_exists_on_remote("feat-a")
    provider.retarget(pr.number, "main")

    writes = [r for r in mock_github_memory.requests if r.method != "GET"]
    assert [(r.method, r.path, r.body) for r in writes] == [
        ("PATCH", f"/repos/acme/app/pulls/{pr.number}", {"base": "main"})
    ]
