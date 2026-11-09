# Retained onedir runtime

The frozen worker keeps the PID that Core records at process creation. The
canonical builder creates two artifacts from the same source and build identity:
a onefile compatibility connector and a separate `nebula-runtime` onedir tree.
Only the onedir executable is placed in the retained manifest's `bin/nebula-core`
slot. The existing PID/birth checks are unchanged.

The full onedir tree, including `_internal` libraries and data, is copied into
the retained bundle and covered by its file manifest. Internal file symlinks are
resolved only after checking their target remains inside the generated runtime
tree, then materialized as regular files. External links, directory links and
unsupported file types are rejected. The public connector and the complete
onedir payload have separate archive audits.

Focused source checks cover retained dependency use after removal of the original
tree, internal/external/directory link handling, rejection of a single executable
as an onedir runtime, and complete external-data auditing. The existing source
worker-dispatch and reviewed-migration tests remain selected. All 12 selected
source/package checks passed in 3.43 seconds; Ruff also passed. These checks do not
replace the required actual frozen-process acceptance.

Actual dual builds completed at `e2468085` and the assembled `48077146` candidate.
The first clean-profile frozen run reached authenticated API readiness with the
exact worker PID/birth checks intact. Project creation then failed with
`sqlite3.OperationalError: database is locked` while inserting its default scope;
the API subsequently lost its runtime lease. This is an incomplete acceptance
result. The separate runtime owner is reviewing the transaction boundary before
another candidate is built. Normal explicit shutdown succeeded, and a read-only
check found no registered or fixture-owned processes alive afterward.

The installer follow-up verifies every retained file and the manifest digest
before allowing only `bin/_internal/playwright/driver/node`. A Node binary at any
other path remains forbidden. Existing top-level browser and notebook resources
remain available to their current consumers; duplicate retained copies must match
every file, including legal and nonexecutable data. Dependency test packages are
excluded from the freezer and rejected by both archive and installer audits.
All 20 selected installer/archive checks passed in 0.55 seconds; Ruff passed.

The frozen driver records registered worker PID/birth identities and verifies
that they are gone after normal explicit shutdown. A native installed GUI, its
viewer close/update flow, and the required browser acceptance remain unverified;
this environment lacks the GTK3/WebKit2GTK development packages needed to build
the native shell.
