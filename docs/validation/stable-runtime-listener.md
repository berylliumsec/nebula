# Stable listener and API code rollback boundary

Repository implementation only; the compatibility launchers still use the legacy
factory. These fixtures never contact an existing installation or operational
provider and never deploy a release.

`RuntimeSupervisor` now owns one public ASGI listener independently of retained
API workers. It stages verified assets, prepares a bounded candidate, authenticates
its incarnation, and promotes traffic through SQL revisions. It requires an
explicit worker-memory admission adapter before launching a new candidate.
Inactive APIs may retire gracefully; accepted commands and responses stay in SQL.
Closing the supervisor leaves independent workers alive for rediscovery.

Selected checks passed on SQLite and disposable PostgreSQL:

- Ten supervisor cases cover the same real Unix listener serving API v1, then v2,
  then retained v1; candidate reuse; authenticated status; rejecting public access
  to internal RPC; survivor rediscovery; required startup admission; and two
  cancellation/cleanup ownership boundaries.
- Two composed SQL-routing/private-HTTP cases fill all 64 ordinary connections
  and verify a stop still crosses the independent control pool within two seconds.
- Fourteen API decision/revocation/steering cases pass after moving classification
  to a shared module used at every forwarding and admission hop.

All 26 cases passed in 25.90 seconds. The two rollback cases were strengthened to
commit a project edit while v2 is active and verify it remains after traffic
returns to v1; both passed in 9.94 seconds. No database snapshot restoration is
part of code rollback. The real API process reports its accepted manifest version
in the response build header. Ruff and targeted mypy checks passed.

Independent fresh-context diagnosis preceded repairs to first-hop control-pool
selection and supervisor cleanup. Cleanup now drains admitted staging/preparation
operations before releasing the host lock/database, and every close caller joins
the same shielded cleanup task. The tests hold a real release copy or proxy close
at those boundaries to verify the ordering.

Local evidence: `/tmp/nebula-stable-listener-{collection,selection,tests}.*` and
`/tmp/nebula-supervisor-rollback-*`; current diff binding is recorded in
`.github/test-selection.json`.

This is API routing/rollback evidence. The served HTML is an inert retained
fixture and worker scripts import repository libraries. Complete execution
construction, shared host services, production worker-memory admission, durable
producer streams, resource handoff, client/desktop integration, compatibility
launchers, packaging and deployer integration remain pending. No active execution
handoff, production LAN/browser matrix or packaged update/reopen claim follows.
Mobile WebKit remains unavailable because its official browser downloads are
blocked by the managed environment policy.
