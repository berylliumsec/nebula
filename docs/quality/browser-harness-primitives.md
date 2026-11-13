# Browser harness primitives: acceptance evidence

Base: `b427e73` (current main at integration). Scope: remove Intruder, payload
sources and generation, guided assessments, recipes, issue validation, and their
execution/API/discovery surfaces. Preserve browser primitives and independent
Findings/Evidence. Risk identifiers use neutral effect names with the existing
approval and scope requirements.

## Verified journeys

- Interception, Repeater, crawling, traffic, HAR and utilities retain their Core
  contracts; retired workflow routes, stored types, and tool schemas are removed.
- Browser tools remain selectable. Retired URLs display an unavailable notice.
- Repeater drafts survive navigation and reload; unsaved edits can be discarded;
  durable creation and deletion are checked against Core.
- Local target navigation and Decoder/Comparer/Sequencer work against real Core.
- Generic browser actions retain approvals, cancellation, scope and isolation.
- Real headless Chromium captures only its scoped request, records its response
  artifact, stops, and rejects subsequent requests.
- Sync and async proxy authorization callbacks enforce authentication and live
  revocation. RuntimeWarning is an error for this boundary test.

## Results

- Python: 122 selected Core, policy, companion, graph, and knowledge cases passed.
- Proxy/headless: 5 additional selected cases passed; only upstream WebSocket
  deprecation warnings remained. No unawaited authorization warnings remain.
- Components: 52 cases passed across five retained browser component files.
- Production UI: `npm --prefix ui run build` passed; existing chunk-size and
  mixed-import warnings remain.
- LAN Playwright: 16 cases passed: the two browser harness journeys in desktop
  Chromium (1440/1024), emulated Android Chromium (320/390/430), and emulated
  iPhone WebKit (320/390/430). Each uses disposable authenticated Core on
  `192.168.1.155` with an ephemeral port and synthetic local targets.
- Product knowledge rebuilt: 427 passages; current source digest begins
  `4a963892a5d0`. Core/Chroma source-digest synchronization and stale-index
  replacement passed against isolated storage.
- Ruff and diff whitespace checks passed.

Local command logs: `/tmp/browser-python-final.log`,
`/tmp/browser-proxy-final.log`, `/tmp/browser-components-final.log`,
`/tmp/browser-e2e-final.log`, `/tmp/browser-build.log`.

No production deployment or physical-device validation was performed. Desktop
Tauri execution was not exercised in this Linux browser matrix; retained native
transport is unchanged. Test fixtures close their owned Core/browser processes.
Two failed early launcher fixtures left read-only runtime directories; those
exact temporary directories were removed after confirming no owner remained.

## Preservation repairs found during testing

Independent troubleshooting confirmed pre-existing missing async boundaries in
browser evidence graph reads and live proxy authorization, a cancellation window
before approval polling, and concurrent revocation conflicts. Narrow repairs are
covered by the retained browser tests. Obsolete synchronous test doubles and
single-event-loop-tick assumptions were corrected. The LAN fixture runs candidate
Core directly and retains normal pairing, avoiding an installed runtime launcher.

## CI selection

`.github/test-selection.json` selects only the 127 Python cases, 52 component
cases, and `area:browser-harness`. The latter resolves to eight jobs, each running
only the two `browser harness` cases. No full suite, unrelated native tests, or
release/deployment action is selected. Python 3.12 is selected for CI because the
pinned mitmproxy sidecar requires it; local Core tests used Python 3.11 with a
separate Python 3.12 sidecar.

The operator explicitly authorized skipping the public-commit guard for this
change. Use its documented bypass tied to this exact staged Git tree only.
