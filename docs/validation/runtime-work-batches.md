# Coordinated Work import and project edits

The existing Work import and bulk project-edit APIs need a SQL coordinator when
their writes span projects or create a project. Routing them to a single
execution owner either rejected the request or lost that owner's fence while
creating the child. The independent causal report is
`/tmp/nebula-work-mutation-validation-39e70a3c.json` and was reviewed by the root
agent before this implementation.

## Contract

`RuntimeWorkBatches` accepts only `POST /api/v1/work/import` and
`PATCH /api/v1/work/projects`. Normal public authentication and an immutable
actor plus Idempotency-Key still apply. The parent uses the ordinary command
lookup, status and response endpoints, including HTTP 202 while waiting. Request
and response content stays encrypted in `RuntimeVault`.

Acceptance confirms the canonical host's batch-progress capability before
creating a new parent. An older host returns an actionable 503 without a new
parent or project barriers. Previously accepted receipts remain replayable.

The host loop creates typed queued barriers in each participant's normal work
lane. An earlier command or uncertain outcome holds the batch and later work;
the coordinator never resets an uncertain command. Older owners without
`project.work-batch/v1` receive no barrier and can continue existing work. The
durable participant projection records the requirement for future owners.

Final SQL holds the execution metadata advisory lock, global sequence lock and
sorted project ownership locks. Every write participant has an exact live lease
or a locked NULL-owner fencing counter, rechecked before commit. Parent metadata
references can be read without borrowing that parent's write authority. Cycles
and revisions retain canonical Work validation.

New projects, their default scopes and runtime policy, all Work items/check-ins,
barrier completion and final response frames commit together. A known SQL
validation failure rolls back all entity writes before publishing its failure
receipt. A lost or unknown SQL outcome stays saved for host reconciliation;
there are no external side effects to replay. Source UUID5 identities preserve
later operator edits on natural import retries.

Metadata-only counter creation explicitly sets `admission_requested=False`.
Real GET or ordinary mutation demand still starts execution normally. A used
project's monotonic counter and existing admission request are preserved.

## Focused evidence

Dependencies were root-owned shared contracts `65698baf` and `16526d7a`, locally
cherry-picked as `e34121ff` and `41d45d7a`. This slice does not edit schema,
shared route/classifier files or the tracked test selection.

The local selection is `/tmp/nebula-work-batches-selection.json`, validated with
`scripts/test_selection.py --baseline 41d45d7a --candidate WORKTREE --plan ...`.
Collection selected exactly 36 cases in one file, 18 scenarios on SQLite and
PostgreSQL. First execution: **36 passed in 23.12 seconds**.

```sh
NEBULA_TEST_POSTGRES_URL=postgresql+psycopg://nebula_test@127.0.0.1:25432/nebula_coordination \
PYTHONPATH=src /workspace/nebula/.venv/bin/pytest --collect-only -q tests/v3/test_runtime_work_batches.py

NEBULA_TEST_POSTGRES_URL=postgresql+psycopg://nebula_test@127.0.0.1:25432/nebula_coordination \
PYTHONPATH=src /workspace/nebula/.venv/bin/pytest -q --tb=short tests/v3/test_runtime_work_batches.py
```

Raw results: `/tmp/nebula-work-batches-first.txt`. Checks cover normal public
ASGI authentication, receipt lookup/response across API replacement, new-project
default scopes, natural retry edits, multi-project revisions/cycles, full late
SQL rollback, work-lane ordering, uncertainty holds, unsupported old owners,
expiry and fence transfer, NULL nonzero counters, handoff/shutdown holds,
capability refusal, actor revocation and isolation, concurrent acceptance,
overlapping batches, and a new host loop resuming with a new Database instance.
The admission test runs the real batch loop with automatic admission, observes
zero prepared workers for completed metadata, then verifies explicit GET demand
admits only its referenced project.

Runtime registrations and the private capability response are synthetic; the
host lock, SQL, public API, delivery/vault and restarted host loop are real. No
provider, harness, worker process or external service was started. Ruff and
coordinator-only mypy pass. Root owns the separately selected signed host RPC,
production LAN and browser acceptance evidence; this backend slice alone does
not claim those unrun gates.

## Blank workspace and saved invalid-plan recovery

Fresh independent review `review_work_batch_coordination` confirmed a second
fault after the original 36-case pass: the import DTO accepts and strips an
explicitly empty or whitespace workspace. A truthiness check skipped folder
validation, so the host could commit its queued barriers before Engagement
construction rejected the empty path. The uncaught domain validation error
aborted reconciliation and repeatedly held following batches.

Acceptance now rejects an explicitly blank workspace with a durable 422 before
creating participants. It never resolves an empty path against Core's current
directory; optional `None` is preserved. A legacy staged plan failing domain
construction is translated to a private typed validation failure only around
the final SQL operation. The final transaction rolls back before the host
publishes the saved 422 and completes only that batch's own barriers as failed.
Unexpected database errors, stale leases and uncertain unrelated commands keep
their existing handling.

Six added SQLite/PostgreSQL regression cases cover empty and whitespace input,
corrected optional workspace import, receipt replay, no premature participants,
and recovery of a real staged legacy barrier followed by a valid batch in the
same reconciliation call. The latter retains an unrelated uncertain command.
The original 36-case first-pass evidence above is unchanged.

The expanded selection collected exactly 42 cases and passed its first execution:
**42 passed in 26.53 seconds**. Raw results are
`/tmp/nebula-work-batches-poison-tests.txt`; collection is
`/tmp/nebula-work-batches-poison-collect.txt`. The diff-bound follow-up selection
is `/tmp/nebula-work-batches-poison-selection.json`, baseline `9116318d`.
Ruff, coordinator-only mypy and whitespace checks pass. No selected behavior
failure required a repair during this verification.
