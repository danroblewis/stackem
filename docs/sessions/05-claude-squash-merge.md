# 05 — The squash-merge cascade

*Claude Code session.* This is the reason stackem exists.

Your company squash-merges. When PR #101 lands, its four commits become one new commit on `main`
that shares no history with your branch. Every branch above it still contains the *old* commits.
Left alone, PR #102 will show both its own work and a duplicate of everything already merged.

The point of this session: **the hard case takes the same one command as the easy case.**

---

> **User:** #101 got merged this morning, sort out the rest of the stack

**Claude:**

```console
$ stackem
main (origin/main, 1 commit behind)
  x  auth-model      #101  MERGED (squashed as 7d3f9a1)
  1. auth-endpoints  #102  needs restack, PR base is a merged branch
  2. auth-ui         #103  needs restack
  3. auth-docs       #104  needs restack

next: stackem sync
```

#101 was squash-merged. `auth-endpoints` is still built on the old `auth-model` commits and its
PR still targets a branch that is now merged. Running sync.

```console
$ stackem sync
fetching origin... done
trunk origin/main moved: 1 new commit

auth-model: merged (squashed as 7d3f9a1) — reparenting its children
  auth-endpoints: parent auth-model -> main

retargeting PR #102 base auth-model -> main... ok
restacking auth-endpoints onto origin/main... ok (2 commits)
restacking auth-ui onto auth-endpoints... ok (3 commits)
restacking auth-docs onto auth-ui... ok (1 commit)

verifying... all 6 commit ranges unchanged
pushing auth-endpoints auth-ui auth-docs... ok (atomic)
PR bases: all correct

done. 3 branches restacked, 3 pushed.

auth-model is merged and no longer part of the stack. Delete it when you are ready:
  git push origin --delete auth-model && git branch -D auth-model

next: nothing — the stack is current.
```

Sorted. The stack is now three PRs deep with #102 at the bottom targeting `main`:

- #102 `auth-endpoints` → `main` — 2 commits, ready for review
- #103 `auth-ui` → `auth-endpoints` — 3 commits
- #104 `auth-docs` → `auth-ui` — 1 commit

The verify step confirmed all six of your commits replayed with identical patches, so nothing
was lost in the history rewrite. `auth-model` is deleted locally and on origin.

---

## What the ordering had to get right

Three things in that output are load-bearing and easy to get wrong by hand:

**Detecting the squash.** `auth-model`'s commits are nowhere in `main` — squashing produces a
new commit with a different patch-id, so `git cherry` and ancestry checks both report "not
merged." stackem confirms it from the PR state, falling back to comparing the tree that merging
the branch into `main` would produce against `main`'s own tree.

**Replaying from the right point.** Rebasing `auth-endpoints` required knowing where
`auth-model`'s tip *used to be*. `git merge-base main auth-endpoints` points below
`auth-model`'s commits, so rebasing from there replays those commits against the squash commit
that already contains them — a guaranteed conflict. The fork point is
`merge-base(origin/auth-model, auth-endpoints)` instead: `origin/auth-model` still points at the
pre-merge tip, so it *is* where the parent used to be. Only the target moves to `main`; the fork
point stays with the branch the work actually grew from, and nothing is stored anywhere.

**Retargeting before deleting.** PR #102's base was changed to `main` *before* `auth-model` was
deleted. Delete the branch first and GitHub closes #102.

---

## The variant: someone deleted the branch first

If the merge was done with "Delete branch" checked, GitHub closes the child PR before stackem
ever runs. That is recoverable:

```console
$ stackem
main (origin/main, 1 commit behind)
  x  auth-model      #101  MERGED (squashed as 7d3f9a1), branch deleted
  !  auth-endpoints  #102  CLOSED — base branch was deleted
  2. auth-ui         #103  needs restack
  3. auth-docs       #104  needs restack

  #102 was closed by the branch deletion, not by a person. sync can reopen it.

next: stackem sync
```

```console
$ stackem sync
fetching origin... done

WARNING  PR #102 (auth-endpoints) is closed and its head branch is gone from origin.
         That is what a branch deletion does to a child PR. It is recoverable, but
         stackem will not reopen a pull request on its own — someone may have closed
         it deliberately. To restore it:

  git fetch origin refs/pull/101/head:rescue-base refs/pull/102/head:rescue-head
  git push origin rescue-base:refs/heads/auth-model rescue-head:refs/heads/auth-endpoints
  gh api -X PATCH /repos/acme/app/pulls/102 -f state=open
  gh pr edit 102 --base main

auth-ui, auth-docs: parent chain reaches a closed PR — skipped this run.

done. 0 branches restacked.

next: run the commands above, then: stackem sync
```

#102 is open again with its review history, approvals and inline comments intact, now targeting
`main`.

The recovery works because GitHub keeps every PR's commits under `refs/pull/N/head` forever,
even after the branch is deleted — so the branches can be recreated exactly. Reopening requires
**both** the head and base branches to exist, which is why the merged `auth-model` is restored
first and deleted again at the end.

You can avoid needing this by not checking "Delete branch" when merging. If your repo has
**Settings → Automatically delete head branches** enabled, it happens on every merge; `stackem`
warns about that setting the first time it runs.

---

Next: [06 — A branch goes empty](06-empty-branch.md)
