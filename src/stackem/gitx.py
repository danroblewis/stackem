"""The only module in stackem that runs git.

Everything else asks this module.  That keeps three promises easy to keep:

* every git invocation can be traced (``Git(..., logger=...)`` and ``Git.trace``),
  which is what ``--verbose`` prints;
* the dangerous flags live in exactly one place (CLAUDE.md invariant 11: never
  bare ``--force``);
* a conflicted rebase is a *typed outcome*, not an exception the caller has to
  string-match (SPEC.md sec 8).

stackem shells out to the real ``git`` binary -- not pygit2 or dulwich -- because
rebase-with-conflict-resolution is the entire product (SPEC.md sec 11).

Nothing here writes configuration.  ``remote_set_head`` writes a ref, which
SPEC.md sec 6.6 explicitly sanctions as the trunk-detection fallback; note that
``stackem`` with no arguments is read-only (CLAUDE.md invariant 5) and must not
call it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "MINIMUM_GIT_VERSION",
    "Git",
    "GitError",
    "GitInvocation",
    "MergeTreeResult",
    "RangeDiffEntry",
    "RebaseOutcome",
    "RebaseResult",
    "RebaseState",
]

#: SPEC.md sec 11: ``merge-tree --write-tree`` needs 2.38; ``--force-if-includes``
#: needs 2.30.  Assert on first run.
MINIMUM_GIT_VERSION = (2, 38)

#: Environment every invocation gets, so git never blocks on a human and its
#: output is stable enough to parse.
_FIXED_ENV = {
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
    "LANG": "C",
}


class GitError(RuntimeError):
    """A git invocation failed.

    Carries everything needed to report it: the argv, the working directory, the
    exit code and both streams.  A conflicted rebase is NOT one of these -- see
    :class:`RebaseOutcome`.
    """

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        cwd: str = "",
        message: str | None = None,
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cwd = cwd
        detail = (stderr or stdout).strip()
        text = message or f"{shlex.join(self.argv)} exited {returncode}"
        if detail:
            text = f"{text}\n{detail}"
        super().__init__(text)


@dataclass(frozen=True)
class GitInvocation:
    """One git run, recorded for ``--verbose`` and for tests."""

    argv: tuple[str, ...]
    cwd: str
    returncode: int
    duration_s: float
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


class RebaseOutcome(Enum):
    """Result of a rebase step.

    ``CONFLICT`` is an ordinary outcome, not an error: sync reports it and exits,
    leaving git's normal rebase state for the user to resolve (SPEC.md sec 8).
    """

    OK = "ok"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class RebaseResult:
    outcome: RebaseOutcome
    branch: str | None
    onto: str | None
    upstream: str | None
    returncode: int
    stdout: str
    stderr: str
    conflicted_files: tuple[str, ...] = ()
    stopped_sha: str | None = None
    stopped_subject: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is RebaseOutcome.OK


@dataclass(frozen=True)
class RebaseState:
    """What ``.git/rebase-merge`` (or ``rebase-apply``) says about a rebase.

    CLAUDE.md invariant 22: a rebase is stackem's iff ``head_name`` is a member of
    the derived stack AND ``onto`` equals the tip of that branch's derived parent.
    Anything else is the user's own rebase and must not be continued.
    """

    kind: str  # "merge" (the default backend) or "apply"
    directory: Path
    head_name: str | None  # e.g. "refs/heads/auth-ui", or "detached HEAD"
    branch: str | None  # head_name reduced to a short branch name, else None
    onto: str | None
    orig_head: str | None
    stopped_sha: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class RangeDiffEntry:
    """One line of ``git range-diff`` (SPEC.md sec 6.1).

    ``status`` is git's own sign: ``=`` identical patch, ``!`` changed patch,
    ``<`` present only in the old range (a dropped commit), ``>`` present only in
    the new range.  Invariant 10: drops are detected here, never by parsing
    rebase output.
    """

    old_index: int | None
    old_sha: str | None
    status: str
    new_index: int | None
    new_sha: str | None
    subject: str


@dataclass(frozen=True)
class MergeTreeResult:
    """``git merge-tree --write-tree`` (SPEC.md sec 6.4).

    A branch is contained in the trunk when ``tree`` equals the trunk's own tree
    -- but only once ``rev_list_count(trunk..branch) > 0`` has ruled out a branch
    that is merely behind (CLAUDE.md invariant 19).
    """

    tree: str | None
    conflicted: bool
    stdout: str
    stderr: str


_RANGE_DIFF_LINE = re.compile(
    r"^(?P<old_index>\d+|-):\s+(?P<old_sha>[0-9a-f]+|-+)\s+"
    r"(?P<status>[=!<>])\s+"
    r"(?P<new_index>\d+|-):\s+(?P<new_sha>[0-9a-f]+|-+)\s+"
    r"(?P<subject>.*)$"
)


class Git:
    """A git command runner bound to one working directory.

    >>> git = Git("/path/to/repo", logger=print)
    >>> git.rev_parse("HEAD")               # doctest: +SKIP
    '9c4e1a2...'

    Pass ``logger`` to see every invocation (that is ``--verbose``); ``trace``
    keeps the same records for tests.
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str],
        *,
        env: Mapping[str, str] | None = None,
        logger: Callable[[GitInvocation], None] | None = None,
        binary: str = "git",
    ) -> None:
        self.cwd = Path(cwd)
        self.binary = binary
        self.logger = logger
        self.trace: list[GitInvocation] = []
        self.env: dict[str, str] = {**os.environ, **_FIXED_ENV, **(env or {})}

    # -- the choke point ---------------------------------------------------

    def run(
        self,
        *args: str | os.PathLike[str],
        check: bool = True,
        cwd: str | os.PathLike[str] | None = None,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git <args>`` and return the CompletedProcess.

        stdout and stderr are always captured as text.  With ``check=True`` a
        non-zero exit raises :class:`GitError`.
        """
        argv = [self.binary, *(str(a) for a in args)]
        where = str(cwd) if cwd is not None else str(self.cwd)
        started = time.monotonic()
        proc = subprocess.run(
            argv,
            cwd=where,
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        invocation = GitInvocation(
            argv=tuple(argv),
            cwd=where,
            returncode=proc.returncode,
            duration_s=time.monotonic() - started,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        self.trace.append(invocation)
        if self.logger is not None:
            self.logger(invocation)
        if check and proc.returncode != 0:
            raise GitError(argv, proc.returncode, proc.stdout, proc.stderr, where)
        return proc

    def out(self, *args: str | os.PathLike[str], **kwargs) -> str:
        """stdout of ``git <args>``, stripped."""
        return self.run(*args, **kwargs).stdout.strip()

    def lines(self, *args: str | os.PathLike[str], **kwargs) -> list[str]:
        """Non-empty stdout lines of ``git <args>``."""
        return [line for line in self.run(*args, **kwargs).stdout.splitlines() if line]

    # -- version ----------------------------------------------------------

    def version(self) -> tuple[int, ...]:
        text = self.out("--version")
        numbers = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
        if not numbers:  # pragma: no cover - git always prints a version
            raise GitError(("git", "--version"), 0, text, "", str(self.cwd))
        return tuple(int(part) for part in numbers.groups() if part is not None)

    def check_version(self, minimum: tuple[int, ...] = MINIMUM_GIT_VERSION) -> None:
        found = self.version()
        if found < minimum:
            pretty_found = ".".join(str(n) for n in found)
            pretty_min = ".".join(str(n) for n in minimum)
            raise GitError(
                ("git", "--version"),
                1,
                "",
                "",
                str(self.cwd),
                message=f"git {pretty_min} or newer is required; found {pretty_found}",
            )

    # -- reading ----------------------------------------------------------

    def rev_parse(self, rev: str) -> str:
        """Resolve ``rev`` to a full object id.  Raises if it does not exist."""
        return self.out("rev-parse", "--verify", rev)

    def try_rev_parse(self, rev: str) -> str | None:
        """Resolve ``rev``, or ``None`` when it does not exist."""
        proc = self.run("rev-parse", "-q", "--verify", rev, check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def tree_id(self, rev: str) -> str:
        """The tree of ``rev`` -- the other half of the SPEC.md sec 6.4 test."""
        return self.rev_parse(f"{rev}^{{tree}}")

    def merge_base(self, a: str, b: str) -> str | None:
        """``git merge-base a b``; ``None`` when the histories are unrelated.

        SPEC.md sec 2: the fork point is ``merge_base("origin/<parent>", branch)``
        -- the parent's *last-synced* state, never its local tip.
        """
        proc = self.run("merge-base", a, b, check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        if proc.returncode == 1:
            return None
        raise GitError(
            proc.args, proc.returncode, proc.stdout, proc.stderr, str(self.cwd)
        )

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """``git merge-base --is-ancestor`` -- the skip-check and the guard.

        CLAUDE.md invariant 3: check "already based on target" with this BEFORE
        running the fork-point guard with it.
        """
        proc = self.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        raise GitError(
            proc.args, proc.returncode, proc.stdout, proc.stderr, str(self.cwd)
        )

    def for_each_ref(self, pattern: str, fmt: str = "%(refname:short)") -> list[str]:
        return self.lines("for-each-ref", pattern, f"--format={fmt}")

    def branches(self, namespace: str = "refs/heads") -> dict[str, str]:
        """``{short name: object id}`` for one ref namespace.

        SPEC.md sec 5.1 gets every branch in one call.  For a remote namespace
        (``refs/remotes/origin``) the symbolic ``HEAD`` entry is left out.
        """
        prefix = namespace.rstrip("/") + "/"
        result: dict[str, str] = {}
        for line in self.for_each_ref(namespace, "%(refname) %(objectname)"):
            refname, _, oid = line.partition(" ")
            if not refname.startswith(prefix):
                continue
            short = refname[len(prefix):]
            if short == "HEAD":
                continue
            result[short] = oid.strip()
        return result

    def rev_list(self, *args: str) -> list[str]:
        """Commit ids for a revision range, newest first."""
        return self.lines("rev-list", *args)

    def rev_list_count(self, *args: str) -> int:
        """``git rev-list --count`` -- the unique-commit guard (invariant 19)."""
        return int(self.out("rev-list", "--count", *args) or 0)

    def commit_subject(self, rev: str) -> str:
        return self.out("log", "-1", "--format=%s", rev)

    def symbolic_ref(self, name: str, *, short: bool = False) -> str | None:
        """Resolve a symbolic ref, or ``None`` when it is unset.

        SPEC.md sec 6.6: ``refs/remotes/origin/HEAD`` can be unset.
        """
        args = ["symbolic-ref", "-q"]
        if short:
            args.append("--short")
        proc = self.run(*args, name, check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def current_branch(self) -> str | None:
        """The checked-out branch, or ``None`` when HEAD is detached.

        HEAD is detached during a conflicted rebase -- use
        :meth:`read_rebase_state` there.
        """
        return self.symbolic_ref("HEAD", short=True)

    def remote_set_head(self, remote: str = "origin") -> None:
        """``git remote set-head <remote> -a`` (SPEC.md sec 6.6 fallback).

        This WRITES a ref.  ``stackem`` with no arguments must not call it
        (CLAUDE.md invariant 5).
        """
        self.run("remote", "set-head", remote, "-a")

    def status_porcelain(self, *, untracked: bool = True) -> str:
        args = ["status", "--porcelain"]
        if not untracked:
            args.append("--untracked-files=no")
        return self.run(*args).stdout

    def is_clean(self, *, untracked: bool = True) -> bool:
        """SPEC.md sec 11: a restack requires a clean worktree.

        ``untracked=False`` ignores untracked files, which is the rule a rebase
        actually has: git refuses one with *modified tracked* files, and a stray
        scratch file in the worktree is not a reason to refuse to sync.  Nothing
        stackem does can destroy an untracked file -- the one restore it performs
        is ``git reset --hard`` (SPEC.md sec 5.2 step 9), which leaves them
        alone.
        """
        return self.status_porcelain(untracked=untracked).strip() == ""

    def git_path(self, relative: str) -> Path:
        """Resolve a path inside the git directory (worktree-safe)."""
        value = self.out("rev-parse", "--git-path", relative)
        path = Path(value)
        return path if path.is_absolute() else self.cwd / path

    # -- rebase: the one primitive (SPEC.md sec 2) -------------------------

    def rebase_onto(self, onto: str, upstream: str, branch: str) -> RebaseResult:
        """``git rebase --onto <onto> <upstream> <branch>``.

        ``onto`` is where the parent is NOW, ``upstream`` is the fork point --
        where the parent WAS.  A conflict comes back as
        ``RebaseOutcome.CONFLICT`` with the conflicted files; any other failure
        (bad ref, dirty worktree) raises :class:`GitError`.
        """
        proc = self.run("rebase", "--onto", onto, upstream, branch, check=False)
        return self._rebase_result(proc, branch=branch, onto=onto, upstream=upstream)

    def rebase_continue(self) -> RebaseResult:
        """``git rebase --continue`` after the user staged their resolution.

        Raises :class:`GitError` when no rebase is in progress -- sync must never
        continue a rebase it did not start (CLAUDE.md invariant 22), so callers
        check :meth:`read_rebase_state` first.
        """
        state = self.read_rebase_state()
        proc = self.run("rebase", "--continue", check=False)
        return self._rebase_result(
            proc,
            branch=state.branch if state else None,
            onto=state.onto if state else None,
            upstream=None,
        )

    def rebase_abort(self) -> None:
        """``git rebase --abort``.  stackem never calls this on the user's behalf
        (CLAUDE.md invariant 24); it exists for tests and for symmetry."""
        self.run("rebase", "--abort")

    def rebase_in_progress(self) -> bool:
        return self._rebase_directory() is not None

    def read_rebase_state(self) -> RebaseState | None:
        """Read ``.git/rebase-merge`` or ``.git/rebase-apply``; ``None`` if idle."""
        directory = self._rebase_directory()
        if directory is None:
            return None
        kind = "merge" if directory.name == "rebase-merge" else "apply"
        head_name = _read_text(directory / "head-name")
        branch = None
        if head_name and head_name.startswith("refs/heads/"):
            branch = head_name[len("refs/heads/"):]
        stopped = _read_text(directory / "stopped-sha") or _read_text(
            directory / "original-commit"
        )
        return RebaseState(
            kind=kind,
            directory=directory,
            head_name=head_name,
            branch=branch,
            onto=_read_text(directory / "onto"),
            orig_head=_read_text(directory / "orig-head"),
            stopped_sha=stopped,
            message=_read_text(directory / "message"),
        )

    def conflicted_files(self) -> tuple[str, ...]:
        return tuple(self.lines("diff", "--name-only", "--diff-filter=U"))

    def _rebase_directory(self) -> Path | None:
        for name in ("rebase-merge", "rebase-apply"):
            path = self.git_path(name)
            if path.is_dir():
                return path
        return None

    def _rebase_result(
        self,
        proc: subprocess.CompletedProcess[str],
        *,
        branch: str | None,
        onto: str | None,
        upstream: str | None,
    ) -> RebaseResult:
        if proc.returncode == 0:
            return RebaseResult(
                outcome=RebaseOutcome.OK,
                branch=branch,
                onto=onto,
                upstream=upstream,
                returncode=0,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        state = self.read_rebase_state()
        if state is None:
            # Not a conflict: a bad ref, a dirty worktree, a missing branch.
            raise GitError(
                proc.args, proc.returncode, proc.stdout, proc.stderr, str(self.cwd)
            )
        stopped_subject = None
        if state.stopped_sha:
            stopped_subject = self.commit_subject(state.stopped_sha)
        elif state.message:
            stopped_subject = state.message.splitlines()[0]
        return RebaseResult(
            outcome=RebaseOutcome.CONFLICT,
            branch=branch or state.branch,
            onto=onto or state.onto,
            upstream=upstream,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            conflicted_files=self.conflicted_files(),
            stopped_sha=state.stopped_sha,
            stopped_subject=stopped_subject,
        )

    # -- verification (SPEC.md sec 6.1, 6.4) -------------------------------

    def range_diff(self, old_range: str, new_range: str) -> list[RangeDiffEntry]:
        """Parsed ``git range-diff``: one entry per commit, in order."""
        entries = []
        for line in self.range_diff_raw(old_range, new_range).splitlines():
            match = _RANGE_DIFF_LINE.match(line)
            if not match:
                continue  # indented diff body of a "!" entry
            entries.append(
                RangeDiffEntry(
                    old_index=_maybe_int(match["old_index"]),
                    old_sha=_maybe_sha(match["old_sha"]),
                    status=match["status"],
                    new_index=_maybe_int(match["new_index"]),
                    new_sha=_maybe_sha(match["new_sha"]),
                    subject=match["subject"].strip(),
                )
            )
        return entries

    def range_diff_raw(self, old_range: str, new_range: str) -> str:
        return self.run("range-diff", "--no-color", old_range, new_range).stdout

    def merge_tree_write_tree(self, base: str, branch: str) -> MergeTreeResult:
        """``git merge-tree --write-tree`` -- offline merge detection."""
        proc = self.run("merge-tree", "--write-tree", base, branch, check=False)
        if proc.returncode > 1:
            raise GitError(
                proc.args, proc.returncode, proc.stdout, proc.stderr, str(self.cwd)
            )
        first = proc.stdout.splitlines()[0].strip() if proc.stdout.strip() else None
        return MergeTreeResult(
            tree=first or None,
            conflicted=proc.returncode == 1,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # -- remote ------------------------------------------------------------

    def fetch(
        self,
        remote: str = "origin",
        *,
        prune: bool = False,
        refspecs: Iterable[str] = (),
        tags: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        args: list[str] = ["fetch"]
        if prune:
            args.append("--prune")
        if tags:
            args.append("--tags")
        args.append(remote)
        args.extend(refspecs)
        return self.run(*args, check=check)

    def push(
        self,
        remote: str,
        refspecs: Sequence[str],
        *,
        atomic: bool = True,
        force_with_lease: bool = True,
        force_if_includes: bool = True,
        delete: bool = False,
        dry_run: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Push, with the only force flags stackem is allowed to use.

        CLAUDE.md invariant 11: ``--force-with-lease --force-if-includes``, never
        bare ``--force`` -- which is why a leading ``+`` in a refspec (a forced
        refspec by another name) is refused here.
        Invariant 12: ``--atomic`` across every changed branch, so a lease failure
        on one rolls the whole push back.

        Returns the CompletedProcess without raising by default: a rejected push
        is an outcome sync reports, not a crash.
        """
        for refspec in refspecs:
            if refspec.startswith("+"):
                raise ValueError(
                    f"refusing a forced refspec {refspec!r}: "
                    "use --force-with-lease --force-if-includes (invariant 11)"
                )
        args: list[str] = ["push"]
        if atomic:
            args.append("--atomic")
        if delete:
            args.append("--delete")
        else:
            if force_with_lease:
                args.append("--force-with-lease")
            if force_if_includes:
                args.append("--force-if-includes")
        if dry_run:
            args.append("--dry-run")
        args.append(remote)
        args.extend(refspecs)
        return self.run(*args, check=check)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _maybe_int(token: str) -> int | None:
    return int(token) if token.isdigit() else None


def _maybe_sha(token: str) -> str | None:
    return None if set(token) == {"-"} else token
