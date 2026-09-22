# 01 — Building a stack by hand

*Human, no AI involved.* Splitting an auth feature into four reviewable PRs.

The point of this session: **stackem is not involved in creating any of it.** You use plain git
and `gh`. stackem only shows up once there is a chain to keep straight.

---

## Build the branches with git

```console
$ git checkout main && git pull
Already up to date.

$ git checkout -b auth-model
$ vim app/models/user.py app/models/session.py
$ git commit -am "auth: add User and Session models"
$ git push -u origin auth-model
```

Open the PR however you normally do. stackem has no opinion:

```console
$ gh pr create --base main --title "auth: models" --body-file .github/pull_request_template.md
https://github.com/acme/app/pull/101
```

Second branch, stacked on the first. This is just `git checkout -b` from where you are:

```console
$ git checkout -b auth-endpoints
$ vim app/api/auth.py
$ git commit -am "auth: add login and logout endpoints"
$ git commit -am "auth: rate-limit login attempts"
$ git push -u origin auth-endpoints
```

Here is the one place plain `gh` gets it wrong — it defaults the base to `main`, which would
show your reviewer both branches' commits:

```console
$ gh pr create --base auth-model --title "auth: endpoints" --body-file .github/pull_request_template.md
https://github.com/acme/app/pull/102
```

Pass `--base` and it is correct from the start. Forget it, and `stackem sync` fixes it later.

Two more branches the same way:

```console
$ git checkout -b auth-ui
$ git commit -am "auth: login form"
$ git push -u origin auth-ui
$ gh pr create --base auth-endpoints --title "auth: login UI" --body-file .github/pull_request_template.md
https://github.com/acme/app/pull/103

$ git checkout -b auth-docs
$ git commit -am "docs: document the auth flow"
$ git push -u origin auth-docs
```

---

## First look at the stack

You have never run stackem in this repo. It infers the chain from the branch topology:

```console
$ stackem
main (origin/main, up to date)
  1. auth-model      #101  synced
  2. auth-endpoints  #102  synced
  3. auth-ui         #103  synced
  4. auth-docs       --    no PR

  auth-docs has no pull request:
    gh pr create --base auth-ui --head auth-docs

everything else is up to date. nothing to sync.
```

It worked out that `auth-docs` sits on `auth-ui` sits on `auth-endpoints` sits on `auth-model`
sits on `main`, and recorded it. Nothing was rewritten and nothing was pushed — the stack was
already correct.

If inference gets it wrong — usually because you branched from the middle of another branch
rather than its tip — stackem says so instead of guessing:

```console
$ stackem
main (origin/main, up to date)
  1. auth-model      #101  synced
  2. auth-endpoints  #102  synced
  ?  spike-caching   --    cannot infer parent

  spike-caching branches from the middle of auth-endpoints, not its tip.
  Tell me which branch it belongs on:
    stackem parent spike-caching --onto auth-endpoints
```

---

## The day-to-day loop

Reviewer leaves comments on #101, the bottom of the stack. Fix it where it belongs:

```console
$ git checkout auth-model
$ vim app/models/user.py
$ git commit -am "auth: make email case-insensitive"
```

Now three branches above it are built on a commit that no longer exists. One command:

```console
$ stackem sync
fetching origin... done
trunk origin/main unchanged

restacking auth-endpoints onto auth-model... ok (2 commits)
restacking auth-ui onto auth-endpoints... ok (1 commit)
restacking auth-docs onto auth-ui... ok (1 commit)

verifying... all 4 commit ranges unchanged
pushing auth-model auth-endpoints auth-ui auth-docs... ok (atomic)
PR bases: #102 #103 correct, #104 none

done. 3 branches restacked, 4 pushed.
```

That is the whole workflow. Write code with git, run `stackem sync` when something underneath
you moved.

---

## What just happened underneath

Worth knowing once, then never again:

- Each branch was replayed onto its parent's new tip with `git rebase --onto`, using a stored
  record of where the parent *used to be*. Computing that with `git merge-base` would replay the
  parent's own commits and conflict.
- All four branches were pushed in a single atomic `git push`, so no reviewer ever loaded a PR
  showing the whole stack's commits.
- The force-push was leased against the SHA stackem last pushed, so if a teammate had pushed to
  one of these branches, the push would have been refused rather than silently overwriting them.

---

Next: [02 — Resolving a conflict without AI](02-human-conflict.md)
