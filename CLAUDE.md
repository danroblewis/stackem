# stackem

A CLI that keeps a chain of stacked branches and their pull requests in sync, especially when the
company squash-merges. It does **not** create branches, write commits, open PRs, or merge
anything.

Design: [SPEC.md](SPEC.md). Example sessions: [docs/](docs/README.md).

## Two constraints that decide most arguments

**stackem stores nothing.** No refs, no config keys, no files. The parent is the pull request's
base branch; the fork point is `merge-base(origin/<parent>, <branch>)`. If you find yourself
wanting to persist something, that is a design smell — check §3 of the spec first, because an
earlier draft stored three things and all three turned out to be derivable.

**stackem changes no configuration.** No `push.default`, no repo settings, no `init`. The user
may not have admin rights on their company's GitHub.

## Command surface — resist growing it

```
stackem                         show the stack (READ-ONLY — never writes)
stackem sync                    make everything correct again (re-entrant, idempotent)
stackem parent <b> --onto <p>   retarget b's pull request to p
```

Three commands. No `init` (nothing to configure), no `continue` (sync is re-entrant), no `abort`
(`git rebase --abort`, and a half-finished cascade self-heals — invariant 24).

The tool exists because `ghstack` burns Claude context and corrupts history. Every decision serves
**ordinary git objects, one verb, self-documenting output.** A new subcommand must justify itself
against that.

## Invariants

Each verified empirically against git 2.39.5 and a live GitHub repo. Violating one causes silent
data loss or destroyed pull requests.

### Deriving state

1. **The PR's base branch is the parent.** Never store a parent. `gh pr create` ignores the
   upstream ref, and `git push -u` overwrites it — storing a parent in a field git manages was a
   verified mistake.
2. **The fork point is `merge-base(origin/<parent>, <branch>)`** — the parent's *last-synced*
   state, not its local tip. Against the local tip the derivation fails after an amend.
3. **Check "already based on target" BEFORE the guard.** `merge-base --is-ancestor <target>
   <branch>` → skip, needing no fork point. Running the guard first reports a violation on
   branches left correctly restacked by an earlier cascade that stopped on a conflict.
3b. **Guard every restack it does not skip, with `merge-base --is-ancestor origin/<parent>
   <branch>`.** False means the parent was force-pushed without restacking its children; stop and
   report rather than rebasing, which would conflict on the parent's own commit.
4. **Rebase roots onto `origin/<trunk>`, never local `<trunk>`.** sync does not fast-forward the
   local trunk.
5. **`stackem` (no args) is read-only.** It must not record inferences or fetch-and-write.

### The cascade

6. **Skip merged branches in the walk.** Replaying one either drops all its commits — so sync
   mistakes it for an emptied branch — or conflicts against the squash commit and halts forever.
7. **Reparenting is transitive.** Hoist to the nearest *unmerged* ancestor, repeating to a
   fixpoint. Two PRs merging in one run otherwise leaves a branch parented to a doomed branch,
   and deleting that branch closes the child's PR.
8. **The stack excludes the trunk from its member set.** Walking "up" from the trunk collects
   every branch in the repo. Spine = HEAD down to trunk; members = spine minus trunk; then
   descendants of members.
9. **All local work completes and verifies before anything touches the remote.** Restack, then
   range-diff, then push. Stop before phase 2 on any unexpected change.
10. **Detect dropped commits structurally**, by comparing ranges — never by parsing rebase output.
    A conflict resolved to an empty diff prints nothing at all.

### Pushing

11. **`git push --atomic --force-with-lease --force-if-includes`.** Bare flags — no stored
    push-point needed. Verified to block a teammate clobber and permit a legitimate post-rebase
    push. Never bare `--force`.
12. **Atomic across all changed branches.** Sequential pushes leave a window where a child's PR
    shows the whole stack.

### Forge

13. **Deleting a branch closes every open PR referencing it as head *or* base**, and such a PR
    cannot be reopened while either branch is missing.
14. **Never close, delete, or rescue automatically.** Report and print the command. Auto-rescue
    has a false positive (a person can close a PR *and* delete its branch) and forms an infinite
    flip-flop with empty-branch removal.
15. **Retarget children's PR bases before anything is deleted** — including before the user runs
    a delete command stackem printed.
16. **Branch → PR is not one-to-one.** Index by head ref with precedence open > most recent
    merged > closed.
17. **Exclude fork PRs** (`isCrossRepository`). A fork PR with a colliding head ref would
    otherwise be indexed as a local branch's PR and retargeted.
18. **Do not rely on `gh pr list --state all --limit 100`.** In an active repo the window fills
    with closed PRs and the stack's open PRs fall outside it.
19. **Merge detection needs a unique-commit guard.** `merge-tree` reports *any* branch with no
    unique commits as merged, including a fresh branch merely behind trunk. Require
    `rev-list --count <trunk>..<branch> > 0` first. `git cherry` does not work — squashing changes
    the patch-id.
20. **PR creation is out of scope.** Print `gh pr create --base <parent> --head <branch>`; the
    team's template needs an agent to write the body.

### UX contract

21. **`stackem sync` is re-entrant and idempotent.** "If you are unsure of the state, run sync"
    must always be correct.
22. **sync must not continue a rebase it did not start.** Read `.git/rebase-merge/head-name` and
    `onto`; it is ours only if head-name is a stack member and `onto` is its parent's tip.
    Otherwise refuse — a user mid-`git rebase -i` would otherwise have their rebase continued and
    cascaded on top of.
23. **Every output ends with the literal next command**, including successful runs.
24. **No `abort` command.** A cascade that stops partway leaves the branches below the conflict
    correctly restacked; the next sync skips them and retries only the failed branch. Backing out
    is `git rebase --abort`. Nothing is pushed during the local phase, so there is nothing on the
    remote to unwind.

## Testing

Every invariant needs an end-to-end test against real git repos in temp dirs. The forge fake must
reproduce the verified behaviors — #13 especially — or the suite passes while reality breaks.
Weight fork-point derivation and the timing cases (late fix on a lower branch: independent,
conflicting, duplicate-dropped, fully emptied) heaviest. See SPEC.md §10 for the matrix.
