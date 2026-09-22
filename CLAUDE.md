# stackem

A CLI that keeps a chain of stacked branches and their GitHub pull requests in sync, especially
when the company squash-merges. It does **not** create branches, write commits, open PRs, or
merge anything — git, `gh` and GitHub's merge button already do those.

Design docs: [SPEC.md](SPEC.md). Example sessions: [docs/](docs/README.md).

## Command surface — resist growing it

```
stackem                         show the stack and what is stale
stackem sync                    make everything correct again  (re-entrant, idempotent)
stackem abort                   undo an in-progress sync, restore every branch tip
stackem parent <b> --onto <p>   fix a wrongly-inferred parent
```

The primary motivation for this tool is that the team's current stacking tool (`ghstack`) burns
enormous Claude context and corrupts git history. Every design decision serves: **ordinary git
objects, one verb, self-documenting output.** A new subcommand needs to justify itself against
that.

## Invariants

These were each verified empirically against git 2.39.5 and a live GitHub repo. They are not
guesses. Violating one causes silent data loss or destroyed pull requests.

### History rewriting

1. **Never compute the rebase base with `git merge-base` once a base ref exists.** After a squash
   merge it points below the parent's commits, so the rebase replays those commits against the
   squash commit that already contains them, and conflicts. Always use the stored
   `refs/stackem/base/<branch>`.
1b. **The one exception is bootstrapping.** For a branch with no base ref yet whose parent still
   exists and is unmerged, `base := merge-base(parent, child)` is correct — it is the fork point.
   Do **not** additionally assert it equals `tip(parent)`; on any stale stack it will not, and
   that is normal. If the parent is already merged or deleted, bootstrap from
   `refs/pull/<parent-pr>/head` instead.
1c. **`rebase --onto` silently flattens merge commits.** Hand-built branches often contain
   `git merge main`. Report and confirm before the first sync of such a branch — it rewrites
   history in a way the user did not request.
2. **Detect a merged branch by tree comparison**, not ancestry or patch-id. Squashing changes the
   patch-id, so `git cherry` and `git merge-base --is-ancestor` both report "not merged".
   `git merge-tree --write-tree <trunk> <branch>` equal to `<trunk>^{tree}` means merged. Prefer
   the GitHub PR state when available; this is the offline fallback.
3. **A dry-run rebase is not a merge test.** It conflicts rather than emptying, and wedges the
   repo.
4. Commits that *become* empty are dropped by rebase automatically; commits that were *already*
   empty are preserved. **Always surface drops** — git mentions them in output that scrolls past.
5. `refs/stackem/*` are GC roots. This is load-bearing: after a merged parent branch is deleted,
   the base ref is the only thing keeping its tip commit reachable.

### Pushing

6. **Never lease a force-push against a freshly fetched SHA.** The fetch absorbs the other
   person's commit, making the lease vacuous — verified to silently clobber a teammate. Lease
   against `refs/stackem/pushed/<branch>`, the SHA *we* last pushed.
7. Always pass `--force-if-includes` as well. Never bare `--force`.
8. **Push every changed branch in one `git push --atomic`.** Pushing sequentially leaves a window
   where a child PR displays the entire stack, already-merged commits included.
9. Nothing is pushed until the whole cascade succeeds, so an abort never leaves a half-updated
   stack on GitHub.

### GitHub

10. **Deleting a branch closes every open PR that references it as head *or* base.** A PR closed
    this way cannot be reopened while either branch is missing.
11. **Retarget child PR bases before deleting any branch.** This is the single most destructive
    ordering mistake available; get it wrong and the child PR's review history goes with it.
12. Closure is recoverable: GitHub retains commits under `refs/pull/N/head` indefinitely. Restore
    **both** the head and base branches, `PATCH /repos/{o}/{r}/pulls/N -f state=open`, then
    retarget. Comments, approvals and conversation survive.
13. **Never pass `--delete-branch` when merging.** Warn when the repo has
    `delete_branch_on_merge = true` — it closes child PRs on every merge.
14a. **Branch → pull request is not one-to-one.** A branch can carry several PRs (a closed one and
    a merged one, say). Index by head ref with precedence: open, else most recent merged, else
    closed. Verified.
14b. **Classify closed PRs before acting.** Closed + not merged + head branch *missing* means a
    branch deletion wrecked it and sync should rescue it. Closed + not merged + head branch
    *present* means a person closed it deliberately — never resurrect that one.
14c. **Git owns structure; GitHub owns PR state.** sync makes PR bases match local parents, never
    the reverse. The sole exception is first sight, where a branch with no recorded parent adopts
    its PR's base.
14. `gh pr create` ignores the upstream ref and bases new PRs on the default branch. sync corrects
    a wrong base after the fact; retargeting an *open* PR is safe and immediate.
15. Creating pull requests is out of scope — the team's PR template needs an agent to write it.
    Print the exact `gh pr create --base <parent> --head <branch>` instead.

### Environment

16. `refs/remotes/origin/HEAD` is often unset. Fall back to `git remote set-head origin -a`, then
    to the GitHub API, before assuming a trunk name.
17. Minimum git is **2.38** (`merge-tree --write-tree`). `--force-if-includes` needs 2.30.
18. If the parent pointer is stored as the upstream ref, `push.default = current` is required —
    and git's own failure message suggests `git push origin HEAD:<parent>`, which would push the
    child's content onto the parent branch. Never surface that hint.

### UX contract

19. **`stackem sync` is re-entrant and idempotent.** Running it mid-conflict with the files
    resolved continues the cascade; running it on a clean stack is a fast no-op. "If you are
    unsure of the state, run sync" must always be correct advice.
20. **Every output ends with the literal next command.** Nothing about the command surface should
    need to be recalled.
21. Snapshot every branch tip to `refs/stackem/undo/<ts>/<branch>` before mutating anything.

## Testing

Every invariant above needs an end-to-end test against real git repos in temp dirs. The GitHub
fake must reproduce the *verified* behaviors — particularly #10, #12 and squash-merge minting a
new commit — or tests will pass while reality breaks. Keep an opt-in suite that runs against a
real throwaway GitHub repo; the recipe is in SPEC.md.

Weight the timing cases heaviest: a late fix on a lower branch that is independent, that
conflicts, that duplicates higher work (commit dropped), and that empties a branch entirely.
