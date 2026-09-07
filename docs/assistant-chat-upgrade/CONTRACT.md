# Assistant chat upgrade contract

Base: local main `1f155b583167ae71551e47b437cc8394b12818d0`.
Worktree: `/home/agent/nebula-assistant-chat-upgrade`.
Production `/home/agent/nebula-live-main` is not an implementation target.

| Phase / operator journey | Observable invariants | Authorities | Required layers |
| --- | --- | --- | --- |
| 1: open, read, stream, inspect settings | User messages have no work summaries; idle is not disconnected; empty activity is quiet; settings readable and scrollable; latest appears only below reader | Core activity and transport; transient presentation | component, browser matrix, production real Core LAN |
| 2: search, bookmark, edit/branch, answer | Results navigate to exact messages; mutations survive refresh; original transcript and manual names preserved; requests never hidden | Core messages/bookmarks, URL identity, harness questions | backend, component, real Core, browser matrix |
| 3: attach, inspect context/results, quote | Host files use host browser; snapshots remain exact; only retained results are previewed; draft/selection survive drawer changes | Core artifacts/context and URL; unsent local draft | backend, component, real Core LAN, browser matrix |
| 4: queue, edit, reorder, pause, recover | Core owns sequential dispatch after tab closure; no blind replay; approval/failure stops draining; revision conflicts visible | Core queue/turns, frozen authorization | concurrent backend/restart tests, two-browser real Core, browser matrix |
| 5: pin, revise, supersede, promote | Explicit operator action only; next-turn snapshot; project promotion explicit; fork provenance retained | Core decisions/revisions and request snapshots | backend, UI and real Core |
| 6: return, catch up, inspect evidence | Per-device cursor; meaningful updates only; sources linked; dismissing summary never dismisses pending actions | Core cursors/activity/citations | backend, UI and real Core |

All phases cover discovery, create/mutate, select/use, refresh, background/reconnect, failure/retry, delete/revoke where persistent records exist. Streaming/interrupt and fork are required for chat, queue, and decision integration; read-only drawers must preserve them. Each phase has a dependent draft PR. No production deployment is implied.

Browser gates: desktop Chromium 1440/1024; mobile Chromium and WebKit 320/390/430; keyboard/touch/focus/labels/reduced motion, long/error/empty/loading states, real-Core production LAN. Physical keyboard checks are reported separately. Missing evidence stays explicit; passing fixtures is not shipping proof.

Preventive rule: show a status/control only when it helps understand work or choose the next action, and bind it to the authority that actually owns that state.
