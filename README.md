# stackem

Keeps a chain of stacked git branches and their GitHub pull requests in sync — especially when
the company squash-merges.

You have a chain of branches, each branched off the last, each with a pull request targeting the
branch below it. When anything underneath one of them moves — the trunk advances, a lower branch
is amended, a PR is squash-merged — every branch above needs replaying onto the new foundation
and every PR base needs checking. `stackem sync` does that.

It does **not** create branches, write commits, open pull requests, merge anything, close
anything, delete anything, or replace CI. Those stay with git, `gh`, your editor and GitHub's
merge button.

## Run it

No install step:

```console
$ uvx stackem                 # from PyPI
$ uvx --from . stackem        # from a checkout
```

Or in a checkout: `uv run stackem`. Python 3.11+, standard library only, and it shells out to
your own `git` (2.38 or newer, for `merge-tree --write-tree`).

Forge access goes through `gh` when it is on your `PATH`, and falls back to the REST API with
`GITHUB_TOKEN` (or `GH_TOKEN`) when it is not.

## Three commands

```
stackem                         show the stack and what is stale  (READ-ONLY)
stackem sync                    make everything correct again     (re-entrant, idempotent)
stackem parent <b> --onto <p>   retarget b's pull request to p
```

`--verbose` prints every git invocation on stderr; `--dry-run` prints the plan without changing
anything. Every output ends with the literal next command, including successful ones.

There is no `init` (nothing to configure), no `continue` (sync is re-entrant: resolve the
conflict, `git add`, run `stackem sync` again) and no `abort` (`git rebase --abort`, and a
half-finished cascade heals itself on the next sync).

```console
$ stackem
main (origin/main, 14 commits behind)
  1. auth-model      #101  needs restack (trunk moved)
  2. auth-endpoints  #102  needs restack
  3. auth-ui         #103  needs restack
  4. auth-docs       --    no PR

  auth-docs has no pull request:
    gh pr create --base auth-ui --head auth-docs

next: stackem sync
```

## Two things worth knowing before you use it

**It stores nothing.** No refs, no config keys, no files, no server state. Your parent is the
pull request's base branch; the fork point is `merge-base(origin/<parent>, <branch>)`. So there
is no metadata to corrupt, nothing to migrate, and nothing to clean up if you stop using it.

**It changes no configuration.** No `push.default`, no repo settings, no setup step — you do not
need admin rights on your company's GitHub.

Anything irreversible it will not do for you: closing a pull request, deleting a branch,
reopening one that a branch deletion closed. It reports those and prints the exact command.

## Reading further

- [SPEC.md](SPEC.md) — the design, and what was verified empirically to arrive at it.
- [CLAUDE.md](CLAUDE.md) — 24 numbered invariants, each one a way to lose data if broken.
- [docs/](docs/README.md) — six worked sessions, including the squash-merge cascade, a conflict
  resolved mid-cascade, and a branch that empties. [docs/AGENT.md](docs/AGENT.md) is the block to
  paste into a repository's own `CLAUDE.md`.

## Development

```console
$ uv run pytest
```

Real git repositories in temp directories (real commits, real branches, real rebases, a real bare
repo acting as `origin`) with GitHub faked at the wire by an in-process mock server, so the HTTP
layer stays under test. `tests/test_e2e.py` drives whole cascades through the command line;
everything else tests one module. The acceptance layer against a live GitHub repository
(SPEC.md §10) is opt-in and not part of this suite.
