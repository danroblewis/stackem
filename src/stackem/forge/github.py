"""GitHub, behind the forge Provider protocol (SPEC.md sec 11).

Two transports, one provider
----------------------------
``gh`` when it is on PATH -- auth is already solved there, including SSO,
enterprise hosts and keyring tokens -- and plain REST over ``urllib`` with
``GITHUB_TOKEN`` when it is not.  ``gh`` is not universally installed, so the
fallback is not optional.  Both answer with a :class:`Response`, so the provider
never knows which one replied, and a test can aim either at the in-process mock
server by passing ``base_url``.

Standard library only: ``uvx stackem`` must work with no install step
(SPEC.md sec 11), so no ``requests``.

Aiming it somewhere else
------------------------
``base_url`` and ``repo`` are arguments, so the caller decides.  Two environment
variables do the same without code, which is how an end-to-end test reaches the
mock server: ``STACKEM_GITHUB_API_URL`` (where the API lives) and
``STACKEM_GITHUB_REPO`` (``owner/name``, for when the git remote is a bare
repository on disk and has no slug to parse).  Neither is state stackem keeps --
nothing is written, and an unset environment behaves exactly as before.

What this module refuses to do
------------------------------
No ``create``, ``close``, ``delete``, ``reopen`` or ``merge``.  CLAUDE.md
invariant 14 and SPEC.md sec 6.5: sync reports and prints the command; the only
pull request write it ever performs is retargeting a base.  The mock server
implements the other verbs because tests must *create* those situations -- that
is not a gap here waiting to be filled.

Three invariants live in this file
----------------------------------
16  Branch to pull request is not one-to-one: index by head ref with precedence
    **open > most recent merged > closed**.  :func:`stackem.forge.select_pull_request`
    is the single implementation of that rule, so every forge agrees; this
    module calls it rather than re-deriving it.
17  Fork pull requests are never indexed as a local branch's pull request.  They
    still come back from :meth:`GitHubProvider.list_open_pull_requests` *marked*,
    because the caller may want to mention one.  A pull request whose head
    repository GitHub has already deleted counts as a fork: unknown provenance
    is treated as foreign, because adopting a stranger's pull request and then
    retargeting it is the damaging mistake.
18  Never a single ``--state all --limit 100`` window.  Open pull requests are
    listed with ``state=open`` and paged to exhaustion; merged and closed state
    is asked for **per branch** with a ``head=`` filter.  An active repository's
    churn therefore cannot push the stack's own pull requests out of view.

A note on merged state: a merged pull request has REST state ``closed`` with
``merged_at`` set.  "merged" is not a REST state, and only the single-pull-request
endpoint carries the boolean ``merged`` field -- so :class:`PullRequestState` is
derived from ``merged_at``, which every endpoint returns.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol

from stackem.forge import ForgeError, select_pull_request
from stackem.model import PullRequest, PullRequestState, RepoSettings

__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "GhTransport",
    "GitHubApiError",
    "GitHubProvider",
    "Response",
    "RestTransport",
    "Transport",
    "api_base_url",
    "parse_remote_url",
]

#: github.com's REST root.  A GitHub Enterprise host is ``https://<host>/api/v3``.
DEFAULT_BASE_URL = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
USER_AGENT = "stackem"

#: Environment variables that aim the provider somewhere else -- an enterprise
#: host, or the mock server in a test.  ``STACKEM_GITHUB_API_URL`` is ours and
#: overrides what the git remote says; ``GITHUB_API_URL`` is GitHub's own (CI
#: sets it) and is only a default.  ``STACKEM_GITHUB_REPO`` supplies the
#: ``owner/name`` when the remote url has none to parse -- which is exactly the
#: case for a test sandbox whose origin is a bare repository on disk.
BASE_URL_OVERRIDE_ENV = "STACKEM_GITHUB_API_URL"
BASE_URL_DEFAULT_ENV = "GITHUB_API_URL"
REPO_ENV = "STACKEM_GITHUB_REPO"
TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")

#: GitHub caps ``per_page`` at 100, so this is the largest page there is.  The
#: answer to invariant 18 is to ask for every page, not for a bigger one.
PER_PAGE = 100
#: A server that always advertises another page must not spin forever.
MAX_PAGES = 200


class GitHubApiError(ForgeError):
    """GitHub answered with a 4xx or 5xx.

    ``status`` is the HTTP status, so a caller can tell "no such branch" (404)
    from "that would be invalid" (422) without reading English.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        payload: Any = None,
        method: str = "",
        url: str = "",
    ) -> None:
        self.status = status
        self.payload = payload
        self.method = method
        self.url = url
        super().__init__(message)

    @classmethod
    def from_response(cls, method: str, url: str, response: "Response") -> "GitHubApiError":
        detail = _error_detail(response.payload)
        text = f"GitHub API {method} {url} -> {response.status}"
        return cls(
            f"{text}: {detail}" if detail else text,
            status=response.status,
            payload=response.payload,
            method=method,
            url=url,
        )


@dataclass(frozen=True)
class Response:
    """One HTTP answer, whichever transport produced it.

    A 4xx comes back as a value rather than an exception because the provider
    decides what it means: a 404 from ``GET /branches/<name>`` is the answer to
    "does this branch still exist" (SPEC.md sec 5.1), while a 404 anywhere else
    is a failure.
    """

    status: int
    payload: Any
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def has_next_page(self) -> bool | None:
        """What the ``Link`` header says, or None when it says nothing."""
        link = self.headers.get("link")
        if not link:
            return None
        return 'rel="next"' in link


class Transport(Protocol):
    """How a request reaches GitHub: the ``gh`` binary, or urllib."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response: ...


class RestTransport:
    """Plain REST over urllib, authenticated with a token."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        url = _join(self.base_url, path, params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, method=method.upper(), data=data)
        request.add_header("Accept", ACCEPT)
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", self.user_agent)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return Response(
                    status=response.status,
                    payload=_decode(response.read()),
                    headers=_lower(response.headers.items()),
                    url=url,
                )
        except urllib.error.HTTPError as err:  # an answer, not a crash
            return Response(
                status=err.code,
                payload=_decode(err.read()),
                headers=_lower(err.headers.items()),
                url=url,
            )
        except urllib.error.URLError as err:
            raise ForgeError(f"GitHub is unreachable: {method.upper()} {url}: {err.reason}") from err
        except TimeoutError as err:
            raise ForgeError(f"GitHub timed out: {method.upper()} {url}") from err


class GhTransport:
    """``gh api`` -- the preferred path, because auth is already solved there.

    ``--include`` is always passed so the status line and the ``Link`` header
    come back.  ``gh`` exits non-zero on a 4xx but still prints the body, so an
    error parses exactly like a success; an exit with no HTTP answer at all
    (not logged in, no network) is the one case that raises.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        binary: str = "gh",
        env: Mapping[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.binary = binary
        self.env = dict(env) if env is not None else None
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        # gh resolves a bare path against the host it is authenticated to; an
        # absolute url is what an enterprise host -- and the test mock -- needs.
        if self.base_url in ("", DEFAULT_BASE_URL):
            endpoint = _join("", path, params).lstrip("/")
        else:
            endpoint = _join(self.base_url, path, params)
        argv = [
            self._executable(),
            "api",
            "--include",
            "--method",
            method.upper(),
            "-H",
            f"Accept: {ACCEPT}",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
        ]
        stdin = None
        if body is not None:
            stdin = json.dumps(body)
            argv += ["--input", "-"]
        argv.append(endpoint)
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                env=self.env,
                timeout=self.timeout,
            )
        except FileNotFoundError as err:
            raise ForgeError(f"{self.binary} is not installed") from err
        except subprocess.TimeoutExpired as err:
            raise ForgeError(f"{self.binary} api {endpoint} timed out") from err

        status, headers, raw = _split_gh_response(proc.stdout)
        if status is None:
            detail = (proc.stderr or proc.stdout).strip()
            raise ForgeError(
                f"{self.binary} api {endpoint} failed (exit {proc.returncode})"
                + (f": {detail}" if detail else "")
            )
        return Response(
            status=status,
            payload=_decode(raw.encode()),
            headers=headers,
            url=endpoint,
        )

    def _executable(self) -> str:
        """Resolve ``gh`` against the PATH we were handed, not the process's."""
        path = None if self.env is None else self.env.get("PATH")
        return shutil.which(self.binary, path=path) or self.binary


class GitHubProvider:
    """The forge Provider (see :mod:`stackem.forge`) for GitHub.

    >>> provider = GitHubProvider.from_remote_url("git@github.com:acme/app.git")
    >>> provider.repo_settings().delete_branch_on_merge     # doctest: +SKIP
    False

    ``transport`` is ``"auto"`` (``gh`` if it is on PATH, else ``GITHUB_TOKEN``
    and REST), or ``"gh"``, or ``"rest"``, or any object with a ``request``
    method -- which is how a test points the provider at the mock server.
    """

    def __init__(
        self,
        repo: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: Transport | str = "auto",
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        gh_binary: str = "gh",
    ) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"repo must be 'owner/name', not {repo!r}")
        self.repo = repo
        self.owner = owner
        self.name = name
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.base_url = (base_url or self._base_url_from_env() or DEFAULT_BASE_URL).rstrip("/")
        self.transport: Transport = self._make_transport(
            transport, token=token, timeout=timeout, gh_binary=gh_binary
        )
        self._settings: RepoSettings | None = None

    # -- construction ------------------------------------------------------

    @classmethod
    def from_remote_url(cls, remote_url: str, **kwargs: Any) -> "GitHubProvider":
        """Build a provider from a git remote url -- ``origin``'s, usually.

        The environment overrides the remote, in both halves: ``STACKEM_GITHUB_REPO``
        says which repository this is when the url has no ``owner/name`` to parse
        (a bare repository on disk, in a test), and ``STACKEM_GITHUB_API_URL``
        says where to reach it.  An explicit ``base_url`` argument still wins.
        """
        env = kwargs.get("env") or os.environ
        override = env.get(BASE_URL_OVERRIDE_ENV)
        slug = env.get(REPO_ENV)
        host = None
        if not slug:
            host, slug = parse_remote_url(remote_url)
        if host and not kwargs.get("base_url") and not override:
            kwargs["base_url"] = api_base_url(host)
        return cls(slug, **kwargs)

    def _base_url_from_env(self) -> str | None:
        return self.env.get(BASE_URL_OVERRIDE_ENV) or self.env.get(BASE_URL_DEFAULT_ENV) or None

    def _token(self, token: str | None) -> str | None:
        if token:
            return token
        for name in TOKEN_ENV:
            value = self.env.get(name)
            if value:
                return value
        return None

    def _make_transport(
        self, choice: Transport | str, *, token: str | None, timeout: float, gh_binary: str
    ) -> Transport:
        if not isinstance(choice, str):
            return choice
        resolved = self._token(token)
        if choice == "rest":
            return RestTransport(self.base_url, resolved, timeout=timeout)
        if choice == "gh":
            return GhTransport(self.base_url, binary=gh_binary, env=self.env, timeout=timeout)
        if choice != "auto":
            raise ValueError(f"unknown transport {choice!r}")
        if shutil.which(gh_binary, path=self.env.get("PATH")):
            # gh first: its auth already covers SSO and enterprise hosts.
            return GhTransport(self.base_url, binary=gh_binary, env=self.env, timeout=timeout)
        if resolved:
            return RestTransport(self.base_url, resolved, timeout=timeout)
        raise ForgeError(
            "no way to reach GitHub: install `gh` (https://cli.github.com) and run "
            "`gh auth login`, or set GITHUB_TOKEN to a token with repo access"
        )

    # -- the Provider protocol --------------------------------------------

    def repo_slug(self) -> str:
        """``owner/name``, for printing the rescue commands of SPEC.md sec 6.3."""
        return self.repo

    def repo_settings(self) -> RepoSettings:
        """Repository settings, read once per run.

        SPEC.md sec 6.3: ``delete_branch_on_merge`` orphans a child pull request
        on every merge, so sync warns when it is on.
        """
        if self._settings is None:
            payload = self._json("GET", self._path())
            self._settings = RepoSettings(
                default_branch=str(payload.get("default_branch") or ""),
                delete_branch_on_merge=bool(payload.get("delete_branch_on_merge")),
            )
        return self._settings

    def default_branch(self) -> str:
        """The repository's default branch -- the last trunk fallback (sec 6.6)."""
        return self.repo_settings().default_branch

    def list_open_pull_requests(self) -> list[PullRequest]:
        """Every OPEN pull request, every page of them (invariant 18).

        Fork pull requests are included but marked ``is_cross_repository``;
        keeping them out of the index is :func:`select_pull_request`'s job
        (invariant 17), and a caller may want to mention one.
        """
        return self._list_pulls({"state": "open"})

    def get_pull_request_for_branch(self, branch: str) -> PullRequest | None:
        """The pull request that represents ``branch``, or None.

        Asked **per branch** with a ``head=`` filter, so the answer cannot be
        crowded out by an active repository's closed pull requests (invariant
        18).  Precedence and fork exclusion are
        :func:`select_pull_request`'s (invariants 16 and 17) -- GitHub's own
        ``head=owner:ref`` filter drops a stranger's fork, but not a fork in a
        second repository belonging to the same owner, so the client-side
        exclusion is load-bearing.
        """
        candidates = self._list_pulls({"state": "all", "head": f"{self.owner}:{branch}"})
        return select_pull_request(candidates, branch)

    def retarget(self, pr_number: int, new_base: str) -> None:
        """Point a pull request's base at ``new_base``.

        The pull request's base IS the parent record (invariant 1), so this is
        the whole of reparenting.  It is idempotent, and it must happen before
        anything is deleted (invariant 15).
        """
        self._json("PATCH", self._path("pulls", str(pr_number)), body={"base": new_base})

    def branch_exists_on_remote(self, branch: str) -> bool:
        """Whether the branch still exists on the forge (SPEC.md sec 5.1).

        A 404 is the answer here, not a failure: closed, not merged, and the
        head branch gone is what makes a pull request an orphan.
        """
        return self._request("GET", self._path("branches", branch), allow=(404,)).ok

    # -- internals ---------------------------------------------------------

    def _path(self, *parts: str) -> str:
        quoted = [urllib.parse.quote(part, safe="/") for part in parts]
        return "/".join(["repos", self.owner, self.name, *quoted])

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        allow: tuple[int, ...] = (),
    ) -> Response:
        response = self.transport.request(method, path, params=params, body=body)
        if not response.ok and response.status not in allow:
            raise GitHubApiError.from_response(method.upper(), response.url or path, response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs).payload
        return payload if isinstance(payload, dict) else {}

    def _list_pulls(self, params: Mapping[str, Any]) -> list[PullRequest]:
        """Page ``/pulls`` to exhaustion (invariant 18).

        The ``Link`` header decides when to stop; a server that does not send
        one is paged until it returns a short page.  A server that never stops
        offering pages is an error rather than a loop -- and rather than a
        silently truncated answer, which is the very failure invariant 18 is
        about.
        """
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = self._request(
                "GET",
                self._path("pulls"),
                params={**params, "per_page": PER_PAGE, "page": page},
            )
            payload = response.payload if isinstance(response.payload, list) else []
            rows.extend(row for row in payload if isinstance(row, dict))
            more = response.has_next_page
            if more is None:
                more = len(payload) == PER_PAGE
            if not more:
                break
        else:
            raise ForgeError(
                f"GitHub kept offering more pull requests after {MAX_PAGES} pages; "
                "refusing to answer from a truncated list"
            )
        return [_pull_request(row, self.repo) for row in rows]


# --------------------------------------------------------------------------
# remote urls
# --------------------------------------------------------------------------

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")


def parse_remote_url(remote_url: str) -> tuple[str, str]:
    """``(host, "owner/name")`` for a git remote url.

    Handles both shapes git uses: ``git@github.com:acme/app.git`` (scp-like)
    and ``https://github.com/acme/app.git``.
    """
    text = remote_url.strip()
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        host, path = parsed.hostname or "", parsed.path
    else:
        match = _SCP_LIKE.match(text)
        if match is None:
            raise ValueError(f"not a git remote url: {remote_url!r}")
        host, path = match["host"], match["path"]
    slug = path.strip("/")
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    owner, _, name = slug.partition("/")
    if not host or not owner or not name or "/" in name:
        raise ValueError(f"not a GitHub remote url: {remote_url!r}")
    return host, f"{owner}/{name}"


def api_base_url(host: str) -> str:
    """The REST root for a host: github.com's, or an enterprise ``/api/v3``."""
    if host.lower() in ("github.com", "www.github.com", "api.github.com"):
        return DEFAULT_BASE_URL
    return f"https://{host}/api/v3"


# --------------------------------------------------------------------------
# JSON -> model
# --------------------------------------------------------------------------


def _pull_request(payload: Mapping[str, Any], repo_full_name: str) -> PullRequest:
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    head_repo = head.get("repo") or {}
    base_repo = base.get("repo") or {}
    base_full = base_repo.get("full_name") or repo_full_name
    head_full = head_repo.get("full_name")
    head_owner = (head_repo.get("owner") or {}).get("login")
    if not head_owner:
        head_owner = (head.get("label") or "").rpartition(":")[0] or None

    if head_full:
        is_cross_repository = head_full != base_full
    else:
        # GitHub nulls head.repo once a fork is deleted.  Invariant 17: unknown
        # provenance is treated as a fork, because adopting a stranger's pull
        # request and retargeting it is the damaging mistake.
        is_cross_repository = True

    merged_at = _timestamp(payload.get("merged_at"))
    if merged_at is not None or payload.get("merged"):
        # "merged" is not a REST state: it is `closed` plus merged_at.
        state = PullRequestState.MERGED
    elif str(payload.get("state", "")).lower() == "open":
        state = PullRequestState.OPEN
    else:
        state = PullRequestState.CLOSED

    return PullRequest(
        number=int(payload["number"]),
        head=head.get("ref") or "",
        base=base.get("ref") or "",
        state=state,
        title=payload.get("title") or "",
        url=payload.get("html_url") or "",
        is_cross_repository=is_cross_repository,
        head_repository_owner=head_owner,
        head_sha=head.get("sha"),
        merge_commit_sha=payload.get("merge_commit_sha"),
        merged_at=merged_at,
        closed_at=_timestamp(payload.get("closed_at")),
        updated_at=_timestamp(payload.get("updated_at")),
        draft=bool(payload.get("draft")),
    )


def _timestamp(value: Any) -> datetime | None:
    """Parse GitHub's ISO-8601 stamps (``2026-01-01T00:01:00Z``)."""
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# HTTP odds and ends
# --------------------------------------------------------------------------


def _join(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> str:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}" if base_url else f"/{path.lstrip('/')}"
    if params:
        pairs = [(key, str(value)) for key, value in params.items() if value is not None]
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
    return url


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _lower(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in items}


def _split_gh_response(text: str) -> tuple[int | None, dict[str, str], str]:
    """Split ``gh api --include`` output into status, headers and body."""
    status: int | None = None
    headers: dict[str, str] = {}
    body = text
    while body.startswith("HTTP/"):
        head, separator, rest = body.partition("\r\n\r\n")
        if not separator:
            head, separator, rest = body.partition("\n\n")
        if not separator:
            break
        lines = head.replace("\r\n", "\n").split("\n")
        fields = lines[0].split()
        if len(fields) > 1 and fields[1].isdigit():
            status = int(fields[1])
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key.strip():
                headers[key.strip().lower()] = value.strip()
        body = rest
    return status, headers, body


def _error_detail(payload: Any) -> str:
    """The useful half of a GitHub error body.

    Real GitHub's top-level ``message`` for a 422 is just "Validation Failed";
    the detail is in ``errors[].message``.  Reading both is right against
    github.com, against enterprise, and against the mock server.
    """
    if payload is None:
        return ""
    if not isinstance(payload, Mapping):
        return str(payload).strip()
    message = str(payload.get("message") or "").strip()
    details: list[str] = []
    for error in payload.get("errors") or []:
        if isinstance(error, Mapping):
            text = error.get("message") or " ".join(
                str(part) for part in (error.get("field"), error.get("code")) if part
            )
        else:
            text = str(error)
        if text:
            details.append(str(text).strip())
    if details:
        joined = "; ".join(details)
        if message and joined not in message:
            return f"{message}: {joined}"
        return message or joined
    return message
