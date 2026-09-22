# stackem docs

## The mental model

You have a chain of branches, each one branched off the last, each with its own pull request
targeting the branch below it. When anything underneath a branch moves — trunk advances, a
lower branch is amended, a PR gets squash-merged — every branch above it needs its history
replayed onto the new foundation, and every PR needs its base checked.

stackem does exactly that and nothing else.

It does not create branches, write commits, open pull requests, or merge anything. Those are
git, your editor, `gh`, and GitHub's merge button. stackem keeps the chain correct.

## The whole command surface

```
stackem                         show the stack and what is out of date
stackem sync                    make everything correct again  (re-run anytime)
stackem abort                   undo an in-progress sync, restore every branch tip
stackem parent <b> --onto <p>   fix a wrongly-inferred parent
```

`sync` is the only one you need day to day. It is **re-entrant**: if it stops on a conflict,
you resolve the files and run `stackem sync` again. There is no separate "continue" command to
remember.

## Example sessions

Human sessions — the tool has to work when no AI is available:

- [01 — Building a stack by hand](sessions/01-human-first-stack.md)
- [02 — Resolving a conflict without AI](sessions/02-human-conflict.md)

Claude Code sessions — the default way this gets used:

- [03 — Routine sync after trunk moves](sessions/03-claude-routine-sync.md)
- [04 — Resolving a rebase conflict mid-cascade](sessions/04-claude-conflict.md)
- [05 — The squash-merge cascade](sessions/05-claude-squash-merge.md)
- [06 — A branch goes empty](sessions/06-empty-branch.md)

Setup for agents:

- [AGENT.md](AGENT.md) — the block to paste into your `CLAUDE.md`

## The running example

Every session below uses the same four-branch stack, an auth feature split for review:

| branch | PR | what it is |
|---|---|---|
| `auth-model` | #101 | User and Session models |
| `auth-endpoints` | #102 | login/logout API routes |
| `auth-ui` | #103 | login form |
| `auth-docs` | #104 | docs for the above |
