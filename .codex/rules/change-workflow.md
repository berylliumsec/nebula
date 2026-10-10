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
