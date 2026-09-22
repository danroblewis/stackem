"""The command line: three commands, and every byte they print.

    stackem                         show the stack (READ-ONLY -- invariant 5)
    stackem sync                    make everything correct again
    stackem parent <b> --onto <p>   retarget b's pull request to p

There is no ``init`` (nothing to configure), no ``continue`` (sync is
re-entrant) and no ``abort`` (``git rebase --abort``).  CLAUDE.md, "Command
surface": a new subcommand must justify itself, and none of those three can.

What lives here
---------------
Orchestration and formatting, and nothing else.  No git logic, no forge logic.
The CLI asks an *engine* for a view of what happened and renders it; deriving
the stack, walking the cascade and talking to GitHub belong to ``stackem.stack``
and ``stackem.sync``.

Two things it does own, because they are boundary concerns:

* :class:`ReadOnlyGit` -- ``stackem`` with no arguments must never write
  (CLAUDE.md invariant 5).  A logger cannot enforce that, since ``Git`` logs
  *after* the subprocess has run, so the guard is a ``run()`` override that
  refuses before anything is spawned.
* ``--verbose`` git tracing, which goes to **stderr** so stdout stays the
  compact, parseable report SPEC.md sec 9 asks for.

The view model
--------------
The dataclasses below are what the renderers consume.  Every renderer reads
them through :func:`_field`, so an engine may hand back its own objects (or
plain dicts) as long as the attribute names match -- the CLI does not require
the sync module to import from here.

The engine contract
-------------------
``engine.status()``, ``engine.sync(dry_run=False)`` and
``engine.set_parent(branch, onto, dry_run=False)``.  It is resolved lazily from
``stackem.sync`` (``build_engine(git=...)``, else ``Engine(git=...)``) so this
module stays importable while that one is being written; ``sync()`` may also be
written without the ``dry_run`` keyword.

Those three return a :class:`StatusView`, a :class:`SyncView` and a
:class:`ParentView` -- or anything with the same attribute names.  Where a
sibling module already names the same fact differently, the renderer reads both
spellings, so its report can be handed straight through:

===========================  ================================================
this module                   also accepted
===========================  ================================================
``Conflict.sha``              ``stopped_sha``
``Conflict.subject``          ``stopped_subject``
``Conflict.onto``             ``target``, ``parent``  (``stackem.restack``)
``Conflict.onto_sha``         omit it and the ``(sha)`` is left out
``ForeignRebase.branch/onto`` ``state.branch`` / ``state.onto``
``Reparent.children``         ``(child, old, new)`` tuples, or objects with
                              ``branch``/``old_parent``/``new_parent``
===========================  ================================================

A run that stopped short sets exactly one of ``SyncView.foreign`` (invariant 22),
``conflict`` (sec 8), ``guard`` (invariant 3b), a ``verification`` whose ``ok``
is false (invariant 9) or a ``push`` whose ``ok`` is false; each renders its own
report, none of them mentions the remote unless it was really touched, and each
exits non-zero.

Output format
-------------
Every rule here comes from SPEC.md sec 9 and the worked sessions in
docs/sessions/, which are the specification for the text:

* compact text, never JSON;
* every output ends with the literal next command (invariant 23), including
  successful ones, where ``next: nothing -- the stack is current.`` is valid;
* dropped commits, emptied branches, merged branches, orphaned PRs and branches
  with no PR are reported TWICE -- inline and in the summary (SPEC.md sec 7.2)
  -- so they reach the reader instead of scrolling past;
* anything stackem will not do itself (close a PR, delete a branch, rescue an
  orphan, create a PR) is printed as the exact command to run (invariant 14,
  invariant 20).
"""

from __future__ import annotations

import os
import shlex
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from stackem import __version__
from stackem.gitx import Git, GitError, GitInvocation

__all__ = [
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "EXIT_USAGE",
    "USAGE",
    "BranchRow",
    "CliError",
    "Conflict",
    "Context",
    "Dropped",
    "Emptied",
    "Engine",
    "ForeignRebase",
    "GuardViolation",
    "MergedBranch",
    "NoPullRequest",
    "Options",
    "Orphan",
    "ParentView",
    "PushResult",
    "ReadOnlyGit",
    "ReadOnlyViolation",
    "Reparent",
    "Restack",
    "RetargetFailure",
    "StatusView",
    "SyncView",
    "UsageError",
    "Verification",
    "build_git",
    "main",
    "parse_args",
    "render_parent",
    "render_status",
    "render_sync",
    "verbose_logger",
]

EXIT_OK = 0
#: The command ran but the work is not finished: a conflict, a fork-point guard
#: violation, a rejected push, a failed retarget.  Non-zero so a wrapper knows.
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2

#: Sessions wrap their prose here; SPEC.md sec 9 wants compact output.
_WIDTH = 78

_NOTHING_TO_DO = "nothing — the stack is current."

USAGE = """\
usage: stackem [--verbose]
       stackem sync [--verbose] [--dry-run]
       stackem parent <branch> --onto <parent> [--verbose] [--dry-run]

  stackem                         show the stack and what is stale (read-only)
  stackem sync                    restack, verify, retarget PR bases, push
  stackem parent <b> --onto <p>   retarget b's pull request to p

  --verbose   print every git invocation on stderr
  --dry-run   print the plan without changing anything

There is no init (nothing to configure), no continue (sync is re-entrant: fix
the conflict, git add, run sync again) and no abort (git rebase --abort).

next: stackem\
"""

_NO_SUCH_COMMAND = {
    "init": (
        "there is no `stackem init`. stackem stores no state and changes no\n"
        "configuration, so there is nothing to set up -- just run it."
    ),
    "continue": (
        "there is no `stackem continue`. sync is re-entrant: resolve the\n"
        "conflicts, `git add` them, then run `stackem sync` again."
    ),
    "abort": (
        "there is no `stackem abort`. Back out of the rebase with\n"
        "`git rebase --abort`; the branches already restacked are correct and\n"
        "the next sync skips them."
    ),
}


# ==========================================================================
# errors
# ==========================================================================


class CliError(RuntimeError):
    """Something stopped the run, reported as text rather than a traceback."""

    def __init__(
        self,
        message: str,
        *,
        detail: Sequence[str] = (),
        next_command: str = "stackem sync",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = tuple(detail)
        self.next_command = next_command

    def render(self) -> str:
        blocks = [self.message.splitlines()]
        if self.detail:
            blocks.append(list(self.detail))
        blocks.append([f"next: {self.next_command}"])
        return _join(blocks)


class UsageError(CliError):
    """The arguments were wrong.  Exits :data:`EXIT_USAGE`."""

    def __init__(
        self,
        message: str,
        *,
        usage: bool = False,
        next_command: str = "stackem",
    ) -> None:
        detail = USAGE.splitlines()[:-2] if usage else ()
        super().__init__(message, detail=detail, next_command=next_command)


class ReadOnlyViolation(RuntimeError):
    """A read-only run tried to write.  CLAUDE.md invariant 5."""


# ==========================================================================
# the view model
# ==========================================================================


@dataclass
class BranchRow:
    """One line of ``stackem``'s stack display."""

    name: str
    pr: int | None = None
    parent: str | None = None
    #: "live", "merged" or "orphaned" -- SPEC.md sec 5.1.
    state: str = "live"
    needs_restack: bool = False
    #: Rendered parenthetically: ``needs restack (trunk moved)``.
    restack_reason: str | None = None
    #: Rendered comma-joined: ``needs restack, PR base is a merged branch``.
    notes: tuple[str, ...] = ()
    restacked_not_pushed: bool = False
    merged_as: str | None = None
    branch_deleted: bool = False


@dataclass
class StatusView:
    trunk: str
    remote: str = "origin"
    #: Commits on ``<remote>/<trunk>`` the stack is not built on.
    trunk_behind: int = 0
    branches: Sequence[Any] = ()
    #: Things the report is less sure of than it looks -- an unreachable forge,
    #: say, which makes every "no PR" line a guess (SPEC.md sec 12).
    warnings: Sequence[str] = ()


@dataclass
class Dropped:
    """A commit the rebase replayed to nothing (SPEC.md sec 7.2)."""

    sha: str
    subject: str


@dataclass
class Reparent:
    """A branch leaving the chain, and the children stitched past it.

    ``cause`` is "merged" (SPEC.md sec 5.2 step 4) or "emptied" (sec 7.2).
    ``children`` is ``(child, old parent, new parent)``.
    """

    branch: str
    cause: str = "merged"
    squashed_as: str | None = None
    children: Sequence[Any] = ()
    retargets: Sequence[Any] = ()


@dataclass
class Restack:
    """One branch's turn in the cascade."""

    branch: str
    onto: str
    #: "ok", "empty" or "conflict".
    outcome: str = "ok"
    commits: int = 0
    commits_before: int | None = None
    dropped: Sequence[Any] = ()
    pr: int | None = None
    #: True when this branch's rebase was resumed rather than started
    #: (SPEC.md sec 5.2 phase 0).
    resumed: bool = False
    no_pull_request: bool = False
    #: A chain repair caused by this branch, printed right after it.
    reparent: Any = None


@dataclass
class Verification:
    """SPEC.md sec 6.1 range-diff verification.

    ``ok`` is the gate on phase 2 (CLAUDE.md invariant 9): anything beyond an
    identical patch or a clean drop stops sync before the remote, and
    ``unexpected`` is what to show for it -- entries with ``branch``, ``status``
    and ``subject``, as ``stackem.restack.VerificationEntry`` has.
    """

    ranges: int = 0
    ok: bool = True
    unexpected: Sequence[Any] = ()


@dataclass
class PushResult:
    branches: Sequence[str] = ()
    ok: bool = True
    rejected: Sequence[str] = ()
    reason: str = ""


@dataclass
class RetargetFailure:
    """A pull request base that would not move (SPEC.md sec 5.2 step 8).

    Nothing is pushed after one: a pull request still based on a branch that is
    about to vanish would show the whole stack the moment its content landed,
    and deleting that branch would close it (invariant 13).
    """

    pr: int
    branch: str = ""
    new_base: str = ""
    error: str = ""


@dataclass
class Conflict:
    """SPEC.md sec 8."""

    branch: str
    sha: str
    subject: str
    onto: str
    onto_sha: str
    files: Sequence[str] = ()
    queued: Sequence[str] = ()


@dataclass
class GuardViolation:
    """CLAUDE.md invariant 3b: the parent was force-pushed outside sync."""

    branch: str
    parent: str
    queued: Sequence[str] = ()


@dataclass
class ForeignRebase:
    """A rebase in progress that stackem did not start (invariant 22).

    sync refuses it: continuing a user's own ``git rebase -i`` and cascading on
    top of it is the failure the check exists to prevent (SPEC.md sec 8).
    ``stackem.restack.ForeignRebase`` keeps the same facts inside a
    ``RebaseState``; the renderer reads either shape.
    """

    branch: str | None = None
    onto: str | None = None
    reason: str = ""


@dataclass
class Orphan:
    """A PR closed by a branch deletion (SPEC.md sec 6.3, invariant 14)."""

    pr: int
    branch: str
    base_pr: int | None = None
    base_branch: str | None = None
    new_base: str = ""
    repo: str = ""
    skipped: Sequence[str] = ()


@dataclass
class MergedBranch:
    name: str
    pr: int | None = None
    squashed_as: str | None = None


@dataclass
class Emptied:
    name: str
    pr: int | None = None
    parent: str = ""


@dataclass
class NoPullRequest:
    name: str
    parent: str = ""


@dataclass
class SyncView:
    trunk: str
    remote: str = "origin"
    dry_run: bool = False
    fetched: bool = True
    #: None hides the trunk line; 0 renders "unchanged".
    trunk_moved: int | None = None
    reparents: Sequence[Any] = ()
    restacks: Sequence[Any] = ()
    verification: Any = None
    push: Any = None
    pr_bases_ok: bool = False
    #: Session 01 names them -- "PR bases: #102 #103 #104 all correct".  Empty
    #: prints the short form sessions 02 to 06 use.
    pr_bases: Sequence[int] = ()
    conflict: Any = None
    guard: Any = None
    foreign: Any = None
    #: Step 8 failed, so step 9 never ran (see :class:`RetargetFailure`).
    retarget_failed: Sequence[Any] = ()
    warnings: Sequence[str] = ()
    orphans: Sequence[Any] = ()
    merged: Sequence[Any] = ()
    emptied: Sequence[Any] = ()
    no_pull_request: Sequence[Any] = ()
    #: Override the derived counts in the "done." line.
    restacked: int | None = None
    removed: int | None = None


@dataclass
class ParentView:
    branch: str
    pr: int | None = None
    old_base: str | None = None
    new_base: str = ""
    changed: bool = True
    error: str | None = None
    dry_run: bool = False


# ==========================================================================
# formatting helpers
# ==========================================================================


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a dataclass, a plain object or a mapping."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
        return default if value is None and default is not None else value
    value = getattr(obj, name, default)
    return default if value is None and default is not None else value


def _first_field(obj: Any, names: Sequence[str], default: Any = "") -> Any:
    """The first of ``names`` that is present and set.

    Sibling modules name the same thing differently -- ``stackem.restack``'s
    ``ConflictReport`` calls it ``stopped_sha`` where :class:`Conflict` calls it
    ``sha`` -- so a renderer accepts either and the engine may hand its own
    report straight through.
    """
    for name in names:
        value = _field(obj, name)
        if value not in (None, ""):
            return value
    return default


def _join(blocks: Iterable[Sequence[str]]) -> str:
    """Join blocks of lines with exactly one blank line between them."""
    kept = [list(block) for block in blocks if block]
    return "\n\n".join("\n".join(block) for block in kept)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _wrap(text: str) -> list[str]:
    return textwrap.wrap(text, width=_WIDTH) or [""]


def _labelled(label: str, text: str) -> list[str]:
    """A ``STOPPED``/``WARNING`` paragraph: the label, then a hanging indent."""
    indent = " " * (len(label) + 2)
    return textwrap.wrap(
        text, width=_WIDTH, initial_indent=f"{label}  ", subsequent_indent=indent
    ) or [label]


def _behind(count: int) -> str:
    if count <= 0:
        return "up to date"
    return _plural(count, "commit") + " behind"


def _pr_create(name: str, parent: str | None) -> str:
    return f"gh pr create --base {parent} --head {name}"


# ==========================================================================
# stackem -- the stack display (SPEC.md sec 9; sessions 01, 03, 05)
# ==========================================================================


def _row_status(row: Any) -> str:
    state = _field(row, "state", "live")
    if state == "merged":
        squashed = _field(row, "merged_as")
        text = f"MERGED (squashed as {squashed})" if squashed else "MERGED"
        if _field(row, "branch_deleted", False):
            text += ", branch deleted"
        return text
    if state == "orphaned":
        return "CLOSED — base branch was deleted"
    if _field(row, "pr") is None:
        return "no PR"
    extra = "".join(f", {note}" for note in _field(row, "notes", ()))
    if _field(row, "needs_restack", False):
        reason = _field(row, "restack_reason")
        return "needs restack" + (f" ({reason})" if reason else "") + extra
    if _field(row, "restacked_not_pushed", False):
        return "restacked, not pushed" + extra
    return "synced" + extra


def _needs_sync(rows: Sequence[Any]) -> bool:
    for row in rows:
        if _field(row, "needs_restack", False):
            return True
        if _field(row, "restacked_not_pushed", False):
            return True
        if _field(row, "state", "live") in ("merged", "orphaned"):
            return True
    return False


def render_status(view: Any) -> str:
    """``stackem``: the stack, what is stale, and the next command."""
    trunk = _field(view, "trunk", "main")
    remote = _field(view, "remote", "origin")
    rows = list(_field(view, "branches", ()))

    header = [f"{trunk} ({remote}/{trunk}, {_behind(_field(view, 'trunk_behind', 0))})"]

    markers: list[str] = []
    position = 0
    for row in rows:
        state = _field(row, "state", "live")
        if state == "merged":
            markers.append("x")
            continue
        position += 1
        markers.append("!" if state == "orphaned" else f"{position}.")

    labels = [f"#{_field(row, 'pr')}" if _field(row, "pr") else "--" for row in rows]
    statuses = [_row_status(row) for row in rows]
    marker_width = max((len(m) for m in markers), default=0)
    name_width = max((len(_field(row, "name", "")) for row in rows), default=0)
    label_width = max((len(label) for label in labels), default=0)

    for marker, row, label, status in zip(markers, rows, labels, statuses):
        header.append(
            "  "
            + marker.ljust(marker_width)
            + " "
            + _field(row, "name", "").ljust(name_width)
            + "  "
            + label.ljust(label_width)
            + "  "
            + status
        )

    blocks: list[list[str]] = [header]

    # Reported here AND carried into the next command (SPEC.md sec 6.5, sec 9).
    creates: list[str] = []
    for row in rows:
        if _field(row, "pr") is None and _field(row, "state", "live") == "live":
            name = _field(row, "name", "")
            command = _pr_create(name, _field(row, "parent"))
            creates.append(command)
            blocks.append([f"  {name} has no pull request:", f"    {command}"])

    for row in rows:
        if _field(row, "state", "live") != "orphaned":
            continue
        # CLAUDE.md invariant 14: never say sync will reopen it.
        blocks.append(
            [
                f"  #{_field(row, 'pr')} was closed by the branch deletion, not by a "
                "person. stackem will not",
                "  reopen it; run 'stackem sync' to get the commands that restore it.",
            ]
        )

    blocks.extend(_warning_blocks(view))

    stale = _needs_sync(rows)
    if not rows:
        blocks.append(["no stack — HEAD is on the trunk."])
    elif not stale:
        blocks.append(
            ["everything else is up to date." if len(blocks) > 1 else "everything is up to date."]
        )

    if stale:
        following = "stackem sync"
    elif creates:
        following = creates[0]
    else:
        following = _NOTHING_TO_DO
    blocks.append([f"next: {following}"])
    return _join(blocks)


# ==========================================================================
# stackem sync -- the cascade (sessions 01 to 06)
# ==========================================================================


def _retarget_line(retarget: Any, *, dry_run: bool, indent: str = "") -> str:
    number = _field(retarget, "pr")
    old = _field(retarget, "old_base", "")
    new = _field(retarget, "new_base", "")
    if dry_run:
        return f"{indent}would retarget PR #{number} base {old} -> {new}"
    outcome = "ok" if _field(retarget, "ok", True) else "FAILED"
    return f"{indent}retargeting PR #{number} base {old} -> {new}... {outcome}"


def _reparent_block(reparent: Any, *, dry_run: bool) -> tuple[list[str], list[str]]:
    """A branch leaving the chain, and the children stitched past it.

    The sessions use two layouts and both are the specification:

    * an EMPTIED branch (session 06) keeps its retargets indented under
      ``removing <branch> from the chain:``;
    * a MERGED parent (session 05) puts them unindented, leading the restack
      block that follows.

    Returns ``(block, lead)`` -- the block to print on its own, and the lines
    that belong at the head of the next block.
    """
    branch = _field(reparent, "branch", "")
    cause = _field(reparent, "cause", "merged")
    emptied = cause == "emptied"
    if emptied:
        lines = [f"removing {branch} from the chain:"]
    else:
        squashed = _field(reparent, "squashed_as")
        merged = f"merged (squashed as {squashed})" if squashed else "merged"
        lines = [f"{branch}: {merged} — reparenting its children"]
    for child in _field(reparent, "children", ()):
        name, old, new = (child[0], child[1], child[2]) if isinstance(child, (tuple, list)) else (
            _field(child, "branch", ""),
            _field(child, "old_parent", ""),
            _field(child, "new_parent", ""),
        )
        lines.append(f"  {name}: parent {old} -> {new}")
    retargets = [
        _retarget_line(retarget, dry_run=dry_run, indent="  " if emptied else "")
        for retarget in _field(reparent, "retargets", ())
    ]
    if emptied:
        return lines + retargets, []
    return lines, retargets


def _restack_line(restack: Any, *, dry_run: bool) -> str:
    branch = _field(restack, "branch", "")
    onto = _field(restack, "onto", "")
    outcome = _field(restack, "outcome", "ok")
    if dry_run:
        return f"would restack {branch} onto {onto}"
    resumed = _field(restack, "resumed", False)
    head = (
        f"continuing rebase of {branch}..."
        if resumed
        else f"restacking {branch} onto {onto}..."
    )
    if outcome == "conflict":
        return f"{head} CONFLICT"
    if outcome == "empty":
        return f"{head} EMPTY"
    if resumed:
        # Session 02/04: the resumed line carries no count -- the branch was
        # only half rebased when the conflict stopped it.
        return f"{head} ok"
    return f"{head} ok ({_plural(_field(restack, 'commits', 0), 'commit')})"


def _empty_paragraph(restack: Any) -> list[str]:
    branch = _field(restack, "branch", "")
    onto = _field(restack, "onto", "")
    count = _field(restack, "commits_before", None) or len(list(_field(restack, "dropped", ())))
    text = (
        f"{branch} is now identical to {onto} — all {count} of its commits are "
        "already in the parent."
    )
    number = _field(restack, "pr")
    if number:
        text += f" PR #{number} would have no commits and could not be merged."
    return _wrap(text)


def _note_block(restack: Any) -> list[str]:
    """The inline half of "reported twice" (SPEC.md sec 7.2)."""
    branch = _field(restack, "branch", "")
    lines: list[str] = []
    dropped = list(_field(restack, "dropped", ()))
    if dropped:
        lines.append(
            f"  note: {branch}: dropped {_plural(len(dropped), 'commit')} (already upstream)"
        )
        for commit in dropped:
            lines.append(f"        {_field(commit, 'sha', '')}  {_field(commit, 'subject', '')}")
    if _field(restack, "no_pull_request", False):
        lines.append(f"  note: {branch} has no pull request")
    return lines


def _orphan_blocks(orphan: Any) -> list[list[str]]:
    """SPEC.md sec 6.3: print the rescue, never run it (invariant 14)."""
    number = _field(orphan, "pr")
    branch = _field(orphan, "branch", "")
    base_pr = _field(orphan, "base_pr")
    base_branch = _field(orphan, "base_branch")
    warning = [
        f"WARNING  PR #{number} ({branch}) is closed and its head branch is gone from origin.",
        "         That is what a branch deletion does to a child PR. It is recoverable, but",
        "         stackem will not reopen a pull request on its own — someone may have closed",
        "         it deliberately. To restore it:",
    ]
    if base_pr and base_branch:
        fetch = (
            f"  git fetch origin refs/pull/{base_pr}/head:rescue-base "
            f"refs/pull/{number}/head:rescue-head"
        )
        push = (
            f"  git push origin rescue-base:refs/heads/{base_branch} "
            f"rescue-head:refs/heads/{branch}"
        )
    else:
        fetch = f"  git fetch origin refs/pull/{number}/head:rescue-head"
        push = f"  git push origin rescue-head:refs/heads/{branch}"
    commands = [
        fetch,
        push,
        f"  gh api -X PATCH /repos/{_field(orphan, 'repo', '')}/pulls/{number} -f state=open",
        f"  gh pr edit {number} --base {_field(orphan, 'new_base', '')}",
    ]
    blocks = [warning, commands]
    skipped = list(_field(orphan, "skipped", ()))
    if skipped:
        blocks.append(
            [f"{', '.join(skipped)}: parent chain reaches a closed PR — skipped this run."]
        )
    return blocks


def _foreign_blocks(foreign: Any) -> list[list[str]]:
    """CLAUDE.md invariant 22: refuse, and say whose rebase it is."""
    state = _field(foreign, "state")
    branch = _first_field(foreign, ("branch",)) or _first_field(
        state, ("branch", "head_name"), "the branch"
    )
    onto = _first_field(foreign, ("onto",)) or _first_field(state, ("onto",), "")
    detail = f"{branch} is being rebased onto {onto}" if onto else f"{branch} is being rebased"
    reason = _field(foreign, "reason", "")
    if reason:
        detail += f" — {reason}"
    detail += (
        ". Continuing it would cascade on top of your own rebase, so nothing was "
        "rebased and nothing was pushed."
    )
    return [
        [
            "STOPPED  a rebase is already in progress and stackem did not start it.",
            *textwrap.wrap(
                detail, width=_WIDTH, initial_indent=" " * 9, subsequent_indent=" " * 9
            ),
        ],
        [
            "  finish yours:  git rebase --continue",
            "  or back out:   git rebase --abort",
        ],
        ["next: finish your own rebase, then: stackem sync"],
    ]


def _verification_failed_blocks(verification: Any) -> list[list[str]]:
    """CLAUDE.md invariant 9: stop before the remote, and show what changed."""
    blocks: list[list[str]] = [
        ["verifying... FAILED"],
        _labelled(
            "STOPPED",
            "the replay changed commits that should have come through unchanged. "
            "Nothing was pushed and no pull request base was retargeted; the "
            "branches are rebased locally and the originals are still in the reflog.",
        ),
    ]
    entries = [
        f"  {_field(entry, 'status', '!')}  {_field(entry, 'branch', '')}  "
        f"{_field(entry, 'subject', '')}"
        for entry in _field(verification, "unexpected", ())
    ]
    if entries:
        blocks.append(entries)
    blocks.append(["next: check the commits above, then: stackem sync"])
    return blocks


def _warning_blocks(view: Any) -> list[list[str]]:
    """Anything that makes the rest of the report less certain (SPEC.md sec 12)."""
    return [_labelled("WARNING", text) for text in _field(view, "warnings", ())]


def _retarget_failed_blocks(failures: Sequence[Any]) -> list[list[str]]:
    """Step 8 failed, so step 9 never ran (invariants 13 and 15).

    The "retargeting ... FAILED" line itself was already printed where the
    retarget belonged, so this is only the stop and the reason.
    """
    blocks: list[list[str]] = []
    blocks.append(
        _labelled(
            "STOPPED",
            "a pull request base could not be moved, so nothing was pushed. It is "
            "still based on a branch this run was taking out of the chain, and "
            "deleting that branch would close it. The branches are restacked "
            "locally and the remote is untouched; rerun stackem sync.",
        )
    )
    details = [
        f"  #{_field(failure, 'pr')}: {_field(failure, 'error', '')}"
        for failure in failures
        if _field(failure, "error", "")
    ]
    if details:
        blocks.append(details)
    blocks.append(["next: fix the error above, then: stackem sync"])
    return blocks


def _rejected_block(push: Any) -> list[str]:
    names = list(_field(push, "rejected", ())) or list(_field(push, "branches", ()))
    first = names[0] if names else "the branch"
    reason = _field(push, "reason", "")
    lines = [
        f"REJECTED  origin/{first} has moved since you last fetched it — someone else pushed",
        "          to it. --force-with-lease refused the whole atomic push, so",
        "          nothing was pushed. Local branch tips have been restored.",
    ]
    if reason:
        lines.append(f"          {reason}")
    return lines


def render_sync(view: Any, *, dry_run: bool | None = None) -> str:  # noqa: C901
    """``stackem sync``: the whole cascade, in the order it happened.

    ``dry_run`` overrides the view's own flag, so ``--dry-run`` on the command
    line is honored even if an engine forgets to echo it back.
    """
    trunk = _field(view, "trunk", "main")
    remote = _field(view, "remote", "origin")
    dry_run = _field(view, "dry_run", False) if dry_run is None else dry_run
    restacks = list(_field(view, "restacks", ()))
    conflict = _field(view, "conflict")
    guard = _field(view, "guard")
    push = _field(view, "push")
    orphans = list(_field(view, "orphans", ()))

    blocks: list[list[str]] = []
    if dry_run:
        blocks.append(["dry run — nothing will be changed"])

    # Phase 0 comes before everything, including the fetch: a rebase that is not
    # ours stops sync where it stands (SPEC.md sec 5.2, invariant 22).
    foreign = _field(view, "foreign")
    if foreign is not None:
        blocks.extend(_foreign_blocks(foreign))
        return _join(blocks)

    opening: list[str] = []
    if _field(view, "fetched", True):
        opening.append(f"fetching {remote}... done")
    moved = _field(view, "trunk_moved")
    if moved is not None:
        if moved:
            opening.append(
                f"trunk {remote}/{trunk} moved: {_plural(moved, 'new commit')}"
            )
        else:
            opening.append(f"trunk {remote}/{trunk} unchanged")
    if opening:
        blocks.append(opening)

    blocks.extend(_warning_blocks(view))

    for orphan in orphans:
        blocks.extend(_orphan_blocks(orphan))

    # A merged parent's retarget line leads the restack block (session 05).
    current: list[str] = []
    for reparent in _field(view, "reparents", ()):
        block, lead = _reparent_block(reparent, dry_run=dry_run)
        blocks.append(block)
        current.extend(lead)

    for restack in restacks:
        current.append(_restack_line(restack, dry_run=dry_run))
        outcome = _field(restack, "outcome", "ok")
        if outcome == "empty":
            for commit in _field(restack, "dropped", ()):
                current.append(
                    f"  dropped  {_field(commit, 'sha', '')}  "
                    f"{_field(commit, 'subject', '')}  (already upstream)"
                )
            blocks.append(current)
            current = []
            blocks.append(_empty_paragraph(restack))
        else:
            note = [] if dry_run else _note_block(restack)
            if note:
                blocks.append(current)
                current = []
                blocks.append(note)
        followup = _field(restack, "reparent")
        if followup is not None:
            if current:
                blocks.append(current)
                current = []
            block, lead = _reparent_block(followup, dry_run=dry_run)
            blocks.append(block)
            current = list(lead)
        if outcome == "conflict" and current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    # -- a run that stopped: nothing verified, nothing pushed ---------------

    if conflict is not None:
        blocks.append(
            _conflict_block(conflict)
        )
        blocks.append(
            [
                "Resolve the conflicts, `git add` them, then run `stackem sync` again.",
                "To back out instead: git rebase --abort",
            ]
        )
        queued = list(_field(conflict, "queued", ()))
        if queued:
            blocks.append([f"still queued after this: {', '.join(queued)}"])
        blocks.append(
            ["next: resolve the conflicts and git add them, then: stackem sync"]
        )
        return _join(blocks)

    if guard is not None:
        branch = _field(guard, "branch", "")
        parent = _field(guard, "parent", "")
        blocks.append(
            [
                f"STOPPED  {branch} is not based on {remote}/{parent}.",
                "         Its parent was force-pushed without restacking its children, so the",
                "         fork point cannot be derived. Nothing was rebased, nothing was pushed.",
            ]
        )
        blocks.append(
            [
                f"  find the old tip:      git reflog {parent}",
                f"  then restack by hand:  git rebase --onto {parent} <old-tip> {branch}",
            ]
        )
        queued = list(_field(guard, "queued", ()))
        if queued:
            blocks.append([f"still queued after this: {', '.join(queued)}"])
        blocks.append([f"next: git reflog {parent}"])
        return _join(blocks)

    # -- verify, then push (SPEC.md sec 5.2 phases 1 and 2) -----------------

    failures = list(_field(view, "retarget_failed", ()))
    if failures and not dry_run:
        blocks.extend(_retarget_failed_blocks(failures))
        return _join(blocks)

    pushed_branches = list(_field(push, "branches", ())) if push is not None else []
    push_ok = _field(push, "ok", True) if push is not None else True
    remote_block: list[str] = []
    if dry_run:
        if pushed_branches:
            remote_block.append(
                f"would push {' '.join(pushed_branches)} "
                "(atomic, --force-with-lease --force-if-includes)"
            )
        if remote_block:
            blocks.append(remote_block)
    else:
        verification = _field(view, "verification")
        if verification is not None and not _field(verification, "ok", True):
            # Invariant 9: not one word about the remote past this point.
            blocks.extend(_verification_failed_blocks(verification))
            return _join(blocks)
        if verification is not None:
            remote_block.append(_verify_line(verification, restacks))
        if push is not None:
            if push_ok:
                remote_block.append(
                    f"pushing {' '.join(pushed_branches)}... ok (atomic)"
                )
            else:
                rejected = list(_field(push, "rejected", ())) or pushed_branches
                remote_block.append(
                    f"pushing {' '.join(pushed_branches)}... REJECTED ({', '.join(rejected)})"
                )
        if push_ok and _field(view, "pr_bases_ok", False):
            numbers = [f"#{number}" for number in _field(view, "pr_bases", ())]
            remote_block.append(
                "PR bases: " + "".join(f"{n} " for n in numbers) + "all correct"
            )
        if remote_block:
            blocks.append(remote_block)

    if not push_ok:
        blocks.append(_rejected_block(push))
        first = (list(_field(push, "rejected", ())) or pushed_branches)[0]
        blocks.append(
            [f"next: reconcile {first} with {remote}/{first}, then: stackem sync"]
        )
        return _join(blocks)

    # -- the summary: every note repeated (SPEC.md sec 7.2, sec 9) ----------

    emptied = list(_field(view, "emptied", ()))
    merged = list(_field(view, "merged", ()))
    missing_prs = list(_field(view, "no_pull_request", ()))

    if not dry_run:
        blocks.append(
            _summary_block(view, restacks, emptied, pushed_branches, orphans)
        )

    for entry in merged:
        name = _field(entry, "name", "")
        blocks.append(
            [
                f"{name} is merged and no longer part of the stack. "
                "Delete it when you are ready:",
                f"  git push origin --delete {name} && git branch -D {name}",
            ]
        )

    for entry in emptied:
        name = _field(entry, "name", "")
        number = _field(entry, "pr")
        parent = _field(entry, "parent", "")
        text = f"{name} is empty and no longer in the stack."
        if number:
            text += f" PR #{number} has no commits and cannot be merged."
        text += " When you are ready:"
        lines = _wrap(text)
        if number:
            lines.append(
                f'  gh pr close {number} -c "emptied — the change moved down into {parent}"'
            )
        lines.append(f"  git push origin --delete {name} && git branch -D {name}")
        blocks.append(lines)

    for entry in missing_prs:
        name = _field(entry, "name", "")
        command = _pr_create(name, _field(entry, "parent"))
        blocks.append([f"{name} has no pull request:", f"  {command}"])

    if dry_run:
        following = "stackem sync"
    elif orphans:
        following = "run the commands above, then: stackem sync"
    elif missing_prs:
        following = _pr_create(
            _field(missing_prs[0], "name", ""), _field(missing_prs[0], "parent")
        )
    else:
        following = _NOTHING_TO_DO
    blocks.append([f"next: {following}"])
    return _join(blocks)


def _conflict_block(conflict: Any) -> list[str]:
    """SPEC.md sec 8: branch, commit, what it was going onto, and the files.

    The field names of ``stackem.restack.ConflictReport`` are accepted as well
    as this module's own, so a sync engine can pass its report through.
    """
    files = list(_field(conflict, "files", ())) or ["(none reported)"]
    onto = _first_field(conflict, ("onto", "target", "parent"))
    onto_sha = _first_field(conflict, ("onto_sha",))
    lines = [
        f"CONFLICT in {_field(conflict, 'branch', '')}",
        f"  {'applying'.ljust(9)}  {_first_field(conflict, ('sha', 'stopped_sha'))}  "
        f"{_first_field(conflict, ('subject', 'stopped_subject'))}",
        f"  {'onto'.ljust(9)}  {onto}" + (f" ({onto_sha})" if onto_sha else ""),
        f"  {'files'.ljust(9)}  {files[0]}",
    ]
    lines.extend(" " * 13 + name for name in files[1:])
    return lines


def _verify_line(verification: Any, restacks: Sequence[Any]) -> str:
    clauses = []
    for restack in restacks:
        dropped = list(_field(restack, "dropped", ()))
        if not dropped or _field(restack, "outcome", "ok") != "ok":
            continue
        before = _field(restack, "commits_before")
        after = _field(restack, "commits", 0)
        if before is None:
            before = after + len(dropped)
        clauses.append(
            f"{_field(restack, 'branch', '')}: {after} of {before} commits unchanged, "
            f"{len(dropped)} dropped"
        )
    if clauses:
        return "verifying... " + "; ".join(clauses)
    return f"verifying... all {_field(verification, 'ranges', 0)} commit ranges unchanged"


def _summary_block(
    view: Any,
    restacks: Sequence[Any],
    emptied: Sequence[Any],
    pushed: Sequence[str],
    orphans: Sequence[Any],
) -> list[str]:
    removed = _field(view, "removed")
    if removed is None:
        removed = len(emptied)
    restacked = _field(view, "restacked")
    if restacked is None:
        restacked = sum(
            1 for r in restacks if _field(r, "outcome", "ok") in ("ok", "empty")
        )
    # Sessions 01 and 06: only the first clause names the unit --
    # "3 branches restacked, 4 pushed", "1 branch removed from the chain, 2
    # restacked, 2 pushed".
    counted: list[tuple[int, str]] = []
    if removed:
        counted.append((removed, "removed from the chain"))
    counted.append((restacked, "restacked"))
    if pushed:
        counted.append((len(pushed), "pushed"))
    clauses = [
        (f"{_plural(count, 'branch', 'branches')} {what}" if index == 0 else f"{count} {what}")
        for index, (count, what) in enumerate(counted)
    ]
    lines = ["done. " + ", ".join(clauses) + "."]

    for restack in restacks:
        dropped = list(_field(restack, "dropped", ()))
        if not dropped or _field(restack, "outcome", "ok") != "ok":
            continue
        before = _field(restack, "commits_before")
        after = _field(restack, "commits", 0)
        if before is None:
            before = after + len(dropped)
        lines.append(
            f"  {_field(restack, 'branch', '')} now has {_plural(after, 'commit')} "
            f"(was {before})"
        )
    for orphan in orphans:
        lines.append(
            f"  #{_field(orphan, 'pr')} is orphaned — the commands that restore it are above."
        )
    return lines


# ==========================================================================
# stackem parent -- retargeting (session 01; invariant 1)
# ==========================================================================


def render_parent(view: Any, *, dry_run: bool | None = None) -> str:
    """``stackem parent <b> --onto <p>``: the PR base IS the parent record."""
    branch = _field(view, "branch", "")
    number = _field(view, "pr")
    old = _field(view, "old_base", "")
    new = _field(view, "new_base", "")
    dry_run = _field(view, "dry_run", False) if dry_run is None else dry_run
    blocks: list[list[str]] = []

    if dry_run:
        blocks.append(["dry run — nothing will be changed"])
        blocks.append([f"would retarget PR #{number} base {old} -> {new}"])
        blocks.append([f"next: stackem parent {branch} --onto {new}"])
        return _join(blocks)

    if number is None:
        # SPEC.md sec 6.5: stackem never creates a pull request.
        command = _pr_create(branch, new)
        blocks.append(
            [
                f"{branch} has no pull request, so there is no base to retarget.",
                f"  {command}",
            ]
        )
        blocks.append([f"next: {command}"])
        return _join(blocks)

    error = _field(view, "error")
    if error:
        blocks.append(
            [
                f"retargeting PR #{number} base {old} -> {new}... FAILED",
                f"  {error}",
            ]
        )
        blocks.append(
            [f"next: fix the error above, then: stackem parent {branch} --onto {new}"]
        )
        return _join(blocks)

    if not _field(view, "changed", True):
        blocks.append([f"PR #{number} already targets {new}. Nothing to do."])
        blocks.append([f"next: {_NOTHING_TO_DO}"])
        return _join(blocks)

    blocks.append([f"retargeting PR #{number} base {old} -> {new}... ok"])
    blocks.append(["next: stackem sync"])
    return _join(blocks)


# ==========================================================================
# invariant 5: `stackem` is read-only
# ==========================================================================

#: git subcommands that only read.  Anything absent is refused -- an allowlist,
#: because a deny-list of write verbs is one forgotten subcommand away from
#: breaking CLAUDE.md invariant 5.
_READ_ONLY = {
    "blame",
    "cat-file",
    "check-ref-format",
    "count-objects",
    "describe",
    "diff",
    "diff-tree",
    "for-each-ref",
    "grep",
    "log",
    "ls-files",
    "ls-remote",
    "ls-tree",
    "merge-base",
    # merge-tree --write-tree writes an unreferenced object, never a ref.
    # SPEC.md sec 6.4 uses it for offline merge detection in this very command.
    "merge-tree",
    "name-rev",
    "patch-id",
    "range-diff",
    "rev-list",
    "rev-parse",
    "shortlog",
    "show",
    "show-branch",
    "show-ref",
    "status",
    "var",
    "verify-commit",
    "version",
    "whatchanged",
}

#: Subcommands that read or write depending on their options.
_CONDITIONAL = {
    "branch": lambda args: not any(
        a in {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
              "-f", "--force", "-u", "--set-upstream-to", "--unset-upstream",
              "--edit-description"}
        or not a.startswith("-")
        for a in args
    ),
    "config": lambda args: any(
        a in {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
        for a in args
    ),
    "reflog": lambda args: not any(
        a in {"expire", "delete", "drop"} for a in args
    ),
    "remote": lambda args: all(
        a in {"-v", "--verbose", "show", "get-url"} for a in args
    ),
    "symbolic-ref": lambda args: not any(
        a in {"-d", "--delete", "-m"} for a in args
    )
    and len([a for a in args if not a.startswith("-")]) <= 1,
}

_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _split_command(args: Sequence[str]) -> tuple[str | None, list[str]]:
    """Find the subcommand, skipping git's own global options."""
    index = 0
    while index < len(args):
        token = str(args[index])
        if token in _GLOBAL_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            if token in ("--version", "--help"):
                return token.lstrip("-"), []
            index += 1
            continue
        return token, [str(a) for a in args[index + 1:]]
    return None, []


def _assert_read_only(args: Sequence[str]) -> None:
    command, rest = _split_command(args)
    if command is None:
        return
    if command in _CONDITIONAL:
        if _CONDITIONAL[command](rest):
            return
    elif command in _READ_ONLY:
        return
    raise ReadOnlyViolation(
        f"`stackem` is read-only and must not run `git {shlex.join(str(a) for a in args)}` "
        "(CLAUDE.md invariant 5). Use `stackem sync` to change anything."
    )


class ReadOnlyGit(Git):
    """A :class:`~stackem.gitx.Git` that refuses to write.

    The check happens *before* the subprocess is spawned, which a logger could
    not do -- ``Git`` logs after the fact -- so a violation is prevented rather
    than merely noticed.
    """

    def run(self, *args, **kwargs):  # type: ignore[override]
        _assert_read_only(args)
        return super().run(*args, **kwargs)


def verbose_logger(stream) -> Callable[[GitInvocation], None]:
    """``--verbose``: one line per git invocation, written to ``stream``."""

    def log(invocation: GitInvocation) -> None:
        line = f"+ {invocation.command}"
        if invocation.returncode != 0:
            line += f" -> exit {invocation.returncode}"
        stream.write(line + "\n")

    return log


def build_git(cwd, *, read_only: bool = False, logger=None, env=None) -> Git:
    """The one place the CLI constructs a git runner."""
    cls = ReadOnlyGit if read_only else Git
    return cls(cwd, env=env, logger=logger)


# ==========================================================================
# the engine seam
# ==========================================================================


class Engine(Protocol):
    """What the CLI needs.  Implemented by ``stackem.sync``."""

    def status(self) -> Any:
        """A :class:`StatusView`-shaped object.  Must not write (invariant 5)."""

    def sync(self, *, dry_run: bool = False) -> Any:
        """A :class:`SyncView`-shaped object."""

    def set_parent(self, branch: str, onto: str, *, dry_run: bool = False) -> Any:
        """A :class:`ParentView`-shaped object."""


@dataclass
class Context:
    """What an engine factory is handed."""

    command: str
    cwd: str
    verbose: bool = False
    dry_run: bool = False
    git: Git | None = None
    branch: str | None = None
    onto: str | None = None


_ENGINE_CONTRACT = (
    "  The CLI expects stackem.sync to provide build_engine(git=...) (or an",
    "  Engine(git=...) class) returning an object with status(),",
    "  sync(dry_run=...) and set_parent(branch, onto, dry_run=...).",
)


def default_engine_factory(context: Context) -> Engine:
    """Resolve the engine lazily, so this module imports without it."""
    try:
        from stackem import sync as sync_module
    except ImportError as exc:
        raise CliError(
            f"stackem.sync is not available ({exc}).",
            detail=_ENGINE_CONTRACT,
            next_command="stackem sync",
        ) from exc
    factory = getattr(sync_module, "build_engine", None) or getattr(
        sync_module, "Engine", None
    )
    if factory is None:
        raise CliError(
            "stackem.sync provides neither build_engine() nor Engine().",
            detail=_ENGINE_CONTRACT,
            next_command="stackem sync",
        )
    try:
        return factory(git=context.git)
    except TypeError as exc:
        raise CliError(
            f"stackem.sync's engine factory could not be called: {exc}",
            detail=_ENGINE_CONTRACT,
            next_command="stackem sync",
        ) from exc


# ==========================================================================
# arguments
# ==========================================================================


@dataclass
class Options:
    command: str = "status"
    branch: str | None = None
    onto: str | None = None
    verbose: bool = False
    dry_run: bool = False


def parse_args(argv: Sequence[str]) -> Options:
    """Three commands, two flags, and honest errors for everything else."""
    options = Options()
    positional: list[str] = []
    index = 0
    argv = [str(a) for a in argv]
    while index < len(argv):
        token = argv[index]
        if token in ("-h", "--help"):
            return Options(command="help")
        if token == "--version":
            return Options(command="version")
        if token in ("-v", "--verbose"):
            options.verbose = True
        elif token == "--dry-run":
            options.dry_run = True
        elif token == "--onto":
            index += 1
            if index >= len(argv):
                raise UsageError("stackem parent needs a branch after --onto")
            options.onto = argv[index]
        elif token.startswith("--onto="):
            options.onto = token.split("=", 1)[1]
        elif token == "--":
            positional.extend(argv[index + 1:])
            break
        elif token.startswith("-"):
            raise UsageError(f"unknown option {token}", usage=True)
        else:
            positional.append(token)
        index += 1

    if positional:
        name = positional[0]
        if name in _NO_SUCH_COMMAND:
            raise UsageError(_NO_SUCH_COMMAND[name], next_command="stackem sync")
        if name == "sync":
            if len(positional) > 1:
                raise UsageError(
                    f"stackem sync takes no arguments (got {positional[1]!r})", usage=True
                )
            options.command = "sync"
        elif name == "parent":
            if len(positional) != 2:
                raise UsageError(
                    "usage: stackem parent <branch> --onto <parent>", usage=False
                )
            options.command = "parent"
            options.branch = positional[1]
            if not options.onto:
                raise UsageError("stackem parent needs --onto <parent>")
        else:
            raise UsageError(f"unknown command {name!r}", usage=True)

    if options.command == "status" and options.dry_run:
        raise UsageError(
            "stackem is already read-only; --dry-run applies to sync and parent"
        )
    return options


# ==========================================================================
# main
# ==========================================================================


def _emit(stream, text: str) -> None:
    stream.write(text + "\n")


def _sync_incomplete(view: Any) -> bool:
    """True when the run stopped short, so a wrapper knows from the exit code."""
    for name in ("conflict", "guard", "foreign"):
        if _field(view, name) is not None:
            return True
    if list(_field(view, "retarget_failed", ())):
        return True
    verification = _field(view, "verification")
    if verification is not None and not _field(verification, "ok", True):
        return True
    push = _field(view, "push")
    return push is not None and not _field(push, "ok", True)


def _call(method, /, **kwargs):
    """Call ``method`` with ``kwargs``, tolerating an engine that omits them."""
    try:
        return method(**kwargs)
    except TypeError as exc:
        if "dry_run" not in str(exc):
            raise
        kwargs.pop("dry_run", None)
        return method(**kwargs)


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: str | None = None,
    out=None,
    err=None,
    engine: Engine | None = None,
    engine_factory: Callable[[Context], Engine] | None = None,
) -> int:
    """Run one stackem command.  Returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    try:
        options = parse_args(argv)
    except UsageError as exc:
        _emit(err, exc.render())
        return EXIT_USAGE

    if options.command == "help":
        _emit(out, USAGE)
        return EXIT_OK
    if options.command == "version":
        _emit(out, f"stackem {__version__}\n\nnext: stackem")
        return EXIT_OK

    context = Context(
        command=options.command,
        cwd=cwd or os.getcwd(),
        verbose=options.verbose,
        dry_run=options.dry_run,
        branch=options.branch,
        onto=options.onto,
    )

    try:
        if engine is None:
            context.git = build_git(
                context.cwd,
                # invariant 5: the display command gets a git that cannot write.
                read_only=options.command == "status",
                logger=verbose_logger(err) if options.verbose else None,
            )
            # SPEC.md sec 11: merge-tree --write-tree needs 2.38 and
            # --force-if-includes needs 2.30 -- assert on first run, which is
            # here, before an engine can start work the git cannot finish.
            try:
                context.git.check_version()
            except GitError as exc:
                raise CliError(
                    str(exc), next_command="install git 2.38 or newer, then: stackem"
                ) from exc
            engine = (engine_factory or default_engine_factory)(context)

        if options.command == "status":
            _emit(out, render_status(engine.status()))
            return EXIT_OK
        if options.command == "sync":
            view = _call(engine.sync, dry_run=options.dry_run)
            _emit(out, render_sync(view, dry_run=options.dry_run or None))
            return EXIT_INCOMPLETE if _sync_incomplete(view) else EXIT_OK
        view = _call(
            engine.set_parent,
            branch=options.branch,
            onto=options.onto,
            dry_run=options.dry_run,
        )
        _emit(out, render_parent(view, dry_run=options.dry_run or None))
        return EXIT_INCOMPLETE if _field(view, "error") else EXIT_OK
    except CliError as exc:
        _emit(err, exc.render())
        return EXIT_INCOMPLETE
    except ReadOnlyViolation as exc:
        _emit(
            err,
            CliError(
                str(exc),
                next_command="stackem sync",
            ).render(),
        )
        return EXIT_INCOMPLETE
    except GitError as exc:
        _emit(err, CliError(str(exc), next_command="stackem sync").render())
        return EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
