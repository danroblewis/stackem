# stackem — keeping a chain of stacked pull requests in sync

**Status:** design spec. Every mechanism below was verified empirically against git 2.39.5 and a
live GitHub repository before being written down. Findings that contradicted the initial design
are marked **Verified** or **Correction** with their evidence.

---

## 1. Scope

You have a chain of branches, each branched off the last, each with a pull request targeting the
branch below it. When anything underneath a branch moves — trunk advances, a lower branch is
amended, a PR is squash-merged — every branch above needs its history replayed onto the new
foundation and every PR base needs checking.

stackem does that. It does **not**:

- create branches, write commits, or open pull requests
- merge anything, or replace CI
- impose a branch naming scheme or store metadata in commit messages

Those belong to git, `gh`, your editor, and GitHub's merge button. The narrow job is keeping the
chain correct, particularly when the team squash-merges.

### The motivating constraint

The team currently uses `ghstack`, whose non-standard model (synthetic ref names, commit-message
metadata, bespoke commands) forces every Claude Code session to re-derive repository state from
scratch, burns large amounts of context, and regularly corrupts git history when the agent
improvises. stackem is designed so an agent needs almost no context to use it correctly:

- **Ordinary git objects** — real branches, real PRs. Everything the model already knows applies.
- **One verb** — `stackem sync` is the whole normal loop, re-entrant so there is no state machine.
- **Self-documenting output** — every message ends with the literal next command.
- **Idempotent** — recovery is repeating one safe command, not improvising git.

---

## 2. Core idea

Every operation is one primitive:

```
git rebase --onto <where the parent is NOW> <where the parent WAS> <branch>
```

Restacking after a parent gains commits, after a parent is amended, and after a parent is
**squash-merged** are all the same call. Only the resolution of "where the parent is now"
changes — after a squash merge it resolves to the trunk. There is no separate history-rewrite
code path.

What makes it work is that "where the parent was" is **remembered as a SHA**, never computed.

### Verified: `merge-base` is the wrong answer

Stack `main → feature-a (2 commits) → feature-b (2 commits)`, then squash-merge `feature-a`:

```
$ git merge-base main feature-b          # -> the INITIAL commit, below feature-a
$ git rebase --onto main $MERGE_BASE feature-b
CONFLICT (add/add): Merge conflict in a.txt
error: could not apply 0cec8da... a: add a1
```

It replays `feature-a`'s commits against the squash commit that already contains them. With the
stored base:

```
$ git rebase --onto main $(git rev-parse refs/stackem/base/feature-b) feature-b
Successfully rebased and updated refs/heads/feature-b.

$ git log --oneline feature-b
2a4f0b6 b: add b2
2c37f81 b: add b1
52b9e5a Squashed feature-a (#1)
26ff509 initial
```

Re-verified against a **real GitHub squash merge**, not only a local `merge --squash`.

---

## 3. State model

All state is native git. No dotfiles, no sidecar config, nothing in the worktree.

| Ref / key | Meaning |
|---|---|
| upstream tracking ref | `parent(B)` — the branch below B |
| `refs/stackem/base/<B>` | parent's tip as of B's last restack |
| `refs/stackem/pushed/<B>` | the SHA we last pushed for B; the force-push lease value |
| `refs/stackem/undo/<ts>/<B>` | pre-operation snapshot of every tip |
| `stackem.op.*` in `.git/config` | position within an interrupted cascade |

Derived: B is in sync when `base(B) == tip(parent(B))`; B's PR contents are `base(B)..B`.

### 3.1 Parent — the upstream tracking ref

```
branch.feature-b.merge = refs/heads/feature-a
branch.feature-b.remote = origin
```

A branch is in a stack when its upstream branch name differs from its own. No marker key needed.

**Verified** — stock git becomes stack-aware for free:

```
$ git status -sb
## feat-b...origin/feat-a [ahead 2]        <- the PR's size, at a glance

$ git branch -vv
  feat-a 8f7b050 [origin/main: ahead 1] a1
* feat-b b5f6db3 [origin/feat-a: ahead 2] b2    <- the whole stack
  main   9539f5d [origin/main] init

$ git log --oneline @{u}..                 <- exactly the PR's commits
```

**Verified cost:** stock `git push` fails, and git's own hint is dangerous —

```
$ git push
fatal: The upstream branch of your current branch does not match
the name of your current branch.  To push to the upstream branch
on the remote, use

    git push origin HEAD:feat-a
```

That would push `feat-b`'s content onto `feat-a`. `stackem init` sets `push.default = current`,
after which `git push` targets the correct branch and the parent is untouched. Setting
`branch.<n>.rebase = true` additionally makes plain `git pull` perform the restack.

**Alternative if `push.default` cannot be changed:** store the parent in
`branch.<n>.stackemParent` instead. Verified to survive `git branch -m` of the child (git migrates
the whole `branch.<name>.*` section) and enumerable via `git config --get-regexp`; note keys read
back lowercased. Leaves upstream conventional at the cost of the free git integration above.

### 3.2 Base — `refs/stackem/base/<branch>`

The parent's tip commit as of the last restack. A ref, so it is a **GC root**.

**Verified** — survives `git reflog expire --expire=now --all && git gc --prune=now`. This is
load-bearing: after a merged parent branch is deleted, this ref is the only thing keeping its tip
commit reachable, and that commit is exactly what the next `rebase --onto` needs.

### 3.3 Pushed, undo, and operation state

`pushed` exists because the obvious force-push lease is unsafe (§6.2). `undo` snapshots every tip
before sync mutates anything. `stackem.op.*` records position within a cascade;
**verified** that `.git/config` is writable while a rebase is in progress.

---

## 4. Commands

```
stackem                         show the stack and what is stale
stackem sync                    make everything correct again  (re-entrant, idempotent)
stackem abort                   undo an in-progress sync, restore every branch tip
stackem parent <b> --onto <p>   fix a wrongly-inferred parent
```

`sync` is the only one needed day to day. There is deliberately no `continue`: sync detects an
in-progress rebase and resumes (§8).

`stackem parent --set` must rewrite **both** the parent pointer and the base ref. Changing the
pointer alone leaves stackem remembering the old parent's position, and the next restack replays
the wrong commit range — silently.

### Parents are inferred, not declared

There is no `create` or `adopt` command. Branches are made with `git checkout -b`; stackem infers
the parent on first sight by nearest ancestor. **Verified** on a hand-built stack:

```
parent(feat-a) = main     (2 commits ahead)
parent(feat-b) = feat-a   (1 commits ahead)
parent(feat-c) = feat-b   (2 commits ahead)
```

The base ref is bootstrapped as `base(child) := merge-base(parent, child)` — the fork point, which
is exactly the commit `rebase --onto` needs. **Do not assert that it equals `tip(parent)`.**

**Correction.** An earlier draft required `merge-base(parent, child) == tip(parent)` and stopped
otherwise, on the theory that a mismatch meant the branch was cut from the middle of its parent.
That is wrong and would fire on almost every real stack. Verified on an ordinary stale one — the
parent simply gained commits after the child was branched:

```
merge-base(feat-a,feat-b) = 1872155
tip(feat-a)               = d1c7afb     <- differs
old tip(feat-a)           = 1872155     <- merge-base IS the fork point

$ git update-ref refs/stackem/base/feat-b $MB
$ git rebase --onto feat-a $MB feat-b
feat-b commits after restack: 2     (b1, b2 — replayed onto a3, no duplication)
```

The two cases are indistinguishable from topology anyway, and it does not matter: the commits to
replay are `merge-base..child` either way.

When pull requests already exist, their `head`/`base` pairs *are* the parent graph and are used in
preference to topology inference. That also reconstructs a stack in a fresh clone.

### 4.1 Adopting a stack that predates stackem

The common case: a chain of PRs built by hand, found stackem afterwards. First `stackem sync`
bootstraps parents from PR bases, bootstraps base refs from merge-base, and proceeds normally.
Three situations need care.

**A pull request in the chain has already been merged.** If its branch is gone, `merge-base` for
the child points *below* the merged commits and would replay them against the squash commit (§2).
The old parent tip is recoverable — GitHub retains it indefinitely:

```
git fetch origin refs/pull/<parent-pr>/head:refs/stackem/base/<child>
```

So the rule is: bootstrap from `merge-base` only when the parent branch still exists and is not
merged; otherwise bootstrap from `refs/pull/N/head`. This is also why the base ref is a ref —
after adoption it keeps that commit alive locally.

**A branch contains a merge commit.** Hand-built stacks often have `git merge main` in them
instead of a rebase. `rebase --onto` silently linearizes it — **verified**:

```
before:  * x2                        after:  * x2
         *   Merge branch 'main'             * x1
         |\                                  * main moved
         | * main moved
         * | x1
```

The merge commit is gone and the PR's commit list changes. That is usually what you want, but it
is an unrequested rewrite, so adoption should report it and require confirmation before the first
sync of a branch containing merges.

**PR bases disagree with topology.** Someone may have left every PR targeting `main`. stackem
should compute both the PR-derived graph and the topological one, and when they disagree, show
both and ask rather than silently picking. Topology usually reflects intent; the PR base is what
reviewers currently see.

---

## 5. How sync works

### 5.1 Orientation

stackem has no database and no create step, so every run re-derives the stack from scratch. The
governing rule:

> **Git is the source of truth for structure. GitHub is the source of truth for pull request
> state. sync makes GitHub's PR bases match git's parents, never the reverse.**

The one exception is first sight: a branch with no recorded parent adopts its PR's base, because
that is what the author already told GitHub.

| Question | Local source | GitHub source | Who wins |
|---|---|---|---|
| What is the trunk? | `origin/HEAD` | default branch | local, API as fallback |
| Which branches exist? | `for-each-ref` | — | local only |
| What is B's parent? | upstream ref | PR base | **local**; GitHub only bootstraps |
| Where is B's base? | `refs/stackem/base/B` | `refs/pull/N/head` | local; bootstrap from either (§4.1) |
| Which PR is B's? | — | index by head ref | GitHub only |
| Is B merged? | `merge-tree` test | PR state | GitHub; `merge-tree` offline |

**Two network calls per sync**, regardless of stack depth: one `git fetch`, one PR list.

#### Every parent in one git call

```console
$ git for-each-ref refs/heads --format='%(refname:short)|%(upstream:short)|%(objectname:short)'
  auth-endpoints|origin/auth-model|657a733
  auth-model|origin/main|b97af14
  auth-ui|origin/auth-endpoints|dc8d91b
  main|origin/main|b4f99b4
  scratch-perf||a3b1cfc
```

A branch is in a stack when its upstream names a **different** branch. `main` tracks itself, so it
is a root. `scratch-perf` has **no upstream at all** — it is untracked, and a parent is inferred
for it on first sight (§4). Note that empty upstreams must be excluded explicitly; treating the
empty string as a branch name makes every untracked branch look like a child of everything.

The stack is then the connected component containing `HEAD`: walk *down* through upstreams to the
trunk, and *up* by finding branches whose parent is already in the set. Run from a branch with no
stack, sync reports every stack in the repo rather than guessing.

#### Every pull request in one GitHub call

```console
$ gh pr list --state all --limit 100 --json number,headRefName,baseRefName,state,mergedAt
  head=feat-d  PR#5  base=feat-c  CLOSED  merged=false
  head=feat-b  PR#4  base=main    MERGED  merged=true
  head=feat-c  PR#3  base=main    OPEN    merged=false
  head=feat-b  PR#2  base=main    CLOSED  merged=false
  head=feat-a  PR#1  base=main    MERGED  merged=true
```

**Verified: branch → PR is not one-to-one.** `feat-b` has two. Precedence when indexing by head
ref: an **open** PR wins; failing that the most recent **merged** one; closed-unmerged PRs are
history.

#### Classifying a closed pull request

A closed PR is either wreckage from a branch deletion, which sync should repair, or a deliberate
act, which it must not undo. **Verified** that the head branch distinguishes them:

```
PR#2: closed merged=false head_ref=feat-b  head branch on remote? 0  -> deleted; rescuable
PR#5: closed merged=false head_ref=feat-d  head branch on remote? 1  -> a person closed it
```

Only the first is a rescue candidate (§6.3). Never resurrect the second.

#### Offline

Without GitHub, sync still restacks: parents come from upstreams, bases from refs, and merge
detection falls back to the `merge-tree` test (§6.4). What it cannot do is retarget PR bases,
rescue closed PRs, or tell a merged branch from a deleted one with certainty — so it reports
those as deferred rather than guessing.

### 5.2 The cascade

```
 0. if a rebase is in progress:
      unresolved conflicts remain -> reprint the conflict report, exit
      conflicts resolved          -> git rebase --continue, resume the cascade at stackem.op.*
 1. snapshot every branch tip -> refs/stackem/undo/<ts>/*
 2. git fetch origin  (+ refs/pull/N/head for any PR needing rescue)
 3. resolve trunk: origin/HEAD -> `git remote set-head origin -a` -> GitHub API
 4. build the stack: recorded parent, else inferred; ambiguous -> stop and ask
 5. reconcile with GitHub: locate each branch's PR, detect merged, detect closed-by-deletion
 6. rescue closed PRs: restore BOTH branches, reopen, retarget          (§6.3)
 7. reparent children of merged branches onto the merged branch's parent
 8. walk bottom-up:
      target = origin/<trunk> for a root, else tip(parent)
      if base(B) == target: skip
      git rebase --onto target base(B) B
        conflict -> record position in stackem.op.*, report, exit
      base(B) := target
      if B now has zero commits: mark for removal
 9. for each emptied branch, bottom-up:                                  (§7.2)
      reparent its children to its parent
      retarget the children's PR bases
      close its PR with an explanatory comment
      delete the branch, local and origin
10. verify each range with git range-diff; collect dropped commits
11. retarget any PR base that does not match its parent
12. atomic force-push every changed branch, leased against refs/stackem/pushed/*
13. update refs/stackem/pushed/*
14. delete merged branches, local and origin       (only now — after step 11)
15. report: restacked, pushed, dropped commits, emptied branches, PRs without a PR number
```

Step 8's `target = origin/<trunk>` for roots is how trunk movement propagates through the whole
stack. Steps 11-before-14 and 9's internal order are not interchangeable (§6.3).

---

## 6. Verified mechanics

### 6.1 Equivalence verification

`git range-diff` compares two versions of a patch series:

```
$ git range-diff $OLD_BASE..$OLD_TIP main..$NEW_TIP
1:  740ac2b = 1:  2c37f81 b: add b1
2:  5617521 = 2:  2a4f0b6 b: add b2
```

`=` means byte-identical patch. sync proceeds silently only when every commit is `=` or was
cleanly dropped as empty; anything else is reported.

### 6.2 Correction: the force-push lease is a trap

The obvious pattern is actively dangerous. Leasing against a freshly fetched SHA makes the lease
vacuous, because the fetch already absorbed the other person's commit:

```
=== 1. lease against FRESHLY FETCHED sha (the anti-pattern) ===
 + e8d9639...37ddad0 feat -> feat (forced update)
>>> teammate work survived? 0 (0 = CLOBBERED)

=== 2. lease against OUR RECORDED pushed-ref (correct) ===
 ! [rejected]  feat -> feat (stale info)
>>> teammate work survived? 1 (1 = PROTECTED)

=== 3. --force-with-lease --force-if-includes ===
>>> teammate work survived? 1 (1 = PROTECTED)
```

Always
`git push --force-with-lease=<b>:$(git rev-parse refs/stackem/pushed/<b>) --force-if-includes`.

**Atomic pushes.** Between pushing a rewritten parent and its rewritten child, the child's PR
displays the entire stack including already-merged commits — verified. One invocation removes the
window:

```
$ git push --atomic --force-with-lease=feat-b:$B --force-with-lease=feat-c:$C \
      --force-if-includes origin feat-b feat-c
```

**Review comments survive** a rewrite with line anchors intact (`line=1`, not outdated).

### 6.3 Branch deletion closes pull requests

**Verified.** Deleting a branch closes every open PR referencing it as head *or* base:

```
$ gh pr merge 1 --squash --delete-branch
#2 base=feat-a head=feat-b CLOSED      <- child PR closed

$ git push origin --delete feat-b      # head branch of open PR #2
#2 base=main head=feat-b CLOSED        <- closed again
```

A PR closed this way cannot be reopened while either branch is missing:

```
$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
422: state cannot be changed. The feat-b branch has been deleted.
```

**It is recoverable.** GitHub retains commits under `refs/pull/N/head` indefinitely. Restore both
branches, then reopen — **verified**:

```
$ git fetch origin refs/pull/1/head:refs/stackem/rescue/pr1
$ git fetch origin refs/pull/2/head:refs/stackem/rescue/pr2
$ git push origin refs/stackem/rescue/pr1:refs/heads/feat-a   # base
$ git push origin refs/stackem/rescue/pr2:refs/heads/feat-b   # head
$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
  #2 state=open base=feat-a head=feat-b
  review comments intact: 1
$ gh pr edit 2 --base main
  #2 base=main OPEN
```

So closure is **disruptive, not fatal** — but the ordering rule stands: retarget children off a
branch before deleting it. Never pass `--delete-branch`; warn when the repo has
`delete_branch_on_merge = true`.

GitHub can auto-retarget instead of closing, but only with its stacked-PR behaviour enabled.
Retargeting before deletion is correct under both.

### 6.4 Merge detection

**Correction.** The initial proposal — dry-run the restack, treat zero remaining commits as
merged — is wrong. It conflicts instead of emptying, and wedges the repo:

```
$ git rebase --onto main $BASE_A probe
CONFLICT — f.txt needs merge
=> NOT merged     # false negative
```

Use tree comparison. If merging the branch into the trunk yields exactly the trunk's own tree, the
branch is contained — **verified**, including after unrelated commits land on trunk, and verified
to report *not merged* correctly for an unmerged branch:

```
$ git merge-tree --write-tree main feat-a
31c2a0c78f5555387c6923950b34f42ccc0ab900
$ git rev-parse main^{tree}
31c2a0c78f5555387c6923950b34f42ccc0ab900     # identical => MERGED
```

Prefer the GitHub PR state; this is the offline fallback. `git cherry` does not work — squashing
changes the patch-id.

### 6.5 Pull request creation stays out of scope

**Verified:** `gh pr create --fill` ignores the upstream ref and bases new PRs on the default
branch (`base=main` for a branch stacked on `feat-c`). Retargeting an open PR afterwards is safe
and immediate — verified, with the PR showing the correct single commit right after.

So the team's PR template stays with the agent that writes it. `stackem` prints the exact command
with the base filled in:

```
auth-docs  --  no PR   →  gh pr create --base auth-ui --head auth-docs
```

### 6.6 Trunk detection

`refs/remotes/origin/HEAD` was **unset** in a test clone. Fall back to
`git remote set-head origin -a`, then the GitHub API, before assuming a name.

---

## 7. The timing problem

You are working on B4 and a reviewer asks for a change to B2. The new B2 commit is authored
*after* everything above it.

### 7.1 Ordering is a non-issue

**Verified.** Committing to B2 on Jan 10 when B3/B4 were authored Jan 4–5 produces correct,
readable history, because rebase refreshes committer dates on everything above:

```
$ git log B4 --oneline
0b19d7c B4: feature four
94161bb B3: feature three
2f35e50 B2: address review feedback
b64eff5 B2: feature two
dca17f2 B1: feature one
```

Author dates go non-monotonic (`01-10` sits between `01-04` and `01-03`), which default git output
never shows. Committer dates stay monotonic. Content was correct.

### 7.2 Dropping is the real hazard

If the change to B2 duplicates work already done higher up, replaying the higher branch produces
nothing and the commit is dropped:

```
Rebasing (1/2) dropping 45cef55 B4: hotfix f() returns 2 -- patch contents already upstream
B4 after: 1 commits
```

Git says so, but in output that scrolls past. **sync must treat dropped commits as a first-class
reportable outcome**, repeated in the summary.

In the limit the branch empties completely — **verified**:

```
C3 commits remaining: 0
C3 == C2? YES — branch is now EMPTY, its PR has no commits
```

A PR with zero commits cannot be merged, so sync **removes the branch from the stack** (§5 step 9):
reparent children, retarget the children's PR bases, close the empty PR with an explanatory
comment, then delete the branch — in that order, per §6.3. `--keep-empty` warns instead.

A branch that loses only *some* commits stays in the stack, with the loss reported.

---

## 8. Conflicts and re-entrancy

When a rebase conflicts, sync records its position and stops. The repo is left in a normal
`git rebase` state — ordinary markers, ordinary `git status`.

```
$ stackem sync
restacking auth-ui onto auth-endpoints... CONFLICT

CONFLICT in auth-ui
  applying   3f2a1bc  auth: handle rate limits in the login form
  onto       auth-endpoints (9c4e1a2)
  files      app/api/client.py

Resolve the conflicts, `git add` them, then run `stackem sync` again.
To undo everything and restore all branches: stackem abort

still queued after this: auth-docs
```

Resolution is standard git. The user — or the agent — then runs **`stackem sync` again**, which
detects the in-progress rebase, continues it, and resumes the cascade.

There is no `continue` command by design. One verb means an agent needs to know one thing, and
"if you are unsure of the state, run `stackem sync`" is correct in all three states: clean,
resolved, unresolved.

`stackem abort` aborts the rebase and restores every tip from the snapshot refs. Because nothing
is pushed until the whole cascade succeeds, abort never leaves a half-updated stack on GitHub.

---

## 9. Agent ergonomics

See [docs/AGENT.md](docs/AGENT.md) for the `CLAUDE.md` block, and [docs/](docs/README.md) for
worked sessions.

Output is compact text, not JSON — fewer tokens, and the model parses it fine.

```
$ stackem
main (origin/main, 14 commits behind)
  1. auth-model      #101  needs restack (trunk moved)
  2. auth-endpoints  #102  needs restack
  3. auth-ui         #103  needs restack
  4. auth-docs       --    no PR

next: stackem sync
```

Every message ends with the literal next command, so nothing about the command surface has to be
recalled. Drops and removals are surfaced twice — inline and in the summary — so they reach the
user rather than scrolling past.

---

## 10. Testing

End-to-end against real git repositories in temp directories, plus a fake GitHub implementing the
endpoints sync touches: list PRs, get PR, create PR, patch base, patch state, merge.

**The fake must reproduce the verified behaviors** — closing children on branch deletion, refusing
reopen while either branch is missing, squash-merge minting a new commit with a different
patch-id — or the suite passes while reality breaks. Keep an opt-in suite against a real
throwaway repository; the spike recipe works.

| Area | Cases |
|---|---|
| **Timing** (weight heaviest) | late fix on a lower branch: independent · conflicting · duplicate → commit dropped · branch fully emptied |
| Squash merge | cascade, retarget, prune merged branch |
| Merge + branch deleted | children closed → restore both branches → reopen → retarget |
| Empty-branch removal | children reparented and retargeted *before* deletion; PR closed with comment |
| Trunk movement | roots rebase onto `origin/<trunk>`, propagating upward |
| Concurrency | teammate pushes to a branch → lease refuses, no data loss |
| Conflicts | sync resumes at the right branch; abort restores every tip; nothing pushed mid-cascade |
| **Adoption** (predates stackem) | stale stack (parent moved after child branched) · parent already squash-merged and deleted → bootstrap from `refs/pull/N/head` · branch containing a merge commit → flattening reported · PR bases disagreeing with topology → asks |
| Inference | fresh stack · PR-derived graph preferred over topology · fresh clone with no local state |
| Environment | `origin/HEAD` unset · git < 2.38 · `delete_branch_on_merge = true` |

---

## 11. Implementation notes

- **Go, shelling out to the `git` binary.** Not libgit2 or go-git — neither implements rebase, and
  rebase-with-conflict-resolution is the entire product. Single static binary, `brew install`,
  no runtime.
- **Minimum git 2.38** for `merge-tree --write-tree`; `--force-if-includes` needs 2.30. Apple git
  2.39.5 suffices. Assert this on first run.
- **GitHub via `gh` when on PATH**, falling back to `GITHUB_TOKEN` + REST. `gh` is not universally
  installed; the fallback is not optional.
- **Require a clean worktree** for any restack, or auto-stash and restore.

---

## 12. Known limitations

- **Upstream-as-parent is non-standard.** IDE git panels and `gh pr create`'s base guess read that
  field and get it wrong. `push.default = current` fixes the CLI, not a GUI. §3.1 has the
  alternative.
- **`.git/config` metadata is per-clone.** Reconstructible from PR bases (§4).
- **`delete_branch_on_merge = true`** closes child PRs on every merge. Recoverable, but disruptive
  enough to warn about at init.
- **A collaborator pushing to a branch in your stack** is caught by the lease and refused. There is
  no way to make that pleasant.
- **Merge queues** produce a different post-merge shape; the `--onto` primitive handles it, but
  merge detection needs a queue-aware path.
- **Fork-based PRs** (head repo ≠ base repo) are not addressed.

---

## Appendix: what testing changed

| Initial assumption | Verified reality |
|---|---|
| GitHub auto-retargets child PRs on base deletion | Closes them, unless GitHub's stacked-PR behaviour is on; recoverable by restoring **both** branches then `PATCH state=open` |
| Only the *base* branch mattered for reopening | The **head** branch closes the PR too, and blocks reopening |
| Detect merge by dry-run rebase yielding 0 commits | Conflicts instead; use `merge-tree` tree comparison |
| `--force-with-lease=b:$(freshly fetched)` | Vacuous — clobbers teammate work; lease against the recorded push-point |
| Push branches sequentially | Leaves a window where child PRs show the whole stack; use `--atomic` |
| Out-of-order commit timestamps would confuse history | Non-issue; rebase refreshes committer dates. Silent commit *dropping* is the real hazard |
| `gh pr create` would honour the upstream ref | Ignores it, bases on the default branch |
| A `continue` command is needed after a conflict | `sync` re-entrancy removes it — one verb, fewer things for an agent to know |
| Bootstrap must assert `merge-base(parent,child) == tip(parent)` | Wrong — fails on any stale stack. merge-base *is* the fork point and is the correct bootstrap; the assertion would block most adoptions |
