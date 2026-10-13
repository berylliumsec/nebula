# Public storage integrations

Nebula can receive storage observations from any system that can send JSON over
HTTPS. Each source is configured independently.
The first release shows current state and incidents; it does not store trend
charts or automatically change storage.

## Operator journey and authority

1. Open **Settings → Advanced → Integrations → Storage**. Add a source, choose
   its Project and assistant runtime, and save the one-time token. New sources
   are disabled. The configured Project owns every incident chat from that
   source; Core never silently chooses another runtime.
2. Configure the sender with the webhook URL and token. Use `/api/v1/storage/test`
   with a sample report to check authentication and JSON while the source is
   disabled. Enable the source, then send its inventory and reports.
3. Open the global **Storage** page. A warning, critical, or unavailable report
   opens one incident and queues one assistant conversation. Worsening reports
   enter the same conversation; healthy reports resolve the incident. A stale
   volume keeps its last state and displays the stale marker.
4. If source callbacks are configured, an action offer can be previewed in the
   dashboard or linked chat. The preview lists exact effects and an expiry.
   An authenticated operator approves that digest before Core asks the source
   to execute. The receipt remains in the incident record.

Core's database owns sources, latest volumes, incidents, dispatches, chats,
action approvals, and receipts. The URL owns the selected dashboard or chat.
Component state owns only unsaved form inputs and the one-time token display.
After reload or reconnect, the dashboard is rebuilt from Core. Disabling or
revoking a source stops new reports; it does not rewrite past incidents.

## Sender contract

`POST /api/v1/storage/events` accepts one report under 64 KiB. It requires a
source token in `Authorization: Bearer <token>`. Remote senders use HTTPS; a
sender on the Core host may use loopback HTTP. A source token authenticates
only that source, including when Core runs in unauthenticated local mode.

```json
{
  "schema_version": 1,
  "event_id": "nas-2026-10-13T14:00:00Z-data",
  "observed_at": "2026-10-13T14:00:00Z",
  "kind": "snapshot",
  "device": { "id": "nas-1", "label": "NAS One" },
  "volume": { "id": "data", "label": "Data", "path": "/data" },
  "state": "warning",
  "free_bytes": 21474836480,
  "total_bytes": 1099511627776
}
```

IDs must be stable for the physical device and logical volume. `event_id` must
be stable across retries of the same observation. `observed_at` needs a time
zone. Use `healthy`, `warning`, `critical`, or `unavailable`; unavailable may
omit capacity. Available states require nonnegative free and positive total
bytes, with free no greater than total. Source labels and paths are untrusted
data. The server returns `202` with `duplicate` and `current` booleans. A
duplicate event ID with changed content returns `409`; older observations are
accepted as receipts but cannot replace current state. The full [JSON Schema](schemas/storage-event-v1.schema.json)
is versioned with this route and appears in Core's OpenAPI document.

The same body can be sent to `POST /api/v1/storage/test` while a source is
disabled. It validates the bearer token and schema without adding a device,
volume, incident, or receipt. `POST /api/v1/storage/inventory` optionally
lists devices before the first report:

```json
{"schema_version":1,"devices":[{"id":"nas-1","label":"NAS One"}]}
```

For a system that cannot format its own webhook, copy the report to a file and
run [the reference sender](../examples/storage_sender.py). It saves a
deterministic event ID into the file on first send, then retries the identical
body with backoff. Keep the file until Nebula returns `202`; do not regenerate
`observed_at` during a retry. Set `NEBULA_STORAGE_TOKEN` in the sender's
environment rather than its command line.

## Optional source callbacks

In source settings, an operator may configure a callback origin and a Nebula
credential reference (`env:`, `systemd:`, `vault:`, or `session:`). Core never
accepts callback destinations or credentials from an event. The origin must
be HTTPS, or loopback HTTP when the source responder runs on the Core host.
Core sends a bearer token from the referenced credential, follows no redirects,
and bounds responses to 64 KiB. An event-only source needs no callbacks.

| Request from Nebula | Body | Response |
| --- | --- | --- |
| `POST /v1/diagnose` | `incident_id`, `event_id` | `diagnostics` string and `offers: [{id,title}]` |
| `POST /v1/actions/{offer_id}/preview` | `incident_id`, `action_id` | `effects` array, `digest`, `expires_at` |
| `POST /v1/actions/{offer_id}/execute` | `incident_id`, `action_id`, `digest`, `idempotency_key` | `status` and durable `receipt` |
| `POST /v1/actions/{offer_id}/status` | `action_id`, `idempotency_key` | `complete`, `failed`, or `unknown`, with a receipt when complete |

The digest is SHA-256 of UTF-8 JSON for the exact `effects` array with sorted
keys and compact separators. The preview expires after its `expires_at`. The
source must recompute and compare the plan when executing. Nebula will not
send an expired or changed digest, and rejects previews more than ten minutes
from expiry. Disabling a source or resolving its incident blocks new previews
and approvals. The source must implement the execution
key idempotently and preserve a receipt. After a timeout or uncertain response,
Nebula queries status; it never sends a second execute automatically.
[The reference responder](../examples/storage_action_responder.py) implements
these checks with a harmless demo effect. Replace its `DemoBackend` with
guarded source-specific operations. Keep its SQLite state durable.

Nebula treats diagnostics, labels, paths, and action effects as untrusted
source data. The assistant sees a bounded snapshot and diagnostics, with
remote tools disabled for the automatic incident turn. Action buttons are
presented outside the model's control, in the linked chat and dashboard.
Callback failures appear on the incident while its event-only alert chat
continues. If the selected assistant runtime is unavailable, the failed
dispatch remains visible and can be retried after the runtime is repaired.
If a queued assistant response fails, the linked chat shows its review and
retry controls.

## Verification scope

The operator journey must be exercised through a disposable Core and a
production UI bundle: enroll, test, enable, report, refresh, chat, callback
preview, approval, receipt, restart, and reconnect. Selected backend and UI
tests cover token rotation/revocation, malformed/duplicate/late events,
disabled sources, runtime and callback failures, plan expiry or drift,
idempotency, and uncertain execution. Desktop Chromium, mobile Chromium,
mobile WebKit, and non-loopback LAN checks apply to this same journey.
