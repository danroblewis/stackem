"""Real git repositories in temp directories, for every stackem test.

SPEC.md sec 10: unit and integration tests run against real repositories; only
the forge is faked.  Nothing here mocks git.

A sandbox is a bare repo acting as ``origin`` plus a clone you work in::

    def test_restacking_after_the_trunk_moves(stacked_sandbox):
        sb = stacked_sandbox                       # main + feat-a/b/c, all pushed
        sb.advance_trunk(1)                        # someone lands on origin/main
        onto = sb.sha("origin/main")
        fork = sb.git.merge_base("origin/main", "feat-a")
        sb.git.rebase_onto(onto, fork, "feat-a")
        assert sb.shape("feat-a") == {"feat-a": ["feat-a: c1"]}

Building history
    ``commit``, ``create_branch``, ``checkout``, ``make_stack``, ``amend``,
    ``write``

Moving the world underneath you
    ``advance_trunk`` (trunk gains commits on origin), ``squash_merge`` (a real
    squash: a NEW commit on the trunk sharing no history with the branch),
    ``delete_remote_branch``, ``teammate_commit`` (someone else pushes to a
    branch in your stack), ``set_pull_ref`` (GitHub's ``refs/pull/N/head``),
    ``unset_origin_head``

Asserting shape
    ``shape`` (branch -> its own commit subjects, oldest first),
    ``assert_stacked``, ``subjects``, ``unique_subjects``, ``tips``,
    ``remote_tips``, ``sha``, ``origin_sha``, ``patch_id``, ``describe``

All subject lists read oldest-first, the order you read a stack bottom-up.
Commits are deterministic: two sandboxes built the same way have the same shas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from stackem.gitx import Git

__all__ = ["GIT_ENV", "Sandbox", "make_sandbox"]

#: Identity and isolation for every git invocation a test makes.  The developer's
#: own ~/.gitconfig must never reach a test repository.
GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "stackem tests",
    "GIT_AUTHOR_EMAIL": "tests@stackem.invalid",
    "GIT_COMMITTER_NAME": "stackem tests",
    "GIT_COMMITTER_EMAIL": "tests@stackem.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: Fixed clock, so repeated runs produce identical object ids.
_EPOCH = 1735689600  # 2025-01-01T00:00:00Z
_STEP = 60


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", text)).strip("-").lower()


@dataclass
class Sandbox:
    """One origin + one clone, both real git repositories."""

    root: Path
    origin: Path  # the bare repo acting as origin
    path: Path  # the clone, with a working tree
    trunk: str
    env: dict[str, str]
    git: Git  # bound to the clone
    origin_git: Git  # bound to the bare origin
    _clock: int = field(default=0, repr=False)
    _teammate: Path | None = field(default=None, repr=False)

    # -- clock ------------------------------------------------------------

    def _tick(self, git: Git) -> None:
        stamp = f"{_EPOCH + self._clock * _STEP} +0000"
        self._clock += 1
        git.env["GIT_AUTHOR_DATE"] = stamp
        git.env["GIT_COMMITTER_DATE"] = stamp

    # -- building history --------------------------------------------------

    def write(self, relpath: str, content: str) -> Path:
        """Write a file in the clone's working tree without committing it."""
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def commit(
        self,
        subject: str,
        *,
        files: dict[str, str] | None = None,
        branch: str | None = None,
        amend: bool = False,
    ) -> str:
        """Commit ``files`` (default: one file named after ``subject``).

        With ``branch=``, commits there and puts HEAD back where it was.
        """
        previous = self.git.current_branch()
        if branch is not None and branch != previous:
            self.checkout(branch)
        payload = files if files is not None else {f"{_slug(subject)}.txt": subject + "\n"}
        for relpath, content in payload.items():
            self.write(relpath, content)
            self.git.run("add", "--", relpath)
        self._tick(self.git)
        args = ["commit", "-m", subject]
        if amend:
            args.append("--amend")
        if not payload:
            args.append("--allow-empty")
        self.git.run(*args)
        sha = self.git.rev_parse("HEAD")
        if branch is not None and previous is not None and branch != previous:
            self.checkout(previous)
        return sha

    def create_branch(self, name: str, *, start: str | None = None) -> str:
        """``git checkout -b name [start]`` in the clone."""
        args = ["checkout", "-b", name]
        if start:
            args.append(start)
        self.git.run(*args)
        return self.sha(name)

    def checkout(self, name: str) -> None:
        self.git.run("checkout", name)

    def make_stack(
        self,
        names: list[str],
        *,
        commits: int = 1,
        base: str | None = None,
        push: bool = True,
    ) -> list[str]:
        """Build a chain of branches, each on top of the last, and push them.

        Commit subjects are ``"<branch>: c<n>"``; HEAD is left on the top branch,
        which is where you work in a real stack.
        """
        self.checkout(base or self.trunk)
        for name in names:
            self.create_branch(name)
            for index in range(1, commits + 1):
                subject = f"{name}: c{index}"
                self.commit(subject, files={f"{name}-{index}.txt": subject + "\n"})
        if push:
            self.push(*names)
        return list(names)

    def amend(
        self,
        branch: str,
        *,
        subject: str | None = None,
        content: str | None = None,
    ) -> str:
        """Rewrite the tip of ``branch`` in place; HEAD goes back where it was."""
        previous = self.git.current_branch()
        self.checkout(branch)
        if content is not None:
            relpath = f"{branch}-amended.txt"
            self.write(relpath, content)
            self.git.run("add", "--", relpath)
        self._tick(self.git)
        args = ["commit", "--amend", "--no-edit"]
        if subject is not None:
            args = ["commit", "--amend", "-m", subject]
        self.git.run(*args)
        sha = self.git.rev_parse("HEAD")
        if previous is not None and previous != branch:
            self.checkout(previous)
        return sha

    # -- moving the world underneath you ----------------------------------

    def advance_trunk(
        self, count: int = 1, *, subjects: list[str] | None = None, fetch: bool = True
    ) -> list[str]:
        """Land ``count`` commits on the trunk ON ORIGIN.

        The local trunk is deliberately left behind: CLAUDE.md invariant 4 says
        sync rebases roots onto ``origin/<trunk>``, never the local trunk, and a
        harness that fast-forwarded the local trunk would hide that bug.
        """
        titles = subjects or [f"trunk: t{i + 1}" for i in range(count)]
        shas = [
            self.teammate_commit(self.trunk, subject=title, fetch=False)
            for title in titles
        ]
        if fetch:
            self.fetch()
        return shas

    def teammate_commit(
        self,
        branch: str,
        *,
        subject: str = "teammate work",
        files: dict[str, str] | None = None,
        fetch: bool = False,
    ) -> str:
        """Someone else commits to ``branch`` and pushes, without telling us.

        We do NOT fetch by default, which is what makes the lease checks in
        CLAUDE.md invariant 11 meaningful.
        """
        work = self._teammate_clone()
        mate = Git(work, env=self.env)
        mate.fetch("origin", prune=True)
        if mate.try_rev_parse(f"refs/remotes/origin/{branch}"):
            mate.run("checkout", "-B", branch, f"origin/{branch}")
        else:
            mate.run("checkout", "-B", branch)
        payload = files if files is not None else {f"{_slug(subject)}.txt": subject + "\n"}
        for relpath, content in payload.items():
            target = work / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            mate.run("add", "--", relpath)
        self._tick(mate)
        mate.run("commit", "-m", subject)
        mate.run("push", "origin", branch)
        sha = mate.rev_parse("HEAD")
        if fetch:
            self.fetch()
        return sha

    def squash_merge(
        self,
        branch: str,
        *,
        into: str | None = None,
        delete_branch: bool = False,
        message: str | None = None,
        fetch: bool = True,
    ) -> str:
        """Squash-merge ``branch`` into ``into`` ON ORIGIN, GitHub-style.

        SPEC.md sec 6.4: the result is a NEW commit on the base whose only parent
        is the base -- the branch's own commits are nowhere in the trunk's
        history and their patch-ids do not survive.  Done with plumbing in the
        bare repo, so no working tree is involved.
        """
        base = into or self.trunk
        merged = self.origin_git.merge_tree_write_tree(
            f"refs/heads/{base}", f"refs/heads/{branch}"
        )
        if merged.conflicted or merged.tree is None:
            raise AssertionError(f"squash merge of {branch} into {base} conflicts")
        self._tick(self.origin_git)
        subject = message or f"{branch}: squashed"
        new_sha = self.origin_git.out(
            "commit-tree", merged.tree, "-p", f"refs/heads/{base}", "-m", subject
        )
        self.origin_git.run("update-ref", f"refs/heads/{base}", new_sha)
        if delete_branch:
            self.delete_remote_branch(branch)
        if fetch:
            self.fetch()
        return new_sha

    def delete_remote_branch(self, name: str) -> None:
        """Delete a branch on origin, leaving the local branch and the local
        remote-tracking ref alone (a fetch --prune removes the latter)."""
        self.origin_git.run("update-ref", "-d", f"refs/heads/{name}")

    def set_pull_ref(self, number: int, rev: str) -> str:
        """Create ``refs/pull/<number>/head`` on origin (SPEC.md sec 6.3)."""
        sha = self.git.rev_parse(rev)
        self.origin_git.run("update-ref", f"refs/pull/{number}/head", sha)
        return sha

    def unset_origin_head(self) -> None:
        """SPEC.md sec 6.6: refs/remotes/origin/HEAD can be unset."""
        self.git.run("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    def push(self, *branches: str, force: bool = False) -> None:
        if force:
            proc = self.git.push("origin", list(branches), check=True)
        else:
            proc = self.git.run("push", "origin", *branches)
        assert proc.returncode == 0

    def fetch(self) -> None:
        self.git.fetch("origin", prune=True)

    # -- asserting shape ---------------------------------------------------

    def sha(self, rev: str) -> str:
        return self.git.rev_parse(rev)

    def origin_sha(self, branch: str) -> str | None:
        return self.origin_git.try_rev_parse(f"refs/heads/{branch}")

    def tips(self) -> dict[str, str]:
        return self.git.branches()

    def remote_tips(self) -> dict[str, str]:
        return self.origin_git.branches("refs/heads")

    def subjects(self, rev_range: str) -> list[str]:
        """Commit subjects for a branch or a range, OLDEST FIRST."""
        return self.git.lines("log", "--reverse", "--format=%s", rev_range)

    def unique_subjects(self, branch: str, base: str) -> list[str]:
        return self.subjects(f"{base}..{branch}")

    def shape(self, *branches: str, base: str | None = None) -> dict[str, list[str]]:
        """``{branch: its own commit subjects}`` down a chain.

        Each branch is measured against the one below it, so this is the stack as
        a reviewer sees it: only the commits that branch owns.
        """
        below = base or self.trunk
        result: dict[str, list[str]] = {}
        for name in branches:
            result[name] = self.subjects(f"{below}..{name}")
            below = name
        return result

    def patch_id(self, rev: str) -> str:
        """The patch-id of one commit (SPEC.md sec 6.4: squashing changes it)."""
        patch = self.git.run("diff-tree", "-p", "--no-commit-id", "--root", rev).stdout
        out = self.git.run("patch-id", "--stable", input=patch).stdout.strip()
        return out.split()[0] if out else ""

    def describe(self, *branches: str, base: str | None = None) -> str:
        """A readable picture of the stack, for assertion messages."""
        below = base or self.trunk
        lines = [f"{below} ({self.sha(below)[:7]})"]
        for name in branches:
            tip = self.git.try_rev_parse(name)
            if tip is None:
                lines.append(f"  {name}: MISSING")
                continue
            own = self.subjects(f"{below}..{name}")
            based = self.git.is_ancestor(below, name)
            marker = "" if based else "   <- NOT based on the branch below"
            lines.append(f"  {name} ({tip[:7]}){marker}")
            lines.extend(f"      {subject}" for subject in own)
            below = name
        return "\n".join(lines)

    def assert_stacked(self, *branches: str, base: str | None = None) -> None:
        """Assert each branch is based on the one below it."""
        below = base or self.trunk
        for name in branches:
            if not self.git.is_ancestor(below, name):
                raise AssertionError(
                    f"{name} is not based on {below}\n" + self.describe(*branches, base=base)
                )
            below = name

    # -- internals ---------------------------------------------------------

    def _teammate_clone(self) -> Path:
        if self._teammate is None:
            work = self.root / "teammate"
            Git(self.root, env=self.env).run("clone", "--quiet", str(self.origin), str(work))
            self._teammate = work
        return self._teammate


def make_sandbox(root: str | Path, *, trunk: str = "main") -> Sandbox:
    """Create a bare origin and a clone with one commit on ``trunk``, pushed."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    origin = root / "origin.git"
    path = root / "repo"
    env = dict(GIT_ENV)

    bootstrap = Git(root, env=env)
    bootstrap.run("init", "--quiet", "--bare", f"--initial-branch={trunk}", str(origin))
    bootstrap.run("init", "--quiet", f"--initial-branch={trunk}", str(path))

    sandbox = Sandbox(
        root=root,
        origin=origin,
        path=path,
        trunk=trunk,
        env=env,
        git=Git(path, env=env),
        origin_git=Git(origin, env=env),
    )
    sandbox.git.run("remote", "add", "origin", str(origin))
    sandbox.commit("root", files={"root.txt": "root\n"})
    sandbox.git.run("push", "--quiet", "--set-upstream", "origin", trunk)
    sandbox.git.remote_set_head("origin")
    sandbox.fetch()
    return sandbox
