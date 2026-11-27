# Butler push notifications

The Nebula iPhone app opens straight into Butler mode and stays there. When a
project needs you or is blocked, Butler sends an alert to the phone, even when
the app is closed or the phone is locked. Tapping the alert opens that project's
**Oversight** in Butler mode.

## What sends an alert

Every Butler incident sends an alert. That covers operator action requests
("Needs you"), blocked or stuck goals, pending approvals and agent input, failed
recoveries, harness quota and rate limits, resource alerts, and review findings.

- An incident sends one alert when it opens, and another if it reopens after
  being resolved. Updates to an open incident do not alert again, and neither
  does acknowledging it.
- When more than three new incidents arrive at once, the phone gets one grouped
  alert ("5 Butler items need you") instead of five.
- Incidents that were already open the first time Core could send alerts are not
  sent one by one. A new phone gets a single "Butler notifications are on" alert
  with the number of items already open.

## Set up (once per Core)

1. In Apple Developer, go to Certificates, IDs & Profiles → **Keys**. Create a
   key with **Apple Push Notifications service (APNs)** enabled and download
   `AuthKey_<KEY ID>.p8`. Apple lets you download it only once.
2. In full Nebula, open **Settings → Identity & Security → Phone
   notifications**. Choose the `.p8` file (the key ID is read from the file
   name) and enter your 10-character **Team ID**, then **Save**. The key goes to
   the credential vault; Core stores only its reference.
3. Open the Nebula iPhone app and allow notifications. The phone registers with
   Core through its existing pairing and gets a "Butler notifications are on"
   alert.
4. Use **Send test** to check delivery at any time.

Butler can open this section for you ("open push notifications"). It never
receives the key.

## On the phone

In the app, **Butler mode → More → Settings** shows whether this phone receives
alerts and why not. The possible reasons:

- notifications are off in iOS Settings;
- Core has no APNs key yet;
- Apple rejected the key or the phone's address;
- alerts are paused for this phone.

From there you can **Pause** or **Resume** alerts for this phone, or **Send
test**.

## Phones and pairing

Each phone's alerts are tied to its paired-device session. Revoking the phone in
Settings → Paired devices stops its alerts. **Pause** stops alerts to one phone
and keeps it registered; **Remove** forgets the phone until the app opens again.
Turn the whole feature off with **Turn off** in Settings → Push notifications.
If Apple reports that a phone's notification address is no longer valid, Core
stops using it until the app registers again on its next launch.

## Limits

- Alerts need Core to be running and able to reach Apple
  (`api.push.apple.com` or `api.sandbox.push.apple.com`, port 443). If Core is
  down, no alert can say so; the app shows **Core is unavailable** when opened.
- Development builds installed from Xcode use Apple's sandbox push service, and
  App Store or TestFlight builds use production. The app tells Core which one it
  uses.
