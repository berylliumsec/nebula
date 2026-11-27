# Butler push notifications and the Butler-only iPhone app

Operator request (2026-11-27): the iPhone app should boot straight into Butler
mode and stay there, and send a notification whenever Butler needs the operator
or anything is blocked. Operator-facing guide: [docs/PUSH-NOTIFICATIONS.md](../PUSH-NOTIFICATIONS.md).

## Decisions

- **Channel: APNs.** Notifications must reach a locked phone or a closed app, so
  Core talks to Apple's push service directly with a token-based (`.p8`)
  provider key. The app must be signed by a paid Apple Developer team; the
  `aps-environment` entitlement is in `ios/Nebula/Nebula.entitlements`.
- **Trigger: every open `ButlerIncident`.** Incidents already cover operator
  action requests, pending approvals and input (`butler_attention`), blocked and
  stuck goals, failed recoveries, harness quota and rate limits, resources and
  review findings. Watching the incident table catches every current and future
  source without changing each writer.
- **Butler only.** The `NebulaShell/1.0` route table has `/butler` and the
  landing redirect. `/projects` and every other path redirect to `/`, which
  opens Butler mode for the remembered project. Butler mode hides All projects
  in the shell. The Projects snapshot page, its tab bar and
  `projectSnapshots.ts` were removed.

## State owners

| State | Owner |
| --- | --- |
| APNs key | OS credential vault, or Nebula's encrypted store when a headless host's keyring is locked (`vault:`/`store:` reference in `PushNotificationSettings.key_ref`) |
| Key ID, team ID, topic, master switch, first-run baseline, last config error | `PushNotificationSettings` (one row, id `push-notification-settings`) |
| Phone token, environment, pairing link, pause, test request, last answer | `PushDevice` (one row per phone; a new token from the same pairing replaces it) |
| Alerted incident episodes | `PushIncidentDelivery` (id `push-<incident id>`), deleted when the incident resolves |
| Device token and iOS permission | iOS; handed to the page by `PushPolicy.pageScript` (a one-way user script, with no page-to-native handler) |
| Which Butler item a tapped alert opens | The alert's `nebula_path`, validated by `PushPolicy.butlerPath` to stay inside `/butler` |

## Dispatcher

`ButlerPushDispatcher.run_pass` runs every 5 s, or every 30 s while push is
unconfigured or after a failed pass.

1. It reads the settings and stops if the key, key ID or team ID is missing or
   the master switch is off.
2. It loads the open incidents and the delivery rows. A row whose incident is
   resolved or deleted is removed, which ends that episode; an acknowledged
   incident keeps its row.
3. On the first configured pass, it records every open incident as `baseline`
   without sending and sets `baseline_at`.
4. It skips phones that are paused, retired by Apple (`unregistered_at`), or
   whose pairing is revoked or past its absolute expiry.
5. It sends queued test alerts (requested within the last 120 s) and the one
   welcome alert per phone (retried after 10 min if it failed).
6. New incidents are due, and so are `alert` rows that failed fewer than 3
   times. More than 3 due incidents become one grouped alert; otherwise each
   becomes its own alert, collapsed by incident id and threaded by project.
7. It handles Apple's answers:
   - `BadDeviceToken`, `Unregistered`, `DeviceTokenNotForTopic` or HTTP 410
     retire the phone.
   - Provider and topic errors are stored as `last_error` on the settings and
     shown in both UIs.
   - `ExpiredProviderToken` drops the cached JWT so the next send mints a new
     one. The token (ES256, signed with `cryptography`) is otherwise refreshed
     every 45 min.

Exactly one dispatcher runs per install, so alerts are never duplicated across
project workers. It starts in `RuntimeSupervisor.start` (on the unscoped host
store), or in `execution_kernel.runtime_lifespan` when the database has no
`execution_runtime_id` (single-process Core). Project workers never send.
`NEBULA_BUTLER_PUSH=0` disables it. APNs needs HTTP/2, so `httpx[http2]`
(adding h2, hpack and hyperframe) is a dependency.

## API (`/api/v1`, authenticated)

- `GET notifications/push` returns the status: configured, missing,
  `key_saved`, IDs, topic, `last_error`, and the phones (each with
  `this_device`).
- `PUT notifications/push/settings` sets `key_ref` (`vault:`, `store:`, `systemd:` or
  `env:` only), `key_id`, `team_id`, `topic` and `enabled`, with
  `expected_revision`.
- `PUT notifications/push/devices` registers the phone idempotently. It requires
  a paired device (403 otherwise).
- `PATCH notifications/push/devices/{id}` sets `enabled`.
- `DELETE notifications/push/devices/{id}` removes the phone.
- `POST notifications/push/devices/{id}/test` queues a test alert (202). The UI
  polls status until `test_pending` clears. It returns 409 while push is
  unconfigured.

The routes are mounted in the single-process app and in the API worker's global
settings app (`mount_global_settings`).

## Lifecycle checks

| Step | Evidence |
| --- | --- |
| Discover / configure | Settings catalog entry `settings.push-notifications`, Butler `nebula.settings.open setting_id=push-notifications`, workflow registry `settings.push_notifications` |
| Register | iPhone launch → user script → `ShellPushRegistration` → `PUT devices`; mocked Playwright in Chromium and WebKit; real phone pending |
| Send / dedupe / reopen / burst / retire | `tests/v3/test_push_notifications.py` |
| Tap → Oversight | `PushPolicy.butlerPath` Swift checks and Playwright `/butler?…&section=oversight` |
| Revoke | dispatcher skips revoked pairings (unit test) |
| Butler only | Playwright: `/projects`, `/settings`, `/library` and `/storage` land in Butler, with no All projects link |

Not covered by automation: real delivery through Apple's service, which needs
the operator's `.p8` key and the signed build on the phone.

## Physical acceptance (iPhone build 9)

Still required on a signed build: the permission prompt on first launch, a real
alert while the phone is locked, tapping it opens Oversight, Send test, Pause,
and revoking the pairing stops alerts. Record the result here when it is done.
