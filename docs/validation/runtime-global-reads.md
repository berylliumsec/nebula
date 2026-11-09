# Contextless global reads

`mount_global_reads(app, store, actor)` mounts SQL-only guide progress and the
Storage dashboard beside the global Settings routes. It does not construct an
execution kernel, controller, workspace or synthetic global project.

Guide progress uses the extracted `guide_progress_router`; the original guide
router reuses it and keeps workspace inspection/starter-file routes separate.
Progress remains keyed by the active operator profile, or `local` before one is
created, so paired devices share that operator's progress. Existing optimistic
revisions, expected-operator checks, first completion timestamps and explicit
reset behavior are preserved. Writes join the same global SQL transaction as
their durable response. Reads page at 1,000 records maximum and advertise the
next offset in `X-Nebula-Next-Offset`.

Storage reads project saved sources, devices, volumes, incidents, actions and
dispatches. Source-token hashes and dispatch prompts are excluded. Freshness is
derived from saved report timestamps and source intervals; missing source
configuration cannot establish freshness. Queue status comes directly from SQL
and must match the source's project before any item detail is returned. The read
never dispatches, retries, resolves credentials or contacts a storage sender.

Each Storage catalog page contains at most 1,000 records and the whole response
is limited to 8 MiB. A complete ordinary first read keeps the existing six-array
shape. A partial page advertises `partial`, `next_offset`, `next_offsets`, `limit`
and response headers. Oversized snapshots explicitly request a smaller page.
Storage now shows the partial state, offers a 44 px **Load more storage reports**
control, preserves reports after a read failure, and discards a late page after
refresh. An expanded snapshot stays available until explicit refresh rather
than being silently collapsed by polling. No page read approves or retries work.

SSH discovery remains a host read even with `resolve=false`: it scans the Core
host's config and Includes and detects the SSH executable. Desktop browser mode
also reflects host-owned state. Those two exact routes belong to the host
Settings bridge and are deliberately absent from this SQL mount.

Focused selection and final results are recorded in
`/tmp/nebula-global-reads-selection-receipt.json`,
`/tmp/nebula-global-reads-tests.log`, `/tmp/nebula-global-reads-tests.xml`,
`/tmp/nebula-global-reads-repair-tests.log`,
`/tmp/nebula-global-reads-ui-tests.json`, and
`/tmp/nebula-global-reads-build.log`. Twenty distinct backend cases passed across
SQLite and PostgreSQL (12 on the initial run; the eight storage cases passed
after an independently validated missing-title fixture repair). Six focused UI
cases, the production build, Ruff and targeted mypy also passed. The fixture
repair changed no endpoint behavior or assertions.

Integrated production/LAN and desktop/mobile Chromium/WebKit browser acceptance
remain separate gates. The change is partially verified: local ASGI and
component checks do not claim those operator-facing acceptance results. The
integration owner must mount the reads and preserve global command routing for
guide progress writes even if a guide identifier matches a project identifier.
