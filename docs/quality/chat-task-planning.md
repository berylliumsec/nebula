# Chat task planning acceptance

| Journey step | Observable invariant | State authority | Selected evidence |
| --- | --- | --- | --- |
| Entry | A saved chat exposes Plan before acting, off by default; Plan only is distinct and incompatible on one turn. | Chat assistant config and request snapshot | Frontend transport tests, real-Core browser journey |
| Discovery | Core offers only bounded workspace reads, filename discovery, catalog reads, and plan submission. No execution tool runs first. | Core planning service and workspace resolver | `tests/v3/test_chat_planning.py` |
| Submit and use | A plan names expected outcomes, exact selected tools, native categories, and verified relative file paths. Core gates provider dispatch, batches, harness gateway calls, and native permission requests. | Saved `ChatTaskPlan` and the existing policy broker | `tests/v3/test_chat_planning.py` |
| Delegate | A child has its own plan; Butler can save the chat switch or force one exact task with a current revision and a recorded reason. Queued turns plan before starting; running harness turns defer to the next turn. | Core child turns, Butler action and directive records | `tests/v3/test_chat_planning.py`, `tests/v3/test_butler_authority.py` |
| Refresh and failure | Plans and directives reload from Core. Unsupported harness configurations show a durable blocked reason and do not connect to their execution adapter. | Core task-plan and directive API | Real-Core browser journey across eight profiles |
| Successful execution | A blocked task uses linked retry after the saved harness changes to a managed adapter. The replacement turn discovers a file, submits its own verified plan, executes the declared tool, and shows the saved plan after refresh. | Distinct Core plan records, tool receipt, chat history | Real-Core browser journey across eight profiles and focused linked-retry test |
| Product help | The settings assistant discovers the switch and Butler directive, and the bundled source digest synchronizes to Chroma. | Settings catalog, destinations, product-help index | `tests/v3/test_product_knowledge.py` |

Selected local commands and results:

- `poetry run pytest -q tests/v3/test_chat_planning.py tests/v3/test_butler_authority.py tests/v3/test_product_knowledge.py` — 37 passed.
- `npm --prefix ui test -- src/api/chatPlanning.test.ts src/components/ButlerOversight.test.tsx src/api/butlerOverview.test.ts src/assistantDestinations.test.ts src/api/client.test.ts` — 118 passed.
- `npm --prefix ui run build` — production bundle built.
- `poetry run python scripts/build_product_knowledge.py --verify` — 218 passages, source digest `0bff34280cd7`.
- `npm --prefix ui run test:e2e -- tests/real-core.spec.ts --project=assistant-real-desktop --project=assistant-real-compact --project=assistant-real-chromium-320 --project=assistant-real-chromium-390 --project=assistant-real-chromium-430 --project=assistant-real-webkit-320 --project=assistant-real-webkit-390 --project=assistant-real-webkit-430 --grep 'assistant planning'` — 16 passed (eight blocked and eight managed execution journeys).

The final browser run against the rebased candidate started disposable Core instances on the host's non-loopback LAN address with temporary synthetic data, normal device pairing and authentication, and the candidate production bundle. Each viewport got its own ephemeral port. Its JSON report recorded these origins (blocked / managed execution):

| Browser profile | Blocked Core origin | Execution Core origin |
| --- | --- | --- |
| Desktop Chromium | `http://192.168.1.155:42713` | `http://192.168.1.155:34277` |
| Compact Chromium | `http://192.168.1.155:33257` | `http://192.168.1.155:38551` |
| Mobile Chromium 320 | `http://192.168.1.155:46667` | `http://192.168.1.155:41445` |
| Mobile WebKit 320 | `http://192.168.1.155:33859` | `http://192.168.1.155:33833` |
| Mobile Chromium 390 | `http://192.168.1.155:34023` | `http://192.168.1.155:43079` |
| Mobile WebKit 390 | `http://192.168.1.155:42335` | `http://192.168.1.155:34973` |
| Mobile Chromium 430 | `http://192.168.1.155:40487` | `http://192.168.1.155:43359` |
| Mobile WebKit 430 | `http://192.168.1.155:35759` | `http://192.168.1.155:45629` |

The journeys verify the bundle asset hash, the saved switch, blocked unsupported harness behavior without execution receipts, a same-chat linked retry after changing the saved harness, managed file discovery, a distinct plan submission, declared read, Core read-back, refresh, accessibility, and responsive settings geometry. The fixture stopped Core and removed its temporary data. Mobile Chromium and WebKit are emulated browser profiles; a physical device was not run. No external provider or real project was used.

## Managed Codex formal-plan acceptance, 2026-10-19

The prior gate rejected Codex before opening its restricted planning session, so
Codex could not call `plan.submit`. Managed MCP-capable Codex now uses the same
Core plan submission and saved receipt as managed Claude. Planned Codex turns
expose the guarded Core gateway in a private scratch workspace with read-only
vendor sandboxing and native execution features disabled. A later ordinary turn
restores the session's native feature selection. Delegated children receive the
main plan's steps, expected outcomes, tools, files, and unresolved dependencies.

- Focused Python selection: 11 cases passed after correcting a synthetic test
  record; the Codex start and resume requests also validate against the pinned
  app-server schemas. The selection includes the saved-effort goal-resume case
  after main added that behavior. Ruff check and format, mypy, and bundled
  product-help digest verification passed. The Chroma source-digest
  synchronization check passed with the selected Python cases.
- Frontend unit selection: 4 cases passed in two files. The candidate production
  bundle built successfully.
- Real Core browser selection: 24 cases passed on desktop Chromium at 1440 and
  1024 px, emulated mobile Chromium at 320, 390, and 430 px, and emulated mobile
  WebKit at 320, 390, and 430 px. Each case used normal pairing against a
  disposable Core on a non-loopback LAN origin with synthetic project data and
  an inert harness. Blocked, linked retry, saved execution, refresh, responsive
  geometry, and accessibility checks were exercised.
- A repeated desktop Codex case captured `http://192.168.1.155:42953` with the
  production bundle, 1440×900 viewport, and saved plan phase `execution`.
  The fixture stopped and removed its temporary data afterward.

These synthetic checks verify Core and UI behavior. They do not establish that
an external Codex model will choose to submit a suitable plan for every task.
No physical device was used.
