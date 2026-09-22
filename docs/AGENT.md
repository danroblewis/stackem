# Agent instructions

Paste this into your `CLAUDE.md`. It is deliberately short — the point of stackem is that an
agent needs almost no context to use it correctly.

```markdown
## Stacked pull requests

This repo uses stacked PRs managed by `stackem`. Branches form a chain, each PR targets the
branch below it.

- `stackem` shows the stack and what is out of date.
- `stackem sync` fixes everything: rebases each branch onto its parent, retargets PR bases,
  force-pushes.
- **`stackem sync` is safe to run at any time, as many times as you like.** If you are unsure
  of the state, run it.
- If `sync` stops on a conflict: resolve the files, `git add` them, then run `stackem sync`
  again. Do not run `git rebase --continue` yourself — sync does it.
- If `sync` says a branch has no PR, it prints the exact `gh pr create --base ... ` command.
  Run it with a body written from `.github/pull_request_template.md`.
- `sync` never closes a PR, deletes a branch, or reopens a closed PR. When one of those is
  needed it prints the command and leaves it to you. Read what it says before running it.
- Do not run `git push --force` or `git push -f`. sync owns pushing.
- Do not delete a branch that has an open child PR; it closes the child PR.
- A pull request's base branch IS its parent. To change a parent, use
  `stackem parent <branch> --onto <new-parent>` rather than editing the PR by hand.

Every stackem command ends its output with the next command to run. Follow it.
```

## Why this is short on purpose

The failure mode with ghstack is that an agent must hold a non-standard model in context —
synthetic branch names, commit-message metadata, a bespoke command surface — and re-derive the
state every session. It burns tokens and it guesses wrong.

stackem inverts that:

- **Ordinary git objects.** Real branches, real PRs, no naming scheme, and no metadata of its
  own — the parent is the PR's base branch, and everything else is derived. Everything the model
  already knows about git applies directly.
- **One verb.** `stackem sync` is the whole normal loop. Re-entrant, so there is no state
  machine to track.
- **Self-documenting output.** Every message ends in `next:` with a literal command. The agent
  reads what to do instead of recalling it.
- **Idempotent.** Re-running `sync` after a partial failure is always correct, so a confused
  agent recovers by repeating itself rather than by improvising git commands.
