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

Source-process acceptance has verified SQLite crashes at all five handoff phases,
API replacement/rollback with continuing data, cold-host reserve recovery,
prepared activation, and an expired owner with an unresolved effect. Remaining
integration defects are tracked explicitly: clean SQLite service startup selects
PostgreSQL-only pool configuration; live memory application queues behind the
allocator's work lane; onefile frozen workers report a different PID from the
bootloader recorded by their parent. The canonical frozen artifact built and
passed its member audit, but its runtime journey has not passed.

The production LAN/browser fixture also needs scoped routing for this executor's
own LAN address and graceful manager shutdown. Mobile WebKit and native packaged
desktop evidence are outstanding. Existing installations and live services have
not been changed. Implementation and isolated validation continue after this
checkpoint.
