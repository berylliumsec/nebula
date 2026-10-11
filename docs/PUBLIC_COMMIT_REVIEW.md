# Public commit review

Nebula is public. Install the local Codex review hooks before committing from a
checkout that may contain private Apple bug bounty research:

```sh
python3 scripts/install_public_commit_guard.py
```

The installer copies `pre-commit`, `commit-msg`, and the guard into the shared Git
hooks directory. All worktrees use that copy, including checkouts that do not
yet contain this branch. It refuses to replace a different active hook. To update
the known earlier Nebula guard after upgrading the checkout, run
`python3 scripts/install_public_commit_guard.py --upgrade`. The installer preserves
any local configuration and refuses to overwrite an unfamiliar hook. Install Codex CLI and
authenticate it before committing. For included content, the hooks call `codex exec` in a read-only,
temporary snapshot with a structured verdict. They block when Codex reports
private research, cannot decide, fails, or does not inspect every staged text file.
The commit message is checked in the second hook. A failed review must be fixed
and retried unless the repository owner explicitly authorizes the per-commit
bypass below.

The local configuration lives at `<git-common-dir>/hooks/nebula-public-commit-review.json`.
It is created on first install and is shared by all worktrees, but is not committed.
It accepts these keys:

```json
{
  "exclude_globs": ["*.css"],
  "max_files": 80,
  "max_file_bytes": 1024000,
  "max_total_bytes": 1048576
}
```

`exclude_globs` matches repository-relative paths without case sensitivity. CSS
files are excluded from content review and size limits by default; adding or
removing patterns changes that scope. Excluded deletions are also omitted. The
commit message is still reviewed when no included files remain. Unknown keys or
invalid values block the commit so configuration mistakes are visible. Inspect
excluded files yourself before publishing; the hook makes no claim about their
content.

For one explicitly authorized commit, bind the bypass to the exact staged Git
tree and record the reason in the commit message:

```sh
review_tree=$(git write-tree)
review_reason="Repository owner approved this staged tree after manual review"
NEBULA_PUBLIC_COMMIT_BYPASS_TREE="$review_tree" \
NEBULA_PUBLIC_COMMIT_BYPASS_REASON="$review_reason" \
git commit -m "Describe the change" -m "Public-Review-Bypass: $review_reason"
```

The two environment values apply to that command only. Both hooks reject a tree
or reason mismatch; the `commit-msg` hook also requires the exact trailer. This
bypasses the Codex review and its file and size checks. It does not prove who
authorized the bypass, so the person running the commit must obtain explicit
owner approval and retain the reason. A different staged tree needs new approval.
Keep the reason free of private details because the trailer becomes public history.

The review prompt contains complete staged versions of included changed text
files, not the unstaged working tree. Codex sends that prompt to its configured
model provider. Included non-text files, submodules, and files over the configured
review limits block for separate inspection. Review the staged content with
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
