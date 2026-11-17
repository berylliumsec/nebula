# Branch, validation, and pull request workflow

## Isolate the change

Start each implementation on a task-specific branch. Create a separate worktree
when the current checkout has unrelated changes or concurrent work; leave those
changes untouched. Base the branch on the intended merge target and check for
upstream changes before opening the pull request. Keep the diff limited to the
requested work.

## Validate an operator-facing change

For each new feature or fix that changes a workflow reachable through Nebula,
start a disposable Nebula Core instance bound to a non-loopback LAN address on an
available port. Give it an isolated temporary data directory and seed it only
with synthetic projects, sessions, files, and credentials. Use inert local
provider or harness fixtures where the workflow needs them. Never point this
instance at an operator's real data, a running installation, or production
services. Keep normal pairing and authentication enabled and restrict access to
the test LAN.

Exercise the changed journey through its actual entry point and visible result.
For UI changes, serve the candidate production bundle from that instance and
verify its build identity. Check the relevant success, failure, retry, refresh,
and reconnect steps with the synthetic data. Use the focused test selection and
the product-quality skill for the required test layers and browser/device
profiles. Record the LAN origin, build, fixture, exact commands, outcomes, and
any unavailable gate. Stop and remove the temporary instance and data after
capturing evidence. A mocked browser response alone does not validate the
real-Core journey.

For documentation-only or workflows that cannot run in a Nebula LAN instance,
record why this validation does not apply. Do not invent a passing LAN result.

## Pull request and merge

Review the final diff and selected-test receipt, then commit and push the task
branch. Open a pull request describing the operator journey, test selection,
LAN evidence, and remaining limitations. Check which workflows the push and PR
will trigger so they cannot broaden testing beyond the reviewed selection.

Review the PR diff and required CI results. Resolve failures and review feedback
on the branch. Merge the PR when required checks and repository rules permit it
and all applicable acceptance gates have evidence. If a required gate cannot be
run, keep the PR unmerged, report the precise missing evidence, and continue when
the gate becomes available. Do not present PR creation or merge as proof that an
untested feature works.

## Public commit review unavailable

The local public-commit guard has Codex review every staged commit. The operator
gives standing approval to bypass it when Codex review is **unavailable**: the
guard reports `Codex review failed`, `could not start or timed out`, or `invalid
review result`, and a trivial `codex exec` confirms a usage limit, an
authentication failure, or an outage. Retry once first. Never bypass a commit that
Codex reviewed and did not allow, and never bypass to commit secrets or research
material the guard exists to keep out of the public repository.

Use only the guard's tree-scoped bypass, for one commit at a time:

```sh
TREE=$(git write-tree)
REASON="Codex review unavailable: <cause, e.g. usage limit until 2026-11-21>"
NEBULA_PUBLIC_COMMIT_BYPASS_TREE=$TREE \
NEBULA_PUBLIC_COMMIT_BYPASS_REASON="$REASON" \
git commit -F <message file ending with "Public-Review-Bypass: $REASON">
```

The reason must be 10-200 printable characters on one line and match the
`Public-Review-Bypass:` trailer exactly. Set the variables only on that command,
never in a shell profile or session environment. Name the bypass and its cause in
the pull request description. Do not create guard bypass marker files or edit the
hooks.

## Core rollback snapshot retention

The legacy simulation deployer in `scripts/simulation/nebula_deployer.py`
creates a coherent rollback snapshot before it switches Core releases.
Supervisor-mode rolling promotion and code rollback preserve live data and do
not create a new coherent snapshot. After a healthy legacy promotion passes
authenticated health and serves the accepted build, retain its exact snapshot
and delete older completed snapshots under `rollback-backups`. Verify that
exactly one completed snapshot remains and that the deployer state points to
it. Record its path, removed names, and any cleanup failure in the deployment
receipt. Do not delete backups from other directories or restore a snapshot
over continuing supervisor-owned work.

Keep all state needed for a failed cutover or rollback until recovery is verified.
A cleanup failure must be reported separately; it must not roll back a healthy
Core or be described as successful retention.
