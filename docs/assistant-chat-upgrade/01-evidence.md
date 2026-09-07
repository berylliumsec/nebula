# Phase 1 validation

Base: 1f155b583167ae71551e47b437cc8394b12818d0.
Journey: open new chat, inspect/close settings, reopen durable provider conversation.
Unit: `npm --prefix ui test -- --run src/components/ActivityLedger.test.tsx src/components/HarnessStatusRail.test.tsx` — 10 passed.
Production build: `npm --prefix ui run build` — passed (existing bundle-size/import warnings).
Production LAN: preview on `http://192.168.1.155:15420`; `assistant upgrade foundation` interface test — 8 passed: desktop 1440/1024, emulated Chromium 320/390/430, emulated WebKit 320/390/430.
Real Core: isolated dynamic LAN port and temporary database, deterministic local model HTTP server; `assistant upgrade foundation production` — 1 passed. Saved transcript survived relaunch and user message had no work card. Initial run failed only in test cleanup (`stub.close`); corrected to shared cleanup helper and rerun passed.
Physical device: not run. Vendor-native live streaming/approvals not covered by this phase's new test. Broader lifecycle coverage remains required in subsequent phases. This is a draft, not a full release acceptance claim.
Production service was not changed.
