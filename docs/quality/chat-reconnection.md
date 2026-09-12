# Chat reconnection evidence

Implemented on `codex/reasoning-display`, following the [connection contract](../design/chat-reconnection.md). Verification is partial: deterministic harnesses exercised real Core; live authenticated providers and physical devices were not exercised.

## Behavior

Accepted chat turns reconnect through a read-only GET with a sequence cursor. Recovery never repeats the initial submission or invokes execution resume. Harness sockets replay after the last delivered event and discard duplicates. Viewer detach cancels recovery; Core retains execution ownership. A visible status announces reconnection. Fifteen-second heartbeats and a 45-second viewer watchdog distinguish quiet work from a stalled connection. Recovery uses abortable exponential backoff, up to eight consecutive retries; online/visibility changes wake a pending attempt. Unknown acceptance, authorization failure and uncertain execution state require explicit review instead of automatic resubmission.

## Focused verification (2026-09-12)

The diff-bound `.github/test-selection.json` retains the previous reasoning-retention selection and adds only these connection journeys. No full suite ran.

- Python: 9 selected cases passed across the initial run and focused fixture-correction retries. Selection: `tests/v3/test_chat_reconnection.py` (5), two viewer-lifetime cases in `test_chat_api.py`, and durable-replay/transport-loss cases in `test_harnesses.py`. Commands used `PYTHONPATH=src .venv/bin/python -m pytest` with those exact selectors; 120-second boundary. Logs: `/tmp/reconnect-python.log`, `/tmp/reconnect-python-retry.log`, `/tmp/reconnect-python-harness.log`.
- UI: `vitest run src/api/chatConnection.test.ts src/pages/chatStreamLifecycle.test.ts`: 9 passed in 1.51 seconds, 120-second boundary. Log: `/tmp/reconnect-ui.log`.
- Production: `npm --prefix ui run build` passed within the 180-second boundary; existing bundle-size warning remains. Log: `/tmp/reconnect-build.log`.
- Browser: guarded `npm --prefix ui run test:e2e -- tests/chat-reconnection.spec.ts` with the eight `model-*` projects and `--grep 'chat reconnects' --max-failures=1`: 16 passed in 1.9 minutes; 60-second case and 900-second run boundaries. Log: `/tmp/reconnect-browser.log`.
- Ruff and `git diff --check` passed.

Browser journey: create/select chat, submit through the composer, drop its initial SSE viewer, verify GET reattachment, reload into a WebSocket follower, drop that socket, observe reconnecting, transition offline/online, reattach with a cursor, complete, verify exactly one execution, reload the exact final transcript, run accessibility analysis on the assistant region and check horizontal clipping. Both Grok and Codex normalized harness paths ran against disposable real Core and the production bundle at `http://192.168.1.155:19448`. Fixture adapters emitted deterministic content without external provider traffic.

Desktop Chromium widths 1440 and 1024 passed. Emulated mobile Chromium and WebKit widths 320, 390 and 430 passed. Screenshots are retained in `ui/test-results/chat-reconnection-*/`; the 320px WebKit screenshot was visually inspected. Fixture-only setup failures (route ordering and globally unique IDs) were corrected before the passing matrix; they did not require changing production connection behavior.

## Limits

Physical phones, software keyboards, manual assistive-technology use, live Grok/Codex accounts and long-duration network outages remain unverified. Browser coverage uses automated emulation, not physical Safari. The disposable Core intentionally lacks unrelated local diagnostics. Existing transcript layout, creation/deletion/fork behavior and unrelated capabilities were outside this change. Core restart does not silently restart interrupted execution. Provider in-memory event retention remains bounded by its existing lifecycle; completed replies can be recovered from durable storage.
