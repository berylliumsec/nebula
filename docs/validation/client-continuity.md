# Client continuity

The browser keeps an immutable submission identity before sending a mutation.
Accepted command receipts are followed with authenticated reads; reopening the
application only checks saved identities and never submits a draft, repeats an
approval decision, or resumes a turn automatically. The **Saved requests** notice
offers **Check saved requests** and a link to the exact saved conversation when
Core has assigned one. Unknown and uncertain requests are retained.

Request identities use authenticated `/api/v1/runtime/identity` Core/principal
namespaces. Bearer tokens, pairing secrets and CSRF tokens are not stored in
pending records. Rotating the operator token preserves the namespace; pairing a
new device creates a separate namespace. Servers without the identity contract
use a visibly limited, isolated in-memory store and do not import old unscoped
drafts.

Pending requests share an atomic IndexedDB limit of 8 MiB, 128 records, and 1 MiB
per encoded record. Bounded text bodies retain their exact serialized bytes;
larger/binary bodies retain their fingerprint and are marked non-replayable.
Credential-bearing bodies retain only their fingerprint, never their secret.
Storage failure rejects a new mutation before transmission. Unresolved records
are not evicted to make room. Requests resolved by Core can be dismissed after a
read-only status check. Normal short mutations resolve their receipt before the
existing UI receives the result.

Text drafts and queued follow-up drafts use the authenticated Core/principal,
project and conversation namespace. They persist across window close within the
same browser profile, with 128 records, 2 MiB total text, 256 KiB per record, and
30-day expiry. Oldest drafts are removed at the cap; a rejected/blocked write
keeps editing available and displays the local recovery limitation. Device-local
drafts do not become server work until the operator sends them.

SSE replay retains sequence/epoch deduplication and the explicit resume command's
segment boundary. A fresh viewer rebuilds from Core history and replay rather
than persisting a cursor without its corresponding rendered snapshot. Each
incomplete frame is capped at 1 MiB. Closing/switching a viewer detaches only its
transport; Stop remains an explicit Core mutation.

When authenticated health reports `api_ready: false`, the UI bypasses workspace
catalog bootstrap and exposes **Memory Settings** directly. A later healthy API
response restarts normal hydration without submitting saved work.

Local validation on November 9, 2026: **95/95 focused unit/component cases passed**
across these explicitly collected files:

- `ui/src/api/{durableRequests,client.durableRequests,chatConnection,chatStreamFollow,client.workReceipts}.test.ts`
- `ui/src/pages/{chatDraftStorage,chatFollowUpStorage,chatStreamLifecycle}.test.ts`
- `ui/src/pages/{chatDraftNavigation,useSessionState}.test.tsx`
- `ui/src/components/RuntimeContinuityNotice.test.tsx`
- `ui/src/App.hostRecovery.test.tsx`
- `ui/src/state/WorkspaceContext.recovery.test.tsx`

Execution used the guarded `npm test -- <listed files> --testTimeout=15000
--reporter=json --outputFile=/tmp/nebula-client-tests-final.json` entry point.
Collection is `/tmp/nebula-client-test-list-final.json`; the diff-bound local
selection is `/tmp/nebula-client-selection-receipt.json`. The shared integration
selection remains owned by the integration workstream.

`tsc -b --pretty false` and `npm run build` passed. The build's reviewed workflow
inventory covers 608/608 call signatures, zero unreviewed signatures and zero
gaps; the new continuity protocol grants no model operation. The initial
TypeScript alias-narrowing diagnostic was independently validated before its
explicit-guard repair. Build output is `/tmp/nebula-client-production-build.log`.

Production LAN, real-Core cross-version continuity, mobile Chromium, mobile
WebKit, and installed-desktop journeys remain integration gates. These local
checks do not claim them.
