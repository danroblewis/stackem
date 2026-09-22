# 03 — Routine sync after trunk moves

*Claude Code session.* The most common interaction there is.

The point of this session: **two commands, no exploration.** Claude does not need to run
`git log --graph`, list PRs, or reconstruct anything. The state is one command away and the
next action is printed at the bottom of it.

---

> **User:** a bunch of stuff landed on main this morning, can you get my auth stack caught up

**Claude:** Let me look at the stack first.

```console
$ stackem
main (origin/main, 14 commits behind)
  1. auth-model      #101  needs restack (trunk moved)
  2. auth-endpoints  #102  needs restack
  3. auth-ui         #103  needs restack
  4. auth-docs       #104  needs restack

next: stackem sync
```

Trunk moved 14 commits ahead, so all four branches need replaying. Running sync.

```console
$ stackem sync
fetching origin... done
trunk origin/main moved: 14 new commits

restacking auth-model onto origin/main... ok (1 commit)
restacking auth-endpoints onto auth-model... ok (2 commits)
restacking auth-ui onto auth-endpoints... ok (3 commits)
restacking auth-docs onto auth-ui... ok (1 commit)

verifying... all 7 commit ranges unchanged
pushing auth-model auth-endpoints auth-ui auth-docs... ok (atomic)
PR bases: all correct

done. 4 branches restacked, 4 pushed.

next: nothing — the stack is current.
```

All four branches replayed onto the new `main` cleanly, no conflicts. The verify step confirmed
all seven of your commits came through with identical patches — nothing was silently changed or
dropped.

All four PRs are updated and still target the right bases: #101 → `main`, #102 → `auth-model`,
#103 → `auth-endpoints`, #104 → `auth-ui`.

---

## Why this session is cheap

Two commands, about 25 lines of output, one round trip. Compare with what the same task costs
against a non-standard stacking tool:

| | stackem | typical ghstack session |
|---|---|---|
| Commands to determine state | 1 | `git log --graph --all`, `git branch -vv`, `gh pr list`, often `ghstack status` |
| Output to read | ~10 lines | hundreds of lines of graph |
| Model of the repo needed | none — ordinary branches | synthetic ref naming, commit metadata, base/head pairs |
| Recovery if confused | re-run `stackem sync` | improvise git commands, frequently making it worse |

The load-bearing design choices are that the objects are ordinary git objects the model already
understands, and that `sync` is idempotent — so "I am not sure what state this is in" resolves
by running one safe command rather than by investigating.

---

## The same session when nothing is wrong

Claude should be able to check cheaply and stop:

```console
$ stackem
main (origin/main, up to date)
  1. auth-model      #101  synced
  2. auth-endpoints  #102  synced
  3. auth-ui         #103  synced
  4. auth-docs       #104  synced

everything is up to date.

next: nothing — the stack is current.
```

No action needed — the stack is already current.

---

Next: [04 — Resolving a rebase conflict mid-cascade](04-claude-conflict.md)
