# Public commit review

Nebula is public. Install the local Codex review hooks before committing from a
checkout that may contain private Apple bug bounty research:

```sh
python3 scripts/install_public_commit_guard.py
```

The installer copies `pre-commit`, `commit-msg`, and the guard into the shared Git
hooks directory. All worktrees use that copy, including checkouts that do not
yet contain this branch. It refuses to replace a different active hook. Install Codex CLI and
authenticate it before committing. Both hooks call `codex exec` in a read-only,
temporary snapshot with a structured verdict. They block when Codex reports
private research, cannot decide, fails, or does not inspect every staged text file.
The commit message is checked in the second hook. A failed review must be fixed
and retried; the hooks have no allow override.

The review prompt contains complete staged versions of changed text files, not
the unstaged working tree. Codex sends that prompt to its configured model
provider. Non-text files, submodules, and files over the review
limits (1000 KiB per file, 1 MiB total) block for separate inspection. Review the staged content with
`git diff --cached` and make a smaller text-only commit where possible. Do not
paste private research into a public issue, PR, or CI log while resolving a block.
Large accepted text files are reviewed in bounded, overlapping segments. Every
segment containing new staged text must receive a complete allow verdict; a
block, uncertainty, or missing segment blocks the commit. A segment is omitted
only when its exact text already exists in the public `HEAD` version of that path.

These are local Git hooks. `git -c core.hooksPath=/dev/null commit` and
`git commit --no-verify` can bypass them. Other clones need installation too.
Use a protected remote branch and an independent required server-side check
if commits made by an untrusted agent must be impossible to publish without
review. The local hook cannot remove content already in Git history.
