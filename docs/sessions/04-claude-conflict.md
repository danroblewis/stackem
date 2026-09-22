# 04 — Resolving a rebase conflict mid-cascade

*Claude Code session.* The case that goes badly with ghstack.

The point of this session: **Claude resolves the conflict with the git skills it already has,
then runs the same command again.** There is no continue command, no stackem-specific conflict
mode, and no state for the model to track.

---

> **User:** I fixed the retry logic properly down in auth-endpoints, push my stack up

**Claude:** Running sync to replay the branches above it.

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

A conflict in `app/api/client.py` — your `auth-ui` commit patched the same retry logic you just
fixed properly in `auth-endpoints`. Let me look.

```console
$ git diff --diff-filter=U
```
```python
<<<<<<< HEAD
    def _request(self, method, url, **kw):
        for attempt in range(3):
            resp = self._session.request(method, url, **kw)
            if resp.status_code != 429:
                return resp
            time.sleep(self._backoff(attempt))
        return resp
=======
    def _request(self, method, url, **kw):
        resp = self._session.request(method, url, **kw)
        if resp.status_code == 429:
            time.sleep(1.0)
            resp = self._session.request(method, url, **kw)
        return resp
>>>>>>> 3f2a1bc (auth: handle rate limits in the login form)
```

`HEAD` is your proper fix in `auth-endpoints` — three attempts with backoff. The incoming side
is the single-retry stopgap from `auth-ui`, which the lower fix supersedes. Keeping `HEAD`.

```console
$ git checkout --ours app/api/client.py
$ git add app/api/client.py
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

Done. One thing worth flagging: `auth-ui` lost a commit.

`3f2a1bc "auth: handle rate limits in the login form"` was dropped because after the proper fix
landed in `auth-endpoints`, replaying it produced no change. That is the correct outcome — the
behavior is still there, it just comes from the lower branch now. PR #103 is down from 3 commits
to 2, and its description still mentions rate limiting, so you may want to trim that.

If you did want the commit kept, it is still in the reflog at `auth-ui@{1}`.

---

## What made this cheap

- **The conflict was a normal git conflict.** Standard markers, `git diff --diff-filter=U`,
  `git checkout --ours`, `git add`. No tool-specific vocabulary.
- **The retry was the same command.** `stackem sync` again — not a different command whose
  existence the model has to know about. Even if Claude had forgotten, the conflict output
  printed it verbatim.
- **The dropped commit was surfaced twice** — inline and in the summary — so it ended up in the
  response to you rather than scrolling past in rebase output.

---

## If Claude gets lost

The recovery instruction is one line, and it is always the same:

> If you are unsure what state the stack is in, run `stackem sync`.

Because sync is idempotent and re-entrant, that is safe whether the repo is clean, mid-rebase
with conflicts resolved, or mid-rebase with conflicts outstanding. Each case prints what
happened and what to do next. The escape hatch, `stackem abort`, restores every branch tip from
a snapshot taken before sync started, and nothing is pushed until the whole cascade succeeds.

---

Next: [05 — The squash-merge cascade](05-claude-squash-merge.md)
