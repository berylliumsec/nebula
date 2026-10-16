# Nebula Voice acceptance contract

Entry: the Voice button in the dedicated Nebula assistant opens a full-screen
voice surface for the selected project. The operator speaks naturally, hears
GPT-Live, asks it to start or control chats and missions, and sees current work.
Voice pauses after the Core-wide timeout (20 seconds by default) or when the
microphone is turned off. The surface and project work remain visible, and
Resume Voice starts a new paid session. A lost connection keeps the surface
and offers recovery without claiming a confirmed pause.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Voice is reachable from the existing Nebula assistant on desktop and mobile | React | component + Playwright |
| iPhone shell | The installed assistant-only shell exposes the same Nebula assistant and Voice launcher; its app declares microphone use and opens settings in place | WKWebView + React | real-Core shell browser + native build + device |
| Start | Permission is requested from a button action; Core creates a project-bound GPT-Live session without returning its API key | Browser permission + Core | Python + browser |
| Configure | Butler settings reads and saves one Core-wide 10, 20, 30, or 60 second timeout with revision checks; every new connection receives the saved snapshot | Core database | Python + component + real Core |
| Select/use | Header and delegated work use the same selected project; changing projects reconnects before another action | Workspace context + Core | component + real Core |
| Speak/interrupt | Microphone and speaker stay connected together; user speech can interrupt GPT-Live speech; the orb rises and expands with microphone level unless reduced motion is selected | GPT-Live WebRTC + browser audio meter | component + browser + live session |
| Idle/mute | Input speech and actual output playback reset the quiet timer; silent delegated work does not keep Live open beyond the timeout. Microphone-off disables input immediately and closes after current playback | Browser audio activity + Live session | component + browser + live session |
| Delegate | Transcript fragments provide context; Nebula's existing control assistant executes project-bound chat and mission tools; a result is spoken only after Core confirms it | Core chat and work services | Python + real Core |
| Status | Chat and mission cards reflect Core's work roster, including pending and failed work; pending approvals and questions remain visible | Core | real Core + browser |
| Decide | On explicit instruction, Nebula reads the exact pending request and its revision before Core records a decision; private secret answers stay in the owning chat or mission UI | Core approval and interaction services | Python + real Core |
| Hear | A missing speaker stream or blocked playback opens speaker recovery; the operator can inspect output, choose a device when supported, or reconnect | Browser media element + WebRTC stats | component + browser + live session |
| Choose input | The active microphone is named; the operator can select a different device and reconnect the same voice surface when the system default is silent | Browser device enumeration + WebRTC | component + browser + physical device |
| Error/retry | Secure-context, permission, credential, provider, and connection errors explain the next valid action; the full-screen mode stays visible | Browser + Core | component + browser |
| Pause/resume | Idle, mute, or background sends `session.close` and waits for `session.closed`; a confirmed pause retains work status and Resume creates a new session. Late Core results never reach the old or replacement Live session | GPT-Live connection + Core chat | component + browser + real Core |
| Close | Exit sends `session.close`, waits for `session.closed` when possible, releases the microphone and audio track, and dismisses the surface; an unconfirmed close stays distinguishable from a confirmed pause | GPT-Live session + React | component + browser |

Transient captions and connection state belong to the browser. The timeout belongs
to Core and is shared across projects and devices; changes apply to the next
connection, not an active one. Chat and mission
status and results belong to Core. The project ID is validated by Core at
session creation and again for work requests. Voice sessions are intentionally
ephemeral; project work is durable. Browser microphone access requires HTTPS or
localhost. An HTTP LAN origin shows a clear in-place recovery message; TLS must
be provided by the deployment's trusted HTTPS origin. No browser flag or
authentication bypass is used.

The installed iPhone shell uses the same Core-backed assistant entry as the web
app. Its native manifest declares microphone use. Native microphone capture and
speaker playback still require a trusted HTTPS Core origin and a signed app build
installed on the phone. A Core web update on its own does not change the native
permission manifest, and a simulated WKWebView user agent does not prove device
audio capture.

Lifecycle coverage: discover, start, select, speak, interrupt, delegate,
background/resume, reconnect, refresh, retry, and close are applicable. Forking
a voice session is not needed because durable work is in Core. Reconnecting
starts a fresh voice conversation; durable chats and missions remain available
in the work roster. Deleting a voice session is close; deleting Core chat or
mission work remains in its existing UI.

Planned layers: a focused Python route/transport test, component tests for the
voice lifecycle, focused Playwright geometry and secure-context tests in desktop
Chromium and emulated mobile Chromium/WebKit, a production bundle, and a real-Core
LAN check on an HTTPS origin when one is available.

Official API contracts: [GPT-Live](https://developers.openai.com/api/docs/guides/live),
[WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live),
[client delegation](https://developers.openai.com/api/docs/guides/live-delegation),
and [session lifecycle](https://developers.openai.com/api/docs/guides/live-conversations).
