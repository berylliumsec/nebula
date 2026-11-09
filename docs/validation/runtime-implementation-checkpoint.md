# Runtime implementation checkpoint

The parallel implementation is on `codex/execution-lifecycle-isolation`. This is
an integration checkpoint, not a release acceptance receipt.

Implemented slices include authenticated host recovery, stable client identity
and request lookup, atomic global SQL APIs, automatic first-project admission,
bounded client request/draft persistence, retained desktop attachment, verified
handoff activation and participant restoration, recovery operation receipts,
code-only deployer rollback, and a volatile desktop browser bridge.

Focused evidence is retained in the sibling validation documents and `/tmp`
receipts. The client slice passed 95 tests and a production build; host bootstrap
and management passed 31 distinct cases; global SQL integration passed 20; global
Settings passed 15 backend and 5 UI cases; browser bridge passed 26. These counts
overlap other selections and must not be summed as a single release result.

## November 9 integration checkpoint

The previously recorded SQLite startup, live-memory control queue and frozen
worker identity defects are fixed. API workers now share their SQL transaction
owner with the global delivery ledger. Positive process evidence permits recovery
from a missing API worker while retaining live or uncertain workers. Global
Storage and guide-progress routes are mounted and keep their explicit global SQL
ownership. Host-wide policy, diagnostics and integration observations are served
without an API worker. These changes have focused receipts in their corresponding
validation documents; they are not a combined release result.

Source-process acceptance passed all five supervisor handoff crash phases on
SQLite and PostgreSQL. Live memory application passed on both databases: a real
bounded allocator's limit changed from 32 MiB to 16 MiB without changing its owner
or resource group, with kernel readback and released capacity on completion.
Participant transfer, paused/disabled work preservation and third-version staging
also have passing isolated process evidence. Five final backend cases remain
selected for the assembled source revision.

The frozen runtime now uses verified retained onedir workers with exact PID/birth
matching. Independently built baseline `557ffd7d` and candidate `66fdcac0` passed
baseline startup/reconnect (one case, 39.91 s), candidate promotion followed by
retained baseline rollback with continuing SQL data and identity (one case,
54.80 s), and candidate cold-host startup (one case, 39.82 s). Normal shutdown
verified all recorded workers exited. See [frozen runtime evidence](frozen-onedir-runtime.md)
for artifact hashes and limits: these are Core process checks, not installed GUI
close/update/reopen evidence, and do not cover later candidate changes.

Production LAN routing and fixture cleanup are corrected. Desktop Chromium passed
cold-host reserve recovery and lost-response/reload continuity on earlier pinned
source/UI builds. The live-memory UI workflow completed, but its accessibility
check identified low text contrast on Apply; the existing primary button variant
repairs that cause. On `49e59a34`, six Memory Settings component cases, two host
diagnostics cases (SQLite/PostgreSQL), Ruff and the production UI build passed.
The final three-journey Chromium matrix (15 cases at 1440/1024 and 320/390/430)
is collected and awaits the combined source pin.

Remaining implementation tracks are canonical host integration mutations and
session credential resolution, provider-only durable tool manifests (including
large successful schema loads), and stateful standing-instruction continuity.
The standing-instruction branch contains the complete durable goal and its own
causal/validation checkpoint. Fresh independent reviews confirmed its bounded
UTF8 digest fault and the tool manifest's pre-delivery ownership fault before
repairs. Host search and image initialization also need memory-policy coverage:
legacy search has a fixed container ceiling but lacks shared admission, while
daemon image work cannot be contained by limiting its client process. A supported
managed search adapter and explicit unsupported runner behavior are being
implemented and reviewed; no unrestricted fallback is authorized.

The nine selected mobile WebKit cases cannot execute here: Playwright 1.61.1's
`webkit-2311/pw_run.sh` is absent and no alternate installed engine was found.
Native installed GUI close/update/reopen also lacks GTK3/WebKit2GTK dependencies.
These required gates block the unqualified continuity claim and full merge.
Product-knowledge synchronization, combined test selection, final PR review and
CI remain outstanding. Existing installations and live services have not been
changed. Continue implementation and isolated validation from this checkpoint;
do not describe these separately pinned receipts as final-build acceptance.
