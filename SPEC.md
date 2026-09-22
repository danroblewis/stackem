# stackem — keeping a chain of stacked pull requests in sync

**Status:** design spec. Every mechanism below was verified empirically against git 2.39.5 and a
live GitHub repository before being written down. Claims marked **Verified** were tested; claims
marked **Correction** replace something an earlier draft got wrong.

---

## 1. Scope

You have a chain of branches, each branched off the last, each with a pull request targeting the
branch below it. When anything underneath a branch moves — trunk advances, a lower branch is
amended, a PR is squash-merged — every branch above needs its history replayed onto the new
foundation and every PR base needs checking.

stackem does that. It does **not** create branches, write commits, open pull requests, merge
anything, or replace CI. Those belong to git, `gh`, your editor and GitHub's merge button.

### Constraints that shaped the design

**It stores nothing.** No refs, no config keys, no files, no server state. Everything is derived
from git topology and the pull requests themselves (§3).

**It changes no configuration.** No `push.default`, no repo settings, no `init` step. A user
without admin rights on their company's GitHub or git setup can run it as-is.

**It is built for agents.** The team's current tool (`ghstack`) forces every Claude Code session
to re-derive repository state from a non-standard model, burns context, and corrupts history when
the agent improvises. stackem uses ordinary git objects, exposes one verb, ends every message with
the literal next command, and is idempotent so recovery means repeating one safe command.

---

## 2. Core idea

Every operation is one primitive:

```
git rebase --onto <where the parent is NOW> <where the parent WAS> <branch>
```

Restacking after a parent gains commits, after a parent is amended, and after a parent is
**squash-merged** are all the same call. Only "where the parent is now" changes — after a squash
merge it resolves to the trunk. There is no separate history-rewrite path.

The third argument is the fork point: it defines *which commits are this branch's own*. Get it
wrong and you either replay the parent's commits (conflicts) or lose your own.

### Verified: the fork point is derivable from the branch name

`merge-base(origin/<parent>, <branch>)` — the last commit the branch and its parent's
**last-synced state** have in common:

```
STEP 1  amend the bottom branch    -> B, C rebased correctly
STEP 2  new commit mid-stack       -> C rebased correctly
STEP 3  trunk moves                -> whole stack rebased
        each branch owns exactly: A=1 B=2 C=1
```

**Correction.** An earlier draft stored the fork point in `refs/stackem/base/<branch>`, justified
by a test showing `merge-base` failing after an amend. That test used the *local, already-amended*
parent. Against `origin/<parent>` — the parent as it was when the stack was last in sync — the
derivation is correct, and no metadata is needed.

### Verified: the one case it fails is detectable

The derivation assumes the parent is never force-pushed without restacking its children in the
same operation. sync always honors that; a human running `git push -f` does not:

```
STEP 4  force-push a parent without syncing children
        guard C=VIOLATED   <- detected BEFORE any rebase is attempted
```

The guard is `git merge-base --is-ancestor origin/<parent> <branch>`. When it fails, sync stops
and says so rather than conflicting on someone else's commit.

---

## 3. State: none

| What sync needs | Where it comes from |
|---|---|
| the trunk | `origin/HEAD`, else `git remote set-head origin -a`, else the API |
| which branches exist | `git for-each-ref refs/heads` |
| B's parent | B's pull request's base branch; topology inference when B has no PR |
| the fork point | `merge-base(origin/<parent>, B)` |
| is a rebase safe | `merge-base --is-ancestor origin/<parent> B` |
| is a push safe | `--force-with-lease --force-if-includes` |
| is B merged | the PR's state; `merge-tree` offline (§6.4) |
| cascade position | re-derived every run |

### Verified: pushing safely needs no stored push-point

An earlier draft kept `refs/stackem/pushed/<branch>` as a lease value. Bare flags do the job in
both directions:

```
CASE 1  legitimate post-rebase force-push  -> PUSH SUCCEEDED
CASE 2  teammate pushed first              -> MATE WORK survived? 1 (protected)
```

`--force-if-includes` checks the remote-tracking tip is reachable from the local reflog, which a
rebase preserves and a teammate's unseen commit does not. Never use bare `--force`.

### The upstream ref is left alone

**Correction.** An earlier draft stored the parent in the upstream tracking ref. Two verified
facts killed it: `gh pr create` ignores the upstream and bases new PRs on the default branch, so
the integration that motivated the choice does not exist; and `git push -u` overwrites the field,
silently destroying the parent record. Storing the parent in a field git actively manages was the
mistake. The PR's base branch is where the parent already lives.

---

## 4. Commands

```
stackem                         show the stack and what is stale
stackem sync                    make everything correct again  (re-entrant, idempotent)
stackem abort                   abort an in-progress restack and restore branch tips
stackem parent <b> --onto <p>   retarget b's pull request to p
```

`sync` is the only one needed day to day. There is no `continue`: sync detects an in-progress
restack and resumes it (§8). There is no `init`: nothing needs configuring.

`stackem parent` retargets the pull request, because the PR base *is* the parent record. It is
not a separate local write that could drift.

**`stackem` is read-only.** It derives and displays; it never writes, pushes, or infers-and-records.
(An earlier draft had it recording inferred parents, which made a display command mutate state.)

---

## 5. How sync works

### 5.1 Orientation

> **The pull request's base branch is the parent.** sync does not reconcile a local record against
> it — when a parent merges, sync retargets the child's PR, and that *is* the reparenting.

For a branch with no PR yet, the parent is inferred by nearest ancestor and used for that run
only. Nothing is recorded.

**Every parent in one git call:**

```console
$ git for-each-ref refs/heads --format='%(refname:short) %(objectname:short)'
```

**Pull requests:** query open PRs for the stack's branches, and query merged/closed state per
branch as needed. **Do not** use `gh pr list --state all --limit 100` as the only query — in an
active repo the 100-row window fills with closed PRs and the open PR for your branch falls
outside it, so sync reports "no PR" and prints a `gh pr create` that would duplicate it.

**Exclude fork pull requests.** **Verified** that `isCrossRepository` and `headRepositoryOwner`
are available. A fork PR whose head ref collides with a local branch name (`main`, `patch-1`, any
shared feature name) would otherwise be indexed as that branch's PR and retargeted.

**Branch → PR is not one-to-one.** **Verified**: one branch had two PRs, one closed and one
merged. Index by head ref with precedence **open > most recent merged > closed**.

#### The stack boundary

**Correction.** An earlier draft defined the stack as "the connected component containing HEAD:
walk down through upstreams to the trunk, and up by finding branches whose parent is in the set."
Since the trunk is in the set, walking up collected **every branch rooted on trunk** — scratch
branches, WIP, unrelated features — and sync would rebase and force-push all of them.

The rule:

1. **Spine:** walk from HEAD down through parents to the trunk.
2. **Members:** the spine, minus the trunk.
3. **Descendants:** transitively, branches whose parent is a member.

The trunk is never a member, so its other children are never collected. A sibling stack rooted at
trunk shares no member and is excluded. A sibling *within* the stack — two branches off the same
parent — is correctly included.

#### Classifying branches

- **live** — has an open PR, or no PR
- **merged** — its PR is merged
- **orphaned** — its PR is closed, not merged, and its head branch is missing from the remote

**Verified** that deleting a branch closes every open PR referencing it as head *or* base, and
that such a PR cannot be reopened while either branch is missing.

**Correction.** An earlier draft auto-rescued orphaned PRs. Two problems. First, a false positive:
GitHub offers a "Delete branch" button on closed PRs, so "a person closed it *and* deleted the
branch" is common and indistinguishable from wreckage — auto-rescue would resurrect deliberately
abandoned work. Second, an infinite flip-flop: step 9 closed an emptied branch's PR and deleted
the branch, and the next sync classified that as orphaned and restored it, forever.

**sync never rescues, closes, or deletes on its own.** It reports and prints the commands (§6.3).
This also makes "running sync on a clean stack is a fast no-op" true, which it was not before.

### 5.2 The cascade

sync runs in three phases. **All local work completes and verifies before anything touches the
remote.**

```
PHASE 0 — resume
  read .git/rebase-merge/{head-name,onto,orig-head}
  the restack is OURS iff head-name is a member of the derived stack
                       AND onto == tip(its derived parent)
    not ours          -> refuse; tell the user to finish their own rebase
    ours, unresolved  -> reprint the conflict report, exit
    ours, resolved    -> git rebase --continue, then continue below

PHASE 1 — local, reversible
  1. git fetch --prune origin
  2. resolve trunk
  3. derive the stack (§5.1), classify each branch
  4. reparent: for each branch whose parent is merged, hoist to the nearest UNMERGED
     ancestor, repeating to a fixpoint
  5. snapshot every branch tip in memory
  6. walk members bottom-up, SKIPPING merged branches:
       guard: merge-base --is-ancestor origin/<parent> <branch>   else report and stop
       fork   = merge-base(origin/<parent>, branch)
       target = origin/<trunk> if the parent is the trunk, else tip(parent)
       if the branch is already based on target: skip
       git rebase --onto target fork branch
         conflict -> report and exit, leaving git's normal rebase state
       note dropped commits; note whether the branch emptied
  7. verify: range-diff each branch's old range against its new one
       anything beyond "= " or a clean drop -> report and STOP before the remote

PHASE 2 — remote, ordered, irreversible
  8. retarget PR bases to match derived parents — children of merged and of emptied
     branches first, and always before any deletion
  9. git push --atomic --force-with-lease --force-if-includes  (all changed branches)
       rejected -> restore local tips from the snapshot and exit; nothing was pushed
 10. report: restacked, pushed, dropped commits, emptied branches, merged branches,
     orphaned PRs, branches with no PR — each with the exact command to act on it
```

**Step 6 skips merged branches.** A merged branch's commits are already in the trunk, so replaying
them either drops them all — making sync mistake a merged branch for an emptied one — or conflicts
against the squash commit and halts the cascade permanently.

**Step 4 is transitive.** When two PRs land the same morning, a child's parent may itself have a
merged parent. Hoisting one level would leave a branch parented to a merged branch that is about
to be deleted, and deleting it closes that child's PR. Repeat to a fixpoint.

**Phase 2 is honest about atomicity.** Step 8 retargets PRs and step 9 pushes; a rejection at 9
leaves PR bases updated but content unpushed. That is recoverable — rerun sync — because
retargeting is idempotent and no branch was deleted. Deletion and closing are never automatic
(§5.1), which is what keeps this phase recoverable at all.

---

## 6. Verified mechanics

### 6.1 Equivalence verification

```console
$ git range-diff $OLD_FORK..$OLD_TIP $NEW_FORK..$NEW_TIP
1:  740ac2b = 1:  2c37f81 b: add b1
2:  5617521 = 2:  2a4f0b6 b: add b2
```

`=` means a byte-identical patch. Each branch is compared against **its own** new fork point, not
against the trunk. Anything other than `=` or a clean drop stops sync before phase 2.

### 6.2 Pushing

```console
$ git push --atomic --force-with-lease --force-if-includes origin <changed branches>
```

**Atomic.** **Verified** that pushing sequentially leaves a window where a child's PR displays the
entire stack, already-merged commits included, and that `--atomic` rolls the whole push back on a
single lease failure.

**Verified** that review comments survive a force-push with their line anchors resolved. Comments
anchored to a hunk that the rebase actually changes will still be marked outdated by GitHub —
that is GitHub's normal behavior, not something stackem can prevent.

### 6.3 Branch deletion closes pull requests

**Verified**, for head *and* base:

```
$ gh pr merge 1 --squash --delete-branch
#2 base=feat-a head=feat-b CLOSED      <- child PR closed

$ git push origin --delete feat-b      # head branch of open PR #2
#2 base=main head=feat-b CLOSED
```

Reopening requires **both** branches to exist:

```
$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
422: state cannot be changed. The feat-b branch has been deleted.
```

**Recovery is possible but manual.** GitHub retains commits under `refs/pull/N/head`
indefinitely. **Verified** end to end:

```console
$ git fetch origin refs/pull/1/head:rescue-base refs/pull/2/head:rescue-head
$ git push origin rescue-base:refs/heads/feat-a rescue-head:refs/heads/feat-b
$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
  #2 state=open base=feat-a head=feat-b   (review comments intact: 1)
$ gh pr edit 2 --base main
```

sync prints these commands for each orphaned PR rather than running them (§5.1). Never pass
`--delete-branch` when merging; warn when the repo has `delete_branch_on_merge = true`, since it
orphans a child PR on every merge.

### 6.4 Merge detection

Prefer the PR state. Offline, compare trees — if merging the branch into the trunk yields exactly
the trunk's own tree, the branch is contained:

```console
$ git merge-tree --write-tree main feat-a
31c2a0c78f5555387c6923950b34f42ccc0ab900
$ git rev-parse main^{tree}
31c2a0c78f5555387c6923950b34f42ccc0ab900     # identical
```

**Correction — the test needs a guard.** **Verified** that it reports *any* branch with no unique
content as merged, including a brand-new branch that is merely behind trunk:

```
behind: unique-commits=0  merge-tree==trunk-tree? yes
real:   unique-commits=1  merge-tree==trunk-tree? no
```

Require `git rev-list --count <trunk>..<branch>` to be greater than zero first. Without it, sync
would classify a fresh WIP branch as merged and report it for deletion.

`git cherry` does not work — squashing changes the patch-id. A dry-run rebase does not work
either; it conflicts rather than emptying, and wedges the repo.

### 6.5 Pull request creation is out of scope

**Verified** that `gh pr create --fill` ignores the upstream ref and bases new PRs on the default
branch. Retargeting an open PR afterwards is safe and immediate.

The team's PR template needs an agent to write it, so sync never creates PRs. It prints the exact
command with the base filled in:

```
auth-docs  --  no PR   ->  gh pr create --base auth-ui --head auth-docs
```

### 6.6 Trunk detection

**Verified** that `refs/remotes/origin/HEAD` can be unset. Fall back to
`git remote set-head origin -a`, then the API.

Always rebase roots onto `origin/<trunk>`, never local `<trunk>` — sync does not fast-forward the
local trunk, so using it would silently rebase the stack onto a stale base.

---

## 7. The timing problem

You are working at the top of the stack and a reviewer asks for a change three branches down. The
new commit is authored *after* everything above it.

### 7.1 Ordering is a non-issue

**Verified.** Rebase refreshes committer dates on everything above, so `git log` reads correctly:

```console
$ git log B4 --oneline
0b19d7c B4: feature four
94161bb B3: feature three
2f35e50 B2: address review feedback
b64eff5 B2: feature two
```

Author dates go non-monotonic, which default git output never shows.

### 7.2 Dropping is the real hazard

If the change duplicates work already done higher up, replaying the higher branch produces nothing:

```
Rebasing (1/2) dropping 45cef55 B4: hotfix f() returns 2 -- patch contents already upstream
```

Git says so in output that scrolls past, and after a conflict resolved to an empty diff it says
nothing at all. **Drop detection must be structural** — compare commit counts across the
range-diff in step 7 — never parsed from rebase output.

In the limit a branch empties completely. **Verified**:

```
C3 commits remaining: 0
C3 == C2? YES — branch is now EMPTY, its PR has no commits
```

A PR with zero commits cannot be merged. sync **retargets its children's PRs to its parent**
(step 8), removing it from the chain, then **reports it** with the commands to close the PR and
delete the branch. It does not close or delete on its own — that is what created the flip-flop
described in §5.1.

A branch that loses only *some* commits stays in the stack, with the loss reported.

---

## 8. Conflicts and re-entrancy

sync stops and leaves the repo in a normal `git rebase` state — ordinary markers, ordinary
`git status`.

```
$ stackem sync
restacking auth-ui onto auth-endpoints... CONFLICT

CONFLICT in auth-ui
  applying   3f2a1bc  auth: handle rate limits in the login form
  onto       auth-endpoints (9c4e1a2)
  files      app/api/client.py

Resolve the conflicts, `git add` them, then run `stackem sync` again.
To undo the restack: stackem abort

still queued after this: auth-docs
```

Resolution is standard git. Then run **`stackem sync` again** — it detects the in-progress
restack, continues it, and resumes the cascade. One verb means an agent needs to know one thing,
and "if you are unsure of the state, run `stackem sync`" is correct in all three states.

**sync must not hijack a rebase it did not start.** **Verified** that `.git/rebase-merge` exposes
enough to tell:

```
head-name = refs/heads/B
onto      = e52e6df   (= tip of A)
orig-head = d03de93
```

The restack is stackem's iff `head-name` is a member of the derived stack **and** `onto` equals
the tip of that branch's derived parent. Otherwise sync refuses and tells the user to finish their
own rebase. Without this check, running sync during an unrelated `git rebase -i` would continue
the user's rebase and then cascade on top of it.

`stackem abort` aborts the restack and restores the tips it changed. It refuses when no restack of
its own is in progress.

---

## 9. Agent ergonomics

See [docs/AGENT.md](docs/AGENT.md) for the `CLAUDE.md` block and [docs/](docs/README.md) for
worked sessions. Output is compact text, not JSON — fewer tokens, and the model parses it fine.

```
$ stackem
main (origin/main, 14 commits behind)
  1. auth-model      #101  needs restack (trunk moved)
  2. auth-endpoints  #102  needs restack
  3. auth-ui         #103  needs restack
  4. auth-docs       --    no PR

next: stackem sync
```

Every message ends with the literal next command — including successful ones, where it is the
most useful next action or an explicit "nothing to do." Drops, emptied branches, merged branches
and orphaned PRs are reported twice, inline and in the summary, so they reach the user instead of
scrolling past.

---

## 10. Testing

End-to-end against real git repositories in temp directories, plus a fake forge implementing the
Provider interface (§11). **The fake must reproduce the verified behaviors** — deleting a branch
closing PRs referencing it as head or base, refusing reopen while either branch is missing,
squash-merge minting a commit with a different patch-id — or the suite passes while reality
breaks. Keep an opt-in suite against a real throwaway repository.

| Area | Cases |
|---|---|
| **Fork-point derivation** | amend a lower branch · new commit mid-stack · trunk moves · parent force-pushed outside sync → guard fires before any rebase |
| **Timing** | late fix on a lower branch: independent · conflicting · duplicate → dropped · branch fully emptied |
| Squash merge | cascade · step 6 skips the merged branch · two PRs merged the same run → transitive hoist |
| Branch deletion | children's PRs closed → reported, not auto-rescued · rescue commands are correct when run |
| Empty-branch removal | children retargeted; branch NOT auto-closed or deleted; rerunning sync is a no-op |
| Stack boundary | unrelated branches rooted on trunk excluded · sibling within the stack included |
| Merge detection | squash-merged → merged · behind-trunk with no unique commits → NOT merged |
| PR indexing | two PRs on one branch → precedence · fork PR with a colliding head ref → excluded · open PR outside a 100-row window → still found |
| Concurrency | teammate pushes → lease refuses, no data loss |
| Conflicts | sync resumes at the right branch · refuses to continue a rebase it did not start · abort restores tips · a rejected push leaves nothing pushed |
| Environment | `origin/HEAD` unset · git < 2.38 · `delete_branch_on_merge = true` |

---

## 11. Implementation notes

- **Go, shelling out to the `git` binary.** Not libgit2 or go-git — neither implements rebase, and
  rebase-with-conflict-resolution is the entire product. Single static binary, no runtime.
- **Minimum git 2.38** for `merge-tree --write-tree`; `--force-if-includes` needs 2.30. Assert on
  first run.
- **Forge access behind a Provider interface.** GitHub first; Gitea and Codeberg speak
  substantially the same API shape, GitLab's merge-request model differs enough to need real
  adaptation. The interface covers what sync uses: list open PRs, look up a branch's PR state,
  retarget a base, and read the data needed to classify a closed PR. It deliberately excludes PR
  creation (§6.5).
- **Require a clean worktree** for any restack, or auto-stash and restore.

---

## 12. Known limitations

- **A parent force-pushed outside sync** cannot have its children's fork points derived. sync
  detects this and stops; recovery means finding the old tip in `git reflog <parent>` — local-only
  and subject to expiry.
- **Parent resolution needs the forge.** Offline, sync falls back to topology inference, which is
  correct for a healthy stack but cannot tell that a parent was merged.
- **`delete_branch_on_merge = true`** orphans a child PR on every merge. Recoverable, but every
  merge then needs manual rescue.
- **A collaborator pushing to a branch in your stack** is refused by the lease. There is no way to
  make that pleasant.
- **Merge queues** produce a different post-merge shape; the `--onto` primitive handles it, but
  merge detection needs a queue-aware path.
- **Fork-based PRs are excluded, not supported.**
- **Trees are supported by the walk**, but the display is a flat chain and there is no cycle
  detection on `stackem parent`. Both need work before branching stacks are usable.

---

## Appendix: what testing changed

| Initial assumption | Verified reality |
|---|---|
| The parent belongs in the upstream tracking ref | `gh pr create` ignores it, and `git push -u` overwrites it. The PR's base branch is the parent |
| The fork point must be stored as a commit | Derivable as `merge-base(origin/<parent>, <branch>)`; storing it was unnecessary |
| A stored push-point is needed for a safe lease | Bare `--force-with-lease --force-if-includes` blocks a clobber and permits a legitimate rebase |
| GitHub auto-retargets child PRs on base deletion | Closes them, unless its stacked-PR behaviour is on; recoverable by restoring **both** branches then `PATCH state=open` |
| Only the *base* branch mattered for reopening | The **head** branch closes the PR too, and blocks reopening |
| Orphaned PRs should be rescued automatically | False positives (person closed *and* deleted) plus an infinite flip-flop with empty-branch removal. Report, never act |
| Detect merge by dry-run rebase yielding 0 commits | Conflicts instead; use `merge-tree`, guarded by a unique-commit count |
| `merge-tree` alone identifies a merged branch | Reports any branch with no unique commits as merged, including fresh WIP branches |
| The stack is HEAD's connected component | Collects every branch rooted on trunk; the trunk must be excluded from the member set |
| Bootstrap must assert `merge-base == tip(parent)` | Fails on any stale stack; merge-base *is* the fork point |
| Push branches sequentially | Leaves a window where child PRs show the whole stack; use `--atomic` |
| Out-of-order commit timestamps confuse history | Non-issue; silent commit *dropping* is the real hazard, and must be detected structurally |
| A `continue` command is needed after a conflict | `sync` re-entrancy removes it — but it must verify the rebase is its own |
