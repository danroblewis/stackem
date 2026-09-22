# stackem — a straightforward implementation of stacked pull requests

**Status:** design spec. Every mechanism below was verified empirically against git 2.39.5
and a live GitHub repo (`danroblewis/stackem-spike`) before being written down. Findings that
contradicted the initial design are called out as **Verified** or **Correction** with the evidence.

---

## 1. The core idea

Every operation in the stacked-PR workflow is one git primitive:

```
git rebase --onto <where the parent is NOW> <where the parent WAS> <branch>
```

Restacking after a parent gains commits, after a parent is amended, and after a parent is
**squash-merged** are all the same call. Only the resolution of "where the parent is now"
changes — after a squash merge it resolves to the trunk.

There is no separate "history rewrite" code path. There is `restack`, and a squash merge is
a restack whose target moved to `main`.

The thing that makes this work is that "where the parent was" must be **remembered as a commit
SHA**, never computed.

### Verified: `merge-base` is the wrong answer

Stack `main → feature-a (2 commits) → feature-b (2 commits)`, then squash-merge `feature-a`:

```
$ git merge-base main feature-b          # -> the INITIAL commit, below feature-a
$ git rebase --onto main $MERGE_BASE feature-b
CONFLICT (add/add): Merge conflict in a.txt
error: could not apply 0cec8da... a: add a1
```

It replays `feature-a`'s commits against the squash commit that already contains them.

With the stored base:

```
$ git rebase --onto main $(git rev-parse refs/stackem/base/feature-b) feature-b
Successfully rebased and updated refs/heads/feature-b.

$ git log --oneline feature-b
2a4f0b6 b: add b2
2c37f81 b: add b1
52b9e5a Squashed feature-a (#1)
26ff509 initial
```

Clean, linear, correct. This was re-verified against a **real GitHub squash merge**, not just a
local `merge --squash`.

---

## 2. State model

All state is native git. No dotfiles, no sidecar config, nothing in the worktree.

### 2.1 Parent — the upstream tracking ref

```
branch.feature-a.merge = refs/heads/main        # stack root
branch.feature-b.merge = refs/heads/feature-a
branch.feature-c.merge = refs/heads/feature-b
```

A branch is *in a stack* when it has an upstream whose branch name differs from its own.
A branch whose upstream name equals its own name is standalone. **No marker key is needed.**

**Verified** — this makes stock git commands stack-aware for free:

```
$ git status -sb
## feat-b...origin/feat-a [ahead 2]        <- your PR's size, at a glance

$ git branch -vv
  feat-a 8f7b050 [origin/main: ahead 1] a1
* feat-b b5f6db3 [origin/feat-a: ahead 2] b2    <- the whole stack
  main   9539f5d [origin/main] init

$ git log --oneline @{u}..                 <- exactly your PR's commits
b5f6db3 b2
6235ffd b1
```

### 2.2 Base — `refs/stackem/base/<branch>`

Points at the parent's tip commit as of the last restack. A ref, not a file:
push/fetchable, inspectable with `git rev-parse`, and a **GC root**.

**Verified** — survives aggressive collection, which matters because after a parent branch is
deleted post-merge this ref is the only thing keeping its tip reachable:

```
$ git reflog expire --expire=now --all && git gc --prune=now
$ git rev-parse --verify refs/stackem/base/feat-b
036df2fcf7ad47894726b94a67dc18283a8c7059      # OK
```

Confirmed live: after `feat-a` was deleted from the remote by a merge, its old tip was still a
valid commit object solely because the base ref held it.

### 2.3 Pushed — `refs/stackem/pushed/<branch>`

The SHA we last pushed for this branch. Used as the force-push lease value. See §5 — this ref
exists because the obvious approach silently destroys other people's work.

### 2.4 Undo — `refs/stackem/undo/<timestamp>/<branch>`

Every branch tip, snapshotted before any multi-branch operation. `stackem undo` restores all of
them at once. Reflog technically covers this; recovering six branches from reflog at 11pm does
not go well.

### 2.5 In-flight operation state — `stackem.op.*` in `.git/config`

A cascade can stop mid-way on a conflict, so `stackem continue` needs to know its place.

**Verified** — `.git/config` is writable while a rebase is in progress:

```
# mid-conflict, .git/rebase-merge exists, HEAD detached
$ git config stackem.op.remaining "B C"
$ git config stackem.op.remaining
B C
```

### State summary

| Key | Meaning |
|---|---|
| `parent(B)` | branch name from B's upstream |
| `base(B)` | `refs/stackem/base/B` — parent tip at last restack |
| `pushed(B)` | `refs/stackem/pushed/B` — our last push, the lease value |
| B is in sync | `base(B) == tip(parent(B))` |
| B's PR contents | `base(B)..B` |
| restack B | `rebase --onto tip(parent(B)) base(B) B`, then rewrite `base(B)` |

---

## 3. Deleting a base branch closes child PRs — and how to undo it

When a merged PR's head branch is deleted, GitHub closes any open PR that used it as a base.
Verified live: PRs #1 (`feat-a`→`main`), #2 (`feat-b`→`feat-a`), #3 (`feat-c`→`feat-b`), then
`gh pr merge 1 --squash --delete-branch`:

```
#3 base=feat-b head=feat-c OPEN
#2 base=feat-a head=feat-b CLOSED      <- child PR closed
#1 base=main   head=feat-a MERGED
```

GitHub *can* auto-retarget instead of closing, but only with its stacked-PR behaviour enabled.
With it off — the default, and the assumption this spec targets — you get the close above.
`stackem` should not depend on either behaviour: **retargeting children before deletion is safe
under both**, so that is the rule (§4).

### Closed PRs are recoverable

A closed PR cannot be retargeted or reopened *while either of its branches is missing*:

```
$ gh pr edit 2 --base main
GraphQL: Cannot change the base branch of a closed pull request.

$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
422: state cannot be changed. The feat-b branch has been deleted.
```

Note the error names the **head** branch. Reopening requires **both** the head and base branches
to exist. Restore both and it reopens cleanly — **verified**:

```
$ git push origin refs/stackem/rescue/pr1:refs/heads/feat-a   # base
$ git push origin refs/stackem/rescue/pr2:refs/heads/feat-b   # head
$ gh api -X PATCH /repos/OWNER/REPO/pulls/2 -f state=open
  #2 state=open base=feat-a head=feat-b
  review comments intact: 1
  issue comments intact:  1

$ gh pr edit 2 --base main
  #2 base=main OPEN
```

The original commits are always retrievable because GitHub retains them under `refs/pull/N/head`
indefinitely, even after branch deletion:

```
$ git fetch origin refs/pull/1/head:refs/stackem/rescue/pr1
$ git fetch origin refs/pull/2/head:refs/stackem/rescue/pr2
```

So closure is **disruptive, not fatal**: reviewers get spurious "closed" notifications, CI state is
lost, and the branch must be restored — but review history, approvals and inline comments survive.

### Consequences for the design

1. **Never pass `--delete-branch` when merging.**
2. **Warn loudly if `delete_branch_on_merge = true`** — it closes every child PR on every merge.
   Check with `gh api /repos/{owner}/{repo} -q .delete_branch_on_merge`.
3. **Retarget every child PR off a branch before deleting it** (§4).
4. **Ship `stackem rescue`** (§13) to repair stacks damaged before adoption, or by a teammate
   clicking "Delete branch" in the web UI.

## 4. The merge cascade — verified correct ordering

Proven end-to-end live. `feat-b` merges while `feat-c` is stacked on it:

```
1. gh pr merge <parent-pr> --squash          # NO --delete-branch
2. gh pr edit <child-pr> --base main         # retarget while parent branch STILL EXISTS
3. git fetch origin
4. git rebase --onto origin/main $(base child) child
   git update-ref refs/stackem/base/child origin/main
   git branch --set-upstream-to=origin/main child
5. git push --force-with-lease=child:$(pushed child) --force-if-includes origin child
6. git push origin --delete <parent-branch>  # only now
```

Result:

```
--- step 5: verify PR3 survived intact ---
  #3 base=main OPEN
     c1d0b98 c: first commit
  review comments: 1

--- step 6: NOW it is safe to delete feat-b ---
  deleted feat-b
  #3 base=main OPEN <- still alive

=== FINAL: history is linear and correct ===
   c1d0b98 c: first commit
   f3323bf PR4: feat-b (#4)
   f857fef PR1: feat-a (#1)
   d6c65f9 initial
```

Step 2 before step 6 is the whole ballgame. Reverse them and you get the dead PR from §3.

### Correction: detecting a squash merge

The initial design proposed "dry-run the restack; if zero commits remain, it merged."
**This is wrong** — it conflicts instead of emptying:

```
$ git rebase --onto main $BASE_A probe
CONFLICT — f.txt needs merge
=> NOT merged     # false negative, and it wedges the repo
```

**Use tree comparison instead.** If merging the branch into the trunk yields exactly the trunk's
own tree, the branch's content is fully contained:

```
$ git merge-tree --write-tree main feat-a
31c2a0c78f5555387c6923950b34f42ccc0ab900
$ git rev-parse main^{tree}
31c2a0c78f5555387c6923950b34f42ccc0ab900     # identical => MERGED
```

**Verified** to still work after unrelated commits land on main, and **verified** to correctly
report *not merged* for a genuinely unmerged branch. Requires git ≥ 2.38.

Detection order: ask the GitHub API whether the PR is `merged` (authoritative); fall back to the
`merge-tree` test offline. `git cherry` does **not** work — squashing changes the patch-id:

```
$ git cherry main feat-a
+ 0cec8da   # "+" = not in main, despite being merged
+ 58627ab
```

---

## 5. Force-pushing without destroying your teammate's work

**Correction — this is a trap.** The obvious pattern is actively dangerous. Leasing against a
freshly fetched SHA makes the lease vacuous, because the fetch already absorbed the other
person's commit:

```
=== 1. lease against FRESHLY FETCHED sha (the anti-pattern) ===
 + e8d9639...37ddad0 feat -> feat (forced update)
>>> teammate work survived? 0 (0 = CLOBBERED)
```

Leasing against **our own recorded push-point** correctly refuses:

```
=== 2. lease against OUR RECORDED pushed-ref (correct) ===
 ! [rejected]  feat -> feat (stale info)
>>> teammate work survived? 1 (1 = PROTECTED)

=== 3. --force-with-lease --force-if-includes ===
hint: to integrate those changes locally (e.g., 'git pull ...') before forcing an update.
>>> teammate work survived? 1 (1 = PROTECTED)
```

**Rule:** always
`git push --force-with-lease=<branch>:$(git rev-parse refs/stackem/pushed/<branch>) --force-if-includes`.
Never lease against a value obtained from a fetch in the same operation. Never bare `--force`.

### Verified: atomic multi-ref push closes the garbage-diff window

Between pushing a rewritten parent and pushing its rewritten child, the child's PR displays the
entire stack — including already-merged commits:

```
=== PR3 after its base was force-pushed, before feat-c was restacked ===
   d17254b a: first commit     <- already merged
   b62898a a: second commit    <- already merged
   0e1717d b: first commit
   1bb6346 b: second commit
   46a65fe c: first commit
```

Restack the whole stack locally, then push every branch in **one atomic invocation**:

```
$ git push --atomic \
    --force-with-lease=feat-b:$PUSHED_B \
    --force-with-lease=feat-c:$PUSHED_C \
    --force-if-includes \
    origin feat-b feat-c
 + 476d9b0...e349d99 feat-b -> feat-b (forced update)
 + e2e3e8a...aff2586 feat-c -> feat-c (forced update)
```

PR3 immediately showed the correct single commit. No window.

### Verified: review comments survive a rewrite

```
review comments still present: 1
   body=review note on c.txt... line=1 original_line=1
```

Line anchors resolved (`line` non-null), so the comment was not even marked outdated.

---

## 6. Ergonomics of upstream-as-parent

Setting upstream to the parent breaks stock `git push`/`git pull`. Both are fixed by two local
config settings written by `stackem init`.

### Verified: the push failure, and its dangerous hint

```
$ git push                                  # push.default=simple (the default)
fatal: The upstream branch of your current branch does not match
the name of your current branch.  To push to the upstream branch
on the remote, use

    git push origin HEAD:feat-a
```

**That suggestion would push `feat-b`'s content onto the `feat-a` branch.** Following git's own
advice here corrupts the parent branch. `stackem doctor` should warn about this explicitly.

Fixed:

```
$ git config push.default current
$ git push
   b5f6db3..036df2f  feat-b -> feat-b          # correct branch
remote feat-a untouched: 8f7b050 a1            # parent safe
```

### Verified: `git pull` becomes `restack`

```
$ git config branch.feat-b.rebase true
$ git pull            # after parent gained a commit
Successfully rebased and updated refs/heads/feat-b.
$ git log --oneline feat-b
4cf2d1b b3
0cf8ee0 b2
243c294 b1
d0ed39e a2      <- parent's new commit
8f7b050 a1
```

Plain `git pull` on a stacked branch *is* the restack operation. This is the strongest argument
for the upstream-as-parent design.

---

## 7. Equivalence verification

`git range-diff` compares two versions of a patch series and is the right tool for
"make sure the changes are equivalent":

```
$ git range-diff $OLD_BASE..$OLD_TIP main..$NEW_TIP
1:  740ac2b = 1:  2c37f81 b: add b1
2:  5617521 = 2:  2a4f0b6 b: add b2
```

`=` means the patch is byte-identical. Under `--verify`, `stackem restack` proceeds silently only
when every commit is `=` or was cleanly dropped as empty; anything else stops and shows the delta.

### Verified: becomes-empty commits are dropped automatically

```
feat before: 2 commits
$ git rebase main feat
feat after rebase onto main: 1 commit(s)
   1f6189a feat: real work          # the duplicate was dropped
```

Note the asymmetry: commits that *become* empty are dropped; commits that were *already* empty
(`--allow-empty`) are preserved. Both behaviors are correct here.

---

## 8. Command surface

```
# setup
stackem init                    # set push.default=current, branch.*.rebase=true, detect trunk
                                #   from origin/HEAD, warn if delete_branch_on_merge is true

# adoption (§12) — none of these require starting over
stackem adopt                   # infer the stack from existing hand-made branches
stackem adopt --from-prs        # infer it from existing PR bases (more reliable)
stackem split [<branch>]        # one branch, N commits -> N stacked branches (no rewrite)
stackem track / untrack         # adopt one branch / fully remove stackem state

# everyday
stackem create <name>           # branch from HEAD, set parent, write base ref, push
                                #   immediately so the branch exists before any PR needs it
stackem sync [--dry-run]        # THE daily command: fetch, detect merges, retarget,
                                #   restack, verify, atomic-push, prune
stackem ls                      # stack as a tree, flagging branches needing restack
stackem up / down / top / bottom
stackem status                  # during a stopped cascade: where it broke, what remains

# changing the stack
stackem restack [--all] [--verify]
stackem continue / abort        # resume a cascade after conflict resolution
stackem absorb                  # route an edit to the branch that owns those lines (§14.3)
stackem insert <name>           # new branch mid-stack, descendants restacked onto it
stackem move --onto <branch>    # reparent a subtree
stackem fold                    # squash a branch into its parent
stackem parent [<b>] [--set <p>]

# shipping
stackem submit [--draft]        # restack all, verify, atomic push, create/update PRs
stackem land [--all]            # merge bottom-up with the §4 ordering

# recovery
stackem undo                    # restore all branch tips from the last snapshot
stackem rescue [<pr>...]        # reopen PRs closed by branch deletion (§13)
stackem doctor                  # check invariants, repair drifted refs
```

**`stackem parent --set` must rewrite the base ref too.** Changing the upstream alone leaves the
tool remembering the *old* parent's position, and the next restack replays the wrong commit
range. It is a two-field write — this is the "fix it if I made it wrong" subcommand, and getting
it wrong is silent and destructive.

---

## 9. Implementation notes

- **Go, shelling out to the `git` binary.** Not libgit2, not go-git — neither implements rebase,
  and rebase-with-conflict-resolution is the entire product. Single static binary, `brew install`
  works, no runtime.
- **Minimum git 2.38** — `merge-tree --write-tree` (§4). `--force-if-includes` needs 2.30.
  Apple git 2.39.5 (macOS default) is sufficient. `stackem init` should assert this.
- **GitHub access via `gh` when on PATH** (auth already solved), falling back to `GITHUB_TOKEN`
  + REST. Note `gh` is not universally installed — the fallback is not optional.
- **Require a clean worktree** for any restack, or auto-stash and restore.

---

## 10. Known limitations

- **Upstream-as-parent is non-standard and load-bearing.** IDE git panels, `gh pr create`'s base
  guess, and teammates' muscle memory will read that field and be wrong. The two config settings
  fix the CLI; they do not fix a GUI.
- **`.git/config` metadata is per-clone.** Lose the clone, lose the structure — though it is
  reconstructible from PR bases via the API, which `stackem doctor` should support. Pushing
  `refs/stackem/*` also covers this.
- **`delete_branch_on_merge = true` closes child PRs** on every merge (§3). Recoverable via
  `stackem rescue`, but disruptive enough that `stackem init` should warn.
- **A collaborator pushing to a branch in your stack** is caught by the lease and refused. There
  is no way to make that pleasant.
- **Merge queues** produce a different post-merge shape again; the `--onto` primitive handles it,
  but merge detection needs a queue-aware path.
- **Fork-based PRs** (head repo != base repo) are not addressed by this spec.

---

## 11. Parent storage — three options

Upstream-as-parent (§2.1, §6) is the **recommended** default: it is the only option that makes
stock `git status`, `git branch -vv`, `git log @{u}..` and `git pull` stack-aware for free. §10
lists its costs as *known limitations*, not as a reason to avoid it.

If those costs are unacceptable — a team that cannot tolerate `push.default` changes, or heavy
GUI use — `stackem` should support `--parent-store` with these alternatives. All three were
verified.

### Option A — upstream tracking ref (default)

```
branch.feature-b.merge = refs/heads/feature-a
branch.feature-b.remote = origin
```

- ✅ Stock git commands become stack-aware (§6)
- ✅ `git pull` becomes `restack`
- ⚠️ Requires `push.default = current`; stock `git push` otherwise fails with a **dangerous hint**
- ⚠️ GUIs misread the field

### Option B — dedicated config key

```
branch.feature-b.stackemParent = feature-a
```

- ✅ Upstream stays conventional (`origin/feature-b`), so `git push`/`git pull`/GUIs are untouched
- ✅ **Verified:** survives `git branch -m` of the child — git migrates the whole
  `branch.<name>.*` section automatically
- ✅ Enumerable: `git config --get-regexp '^branch\..*\.stackemParent$'`
- ⚠️ **Implementation note:** git config keys are case-insensitive and read back lowercased
  (`branch.feat-b.stackemparent`). Match case-insensitively.
- ❌ No free git integration — `git status` tells you nothing about the stack

### Option C — symbolic refs

```
git symbolic-ref refs/stackem/parent/feature-b refs/heads/feature-a
```

- ✅ **Verified:** stores a *name*, survives `gc --prune=now`, enumerable via
  `git for-each-ref refs/stackem/parent --format='%(refname:short) -> %(symref:short)'`
- ✅ Dereferences to the parent's tip commit for free: `git rev-parse refs/stackem/parent/feature-b`
- ✅ Lives outside `branch.*`, so nothing can collide with it
- ⚠️ **Verified:** does **not** follow a parent rename — becomes
  `warning: ignoring dangling symref`. (Options A and B go stale in the same situation.)

### Recommendation

Default to **A**. Offer **B** as `--parent-store=config` for teams that cannot change
`push.default`. **C** is elegant but buys nothing over B in practice. Whichever is chosen, the
`base`, `pushed` and `undo` refs (§2.2–2.4) are unchanged — only the parent pointer differs.

---

## 12. Adopting work that already exists

Nobody starts in `stackem`. Every adoption path below must work on repos and PRs created with
plain git and the GitHub web UI. **All were verified.**

### 12.1 `stackem adopt` — a stack of branches you already made by hand

Parent inference by nearest-ancestor. For each branch, the candidate whose tip is an ancestor and
which is fewest commits away:

```
parent(feat-a) = main     (2 commits ahead)
parent(feat-b) = feat-a   (1 commits ahead)
parent(feat-c) = feat-b   (2 commits ahead)
```

Setting the base ref is safe here, because on a healthy (not-yet-squash-merged) stack
`merge-base(parent, child) == tip(parent)` — **verified for every pair**:

```
merge-base(main,feat-a)  =1a88e01  tip(main)  =1a88e01  YES
merge-base(feat-a,feat-b)=73f9f97  tip(feat-a)=73f9f97  YES
merge-base(feat-b,feat-c)=83a2697  tip(feat-b)=83a2697  YES
```

So: `base(child) := merge-base(parent, child)`, and **assert it equals the parent's tip**. If it
does not, the branches have diverged — stop and ask rather than guess. This is the one moment
`merge-base` is trustworthy; after a squash merge it is not (§1).

`stackem adopt` should print the inferred stack and require confirmation before writing anything.

### 12.2 `stackem adopt --from-prs` — PRs you already opened

When PRs exist, **GitHub's PR bases already are the stack** — `head`/`base` pairs form the parent
graph directly. No inference needed:

```
gh pr list --json number,headRefName,baseRefName
  #3 head=feat-c base=feat-b
  #2 head=feat-b base=feat-a
  #1 head=feat-a base=main
```

This is strictly more reliable than topology inference and should be preferred whenever the PRs
exist. It also recovers a stack into a fresh clone, which answers the "`.git/config` is
per-clone" limitation in §10.

### 12.3 `stackem split` — a ghstack-style branch where each commit is a PR

**Verified: this requires no history rewriting at all.** Branches are created *pointing at the
existing commits*, so SHAs are unchanged and the operation is instant:

```
source (ghstack-style: each commit = a PR):
   7d6ebd6 feat: part three
   0d94472 feat: part two
   1411939 feat: part one

   created split/1-feat-part-one  -> parent=main
   created split/2-feat-part-two  -> parent=split/1-feat-part-one
   created split/3-feat-part-three-> parent=split/2-feat-part-two

   split/1-feat-part-one   base=1168535  commits=1
   split/2-feat-part-two   base=1411939  commits=1
   split/3-feat-part-three base=0d94472  commits=1
```

Each branch carries exactly one commit beyond its base, and the top branch still has the full
linear series. Branch names are slugged from commit subjects, overridable interactively.

**It is perfectly reversible** — folding the stack back yields a byte-identical SHA:

```
rejoined == ghstyle? YES, identical sha
```

That reversibility is worth preserving as `stackem fold`: it is the escape hatch that makes
adoption low-risk. If someone dislikes the split, they get their exact original branch back.

`stackem split` is also the answer for the much more common case: a developer who has been
working in one branch for two days and realises it should have been three PRs.

---

## 13. `stackem rescue` — repairing damaged stacks

For stacks broken before adoption, or by someone clicking "Delete branch" in the web UI. Verified
end-to-end in §3.

```
stackem rescue [<pr-number>...]
```

1. Find closed-but-not-merged PRs whose head or base branch is missing.
2. Recover their commits from `refs/pull/N/head` (GitHub retains these indefinitely):
   `git fetch origin refs/pull/N/head:refs/stackem/rescue/prN`
3. Restore **both** the head and base branches — reopening fails if *either* is missing.
4. `PATCH /repos/{owner}/{repo}/pulls/N -f state=open`
5. Retarget to the correct parent, restack, atomic-push.
6. Delete the temporary base branch only after retargeting (§4).

Review comments, approvals and conversation survive all of this.

---

## 14. Ease-of-use principles

### 14.1 Plain git must keep working

The design constraint that matters most for adoption. After any `stackem` command the repo must
be in a state where someone who has never heard of the tool can `git checkout`, `git commit`,
`git push`, and open a PR in the web UI without anything breaking. Concretely:

- Branches are ordinary branches. PRs are ordinary PRs. No `gh/user/1/head` naming schemes.
- No command is *required*. `stackem` accelerates the workflow; it never becomes the only way in.
- A teammate without `stackem` installed can review, comment, and merge normally.
- `stackem untrack` fully removes the tool's state, leaving plain branches behind.

This is the main thing to preserve versus tools that take the repo hostage.

### 14.2 One command for the daily loop

`stackem sync` should be the only command most people run most days: fetch, detect merges,
retarget, restack the whole stack, verify, atomic-push, prune. It must be **idempotent and
cheap** — when `base(B) == tip(parent(B))` for every branch, it does nothing and says so quickly,
so running it constantly is free and safe.

### 14.3 `stackem absorb` — the highest-value convenience

The most common annoyance in a stack: you are at the top, and a reviewer asks for a change that
belongs three branches down. The manual dance is checkout, amend, restack everything.

Instead: make the edit where you are and run `stackem absorb`. Use `git blame` against the stack
to find which branch and commit own the touched lines, amend there, and restack automatically.
`git absorb` already does this within a branch; extending it across a stack is the natural move
and removes the single biggest source of friction.

### 14.4 Tell reviewers what they are looking at

Maintain a delimited block in each PR body, rewritten on every sync:

```markdown
<!-- stackem:begin -->
**Stack** (this PR is #2 of 3)
- #1 feat-a — ✅ merged
- **#2 feat-b — you are here**
- #3 feat-c — open
<!-- stackem:end -->
```

Strictly inside markers so a human's own description is never touched. When a PR is retargeted,
post a one-line comment explaining why — otherwise reviewers see the base change and assume
something broke.

Because `base` is the parent branch, GitHub already scopes each PR's diff to just that branch's
commits — verified in §5. The stack table supplies the context GitHub does not.

### 14.5 Never surprise, never lose work

- **Dry-run first.** `stackem sync --dry-run` prints the plan: what restacks, what retargets,
  what force-pushes, what gets deleted. Consider making `--dry-run` the default for `sync` until
  the user opts out.
- **Refuse dirty worktrees** unless `--autostash`.
- **`stackem undo`** restores every branch tip from the pre-operation snapshot (§2.4).
- **Never bare `--force`** (§5).
- **`stackem status`** during a stopped cascade says exactly which branch conflicted, what remains
  queued, and the two commands that continue or abort.

### 14.6 Structural edits people actually need

- `stackem insert <name>` — new branch in the middle, descendants restacked onto it
- `stackem fold` — squash a branch into its parent, closing the PR (§12.3; also the split escape hatch)
- `stackem move --onto <branch>` — reparent a subtree
- `stackem split` — one branch into several (§12.3)
- `stackem parent --set` — fix a wrong parent; **must rewrite the base ref too** (§8)

### 14.7 Landing

`stackem land` merges the bottom PR and runs the §4 cascade. `stackem land --all` walks the whole
stack bottom-up, refusing to proceed past a PR with failing checks or unresolved reviews. Never
`--delete-branch`; prune explicitly after retargeting.

---

## Appendix: what testing changed

| Initial assumption | Verified reality |
|---|---|
| GitHub auto-retargets child PRs on base deletion | Closes them (unless GitHub's stacked-PR behaviour is on); recoverable by restoring **both** branches then `PATCH state=open` |
| Detect merge by dry-run rebase yielding 0 commits | Conflicts instead; use `merge-tree` tree comparison |
| Lease with `--force-with-lease=b:$(freshly fetched)` | Vacuous — clobbers teammate work; lease against recorded push-point |
| Push branches sequentially | Leaves a window where child PRs show the whole stack; use `--atomic` |
| Two refs of state (base) | Three (base, pushed, undo) plus `stackem.op.*` |

---
