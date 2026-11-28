# Local test policy

Nebula has no CI test workflows. No pull request, push, tag, release, or deployment
waits on a CI test run or a committed test-selection receipt. Tests run locally, at
the discretion of the agent or human who makes the change, before they open and
admin-merge the pull request.

## Choosing what to run

Pick the tests that prove the changed journeys and integration boundaries: individual
Python/frontend files, exact Rust test names, and the Playwright specs and projects for
the affected journeys. Prefer focused runs. A full suite is rarely worth its cost, and
a general request to "test it" or "merge it" does not by itself call for one.

In the pull request description, say what you ran, the outcome, and what you chose not
to run. Do not describe an untested workflow as verified.

Focused local examples:

```sh
poetry run pytest -q tests/test_daily_stable_release.py
npm --prefix ui test -- src/api/runtimeDefaults.test.ts
npm --prefix ui run test:e2e -- tests/interface.spec.ts --project=mobile-webkit --grep 'configured harness default'
```

For setup tests, you can invoke pytest through
`python scripts/run_pytest_thread_guard.py -- <pytest arguments>`. The opt-in setup
thread check reports newly leaked non-daemon threads; the wrapper bounds interpreter
shutdown after that report and preserves failure.

The npm entry points reject unscoped runs, and Playwright's configuration also guards
direct `npx playwright test`. For a deliberate local full UI run, set both
`NEBULA_FULL_SUITE_APPROVAL=RUN_FULL_SUITE` and `NEBULA_FULL_SUITE_REASON` for that one
command. Do not persist these variables in agent profiles. The guards prevent
accidental full runs; they are not a security boundary.

## Releases

Release preparation has three jobs: validate the tagged commit, build the DEB and
AppImage, and perform one Ubuntu 24.04 clean install with one desktop Chromium case
against the installed Core and embedded production UI. That installed-Core smoke is
part of the release build, not a CI test gate. Its passing receipt must match the tag,
commit, and installed DEB hash before a draft can be created or a daily release
published. Packaging unit tests, sandbox integration, crash cleanup, negative
signature checks, and Debian/Kali installs run locally when the release manager
chooses to run them.

## Focused Core fixture journeys

`area:side-chat-close` selects eight production-LAN profiles for closing a side
chat on an execution-fenced Core. The inert fixture uses normal browser pairing
and verifies refresh, durable deletion, duplicate close, main-chat protection,
and preserved parent history without provider or harness traffic. Pair this
journey with the activity and isolation guards in `tests/v3/test_execution_scope.py`.

The following areas each have one
case in each of the eight permanent `model-*` projects (desktop 1440/1024 and
Chromium/WebKit mobile 320/390/430). Select only journeys affected by the change.

| Area / spec basename | Fixture configuration |
| --- | --- |
| `assistant-browser-viewer`, `working-knowledge` | `playwright.application-model.config.ts` |
| `host-mode` | `playwright.host-mode.config.ts` |
| `project-deletion` | `playwright.project-deletion.config.ts` |
| `workspace-quota` | `playwright.workspace-quota.config.ts` |
| `workspace-recovery` | `playwright.workspace-recovery.config.ts` |

The guarded default configuration routes an exact spec filename to its fixture. Incompatible fixture files in a
single invocation are rejected instead of silently running against the wrong
server. For example, collect before running:

```sh
npm --prefix ui run test:e2e -- tests/host-mode.spec.ts --project=model-desktop --list
NEBULA_TEST_PYTHON='poetry run python' npm --prefix ui run test:e2e -- tests/host-mode.spec.ts --project=model-desktop
```

Build the candidate production UI before browser execution. Fixtures own temporary
Core databases and workspaces; browser pairing remains enabled. Core owns saved
objects and read state; the URL owns navigation identity. Existing journey
assertions cover read-state retry, linked-folder deletion safety, host policy
rollover, knowledge navigation, large-workspace uploads, saved failure recovery,
and read-only browser input/reconnect. This wiring does not add product features
or claim live-provider, physical-device, or installed-desktop coverage.

Supply the Poetry interpreter (`NEBULA_TEST_PYTHON`) and stage managed Chromium for
the browser viewer even when its viewing browser is WebKit. The quota configuration enables
the large-workspace fixture explicitly; host-mode uses its own port. Required
results must distinguish collection from execution and report unavailable gates.
