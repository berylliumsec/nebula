# Non-disruptive deployment foundation validation

Date: November 8, 2026. Base: `be813b3c97a7187bf61952b984600c697fbfcc01`.

Scope: the first internal SQL foundation change described in
[the rollout design](../design/non-disruptive-deployments.md). Existing public
routes, process lifetimes and desktop behavior are not integrated with these
primitives. This evidence does not establish non-disruptive deployments.

## Focused selection and results

The diff-bound selection is `.github/test-selection.json`.

```sh
.venv/bin/pytest --collect-only -q \
  tests/v3/test_runtime_coordination.py tests/v3/test_async_database.py
NEBULA_TEST_POSTGRES_URL='<disposable local database>' .venv/bin/pytest -q \
  tests/v3/test_runtime_coordination.py tests/v3/test_async_database.py
```

Collected **61**; final run **61 passed, 0 failed, 0 skipped** in 20.05 seconds.
Python 3.12.14; SQLite file-backed databases and PostgreSQL 16 in a disposable
local container. Each PostgreSQL case owns a unique schema. No real projects,
providers, harnesses or operator credentials were used. No full suite ran.

The 44 new cases exercise both databases: contested ownership, lease expiry,
heartbeat renewal, stale-owner rejection, rollback after expiry, concurrent
duplicate submissions, actor and payload conflicts, receipts after deletion,
command ordering, preservation of uncertain outcomes, atomic command/state
commit, cross-connection stream replay, replay after owner replacement, batch
idempotency and existing owner-based stream retention. The 17 existing async
database cases cover bootstrap, transaction cleanup and connection bounds.

Earlier runs exposed two test-fixture faults: a missing synthetic provider
reference and an assertion comparing a SQL projection with unchanged entity
JSON. Independent review reproduced both causes. Fixtures now use a coherent
inert turn and `StoreTransaction.update()`; expiry and atomic-write assertions
check authoritative entity content. The six affected cases passed on retry
before the final complete selected run above.

Additional checks:

- Ruff lint and formatting for all changed Python files: passed.
- Mypy for `src/nebula/v3/runtime_coordination.py`: passed.
- `poetry check`: passed with existing metadata-deprecation warnings.
- `git diff --check`: passed.
- Independent review: no remaining confirmed defect in this foundation; reviewed
  locks, clock checks, receipt uniqueness, uncertainty handling, atomic writes,
  stream sequence/retention and migration/model alignment.
- Repository diagnostic audit: passed after restoring the expected-failure
  classification lost when `_io_psi` was extracted in `cc647fe3`. Independent
  diagnosis confirmed that the audit reads source and the optional metric parser
  still correctly returns unknown for malformed data. Only the explanatory
  comment changed; runtime behavior is unchanged.

## Acceptance still outstanding

Production-bundle LAN, desktop Chromium, mobile Chromium, mobile WebKit, and
packaged desktop update/reopen were not run: there is no public entry point or
client-lifecycle change in this slice. They remain required for the later
integration and continuity claims, together with API replacement, safe handoff,
supervisor restart, resource pinning, third-version staging, authentication,
revocation, failed migrations and code rollback.

Do not enable overlapping legacy Core processes. Legacy execution does not use
these fences, and pre-0031 launchers cannot resolve the new Alembic revision.
Supervisor-owned migrations and explicit worker compatibility validation are
still required. Keep this change unmerged while a required CI gate is failing.
