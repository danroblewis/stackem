# 06 — A branch goes empty

*Claude Code session.* The case that is confusing when it happens by hand.

When you fix something low in the stack that you had already worked around higher up, replaying
the higher branch produces nothing. If *every* commit on a branch is redundant, the branch
becomes identical to its parent: its PR has zero commits and cannot be merged.

The point of this session: **stackem removes the branch from the chain and stitches the stack
back together**, rather than leaving a dead PR for you to discover later.

---

> **User:** I moved the session-expiry fix down into auth-endpoints where it belongs. sync it up

**Claude:**

```console
$ stackem sync
fetching origin... done
trunk origin/main unchanged

restacking auth-ui onto auth-endpoints... EMPTY
  dropped  3f2a1bc  auth: expire sessions on logout  (already upstream)

auth-ui is now identical to auth-endpoints — all 1 of its commits are already
in the parent. PR #103 would have no commits and could not be merged.

removing auth-ui from the stack:
  auth-docs: parent auth-ui -> auth-endpoints
  retargeting PR #104 base auth-ui -> auth-endpoints... ok
  commenting and closing PR #103... ok
  deleting branch auth-ui (local and origin)... ok

restacking auth-docs onto auth-endpoints... ok (1 commit)

verifying... all 3 commit ranges unchanged
pushing auth-endpoints auth-docs... ok (atomic)
PR bases: all correct

done. 1 branch removed, 2 restacked, 2 pushed.
```

Moving that fix down emptied `auth-ui` completely — its only commit,
`3f2a1bc "auth: expire sessions on logout"`, is now redundant because the same change lives in
`auth-endpoints`.

So `auth-ui` was removed from the stack. PR #103 is closed with a comment explaining why, and
PR #104 now targets `auth-endpoints` directly. Your stack is three deep instead of four:

- #102 `auth-endpoints` → `main` — 3 commits
- #104 `auth-docs` → `auth-endpoints` — 1 commit

Nothing was lost — the work is in `auth-endpoints` where you put it.

---

## The ordering this depends on

Removing a branch from the middle of a stack is the most destructive thing sync does, and the
sequence is not interchangeable:

1. **Reparent the children locally** — `auth-docs`'s parent becomes `auth-endpoints`.
2. **Retarget the children's PRs on GitHub** — #104's base moves to `auth-endpoints`.
3. **Close the empty PR** with an explanatory comment.
4. **Only then delete the branch.**

Step 2 must precede step 4. Deleting a branch closes every open PR that points at it as base
*or* head, and a PR closed that way cannot be reopened while either branch is missing. Get the
order wrong and you lose #104's review history along with the branch you meant to remove.

Step 3 before step 4 is cosmetic but worth doing: deleting `auth-ui` would close #103 anyway,
but as an unexplained event in the reviewer's notifications. The comment stackem leaves says the
branch was emptied by a change moving lower in the stack.

---

## Telling this apart from a mistake

An emptied branch is sometimes a signal that you did something you did not intend — squashed
the wrong direction, or rebased a branch onto its own child. Two guards:

**sync reports it before acting**, naming every dropped commit, so the summary carries it back
into the conversation instead of scrolling past in rebase output.

**It is reversible.** `stackem abort` restores every tip from a snapshot ref, and the dropped
commits stay reachable through the reflog. If the branch was already removed by an earlier sync,
its commits remain retrievable from `refs/pull/103/head` on GitHub indefinitely:

```console
$ git fetch origin refs/pull/103/head:recover-auth-ui
$ git log --oneline recover-auth-ui
3f2a1bc auth: expire sessions on logout
```

If you would rather sync never did this automatically:

```console
$ stackem sync --keep-empty
...
restacking auth-ui onto auth-endpoints... EMPTY

WARNING  auth-ui is empty — PR #103 has no commits and cannot be merged.
         Leaving it in place because --keep-empty was given.
         To remove it: stackem sync
```

---

## Partially emptied branches are left alone

A branch that loses *some* commits is still a real branch and stays in the stack. sync reports
the loss and moves on:

```console
restacking auth-ui onto auth-endpoints... ok (2 commits)

  note: auth-ui: dropped 1 commit (already upstream)
        3f2a1bc  auth: expire sessions on logout

done. 3 branches restacked, 3 pushed.
  auth-ui now has 2 commits (was 3)
```

Worth checking the PR description afterwards — it probably still describes the dropped commit.
Only a branch reduced to *zero* commits triggers removal.

---

Back to the [index](../README.md).
