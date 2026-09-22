"""An in-process mock GitHub, speaking the REST endpoints stackem uses.

SPEC.md sec 10: the real provider is pointed at this server rather than stubbed,
so the HTTP layer stays under test -- and "the mock must reproduce the verified
behaviors ... or the suite passes while reality breaks."

Verified behaviors reproduced here, each cited at the code that implements it:

* deleting a branch closes EVERY open PR referencing it as head OR base
  (SPEC.md sec 6.3, CLAUDE.md invariant 13)
* reopening fails with 422 while either the head or the base branch is missing
  (SPEC.md sec 6.3)
* ``refs/pull/N/head`` persists after the branch is deleted (SPEC.md sec 6.3)
* a squash merge mints a NEW commit on the base with a different patch-id
  (SPEC.md sec 6.4)
* one branch may have several pull requests (SPEC.md sec 5.1)
* pull requests can be cross-repository, and are distinguishable (SPEC.md sec 5.1)
* a merged PR has REST state ``closed`` with ``merged_at`` set -- "merged" is not
  a REST state
* listing is paginated and capped at 100 per page, which is how the ``--state all
  --limit 100`` window fills up and hides an open PR (CLAUDE.md invariant 18)

Usage::

    api = MockGitHub(repo="acme/app", default_branch="main", repo_path=bare_repo)
    with MockGitHubServer(api) as server:
        pr = api.open_pull_request("feat-b", "feat-a")
        api.delete_branch("feat-a")          # closes pr, like GitHub
        provider = GitHubProvider(base_url=server.url, ...)

Bound to a real bare repository (``repo_path``), branch existence is read from
its refs and a squash merge is a real commit made with plumbing, so git-side and
forge-side state cannot drift apart in a test.  Without one it keeps the same
bookkeeping in memory with synthetic object ids.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from stackem.gitx import Git

__all__ = [
    "MockGitHub",
    "MockGitHubError",
    "MockGitHubServer",
    "MockPullRequest",
    "RecordedRequest",
]

_CLOCK_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CLOCK_STEP = timedelta(minutes=1)


class MockGitHubError(Exception):
    """A GitHub-shaped API error, carrying the status the real API returns."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


@dataclass
class MockPullRequest:
    """One pull request.  ``state`` is GitHub's own: "open" or "closed" only."""

    number: int
    head: str
    base: str
    head_repo: str
    base_repo: str
    title: str = ""
    state: str = "open"
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    draft: bool = False
    #: True when the close was collateral damage from a branch deletion rather
    #: than a person's decision (SPEC.md sec 5.1: the two are indistinguishable
    #: to sync, which is why it never auto-rescues).
    closed_by_branch_deletion: bool = False

    @property
    def merged(self) -> bool:
        return self.merged_at is not None

    @property
    def is_cross_repository(self) -> bool:
        return self.head_repo != self.base_repo


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class MockGitHub:
    """The state behind the server, with a Python API for setting up scenarios."""

    def __init__(
        self,
        repo: str = "acme/app",
        *,
        default_branch: str = "main",
        repo_path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        delete_branch_on_merge: bool = False,
    ) -> None:
        self.repo_full_name = repo
        self.owner, _, self.name = repo.partition("/")
        self.default_branch = default_branch
        self.delete_branch_on_merge = delete_branch_on_merge
        self.pull_requests: list[MockPullRequest] = []
        self.lock = threading.RLock()

        self._git = Git(repo_path, env=env) if repo_path is not None else None
        self._branches: dict[str, str] = {}
        self._pull_refs: dict[int, str] = {}
        self._next_number = 1
        self._clock = _CLOCK_START
        if self._git is None:
            self._branches[default_branch] = self._fake_sha(default_branch)

    # -- clock -------------------------------------------------------------

    def _now(self) -> datetime:
        self._clock += _CLOCK_STEP
        return self._clock

    def _fake_sha(self, seed: str) -> str:
        return hashlib.sha1(f"{self.repo_full_name}:{seed}".encode()).hexdigest()

    # -- branches ----------------------------------------------------------

    def branch_exists(self, name: str) -> bool:
        return self.branch_sha(name) is not None

    def branch_sha(self, name: str) -> str | None:
        if self._git is not None:
            return self._git.try_rev_parse(f"refs/heads/{name}")
        return self._branches.get(name)

    def _snapshot_refs(self) -> dict[str, str]:
        """Every branch in one call.

        Serializing a page of pull requests otherwise asks git for the same refs
        hundreds of times.  The snapshot is never cached between calls: a test
        may push to the bound repository behind the mock's back, and the mock
        must see that.
        """
        if self._git is not None:
            return self._git.branches("refs/heads")
        return dict(self._branches)

    def create_branch(self, name: str, sha: str | None = None) -> str:
        """Create (or move) a branch, in the bound repository when there is one."""
        with self.lock:
            if sha is None:
                sha = self.branch_sha(self.default_branch) or self._fake_sha(name)
            if self._git is not None:
                self._git.run("update-ref", f"refs/heads/{name}", sha)
            else:
                self._branches[name] = sha
            return sha

    def delete_branch(self, name: str) -> list[int]:
        """Delete a branch and close every open PR that references it.

        SPEC.md sec 6.3 / CLAUDE.md invariant 13: **Verified** that deleting a
        branch closes every open PR referencing it as head *or* base.  The
        commits stay reachable under refs/pull/N/head, which is what makes the
        manual rescue possible.
        """
        with self.lock:
            refs = self._snapshot_refs()
            for pr in self.pull_requests:
                if pr.head == name:
                    # refs/pull/N/head outlives the branch (SPEC.md sec 6.3)
                    self._sync_pull_ref(pr, refs)
            if self._git is not None:
                self._git.run("update-ref", "-d", f"refs/heads/{name}", check=False)
            else:
                self._branches.pop(name, None)

            closed: list[int] = []
            for pr in self.pull_requests:
                if pr.state != "open":
                    continue
                if pr.head != name and pr.base != name:
                    continue
                pr.state = "closed"
                pr.closed_at = self._now()
                pr.updated_at = pr.closed_at
                pr.closed_by_branch_deletion = True
                closed.append(pr.number)
            return closed

    # -- pull requests -----------------------------------------------------

    def open_pull_request(
        self,
        head: str,
        base: str,
        *,
        title: str | None = None,
        head_repo: str | None = None,
        number: int | None = None,
    ) -> MockPullRequest:
        """Open a pull request.

        SPEC.md sec 5.1: one branch may have several pull requests -- nothing
        here stops you opening a second one on the same head ref, because
        **Verified** that GitHub does not either.

        ``head_repo="stranger/app"`` makes it a fork PR (invariant 17).
        """
        with self.lock:
            if number is None:
                number = self._next_number
            self._next_number = max(self._next_number, number) + 1
            head_repo = head_repo or self.repo_full_name
            refs = self._snapshot_refs()
            if head_repo == self.repo_full_name and head not in refs:
                refs[head] = self.create_branch(head)
            if base not in refs:
                refs[base] = self.create_branch(base)
            stamp = self._now()
            pr = MockPullRequest(
                number=number,
                head=head,
                base=base,
                head_repo=head_repo,
                base_repo=self.repo_full_name,
                title=title or f"{head} -> {base}",
                head_sha=refs.get(head) or self._fake_sha(head),
                created_at=stamp,
                updated_at=stamp,
            )
            self.pull_requests.append(pr)
            self._sync_pull_ref(pr, refs)
            return pr

    def pull_request(self, number: int) -> MockPullRequest:
        for pr in self.pull_requests:
            if pr.number == number:
                return pr
        raise MockGitHubError(404, "Not Found")

    def close_pull_request(self, number: int) -> MockPullRequest:
        with self.lock:
            pr = self.pull_request(number)
            pr.state = "closed"
            pr.closed_at = self._now()
            pr.updated_at = pr.closed_at
            return pr

    def reopen_pull_request(self, number: int) -> MockPullRequest:
        """Reopen a closed pull request.

        SPEC.md sec 6.3: **Verified** that this fails with 422 while EITHER the
        head or the base branch is missing -- which is why the rescue restores
        both branches before the PATCH.
        """
        with self.lock:
            pr = self.pull_request(number)
            if pr.merged:
                raise MockGitHubError(
                    422, "Validation Failed: state cannot be changed. This pull request is merged."
                )
            for ref in (pr.head, pr.base):
                if not self.branch_exists(ref):
                    raise MockGitHubError(
                        422,
                        "Validation Failed: state cannot be changed. "
                        f"The {ref} branch has been deleted.",
                    )
            pr.state = "open"
            pr.closed_at = None
            pr.closed_by_branch_deletion = False
            pr.updated_at = self._now()
            return pr

    def retarget(self, number: int, new_base: str) -> MockPullRequest:
        """Change a PR's base -- the whole of reparenting (invariant 1)."""
        with self.lock:
            pr = self.pull_request(number)
            if not self.branch_exists(new_base):
                raise MockGitHubError(
                    422, f"Validation Failed: base branch '{new_base}' does not exist"
                )
            pr.base = new_base
            pr.updated_at = self._now()
            return pr

    def squash_merge(self, number: int, *, message: str | None = None) -> str:
        """Squash-merge a pull request.

        SPEC.md sec 6.4: **Verified** that a squash mints a NEW commit on the
        base whose patch-id differs from every commit it replaced -- which is
        why ``git cherry`` cannot detect the merge and why every branch above
        has to be replayed.  Bound to a real repository this is a real
        ``commit-tree`` on the base; the branch is left exactly where it was.

        The head branch is deleted afterwards only when the repository has
        ``delete_branch_on_merge`` set -- and then it closes the child PR,
        exactly as SPEC.md sec 12 warns.
        """
        with self.lock:
            pr = self.pull_request(number)
            if pr.state != "open":
                raise MockGitHubError(405, "Pull Request is not mergeable")
            merge_sha = self._mint_squash_commit(pr, message)
            stamp = self._now()
            pr.state = "closed"
            pr.merged_at = stamp
            pr.closed_at = stamp
            pr.updated_at = stamp
            pr.merge_commit_sha = merge_sha
            self._sync_pull_ref(pr)
            if self.delete_branch_on_merge:
                self.delete_branch(pr.head)
            return merge_sha

    def pull_ref(self, number: int) -> str | None:
        """``refs/pull/<number>/head``.

        SPEC.md sec 6.3: GitHub retains a PR's commits here indefinitely, even
        after the branch is deleted.  That is what the rescue fetches.
        """
        return self._pull_refs.get(number)

    # -- internals ---------------------------------------------------------

    def _sync_pull_ref(self, pr: MockPullRequest, refs: dict[str, str] | None = None) -> None:
        known = refs if refs is not None else self._snapshot_refs()
        sha = known.get(pr.head) or pr.head_sha
        if sha is None:
            return
        pr.head_sha = sha
        if self._pull_refs.get(pr.number) == sha:
            return
        self._pull_refs[pr.number] = sha
        # `rev-parse --verify` answers a well-formed sha with itself whether or
        # not the object exists; `cat-file -e` is the question being asked.
        known = self._git is not None and self._git.run(
            "cat-file", "-e", f"{sha}^{{commit}}", check=False
        ).returncode == 0
        if known:
            # A fork pull request's head commit is not in this repository at all
            # (its sha is synthetic), and `git update-ref <missing object>` is a
            # fatal error.  GitHub has no refs/pull/N/head to offer there either,
            # which is exactly why invariant 17 excludes fork pull requests.
            self._git.run("update-ref", f"refs/pull/{pr.number}/head", sha)

    def _mint_squash_commit(self, pr: MockPullRequest, message: str | None) -> str:
        subject = message or f"{pr.title} (#{pr.number})"
        if self._git is None:
            return self._fake_sha(f"squash:{pr.number}")
        base_ref = f"refs/heads/{pr.base}"
        merged = self._git.merge_tree_write_tree(base_ref, f"refs/heads/{pr.head}")
        if merged.conflicted or merged.tree is None:
            raise MockGitHubError(405, "Pull Request is not mergeable")
        # A fixed clock keeps the minted commit deterministic across runs.
        stamp = f"{int(self._now().timestamp())} +0000"
        self._git.env["GIT_AUTHOR_DATE"] = stamp
        self._git.env["GIT_COMMITTER_DATE"] = stamp
        new_sha = self._git.out(
            "commit-tree", merged.tree, "-p", base_ref, "-m", subject
        )
        self._git.run("update-ref", base_ref, new_sha)
        return new_sha

    # -- serialization (GitHub REST shapes) --------------------------------

    def _repo_json(self) -> dict[str, Any]:
        return {
            "full_name": self.repo_full_name,
            "name": self.name,
            "owner": {"login": self.owner},
            "default_branch": self.default_branch,
            "delete_branch_on_merge": self.delete_branch_on_merge,
        }

    def _ref_json(self, pr: MockPullRequest, side: str, refs: dict[str, str]) -> dict[str, Any]:
        if side == "head":
            ref, repo, sha = pr.head, pr.head_repo, pr.head_sha
        else:
            ref, repo, sha = pr.base, pr.base_repo, refs.get(pr.base)
        owner = repo.partition("/")[0]
        return {
            "label": f"{owner}:{ref}",
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": repo, "name": repo.partition("/")[2], "owner": {"login": owner}},
            "user": {"login": owner},
        }

    def pull_request_json(
        self,
        pr: MockPullRequest,
        *,
        detailed: bool = False,
        refs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        known = refs if refs is not None else self._snapshot_refs()
        if pr.head in known:
            # GitHub updates head.sha when the branch moves.
            self._sync_pull_ref(pr, known)
        payload = {
            "number": pr.number,
            "state": pr.state,
            "title": pr.title,
            "draft": pr.draft,
            "html_url": f"https://github.com/{self.repo_full_name}/pull/{pr.number}",
            "created_at": _stamp(pr.created_at),
            "updated_at": _stamp(pr.updated_at),
            "closed_at": _stamp(pr.closed_at),
            # A merged PR is state "closed" with merged_at set: "merged" is not a
            # REST state, and a provider that treats it as one gets it wrong.
            "merged_at": _stamp(pr.merged_at),
            "merge_commit_sha": pr.merge_commit_sha,
            "head": self._ref_json(pr, "head", known),
            "base": self._ref_json(pr, "base", known),
        }
        if detailed:
            payload["merged"] = pr.merged
            payload["mergeable_state"] = "clean" if pr.state == "open" else "unknown"
        return payload

    def pull_requests_json(
        self, pull_requests: list[MockPullRequest], *, detailed: bool = False
    ) -> list[dict[str, Any]]:
        """Serialize a page of pull requests with a single refs snapshot."""
        refs = self._snapshot_refs()
        return [
            self.pull_request_json(pr, detailed=detailed, refs=refs)
            for pr in pull_requests
        ]

    def list_pull_requests(
        self,
        *,
        state: str = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[MockPullRequest]:
        selected = []
        for pr in self.pull_requests:
            if state == "open" and pr.state != "open":
                continue
            if state == "closed" and pr.state != "closed":
                continue
            if base is not None and pr.base != base:
                continue
            if head is not None:
                owner, _, ref = head.rpartition(":")
                if pr.head != ref:
                    continue
                if owner and pr.head_repo.partition("/")[0] != owner:
                    continue
            selected.append(pr)
        # GitHub's default for pulls is sort=created, direction=desc.
        return sorted(selected, key=lambda pr: pr.number, reverse=True)


def _stamp(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockGitHub/1.0"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, *args) -> None:  # noqa: D102 - silence the test output
        pass

    @property
    def api(self) -> MockGitHub:
        return self.server.api  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/v3"):  # GitHub Enterprise style base url
            path = path[len("/api/v3"):]
        query = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.server.requests.append(  # type: ignore[attr-defined]
            RecordedRequest(
                method=method,
                path=path,
                query=query,
                body=body,
                headers=dict(self.headers),
            )
        )
        try:
            with self.api.lock:
                status, payload, headers = self._route(method, path, query, body)
        except MockGitHubError as err:
            status, payload, headers = err.status, _error_json(err.message), {}
        self._respond(status, payload, headers)

    def _respond(self, status: int, payload: Any, headers: Mapping[str, str]) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    # -- routing -----------------------------------------------------------

    def _route(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any, dict[str, str]]:
        api = self.api
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3 or parts[0] != "repos":
            raise MockGitHubError(404, "Not Found")
        if f"{parts[1]}/{parts[2]}" != api.repo_full_name:
            raise MockGitHubError(404, "Not Found")
        rest = parts[3:]

        if method == "GET" and not rest:
            return 200, api._repo_json(), {}

        if rest[:1] == ["pulls"]:
            return self._route_pulls(method, rest[1:], query, body)

        if method == "GET" and rest[:1] == ["branches"] and len(rest) >= 2:
            name = "/".join(rest[1:])
            sha = api.branch_sha(name)
            if sha is None:
                raise MockGitHubError(404, "Branch not found")
            return 200, {"name": name, "commit": {"sha": sha}}, {}

        if rest[:2] == ["git", "ref"] and method == "GET":
            return self._route_ref("/".join(rest[2:]))

        if rest[:2] == ["git", "refs"] and method == "DELETE":
            ref = "/".join(rest[2:])
            if not ref.startswith("heads/"):
                raise MockGitHubError(422, "Validation Failed: only heads/* may be deleted")
            name = ref[len("heads/"):]
            if not api.branch_exists(name):
                raise MockGitHubError(422, f"Validation Failed: reference '{ref}' does not exist")
            api.delete_branch(name)
            return 204, None, {}

        raise MockGitHubError(404, "Not Found")

    def _route_ref(self, ref: str) -> tuple[int, Any, dict[str, str]]:
        api = self.api
        if ref.startswith("heads/"):
            sha = api.branch_sha(ref[len("heads/"):])
        elif ref.startswith("pull/") and ref.endswith("/head"):
            # SPEC.md sec 6.3: refs/pull/N/head outlives the branch.
            number = ref[len("pull/"):-len("/head")]
            sha = api.pull_ref(int(number)) if number.isdigit() else None
        else:
            sha = None
        if sha is None:
            raise MockGitHubError(404, "Not Found")
        return 200, {"ref": f"refs/{ref}", "object": {"sha": sha, "type": "commit"}}, {}

    def _route_pulls(
        self, method: str, rest: list[str], query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any, dict[str, str]]:
        api = self.api
        if method == "GET" and not rest:
            return self._list_pulls(query)

        if not rest or not rest[0].isdigit():
            raise MockGitHubError(404, "Not Found")
        number = int(rest[0])
        pr = api.pull_request(number)
        tail = rest[1:]

        if method == "GET" and not tail:
            return 200, api.pull_request_json(pr, detailed=True), {}

        if method == "PATCH" and not tail:
            payload = body or {}
            if "base" in payload:
                api.retarget(number, payload["base"])
            if "title" in payload:
                pr.title = payload["title"]
            if payload.get("state") == "open":
                api.reopen_pull_request(number)
            elif payload.get("state") == "closed":
                api.close_pull_request(number)
            return 200, api.pull_request_json(pr, detailed=True), {}

        if method == "PUT" and tail == ["merge"]:
            method_name = (body or {}).get("merge_method", "merge")
            if method_name != "squash":
                raise MockGitHubError(
                    422, "Validation Failed: this mock only performs squash merges"
                )
            sha = api.squash_merge(number, message=(body or {}).get("commit_title"))
            return 200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}, {}

        raise MockGitHubError(404, "Not Found")

    def _list_pulls(self, query: dict[str, list[str]]) -> tuple[int, Any, dict[str, str]]:
        api = self.api
        state = query.get("state", ["open"])[0]
        head = query.get("head", [None])[0]
        base = query.get("base", [None])[0]
        # GitHub caps per_page at 100.  CLAUDE.md invariant 18: that cap is how a
        # `--state all --limit 100` window fills with closed PRs and hides the
        # open one you were looking for.
        per_page = max(1, min(100, int(query.get("per_page", ["30"])[0])))
        page = max(1, int(query.get("page", ["1"])[0]))

        matching = api.list_pull_requests(state=state, head=head, base=base)
        start = (page - 1) * per_page
        window = matching[start:start + per_page]
        payload = api.pull_requests_json(window)

        headers: dict[str, str] = {}
        links = []
        base_url = f"{self.server.url}{urllib.parse.urlparse(self.path).path}"  # type: ignore[attr-defined]

        def link(target: int, rel: str) -> str:
            params = {k: v[0] for k, v in query.items()}
            params["page"] = str(target)
            params["per_page"] = str(per_page)
            return f'<{base_url}?{urllib.parse.urlencode(params)}>; rel="{rel}"'

        last_page = max(1, (len(matching) + per_page - 1) // per_page)
        if page < last_page:
            links.append(link(page + 1, "next"))
            links.append(link(last_page, "last"))
        if page > 1:
            links.append(link(1, "first"))
            links.append(link(page - 1, "prev"))
        if links:
            headers["Link"] = ", ".join(links)
        return 200, payload, headers


def _error_json(message: str) -> dict[str, Any]:
    detail = message.split(": ", 1)[1] if message.startswith("Validation Failed: ") else message
    return {
        "message": message,
        "errors": [{"resource": "PullRequest", "code": "custom", "message": detail}],
        "documentation_url": "https://docs.github.com/rest",
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, api: MockGitHub) -> None:
        super().__init__(address, handler)
        self.api = api
        self.requests: list[RecordedRequest] = []
        self.url = ""


class MockGitHubServer:
    """Runs a :class:`MockGitHub` on an ephemeral port, in this process."""

    def __init__(self, api: MockGitHub | None = None, **kwargs) -> None:
        self.api = api if api is not None else MockGitHub(**kwargs)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """API base url, e.g. ``http://127.0.0.1:53421`` (``/api/v3`` also works)."""
        if self._server is None:
            raise RuntimeError("the mock server is not running")
        return self._server.url

    @property
    def requests(self) -> list[RecordedRequest]:
        if self._server is None:
            raise RuntimeError("the mock server is not running")
        return self._server.requests

    def start(self) -> "MockGitHubServer":
        if self._server is not None:
            return self
        server = _Server(("127.0.0.1", 0), _Handler, self.api)
        server.url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server, self._thread = server, thread
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self) -> "MockGitHubServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
