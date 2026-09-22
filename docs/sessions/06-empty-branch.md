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

removing auth-ui from the chain:
  auth-docs: parent auth-ui -> auth-endpoints
  retargeting PR #104 base auth-ui -> auth-endpoints... ok

restacking auth-docs onto auth-endpoints... ok (1 commit)

verifying... all 3 commit ranges unchanged
pushing auth-endpoints auth-docs... ok (atomic)
PR bases: all correct

done. 1 branch removed from the chain, 2 restacked, 2 pushed.

auth-ui is empty and no longer in the stack. PR #103 has no commits and cannot
be merged. When you are ready:
  gh pr close 103 -c "emptied — the change moved down into auth-endpoints"
  git push origin --delete auth-ui && git branch -D auth-ui

next: nothing — the stack is current.
```

Moving that fix down emptied `auth-ui` completely — its only commit,
`3f2a1bc "auth: expire sessions on logout"`, is now redundant because the same change lives in
`auth-endpoints`.

So `auth-ui` was taken out of the chain: PR #104 now targets `auth-endpoints` directly, so the
stack is three deep instead of four. PR #103 is left open and flagged — closing a pull request is
visible to reviewers, so stackem prints the command rather than running it:

- #102 `auth-endpoints` → `main` — 3 commits
- #104 `auth-docs` → `auth-endpoints` — 1 commit

Nothing was lost — the work is in `auth-endpoints` where you put it.

---

## The ordering this depends on

The order matters, and it is why stackem retargets but does not delete:

1. **Reparent the child** — `auth-docs`'s parent becomes `auth-endpoints`.
2. **Retarget the child's PR on GitHub** — #104's base moves to `auth-endpoints`.
3. **Only then may `auth-ui` be deleted** — and that is left to you.

Step 2 must precede any deletion. Deleting a branch closes every open PR that points at it as
base *or* head, and such a PR cannot be reopened while either branch is missing. Run the delete
before the retarget and you lose #104's review history along with the branch you meant to remove.

Because sync has already done step 2, the commands it prints are safe to run in any order after
that. That is the whole reason deletion is not automatic: the irreversible half stays with you,
and the half that makes it safe has already happened.

---

## Telling this apart from a mistake

An emptied branch is sometimes a signal that you did something you did not intend — squashed
the wrong direction, or rebased a branch onto its own child. Two guards:

**sync reports it before acting**, naming every dropped commit, so the summary carries it back
into the conversation instead of scrolling past in rebase output.

**It is reversible.** `stackem abort` restores the tips sync changed, and the dropped commits stay
reachable through the reflog. Even after you delete the branch, its commits remain retrievable
from `refs/pull/103/head` on GitHub indefinitely:

```console
$ git fetch origin refs/pull/103/head:recover-auth-ui
$ git log --oneline recover-auth-ui
3f2a1bc auth: expire sessions on logout
```

Rerunning `stackem sync` before you act on those commands is a no-op — `auth-ui` is already out
of the chain and `auth-docs` already points past it. sync will keep reminding you that #103 is
empty until you close it, and will never close it for you.

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
