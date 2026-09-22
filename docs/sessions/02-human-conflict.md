# 02 — Resolving a conflict without AI

*Human, no AI involved.* A change low in the stack collides with work higher up.

The point of this session: **conflict resolution is ordinary git.** stackem stops, tells you
exactly where you are, and gets out of the way. There is no special mode and no "continue"
command to remember — you run `stackem sync` again.

---

## The setup

While building `auth-ui` you hit a bug in the API client and patched it there. Now the reviewer
on `auth-endpoints` — two branches lower — asks for the same fix, properly, at the source.

```console
$ git checkout auth-endpoints
$ vim app/api/client.py
$ git commit -am "auth: retry on 429 from the login endpoint"
```

---

## sync stops

```console
$ stackem sync
fetching origin... done
trunk origin/main unchanged

restacking auth-ui onto auth-endpoints... CONFLICT

CONFLICT in auth-ui
  applying   3f2a1bc  auth: handle rate limits in the login form
  onto       auth-endpoints (9c4e1a2)
  files      app/api/client.py

Resolve the conflicts, `git add` them, then run `stackem sync` again.
To undo everything and restore all branches: stackem abort

still queued after this: auth-docs
```

Three things that matter in that output: which branch it was building, which commit failed to
apply, and that `auth-docs` has not been touched yet. You are in a normal `git rebase`, on a
detached HEAD, exactly as if you had run it yourself.

---

## Resolve it

Nothing stackem-specific here:

```console
$ git status
interactive rebase in progress; onto 9c4e1a2
Unmerged paths:
  (use "git restore --staged <file>..." to unstage)
  (use "git add <file>..." to mark resolution)
	both modified:   app/api/client.py

$ vim app/api/client.py
```

The conflict is the fix you already made in `auth-ui` against the fix you just made properly in
`auth-endpoints`. Keep the lower one:

```console
$ git add app/api/client.py
```

Do **not** run `git rebase --continue`. Run sync again:

```console
$ stackem sync
continuing rebase of auth-ui... ok

  note: auth-ui: dropped 1 commit (already upstream)
        3f2a1bc  auth: handle rate limits in the login form

restacking auth-docs onto auth-ui... ok (1 commit)

verifying... auth-ui: 2 of 3 commits unchanged, 1 dropped
pushing auth-endpoints auth-ui auth-docs... ok (atomic)
PR bases: all correct

done. 3 branches restacked, 3 pushed.
  auth-ui now has 2 commits (was 3)
```

sync noticed the rebase in progress, finished it, and carried on with the rest of the stack.

---

## The bit to actually read

```
note: auth-ui: dropped 1 commit (already upstream)
```

Your commit on `auth-ui` disappeared. That is correct — the same change now lives lower down in
`auth-endpoints`, so replaying it produced nothing. Git does this automatically and normally
mentions it in output that scrolls past; stackem pulls it out and repeats it in the summary,
because a PR quietly losing a commit is worth noticing.

If that was *not* what you wanted, the commit is still reachable:

```console
$ git reflog auth-ui
3f2a1bc auth-ui@{1}: rebase (start): checkout auth-endpoints
```

---

## If you want out

At any point, including mid-conflict:

```console
$ stackem abort
aborting rebase of auth-ui... done
restoring branch tips from snapshot 2026-09-22T14:31:07:
  auth-endpoints  9c4e1a2  (unchanged)
  auth-ui         7b1d9e4  restored
  auth-docs       2e8f3a1  (unchanged)

done. nothing was pushed.
```

Every branch tip is snapshotted to a ref before sync touches anything, so this is exact rather
than best-effort. Note the last line: sync pushes only after the entire cascade succeeds, so an
abort never leaves a half-updated stack on GitHub.

---

Next: [03 — Routine sync after trunk moves](03-claude-routine-sync.md)
