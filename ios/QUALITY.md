# iOS shell acceptance contract

Journey: launch Nebula on iPhone, confirm a LAN server, pair through Nebula's existing screen, select and use a chat, background/resume, and recover from a connection failure.

| Step | Invariant | Authority | Evidence planned |
| --- | --- | --- | --- |
| Entry/select | Saved server is visible and editable; invalid URLs rejected | UserDefaults | Swift policy checks, physical app |
| Pair/create/use | Existing web pairing and Core workflows remain authoritative | WKWebsiteDataStore.default + Core | production LAN, real Core |
| Stream/interrupt | Existing UI owns streaming and cancellation | Core/web event client | physical device and browser matrix |
| Refresh/reconnect | Backgrounding retains web view; reload is explicit | WKWebView + Core | physical keyboard/background/network checks |
| Failure/retry | A failed main navigation offers retry and server settings | native transient state | physical failure/retry |
| Delete/revoke | Existing Nebula settings revoke pairing | Core | real Core; no native duplicate API |
| External navigation | Cross-origin pages require explicit Safari handoff | native navigation policy | Swift checks and device |

Native shell does not add chat APIs or copy credentials. Persistent WebKit storage holds the existing web session. Server address alone is stored in UserDefaults. Local HTTP requires explicit selection and disclosure; TLS certificate checks remain default. No native bridge is exposed to page scripts.

Required acceptance: unsigned iPhone Release build; URL policy checks; physical install/launch, pairing, keyboard, rotate, background/resume, failure/retry; existing desktop Chromium/mobile Chromium/mobile WebKit workflow checks; production LAN and real-Core chat/persistence. Missing signing or device access leaves physical gates incomplete. Simulator runtimes are not required for USB installation.

## Current evidence (2026-09-07)

- Xcode 27 beta 6, build 27A5252f, research Mac mini.
- `swiftc Nebula/ServerAddress.swift Tests/main.swift` and executable: 16 checks passed.
- iPhone Release build, `CODE_SIGNING_ALLOWED=NO`: BUILD SUCCEEDED. This is unsigned.
- Signed device build: failed because no development team was selected. Xcode project opened for account/team setup.
- Physical iPhone 11, iOS 26.5: connected, paired, Developer Mode enabled. App installation and native UI journey pending signing.
- Existing production Core port 8000: requests from Mac to both host LAN addresses and host loopback timed out. Listener exists; this is not a verified usable server.
- Desktop/mobile Chromium, mobile WebKit, real-Core pairing/chat/reconnect, and physical keyboard/rotation/background gates remain unrun. No completed-workflow claim.

## Physical installation update (2026-09-07)

- User selected a signing team. SSH codesign hit `errSecInternalComponent`; building with Product > Run in the logged-in Xcode UI succeeded.
- `devicectl device info apps` confirms installed `com.berylliumsec.nebula.ios`, version 0.1.0 (1).
- `devicectl device process launch` succeeded. Physical screenshot (828x1792) shows first-use server dialog, Connect/Cancel, native navigation, and software keyboard without clipped dialog content.
- Mac-to-production LAN `http://192.168.1.155:8000/` and host loopback now return HTTP 200. No server changes were made in this task.
- Pairing, chat, persistence, rotation, background/resume, and browser matrices still require verification. Installation and launch are confirmed; full workflow acceptance is not.

## Native pairing handoff

Added a Pair control and `nebula://pair?url=...` handling. Pairing links must match
the selected server origin and contain exactly one bounded secret and six-digit
code; native preferences never store them. A fresh page query forces PairingGate
to mount and consume/clear the fragment. The existing web form performs redemption
and sets cookies in the persistent WKWebsiteDataStore.

All 21 Swift address/origin/pairing checks passed. Updated signed app installed
through Xcode. Physical screenshot confirms the real production Pair this device
form and prefilled matching confirmation code. Device-side submission is pending.
Native navigation title truncates with the additional Pair button at 414 pt;
controls remain visible, but compact navigation needs further refinement.

Pairing completed: Core's authenticated `/api/v1/auth/devices` list shows a new
`My phone` device created 2026-09-07T15:11:18.790474Z, immediately after the native
app's pairing form was presented and the user was instructed to submit it.
This confirms redemption and durable device registration. Full chat/reconnect
acceptance remains outstanding.

## Compact mobile layout contract

Journey: existing paired chat -> focus composer -> type/attach/configure -> send or
cancel -> dismiss keyboard -> navigate. Draft and transcript remain owned by the
existing React/Core flow; keyboard state is transient. Invariants: one compact
native menu; no steady-state IP row; composer controls do not wrap; mobile tabs
hide while editing; input stays above keyboard; settings, attachments, failures,
and approvals remain reachable. Validate production desktop/mobile browser matrix
and physical iPhone keyboard geometry, preserving signing and pairing.

## Compact layout verification (2026-09-07)

- Unit: `npm --prefix ui test -- src/components/MobileDisclosure.test.tsx`: 2 passed.
- Production build passed in both working and live checkouts.
- Focused foundation + compact composer Playwright: 12 passed on desktop Chromium
  1440/1024, mobile Chromium 320/393, mobile WebKit 390/430.
- Final live-checkout production bundle rerun: 4 passed (desktop, mobile Chromium
  320, mobile WebKit 390/430). Fixtures validate draft preservation, attachment
  access, menu access, no horizontal overflow, <=60px action row, and navigation
  hidden on composer focus; emulation does not produce an iOS software keyboard.
- Production LAN index at http://192.168.1.155:8000/ matches live built index.
- Physical iPhone 11 screenshot confirms compact native header, collapsed message
  actions, and compact composer on the existing paired chat after relaunch. A
  cached web page initially showed old content; native initial requests now bypass
  local cache and Reload uses reloadFromOrigin. Pairing and chat persisted.
- Actual focused physical keyboard, keyboard dismissal, rotation, VoiceOver,
  streaming/cancellation and network transition remain unverified for this change.
- WebKit's keyboard accessory bar removal is not claimed; only public input
  assistant APIs are used. No private WKContentView subclassing or swizzling.
- Unrelated credential edits retained in both checkouts. Live UI source copied
  only for these changes; existing live revision otherwise retained.

## Project removal deployment (2026-09-07)

- Project removal is supplied by the production server UI, with archive/restore,
  file/history retention, and eight passing real-Core desktop/mobile profiles.
- Native source hashes match the current Xcode project at
  `/Users/research/nebula-ios`: App.swift
  `4f3676da44c0200c80e478bde827e337912f740b4de01fc3982d6173e9a51f59`,
  ServerAddress.swift
  `79ec20da517d6dca50098c16875f02a6a18e550311939e940270cf5a1611235e`.
- All 21 Swift policy checks passed again. Signed Release build 0.1.0 (3)
  succeeded through the enrolled Primary Mac control broker; strict codesign
  verification passed.
- Build 3 installed successfully on David's iPhone 11 and His H (iPhone 16 Plus).
  The first launch attempt on David's iPhone was blocked by its locked screen.
  Installation does not prove the full native project-removal journey.

His H launch verification: CoreDevice reports build 3 installed and the app
launched successfully. David's iPhone remains installed but launch verification
requires unlocking it. The Mac Xcode project build number was updated using
`agvtool new-version -all 3`, preserving the selected signing team.
