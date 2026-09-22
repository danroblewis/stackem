"""The command line surface: three commands, and the exact text they print.

SPEC.md sec 4 and sec 9, and the six worked sessions in docs/sessions/ -- those
sessions ARE the specification for the output format, so most of what follows
compares against text lifted from them verbatim.

What is under test here
-----------------------
* the renderers, which own every byte stackem prints.  They are pure functions
  of a view object, so the assertions can be exact.
* the argument parser: three commands, no ``init``, no ``continue``, no
  ``abort`` (CLAUDE.md, "Command surface").
* CLAUDE.md invariant 23 -- every output ends with the literal next command --
  checked over every view this file builds, not just a chosen few.
* CLAUDE.md invariant 5 -- ``stackem`` with no arguments is READ-ONLY.  That one
  is tested against a real git repository, because the only honest test of "it
  never writes" is to point it at a repository and check nothing moved.

SPEC.md sec 7.2 and sec 9 require dropped commits, emptied branches, merged
branches, orphaned PRs and branches with no PR to be reported TWICE -- inline and
in the summary.  ``test_*_reported_twice`` pins each one.

Note on the engine seam: ``stackem.stack`` / ``stackem.sync`` are being written
in parallel and are not importable yet, so the CLI defines the view model it
renders (see stackem.cli) and resolves the engine lazily.  These tests drive the
renderers and the parser directly, plus a recording engine for the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from stackem.cli import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_USAGE,
    BranchRow,
    CliError,
    Conflict,
    Dropped,
    Emptied,
    ForeignRebase,
    GuardViolation,
    MergedBranch,
    NoPullRequest,
    Orphan,
    ParentView,
    PushResult,
    ReadOnlyGit,
    ReadOnlyViolation,
    Reparent,
    Restack,
    StatusView,
    SyncView,
    Verification,
    build_git,
    main,
    render_parent,
    render_status,
    render_sync,
    verbose_logger,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@dataclass
class RecordingEngine:
    """Stands in for stackem.sync until it exists.  It runs no git and no HTTP."""

    status_view: Any = None
    sync_view: Any = None
    parent_view: Any = None
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def status(self):
        self.calls.append(("status", {}))
        return self.status_view

    def sync(self, *, dry_run: bool = False):
        self.calls.append(("sync", {"dry_run": dry_run}))
        return self.sync_view

    def set_parent(self, branch: str, onto: str, *, dry_run: bool = False):
        self.calls.append(
            ("set_parent", {"branch": branch, "onto": onto, "dry_run": dry_run})
        )
        return self.parent_view


class Capture:
    """A minimal text stream that keeps what was written."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing buffered
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def run_cli(argv, engine=None, **kwargs):
    """Run main() with captured streams.  Returns (code, stdout, stderr)."""
    out, err = Capture(), Capture()
    code = main(list(argv), out=out, err=err, engine=engine, **kwargs)
    return code, out.text, err.text


# --- the four-branch auth stack the sessions all use ----------------------


def auth_rows(**overrides) -> list[BranchRow]:
    rows = [
        BranchRow(name="auth-model", pr=101, parent="main"),
        BranchRow(name="auth-endpoints", pr=102, parent="auth-model"),
        BranchRow(name="auth-ui", pr=103, parent="auth-endpoints"),
        BranchRow(name="auth-docs", pr=104, parent="auth-ui"),
    ]
    for index, row in enumerate(rows):
        if row.name in overrides:
            rows[index] = overrides[row.name]
    return rows


# --------------------------------------------------------------------------
# stackem -- the status display (SPEC.md sec 9, sessions 01, 03, 05)
# --------------------------------------------------------------------------


def test_status_up_to_date_matches_session_03():
    view = StatusView(trunk="main", branches=auth_rows())
    assert render_status(view) == (
        "main (origin/main, up to date)\n"
        "  1. auth-model      #101  synced\n"
        "  2. auth-endpoints  #102  synced\n"
        "  3. auth-ui         #103  synced\n"
        "  4. auth-docs       #104  synced\n"
        "\n"
        "everything is up to date.\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_status_needing_restack_matches_session_03():
    view = StatusView(
        trunk="main",
        trunk_behind=14,
        branches=[
            BranchRow(
                name="auth-model",
                pr=101,
                parent="main",
                needs_restack=True,
                restack_reason="trunk moved",
            ),
            BranchRow(name="auth-endpoints", pr=102, parent="auth-model", needs_restack=True),
            BranchRow(name="auth-ui", pr=103, parent="auth-endpoints", needs_restack=True),
            BranchRow(name="auth-docs", pr=104, parent="auth-ui", needs_restack=True),
        ],
    )
    assert render_status(view) == (
        "main (origin/main, 14 commits behind)\n"
        "  1. auth-model      #101  needs restack (trunk moved)\n"
        "  2. auth-endpoints  #102  needs restack\n"
        "  3. auth-ui         #103  needs restack\n"
        "  4. auth-docs       #104  needs restack\n"
        "\n"
        "next: stackem sync"
    )


def test_status_branch_with_no_pr_matches_session_01():
    view = StatusView(
        trunk="main",
        branches=auth_rows(**{"auth-docs": BranchRow(name="auth-docs", pr=None, parent="auth-ui")}),
    )
    assert render_status(view) == (
        "main (origin/main, up to date)\n"
        "  1. auth-model      #101  synced\n"
        "  2. auth-endpoints  #102  synced\n"
        "  3. auth-ui         #103  synced\n"
        "  4. auth-docs       --    no PR\n"
        "\n"
        "  auth-docs has no pull request:\n"
        "    gh pr create --base auth-ui --head auth-docs\n"
        "\n"
        "everything else is up to date.\n"
        "\n"
        "next: gh pr create --base auth-ui --head auth-docs"
    )


def test_status_with_a_merged_branch_matches_session_05():
    view = StatusView(
        trunk="main",
        trunk_behind=1,
        branches=[
            BranchRow(
                name="auth-model",
                pr=101,
                parent="main",
                state="merged",
                merged_as="7d3f9a1",
            ),
            BranchRow(
                name="auth-endpoints",
                pr=102,
                parent="auth-model",
                needs_restack=True,
                notes=("PR base is a merged branch",),
            ),
            BranchRow(name="auth-ui", pr=103, parent="auth-endpoints", needs_restack=True),
            BranchRow(name="auth-docs", pr=104, parent="auth-ui", needs_restack=True),
        ],
    )
    assert render_status(view) == (
        "main (origin/main, 1 commit behind)\n"
        "  x  auth-model      #101  MERGED (squashed as 7d3f9a1)\n"
        "  1. auth-endpoints  #102  needs restack, PR base is a merged branch\n"
        "  2. auth-ui         #103  needs restack\n"
        "  3. auth-docs       #104  needs restack\n"
        "\n"
        "next: stackem sync"
    )


def test_status_with_an_orphaned_pr_keeps_the_numbering_and_never_promises_a_reopen():
    # Session 05's variant.  A merged row takes an "x" and no number; an orphaned
    # row takes a "!" but still occupies its slot, so auth-ui stays number 2.
    view = StatusView(
        trunk="main",
        trunk_behind=1,
        branches=[
            BranchRow(
                name="auth-model",
                pr=101,
                parent="main",
                state="merged",
                merged_as="7d3f9a1",
                branch_deleted=True,
            ),
            BranchRow(name="auth-endpoints", pr=102, parent="auth-model", state="orphaned"),
            BranchRow(name="auth-ui", pr=103, parent="auth-endpoints", needs_restack=True),
            BranchRow(name="auth-docs", pr=104, parent="auth-ui", needs_restack=True),
        ],
    )
    text = render_status(view)
    assert text == (
        "main (origin/main, 1 commit behind)\n"
        "  x  auth-model      #101  MERGED (squashed as 7d3f9a1), branch deleted\n"
        "  !  auth-endpoints  #102  CLOSED — base branch was deleted\n"
        "  2. auth-ui         #103  needs restack\n"
        "  3. auth-docs       #104  needs restack\n"
        "\n"
        "  #102 was closed by the branch deletion, not by a person. stackem will not\n"
        "  reopen it; run 'stackem sync' to get the commands that restore it.\n"
        "\n"
        "next: stackem sync"
    )
    # CLAUDE.md invariant 14: never promise an automatic rescue.
    assert "sync can reopen it" not in text


def test_status_reports_a_branch_restacked_but_not_pushed():
    # Session 02: after `git rebase --abort`, the branch below the conflict is
    # already restacked locally and that is not damage.
    view = StatusView(
        trunk="main",
        branches=[
            BranchRow(name="auth-endpoints", pr=102, parent="main", restacked_not_pushed=True),
            BranchRow(name="auth-ui", pr=103, parent="auth-endpoints", needs_restack=True),
            BranchRow(name="auth-docs", pr=104, parent="auth-ui", needs_restack=True),
        ],
    )
    assert render_status(view) == (
        "main (origin/main, up to date)\n"
        "  1. auth-endpoints  #102  restacked, not pushed\n"
        "  2. auth-ui         #103  needs restack\n"
        "  3. auth-docs       #104  needs restack\n"
        "\n"
        "next: stackem sync"
    )


def test_status_a_stale_stack_asks_for_sync_even_when_a_pr_is_missing():
    # Two things want to be the next command; sync wins, because the stack is
    # wrong until it runs and `gh pr create` on a stale branch opens a PR full
    # of the wrong commits.  The create command is still printed inline.
    view = StatusView(
        trunk="main",
        trunk_behind=3,
        branches=[
            BranchRow(
                name="auth-model",
                pr=101,
                parent="main",
                needs_restack=True,
                restack_reason="trunk moved",
            ),
            BranchRow(name="auth-docs", pr=None, parent="auth-model"),
        ],
    )
    text = render_status(view)
    assert "  auth-docs has no pull request:" in text
    assert "    gh pr create --base auth-model --head auth-docs" in text
    assert text.endswith("next: stackem sync")


def test_status_singular_commit_behind():
    view = StatusView(trunk="main", trunk_behind=1, branches=auth_rows())
    assert render_status(view).startswith("main (origin/main, 1 commit behind)\n")


def test_status_with_no_stack_says_so_and_still_ends_with_next():
    view = StatusView(trunk="main", branches=[])
    assert render_status(view) == (
        "main (origin/main, up to date)\n"
        "\n"
        "no stack — HEAD is on the trunk.\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_status_column_widths_follow_the_longest_branch_name():
    view = StatusView(
        trunk="main",
        branches=[
            BranchRow(name="a", pr=1, parent="main"),
            BranchRow(name="a-very-long-branch-name", pr=2222, parent="a"),
        ],
    )
    assert render_status(view).splitlines()[1:3] == [
        "  1. a                        #1     synced",
        "  2. a-very-long-branch-name  #2222  synced",
    ]


# --------------------------------------------------------------------------
# stackem sync -- the cascade (sessions 01 to 06)
# --------------------------------------------------------------------------


def test_sync_clean_run_matches_session_01():
    # Session 01 names the pull requests it checked: "PR bases: #102 #103 #104
    # all correct".  Sessions 02 to 06 print the short form; both are supported,
    # and the engine decides by handing over the numbers or not.
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[
            Restack(branch="auth-endpoints", onto="auth-model", commits=2),
            Restack(branch="auth-ui", onto="auth-endpoints", commits=1),
            Restack(branch="auth-docs", onto="auth-ui", commits=1),
        ],
        verification=Verification(ranges=4),
        push=PushResult(branches=("auth-model", "auth-endpoints", "auth-ui", "auth-docs")),
        pr_bases_ok=True,
        pr_bases=(102, 103, 104),
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main unchanged\n"
        "\n"
        "restacking auth-endpoints onto auth-model... ok (2 commits)\n"
        "restacking auth-ui onto auth-endpoints... ok (1 commit)\n"
        "restacking auth-docs onto auth-ui... ok (1 commit)\n"
        "\n"
        "verifying... all 4 commit ranges unchanged\n"
        "pushing auth-model auth-endpoints auth-ui auth-docs... ok (atomic)\n"
        "PR bases: #102 #103 #104 all correct\n"
        "\n"
        "done. 3 branches restacked, 4 pushed.\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_sync_after_the_trunk_moves_matches_session_03():
    view = SyncView(
        trunk="main",
        trunk_moved=14,
        restacks=[
            Restack(branch="auth-model", onto="origin/main", commits=1),
            Restack(branch="auth-endpoints", onto="auth-model", commits=2),
            Restack(branch="auth-ui", onto="auth-endpoints", commits=3),
            Restack(branch="auth-docs", onto="auth-ui", commits=1),
        ],
        verification=Verification(ranges=7),
        push=PushResult(branches=("auth-model", "auth-endpoints", "auth-ui", "auth-docs")),
        pr_bases_ok=True,
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main moved: 14 new commits\n"
        "\n"
        "restacking auth-model onto origin/main... ok (1 commit)\n"
        "restacking auth-endpoints onto auth-model... ok (2 commits)\n"
        "restacking auth-ui onto auth-endpoints... ok (3 commits)\n"
        "restacking auth-docs onto auth-ui... ok (1 commit)\n"
        "\n"
        "verifying... all 7 commit ranges unchanged\n"
        "pushing auth-model auth-endpoints auth-ui auth-docs... ok (atomic)\n"
        "PR bases: all correct\n"
        "\n"
        "done. 4 branches restacked, 4 pushed.\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_sync_conflict_report_matches_session_04():
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[
            Restack(branch="auth-ui", onto="auth-endpoints", outcome="conflict"),
        ],
        conflict=Conflict(
            branch="auth-ui",
            sha="3f2a1bc",
            subject="auth: handle rate limits in the login form",
            onto="auth-endpoints",
            onto_sha="9c4e1a2",
            files=("app/api/client.py",),
            queued=("auth-docs",),
        ),
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main unchanged\n"
        "\n"
        "restacking auth-ui onto auth-endpoints... CONFLICT\n"
        "\n"
        "CONFLICT in auth-ui\n"
        "  applying   3f2a1bc  auth: handle rate limits in the login form\n"
        "  onto       auth-endpoints (9c4e1a2)\n"
        "  files      app/api/client.py\n"
        "\n"
        "Resolve the conflicts, `git add` them, then run `stackem sync` again.\n"
        "To back out instead: git rebase --abort\n"
        "\n"
        "still queued after this: auth-docs\n"
        "\n"
        "next: resolve the conflicts and git add them, then: stackem sync"
    )


def test_conflict_report_lists_every_conflicted_file():
    view = SyncView(
        trunk="main",
        conflict=Conflict(
            branch="auth-ui",
            sha="3f2a1bc",
            subject="auth: rate limits",
            onto="auth-endpoints",
            onto_sha="9c4e1a2",
            files=("app/api/client.py", "app/models/user.py"),
        ),
    )
    assert (
        "  files      app/api/client.py\n"
        "             app/models/user.py\n"
    ) in render_sync(view)


def test_conflict_report_without_a_queue_omits_the_queued_line():
    view = SyncView(
        trunk="main",
        conflict=Conflict(
            branch="auth-docs",
            sha="1111111",
            subject="docs",
            onto="auth-ui",
            onto_sha="2222222",
            files=("README.md",),
        ),
    )
    text = render_sync(view)
    assert "still queued after this" not in text
    assert text.endswith("next: resolve the conflicts and git add them, then: stackem sync")


def test_sync_dropped_commit_matches_session_02():
    view = SyncView(
        trunk="main",
        fetched=False,
        trunk_moved=None,
        restacks=[
            Restack(
                branch="auth-ui",
                onto="auth-endpoints",
                resumed=True,
                commits=2,
                commits_before=3,
                dropped=(
                    Dropped(sha="3f2a1bc", subject="auth: handle rate limits in the login form"),
                ),
            ),
            Restack(branch="auth-docs", onto="auth-ui", commits=1),
        ],
        verification=Verification(ranges=3),
        push=PushResult(branches=("auth-endpoints", "auth-ui", "auth-docs")),
        pr_bases_ok=True,
        restacked=3,
    )
    assert render_sync(view) == (
        "continuing rebase of auth-ui... ok\n"
        "\n"
        "  note: auth-ui: dropped 1 commit (already upstream)\n"
        "        3f2a1bc  auth: handle rate limits in the login form\n"
        "\n"
        "restacking auth-docs onto auth-ui... ok (1 commit)\n"
        "\n"
        "verifying... auth-ui: 2 of 3 commits unchanged, 1 dropped\n"
        "pushing auth-endpoints auth-ui auth-docs... ok (atomic)\n"
        "PR bases: all correct\n"
        "\n"
        "done. 3 branches restacked, 3 pushed.\n"
        "  auth-ui now has 2 commits (was 3)\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_dropped_commits_are_reported_twice():
    # SPEC.md sec 7.2 and sec 9: inline, and again in the summary.
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[
            Restack(
                branch="auth-ui",
                onto="auth-endpoints",
                commits=2,
                commits_before=3,
                dropped=(Dropped(sha="3f2a1bc", subject="auth: expire sessions on logout"),),
            )
        ],
        verification=Verification(ranges=3),
        push=PushResult(branches=("auth-ui",)),
        pr_bases_ok=True,
    )
    text = render_sync(view)
    assert "  note: auth-ui: dropped 1 commit (already upstream)" in text
    assert "        3f2a1bc  auth: expire sessions on logout" in text
    assert "  auth-ui now has 2 commits (was 3)" in text


def test_sync_emptied_branch_matches_session_06():
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[
            Restack(
                branch="auth-ui",
                onto="auth-endpoints",
                outcome="empty",
                pr=103,
                commits_before=1,
                dropped=(Dropped(sha="3f2a1bc", subject="auth: expire sessions on logout"),),
                reparent=Reparent(
                    branch="auth-ui",
                    cause="emptied",
                    children=(("auth-docs", "auth-ui", "auth-endpoints"),),
                    retargets=(
                        {
                            "pr": 104,
                            "old_base": "auth-ui",
                            "new_base": "auth-endpoints",
                        },
                    ),
                ),
            ),
            Restack(branch="auth-docs", onto="auth-endpoints", commits=1),
        ],
        verification=Verification(ranges=3),
        push=PushResult(branches=("auth-endpoints", "auth-docs")),
        pr_bases_ok=True,
        emptied=(Emptied(name="auth-ui", pr=103, parent="auth-endpoints"),),
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main unchanged\n"
        "\n"
        "restacking auth-ui onto auth-endpoints... EMPTY\n"
        "  dropped  3f2a1bc  auth: expire sessions on logout  (already upstream)\n"
        "\n"
        "auth-ui is now identical to auth-endpoints — all 1 of its commits are already\n"
        "in the parent. PR #103 would have no commits and could not be merged.\n"
        "\n"
        "removing auth-ui from the chain:\n"
        "  auth-docs: parent auth-ui -> auth-endpoints\n"
        "  retargeting PR #104 base auth-ui -> auth-endpoints... ok\n"
        "\n"
        "restacking auth-docs onto auth-endpoints... ok (1 commit)\n"
        "\n"
        "verifying... all 3 commit ranges unchanged\n"
        "pushing auth-endpoints auth-docs... ok (atomic)\n"
        "PR bases: all correct\n"
        "\n"
        "done. 1 branch removed from the chain, 2 restacked, 2 pushed.\n"
        "\n"
        "auth-ui is empty and no longer in the stack. PR #103 has no commits and cannot\n"
        "be merged. When you are ready:\n"
        '  gh pr close 103 -c "emptied — the change moved down into auth-endpoints"\n'
        "  git push origin --delete auth-ui && git branch -D auth-ui\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_sync_squash_merge_cascade_matches_session_05():
    # The two reparenting layouts differ in the sessions and both are pinned:
    # a MERGED parent (here, session 05) puts its retarget line unindented, at
    # the head of the restack block; an EMPTIED branch (session 06, below) keeps
    # its retarget indented under "removing <branch> from the chain:".
    view = SyncView(
        trunk="main",
        trunk_moved=1,
        reparents=(
            Reparent(
                branch="auth-model",
                cause="merged",
                squashed_as="7d3f9a1",
                children=(("auth-endpoints", "auth-model", "main"),),
                retargets=({"pr": 102, "old_base": "auth-model", "new_base": "main"},),
            ),
        ),
        restacks=[
            Restack(branch="auth-endpoints", onto="origin/main", commits=2),
            Restack(branch="auth-ui", onto="auth-endpoints", commits=3),
            Restack(branch="auth-docs", onto="auth-ui", commits=1),
        ],
        verification=Verification(ranges=6),
        push=PushResult(branches=("auth-endpoints", "auth-ui", "auth-docs")),
        pr_bases_ok=True,
        merged=(MergedBranch(name="auth-model", pr=101, squashed_as="7d3f9a1"),),
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main moved: 1 new commit\n"
        "\n"
        "auth-model: merged (squashed as 7d3f9a1) — reparenting its children\n"
        "  auth-endpoints: parent auth-model -> main\n"
        "\n"
        "retargeting PR #102 base auth-model -> main... ok\n"
        "restacking auth-endpoints onto origin/main... ok (2 commits)\n"
        "restacking auth-ui onto auth-endpoints... ok (3 commits)\n"
        "restacking auth-docs onto auth-ui... ok (1 commit)\n"
        "\n"
        "verifying... all 6 commit ranges unchanged\n"
        "pushing auth-endpoints auth-ui auth-docs... ok (atomic)\n"
        "PR bases: all correct\n"
        "\n"
        "done. 3 branches restacked, 3 pushed.\n"
        "\n"
        "auth-model is merged and no longer part of the stack. Delete it when you are ready:\n"
        "  git push origin --delete auth-model && git branch -D auth-model\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_merged_and_emptied_branches_are_reported_twice():
    view = SyncView(
        trunk="main",
        trunk_moved=1,
        reparents=(
            Reparent(
                branch="auth-model",
                cause="merged",
                squashed_as="7d3f9a1",
                children=(("auth-endpoints", "auth-model", "main"),),
            ),
        ),
        merged=(MergedBranch(name="auth-model", pr=101, squashed_as="7d3f9a1"),),
    )
    text = render_sync(view)
    assert text.count("auth-model") >= 3
    assert "auth-model: merged (squashed as 7d3f9a1) — reparenting its children" in text
    assert "git push origin --delete auth-model && git branch -D auth-model" in text


def test_sync_orphaned_pr_prints_the_rescue_commands_and_never_runs_them():
    view = SyncView(
        trunk="main",
        trunk_moved=None,
        orphans=(
            Orphan(
                pr=102,
                branch="auth-endpoints",
                base_pr=101,
                base_branch="auth-model",
                new_base="main",
                repo="acme/app",
                skipped=("auth-ui", "auth-docs"),
            ),
        ),
        restacked=0,
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "\n"
        "WARNING  PR #102 (auth-endpoints) is closed and its head branch is gone from origin.\n"
        "         That is what a branch deletion does to a child PR. It is recoverable, but\n"
        "         stackem will not reopen a pull request on its own — someone may have closed\n"
        "         it deliberately. To restore it:\n"
        "\n"
        "  git fetch origin refs/pull/101/head:rescue-base refs/pull/102/head:rescue-head\n"
        "  git push origin rescue-base:refs/heads/auth-model rescue-head:refs/heads/auth-endpoints\n"
        "  gh api -X PATCH /repos/acme/app/pulls/102 -f state=open\n"
        "  gh pr edit 102 --base main\n"
        "\n"
        "auth-ui, auth-docs: parent chain reaches a closed PR — skipped this run.\n"
        "\n"
        "done. 0 branches restacked.\n"
        "  #102 is orphaned — the commands that restore it are above.\n"
        "\n"
        "next: run the commands above, then: stackem sync"
    )


def test_orphan_rescue_without_a_deleted_base_branch_only_restores_the_head():
    view = SyncView(
        trunk="main",
        orphans=(
            Orphan(pr=102, branch="auth-endpoints", new_base="main", repo="acme/app"),
        ),
    )
    text = render_sync(view)
    assert "  git fetch origin refs/pull/102/head:rescue-head\n" in text
    assert "  git push origin rescue-head:refs/heads/auth-endpoints\n" in text
    assert "rescue-base" not in text


def test_sync_reports_a_branch_with_no_pull_request_twice():
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[
            Restack(branch="auth-docs", onto="auth-ui", commits=1, no_pull_request=True),
        ],
        verification=Verification(ranges=1),
        push=PushResult(branches=("auth-docs",)),
        pr_bases_ok=True,
        no_pull_request=(NoPullRequest(name="auth-docs", parent="auth-ui"),),
    )
    text = render_sync(view)
    assert "  note: auth-docs has no pull request" in text
    assert (
        "auth-docs has no pull request:\n"
        "  gh pr create --base auth-ui --head auth-docs"
    ) in text
    assert text.endswith("next: gh pr create --base auth-ui --head auth-docs")


def test_sync_guard_violation_stops_before_any_rebase():
    # SPEC.md sec 2 and CLAUDE.md invariant 3b.
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        guard=GuardViolation(
            branch="auth-ui", parent="auth-endpoints", queued=("auth-docs",)
        ),
    )
    assert render_sync(view) == (
        "fetching origin... done\n"
        "trunk origin/main unchanged\n"
        "\n"
        "STOPPED  auth-ui is not based on origin/auth-endpoints.\n"
        "         Its parent was force-pushed without restacking its children, so the\n"
        "         fork point cannot be derived. Nothing was rebased, nothing was pushed.\n"
        "\n"
        "  find the old tip:      git reflog auth-endpoints\n"
        "  then restack by hand:  git rebase --onto auth-endpoints <old-tip> auth-ui\n"
        "\n"
        "still queued after this: auth-docs\n"
        "\n"
        "next: git reflog auth-endpoints"
    )


def test_sync_refuses_a_rebase_it_did_not_start():
    # CLAUDE.md invariant 22 / SPEC.md sec 5.2 phase 0: continuing a user's own
    # `git rebase -i` and cascading on top of it is the failure this prevents,
    # so the refusal has to say whose rebase it is and how to finish it.
    view = SyncView(
        trunk="main",
        fetched=False,
        foreign=ForeignRebase(
            branch="wip",
            onto="9c4e1a2",
            reason="wip is not a member of this stack",
        ),
    )
    assert render_sync(view) == (
        "STOPPED  a rebase is already in progress and stackem did not start it.\n"
        "         wip is being rebased onto 9c4e1a2 — wip is not a member of this\n"
        "         stack. Continuing it would cascade on top of your own rebase, so\n"
        "         nothing was rebased and nothing was pushed.\n"
        "\n"
        "  finish yours:  git rebase --continue\n"
        "  or back out:   git rebase --abort\n"
        "\n"
        "next: finish your own rebase, then: stackem sync"
    )


def test_the_foreign_rebase_block_reads_the_restack_modules_report():
    # stackem.restack.ForeignRebase carries a gitx.RebaseState, not flat fields.
    @dataclass
    class State:
        branch: str = "wip"
        onto: str = "9c4e1a2"

    @dataclass
    class Foreign:
        state: State = field(default_factory=State)
        reason: str = "onto does not match the derived parent's tip"

    text = render_sync(SyncView(trunk="main", fetched=False, foreign=Foreign()))
    assert "         wip is being rebased onto 9c4e1a2 — onto does not match the" in text
    assert text.endswith("next: finish your own rebase, then: stackem sync")


def test_sync_stops_before_the_remote_when_verification_fails():
    # CLAUDE.md invariant 9 / SPEC.md sec 5.2 step 7: anything beyond "=" or a
    # clean drop stops sync BEFORE phase 2.
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[Restack(branch="auth-ui", onto="auth-endpoints", commits=3)],
        verification=Verification(
            ranges=3,
            ok=False,
            unexpected=(
                {
                    "branch": "auth-ui",
                    "status": "!",
                    "subject": "auth: handle rate limits in the login form",
                },
            ),
        ),
        push=PushResult(branches=("auth-ui",)),
    )
    text = render_sync(view)
    assert text == (
        "fetching origin... done\n"
        "trunk origin/main unchanged\n"
        "\n"
        "restacking auth-ui onto auth-endpoints... ok (3 commits)\n"
        "\n"
        "verifying... FAILED\n"
        "\n"
        "STOPPED  the replay changed commits that should have come through unchanged.\n"
        "         Nothing was pushed and no pull request base was retargeted; the\n"
        "         branches are rebased locally and the originals are still in the\n"
        "         reflog.\n"
        "\n"
        "  !  auth-ui  auth: handle rate limits in the login form\n"
        "\n"
        "next: check the commits above, then: stackem sync"
    )
    # Invariant 9: not one word about the remote.
    assert "pushing" not in text
    assert "PR bases" not in text
    assert "done." not in text


def test_a_failed_verification_exits_nonzero():
    engine = RecordingEngine(
        sync_view=SyncView(
            trunk="main",
            verification=Verification(ranges=1, ok=False),
            push=PushResult(branches=("b",)),
        )
    )
    code, out, _ = run_cli(["sync"], engine=engine)
    assert code == EXIT_INCOMPLETE
    assert "verifying... FAILED" in out


def test_a_foreign_rebase_exits_nonzero():
    engine = RecordingEngine(
        sync_view=SyncView(trunk="main", foreign=ForeignRebase(branch="wip", onto="abc1234"))
    )
    code, out, _ = run_cli(["sync"], engine=engine)
    assert code == EXIT_INCOMPLETE
    assert out.startswith("STOPPED  a rebase is already in progress")


def test_sync_rejected_push_says_nothing_was_pushed():
    view = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[Restack(branch="auth-ui", onto="auth-endpoints", commits=1)],
        verification=Verification(ranges=1),
        push=PushResult(branches=("auth-ui", "auth-docs"), ok=False, rejected=("auth-ui",)),
    )
    text = render_sync(view)
    assert "pushing auth-ui auth-docs... REJECTED (auth-ui)" in text
    assert "nothing was pushed" in text
    assert "PR bases: all correct" not in text
    assert "done." not in text
    assert text.endswith("next: reconcile auth-ui with origin/auth-ui, then: stackem sync")


def test_sync_dry_run_touches_nothing_and_prints_the_plan():
    view = SyncView(
        trunk="main",
        dry_run=True,
        trunk_moved=14,
        reparents=(
            Reparent(
                branch="auth-model",
                cause="merged",
                squashed_as="7d3f9a1",
                children=(("auth-endpoints", "auth-model", "main"),),
                retargets=({"pr": 102, "old_base": "auth-model", "new_base": "main"},),
            ),
        ),
        restacks=[
            Restack(branch="auth-endpoints", onto="origin/main", commits=2),
            Restack(branch="auth-ui", onto="auth-endpoints", commits=1),
        ],
        push=PushResult(branches=("auth-endpoints", "auth-ui")),
    )
    assert render_sync(view) == (
        "dry run — nothing will be changed\n"
        "\n"
        "fetching origin... done\n"
        "trunk origin/main moved: 14 new commits\n"
        "\n"
        "auth-model: merged (squashed as 7d3f9a1) — reparenting its children\n"
        "  auth-endpoints: parent auth-model -> main\n"
        "\n"
        "would retarget PR #102 base auth-model -> main\n"
        "would restack auth-endpoints onto origin/main\n"
        "would restack auth-ui onto auth-endpoints\n"
        "\n"
        "would push auth-endpoints auth-ui (atomic, --force-with-lease --force-if-includes)\n"
        "\n"
        "next: stackem sync"
    )


def test_dry_run_never_reports_work_as_done():
    # --dry-run prints the plan; nothing in it may read as something that
    # already happened, or an agent will believe the stack was pushed.
    view = SyncView(
        trunk="main",
        dry_run=True,
        trunk_moved=2,
        reparents=(
            Reparent(
                branch="auth-model",
                cause="merged",
                children=(("auth-endpoints", "auth-model", "main"),),
                retargets=({"pr": 102, "old_base": "auth-model", "new_base": "main"},),
            ),
        ),
        restacks=[
            Restack(
                branch="auth-ui",
                onto="auth-endpoints",
                commits=1,
                dropped=(Dropped(sha="3f2a1bc", subject="dupe"),),
            )
        ],
        verification=Verification(ranges=1),
        push=PushResult(branches=("auth-ui",)),
        pr_bases_ok=True,
    )
    text = render_sync(view)
    for banned in (
        "restacking ",
        "retargeting ",
        "pushing ",
        "verifying...",
        "done. ",
        "PR bases:",
        "... ok",
    ):
        assert banned not in text, banned
    assert text.startswith("dry run — nothing will be changed\n")
    assert "would restack auth-ui onto auth-endpoints" in text
    assert "would retarget PR #102 base auth-model -> main" in text
    assert text.endswith("next: stackem sync")


def test_conflict_block_reads_the_restack_modules_own_field_names():
    # stackem.restack.ConflictReport (another agent's module) calls these
    # stopped_sha / stopped_subject / target, and has no tip sha for the parent.
    # The renderer reads either spelling so a sync engine can hand its report
    # straight through, and drops the "(sha)" when there is no sha to print.
    @dataclass
    class RestackConflict:
        branch: str = "auth-ui"
        parent: str = "auth-endpoints"
        target: str = "auth-endpoints"
        fork: str = "9c4e1a2"
        stopped_sha: str = "3f2a1bc"
        stopped_subject: str = "auth: handle rate limits in the login form"
        files: tuple = ("app/api/client.py",)
        queued: tuple = ("auth-docs",)

    text = render_sync(SyncView(trunk="main", conflict=RestackConflict()))
    assert (
        "CONFLICT in auth-ui\n"
        "  applying   3f2a1bc  auth: handle rate limits in the login form\n"
        "  onto       auth-endpoints\n"
        "  files      app/api/client.py\n"
    ) in text
    assert "still queued after this: auth-docs" in text
    assert text.endswith("next: resolve the conflicts and git add them, then: stackem sync")


def test_sync_no_op_run_still_ends_with_a_next_command():
    view = SyncView(trunk="main", trunk_moved=0, verification=Verification(ranges=0))
    text = render_sync(view)
    assert text.endswith("next: nothing — the stack is current.")
    assert "done. 0 branches restacked." in text


def test_sync_pluralises_the_summary():
    one = SyncView(
        trunk="main",
        trunk_moved=0,
        restacks=[Restack(branch="b", onto="a", commits=1)],
        push=PushResult(branches=("b",)),
    )
    assert "done. 1 branch restacked, 1 pushed." in render_sync(one)


# --------------------------------------------------------------------------
# stackem parent -- retargeting (session 01)
# --------------------------------------------------------------------------


def test_parent_retarget_matches_session_01():
    view = ParentView(branch="auth-docs", pr=104, old_base="auth-endpoints", new_base="auth-ui")
    assert render_parent(view) == (
        "retargeting PR #104 base auth-endpoints -> auth-ui... ok\n"
        "\n"
        "next: stackem sync"
    )


def test_parent_when_the_base_is_already_right():
    view = ParentView(
        branch="auth-docs", pr=104, old_base="auth-ui", new_base="auth-ui", changed=False
    )
    assert render_parent(view) == (
        "PR #104 already targets auth-ui. Nothing to do.\n"
        "\n"
        "next: nothing — the stack is current."
    )


def test_parent_without_a_pull_request_prints_the_create_command():
    view = ParentView(branch="auth-docs", pr=None, new_base="auth-ui")
    assert render_parent(view) == (
        "auth-docs has no pull request, so there is no base to retarget.\n"
        "  gh pr create --base auth-ui --head auth-docs\n"
        "\n"
        "next: gh pr create --base auth-ui --head auth-docs"
    )


def test_parent_failure_reports_the_error_and_the_retry():
    view = ParentView(
        branch="auth-docs",
        pr=104,
        old_base="auth-endpoints",
        new_base="auth-ui",
        error="422 Validation Failed: base branch auth-ui does not exist",
    )
    assert render_parent(view) == (
        "retargeting PR #104 base auth-endpoints -> auth-ui... FAILED\n"
        "  422 Validation Failed: base branch auth-ui does not exist\n"
        "\n"
        "next: fix the error above, then: stackem parent auth-docs --onto auth-ui"
    )


def test_parent_dry_run():
    view = ParentView(
        branch="auth-docs",
        pr=104,
        old_base="auth-endpoints",
        new_base="auth-ui",
        dry_run=True,
    )
    assert render_parent(view) == (
        "dry run — nothing will be changed\n"
        "\n"
        "would retarget PR #104 base auth-endpoints -> auth-ui\n"
        "\n"
        "next: stackem parent auth-docs --onto auth-ui"
    )


# --------------------------------------------------------------------------
# CLAUDE.md invariant 23 -- every output ends with the literal next command
# --------------------------------------------------------------------------


def _every_view():
    yield StatusView(trunk="main", branches=auth_rows())
    yield StatusView(trunk="main", branches=[])
    yield StatusView(
        trunk="main",
        branches=[BranchRow(name="a", pr=None, parent="main")],
    )
    yield SyncView(trunk="main", trunk_moved=0)
    yield SyncView(
        trunk="main",
        conflict=Conflict(
            branch="b", sha="1", subject="s", onto="a", onto_sha="2", files=("f",)
        ),
    )
    yield SyncView(trunk="main", guard=GuardViolation(branch="b", parent="a"))
    yield SyncView(trunk="main", foreign=ForeignRebase(branch="b", onto="abc1234"))
    yield SyncView(
        trunk="main",
        verification=Verification(ranges=1, ok=False),
        push=PushResult(branches=("b",)),
    )
    yield SyncView(
        trunk="main", push=PushResult(branches=("b",), ok=False, rejected=("b",))
    )
    yield SyncView(trunk="main", orphans=(Orphan(pr=1, branch="b", new_base="main", repo="o/r"),))
    yield SyncView(trunk="main", dry_run=True)
    yield ParentView(branch="b", pr=1, old_base="a", new_base="c")
    yield ParentView(branch="b", pr=None, new_base="c")
    yield ParentView(branch="b", pr=1, old_base="a", new_base="c", error="boom")
    yield ParentView(branch="b", pr=1, old_base="c", new_base="c", changed=False)


@pytest.mark.parametrize("view", list(_every_view()))
def test_every_output_ends_with_the_literal_next_command(view):
    render = {
        "StatusView": render_status,
        "SyncView": render_sync,
        "ParentView": render_parent,
    }[type(view).__name__]
    text = render(view)
    last = text.splitlines()[-1]
    assert last.startswith("next: "), text
    assert text.splitlines()[-2] == "", text  # a blank line sets it apart


# --------------------------------------------------------------------------
# argument parsing -- three commands, and no more
# --------------------------------------------------------------------------


def test_bare_stackem_calls_status():
    engine = RecordingEngine(status_view=StatusView(trunk="main", branches=auth_rows()))
    code, out, err = run_cli([], engine=engine)
    assert code == EXIT_OK
    assert engine.calls == [("status", {})]
    assert out.endswith("next: nothing — the stack is current.\n")
    assert err == ""


def test_sync_passes_dry_run_through():
    engine = RecordingEngine(sync_view=SyncView(trunk="main", trunk_moved=0))
    code, out, _ = run_cli(["sync", "--dry-run"], engine=engine)
    assert code == EXIT_OK
    assert engine.calls == [("sync", {"dry_run": True})]
    assert out.startswith("dry run — nothing will be changed\n")


def test_parent_requires_onto():
    code, _, err = run_cli(["parent", "auth-docs"])
    assert code == EXIT_USAGE
    assert "--onto" in err
    assert err.rstrip().splitlines()[-1].startswith("next: ")


def test_parent_passes_both_branches_through():
    engine = RecordingEngine(
        parent_view=ParentView(branch="auth-docs", pr=104, old_base="x", new_base="auth-ui")
    )
    code, out, _ = run_cli(["parent", "auth-docs", "--onto", "auth-ui"], engine=engine)
    assert code == EXIT_OK
    assert engine.calls == [
        ("set_parent", {"branch": "auth-docs", "onto": "auth-ui", "dry_run": False})
    ]
    assert out.startswith("retargeting PR #104 base x -> auth-ui... ok")


@pytest.mark.parametrize("name", ["init", "continue", "abort"])
def test_the_commands_that_do_not_exist_say_why(name):
    code, _, err = run_cli([name])
    assert code == EXIT_USAGE
    assert "there is no" in err
    assert err.rstrip().endswith("next: stackem sync")


def test_unknown_command_prints_usage():
    code, _, err = run_cli(["frobnicate"])
    assert code == EXIT_USAGE
    assert "usage: stackem" in err
    assert err.rstrip().splitlines()[-1].startswith("next: ")


def test_unknown_flag_is_refused():
    code, _, err = run_cli(["sync", "--yolo"])
    assert code == EXIT_USAGE
    assert "--yolo" in err


def test_dry_run_is_refused_on_the_read_only_command():
    code, _, err = run_cli(["--dry-run"])
    assert code == EXIT_USAGE
    assert "already read-only" in err


def test_help_and_version_end_with_a_next_command():
    for argv in (["--help"], ["-h"], ["--version"]):
        code, out, _ = run_cli(argv)
        assert code == EXIT_OK
        assert out.rstrip().splitlines()[-1].startswith("next: ")
    code, out, _ = run_cli(["--version"])
    assert out.startswith("stackem ")


def test_a_conflict_exits_nonzero_so_a_wrapper_knows_the_run_did_not_finish():
    engine = RecordingEngine(
        sync_view=SyncView(
            trunk="main",
            conflict=Conflict(
                branch="b", sha="1", subject="s", onto="a", onto_sha="2", files=("f",)
            ),
        )
    )
    code, out, _ = run_cli(["sync"], engine=engine)
    assert code == EXIT_INCOMPLETE
    assert "CONFLICT in b" in out


def test_engine_failure_is_reported_with_a_next_command():
    class Boom:
        def status(self):
            raise CliError("no git repository here", next_command="cd into a repository")

    code, _, err = run_cli([], engine=Boom())
    assert code == EXIT_INCOMPLETE
    assert "no git repository here" in err
    assert err.rstrip().endswith("next: cd into a repository")


def test_missing_sync_module_explains_the_contract(sandbox):
    # The engine is resolved lazily so the CLI stays importable while
    # stackem.sync is written in parallel.  When it is absent, say what is
    # expected rather than dying on an ImportError traceback.
    code, _, err = run_cli([], cwd=str(sandbox.path))
    if code == EXIT_OK:
        pytest.skip("stackem.sync now exists; the fallback path is unreachable")
    assert code == EXIT_INCOMPLETE
    assert "stackem.sync" in err
    assert err.rstrip().splitlines()[-1].startswith("next: ")


def test_an_engine_that_takes_no_dry_run_keyword_still_runs():
    # The sync module is being written in parallel; a plain sync() must not make
    # `stackem sync` die on a TypeError.
    class Old:
        def __init__(self):
            self.calls = 0

        def sync(self):
            self.calls += 1
            return SyncView(trunk="main", trunk_moved=0)

    engine = Old()
    code, out, _ = run_cli(["sync"], engine=engine)
    assert code == EXIT_OK
    assert engine.calls == 1
    assert out.rstrip().endswith("next: nothing — the stack is current.")


def test_a_read_only_violation_is_reported_as_text_not_a_traceback(sandbox):
    # An engine that forgets invariant 5 and fetches during `stackem` must be
    # stopped, and the stop has to reach the user as a normal report.
    def factory(context):
        class Fetcher:
            def status(self):
                context.git.fetch("origin", prune=True)

        return Fetcher()

    before = sandbox.git.branches("refs/remotes/origin")
    code, out, err = run_cli([], cwd=str(sandbox.path), engine_factory=factory)
    assert code == EXIT_INCOMPLETE
    assert out == ""
    assert "read-only" in err
    assert "invariant 5" in err
    assert err.rstrip().endswith("next: stackem sync")
    assert sandbox.git.branches("refs/remotes/origin") == before


def test_a_git_failure_is_reported_with_a_next_command(sandbox):
    def factory(context):
        class Broken:
            def status(self):
                context.git.rev_parse("refs/heads/no-such-branch")

        return Broken()

    code, _, err = run_cli([], cwd=str(sandbox.path), engine_factory=factory)
    assert code == EXIT_INCOMPLETE
    assert "rev-parse" in err
    assert err.rstrip().endswith("next: stackem sync")


def test_an_ancient_git_is_refused_before_anything_runs(tmp_path, monkeypatch):
    # SPEC.md sec 11: merge-tree --write-tree needs git 2.38 and
    # --force-if-includes needs 2.30, and the version is asserted on first run.
    # The entry point is where "first run" happens.
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text("#!/bin/sh\necho 'git version 2.20.1'\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    reached = []

    def factory(context):
        reached.append(context.command)
        return RecordingEngine(status_view=StatusView(trunk="main", branches=auth_rows()))

    code, out, err = run_cli([], cwd=str(tmp_path), engine_factory=factory)
    assert code == EXIT_INCOMPLETE
    assert reached == []  # refused before an engine could touch the repository
    assert out == ""
    assert "2.38" in err and "2.20.1" in err
    assert err.rstrip().splitlines()[-1].startswith("next: ")


def test_a_supported_git_runs_the_command(sandbox):
    # The other half of the version check: the real git in this environment must
    # not be refused.
    engine = RecordingEngine(status_view=StatusView(trunk="main", branches=auth_rows()))
    code, out, err = run_cli([], cwd=str(sandbox.path), engine_factory=lambda ctx: engine)
    assert code == EXIT_OK, err
    assert engine.calls == [("status", {})]
    assert out.endswith("next: nothing — the stack is current.\n")


def test_the_module_entry_point_is_the_same_command(sandbox):
    # SPEC.md sec 11 ships a console script for uvx; `python -m stackem` must
    # not drift from it.  Run it as a real process, in a real repository.
    import os
    import subprocess
    import sys

    env = {**os.environ, **sandbox.env}
    helped = subprocess.run(
        [sys.executable, "-m", "stackem", "--help"],
        cwd=str(sandbox.path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert helped.returncode == EXIT_OK
    assert helped.stdout.startswith("usage: stackem")
    assert helped.stdout.rstrip().endswith("next: stackem")

    run = subprocess.run(
        [sys.executable, "-m", "stackem"],
        cwd=str(sandbox.path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode in (EXIT_OK, EXIT_INCOMPLETE)
    printed = (run.stdout + run.stderr).rstrip()
    assert printed.splitlines()[-1].startswith("next: ")


# --------------------------------------------------------------------------
# CLAUDE.md invariant 5 -- `stackem` is read-only.  Real git, no mocks.
# --------------------------------------------------------------------------


def test_read_only_git_allows_the_reads_status_needs(sandbox):
    git = ReadOnlyGit(sandbox.path, env=sandbox.env)
    assert git.rev_parse("HEAD")
    assert git.branches("refs/heads")
    assert git.merge_base("HEAD", "HEAD")
    assert git.symbolic_ref("refs/remotes/origin/HEAD", short=True) == "origin/main"
    assert git.rev_list_count("HEAD") >= 1
    assert git.commit_subject("HEAD") == "root"


@pytest.mark.parametrize(
    "args",
    [
        ("fetch", "origin"),
        ("push", "origin", "main"),
        ("rebase", "--onto", "main", "main", "main"),
        ("remote", "set-head", "origin", "-a"),
        ("update-ref", "refs/heads/x", "HEAD"),
        ("branch", "-D", "nope"),
        ("checkout", "-b", "nope"),
        ("commit", "--allow-empty", "-m", "x"),
        ("symbolic-ref", "--delete", "refs/remotes/origin/HEAD"),
        ("config", "push.default", "simple"),
        ("gc",),
    ],
)
def test_read_only_git_refuses_every_write(sandbox, args):
    git = ReadOnlyGit(sandbox.path, env=sandbox.env)
    with pytest.raises(ReadOnlyViolation):
        git.run(*args)
    assert git.trace == []  # refused BEFORE the subprocess ran


def test_read_only_git_leaves_the_repository_untouched(sandbox):
    # A teammate pushes; a read-only run must not even learn about it, because
    # learning about it means writing remote-tracking refs.
    sandbox.make_stack(["feat-a"])
    sandbox.teammate_commit("feat-a", subject="teammate work")
    before_local = sandbox.tips()
    before_remote_tracking = sandbox.git.branches("refs/remotes/origin")

    git = ReadOnlyGit(sandbox.path, env=sandbox.env)
    git.branches("refs/heads")
    git.rev_parse("HEAD")
    with pytest.raises(ReadOnlyViolation):
        git.fetch("origin", prune=True)

    assert sandbox.tips() == before_local
    assert sandbox.git.branches("refs/remotes/origin") == before_remote_tracking


def test_build_git_is_read_only_for_status_and_writable_for_sync(sandbox):
    assert isinstance(build_git(sandbox.path, read_only=True), ReadOnlyGit)
    writable = build_git(sandbox.path, read_only=False)
    assert not isinstance(writable, ReadOnlyGit)
    assert writable.rev_parse("HEAD")


def test_the_status_command_is_handed_a_read_only_git(sandbox):
    seen = {}

    def factory(context):
        seen["git"] = context.git
        seen["command"] = context.command
        return RecordingEngine(status_view=StatusView(trunk="main", branches=auth_rows()))

    code, _, _ = run_cli([], cwd=str(sandbox.path), engine_factory=factory)
    assert code == EXIT_OK
    assert seen["command"] == "status"
    assert isinstance(seen["git"], ReadOnlyGit)


def test_the_sync_command_is_handed_a_writable_git(sandbox):
    seen = {}

    def factory(context):
        seen["git"] = context.git
        return RecordingEngine(sync_view=SyncView(trunk="main", trunk_moved=0))

    code, _, _ = run_cli(["sync"], cwd=str(sandbox.path), engine_factory=factory)
    assert code == EXIT_OK
    assert not isinstance(seen["git"], ReadOnlyGit)


# --------------------------------------------------------------------------
# --verbose: every git invocation, on stderr so stdout stays parseable
# --------------------------------------------------------------------------


def test_verbose_logs_every_real_git_invocation(sandbox):
    err = Capture()
    git = build_git(sandbox.path, read_only=True, logger=verbose_logger(err))
    git.rev_parse("HEAD")
    git.run("rev-parse", "-q", "--verify", "refs/heads/does-not-exist", check=False)

    lines = err.text.splitlines()
    assert lines[0] == "+ git rev-parse --verify HEAD"
    assert lines[1] == "+ git rev-parse -q --verify refs/heads/does-not-exist -> exit 1"


def test_verbose_output_goes_to_stderr_not_stdout(sandbox):
    def factory(context):
        context.git.rev_parse("HEAD")
        return RecordingEngine(status_view=StatusView(trunk="main", branches=auth_rows()))

    code, out, err = run_cli(["--verbose"], cwd=str(sandbox.path), engine_factory=factory)
    assert code == EXIT_OK
    assert "+ git rev-parse --verify HEAD" in err
    assert "+ git" not in out
    assert out.endswith("next: nothing — the stack is current.\n")
