# Chat task planning acceptance

| Journey step | Observable invariant | State authority | Selected evidence |
| --- | --- | --- | --- |
| Entry | A saved chat exposes Plan before acting, off by default; Plan only is distinct and incompatible on one turn. | Chat assistant config and request snapshot | Frontend transport tests, real-Core browser journey |
| Discovery | Core offers only bounded workspace reads, filename discovery, catalog reads, and plan submission. No execution tool runs first. | Core planning service and workspace resolver | `tests/v3/test_chat_planning.py` |
| Submit and use | A plan names expected outcomes, exact selected tools, native categories, and verified relative file paths. Core gates provider dispatch, batches, harness gateway calls, and native permission requests. | Saved `ChatTaskPlan` and the existing policy broker | `tests/v3/test_chat_planning.py` |
| Delegate | A child has its own plan; Butler can save the chat switch or force one exact task with a current revision and a recorded reason. Queued turns plan before starting; running harness turns defer to the next turn. | Core child turns, Butler action and directive records | `tests/v3/test_chat_planning.py`, `tests/v3/test_butler_authority.py` |
| Refresh and failure | Plans and directives reload from Core. Unsupported harness configurations show a durable blocked reason and do not connect to their execution adapter. | Core task-plan and directive API | Real-Core browser journey across eight profiles |
| Successful execution | A managed harness discovers a file, submits a verified plan, then executes the declared tool and shows the saved plan after refresh. | Core plan record, tool receipt, chat history | Real-Core browser journey across eight profiles |
| Product help | The settings assistant discovers the switch and Butler directive, and the bundled source digest synchronizes to Chroma. | Settings catalog, destinations, product-help index | `tests/v3/test_product_knowledge.py` |

Selected local commands and results:

- `poetry run pytest -q tests/v3/test_chat_planning.py tests/v3/test_butler_authority.py tests/v3/test_product_knowledge.py` — 34 passed.
- `npm --prefix ui test -- src/api/chatPlanning.test.ts src/components/ButlerOversight.test.tsx src/api/butlerOverview.test.ts src/assistantDestinations.test.ts src/api/client.test.ts` — 118 passed.
- `npm --prefix ui run build` — production bundle built.
- `poetry run python scripts/build_product_knowledge.py --verify` — 218 passages, source digest `0bff34280cd7`.
- `npm --prefix ui run test:e2e -- tests/real-core.spec.ts --project=assistant-real-desktop --project=assistant-real-compact --project=assistant-real-chromium-320 --project=assistant-real-chromium-390 --project=assistant-real-chromium-430 --project=assistant-real-webkit-320 --project=assistant-real-webkit-390 --project=assistant-real-webkit-430 --grep 'assistant planning'` — 16 passed (eight blocked and eight managed execution journeys).

The browser journeys start disposable Core instances on the host's non-loopback LAN address with temporary synthetic data, normal device pairing and authentication, and the candidate production bundle. The blocked desktop run used `http://192.168.1.155:33329`; the managed execution desktop run used `http://192.168.1.155:44747`. Each viewport got its own ephemeral port. They verify the bundle asset hash, the saved switch, blocked unsupported harness behavior without execution receipts, and managed file discovery, plan submission, declared read, Core read-back, refresh, accessibility, and responsive settings geometry. The fixture stops Core and removes its temporary data. Mobile Chromium and WebKit are emulated browser profiles; a physical device was not run. No external provider or real project was used.
