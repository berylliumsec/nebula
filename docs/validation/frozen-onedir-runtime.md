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

Actual build, default SQLite startup, worker IPC, reconnect and shutdown evidence
will be recorded after the source change is committed and the independently
owned SQLite startup fix is included. No installed native GUI result is implied.
