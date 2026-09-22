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
To back out instead: git rebase --abort

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

next: nothing — the stack is current.
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

There is no stackem command for this. Back out of the rebase the normal way:

```console
$ git rebase --abort
```

That is the whole recovery. Nothing was pushed — sync does all its rebasing locally and only
pushes once the entire cascade succeeds — so there is nothing on GitHub to unwind.

`auth-endpoints` stays rewritten locally, because it was restacked successfully before the
conflict. That is not damage and needs no repair:

```console
$ stackem
main (origin/main, up to date)
  1. auth-endpoints  #102  restacked, not pushed
  2. auth-ui         #103  needs restack
  3. auth-docs       #104  needs restack

next: stackem sync
```

The next sync recognises `auth-endpoints` is already sitting on the right parent, skips it, and
retries `auth-ui`. A half-finished cascade heals itself.

---

Next: [03 — Routine sync after trunk moves](03-claude-routine-sync.md)
