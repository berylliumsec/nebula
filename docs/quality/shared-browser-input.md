# Shared browser manual input recovery

The shared Chromium surface supports operator clicks/touch and keyboard input.
The displayed frame is an image; input travels through the authenticated Core
WebSocket to the host Chromium CDP session. It is not an AI-only viewer.

## Observed live failure

On 2026-09-09, the server stopped answering HTTP as well as browser input.
Read-only process stack inspection showed the main thread waiting in
`secretstorage.util.exec_prompt`, reached via `delete_vpn_profile` →
`CredentialStore.delete` → `keyring.delete_password`. After restarting, the same
wait recurred through `create_vpn_profile` → `CredentialStore.create` →
`keyring.set_password`. Both waited for an interactive locked-vault prompt.

`3b773eb` prevents Linux vault save/delete from invoking interactive unlock or
confirmation prompts and moves credential API mutations off the event loop.
Locked vaults fail with host-unlock/session-only recovery. A failed VPN credential
deletion preserves its profile for retry; stale revision checks run before vault
mutation. Existing authentication, browser scope, approval and credential-storage
boundaries remain enforced.

## Contract and evidence

Journey: project Workbench → shared browser → navigate to a local authorized
fixture → click/tap → type → reconnect → click again. Core owns session and tab
identity, Chromium owns page state, and the UI owns only the displayed frame and
unsent address. Successful manual input must reach the same page, with no AI turn.
Disconnected/reconnecting frames are not proof that a new connection is ready.
Vault failures must leave the server responsive and explain recovery.

`tests/v3/test_credentials.py` plus
`tests/v3/test_automation_runtime.py::test_api_saves_vpn_secret_outside_public_profile_and_selects_it`:
17 passed, including locked/confirmation/ready vault cases, API responsiveness,
failed-delete retention and retry. Credential source/tests passed focused Ruff.

`ui/tests/shared-browser-input.spec.ts` is a permanent real-Core browser journey
in `ui/playwright.application-model.config.ts`, with desktop Chromium 1440/1024
and emulated Android Chromium/iPhone WebKit 320/390/430. The test clicks the image
surface and verifies the host page content through Core; no input or browser
operation is mocked. It uses the production bundle at LAN origin
`http://192.168.1.155:19568`, the verified packaged Chromium runtime, and an
explicitly scoped local fixture page. Physical devices and software keyboards
are not verified; no third-party browsing or provider calls occur.

All eight profiles passed: five in the initial completed run and three after
waiting for reconnect readiness before the next click. Artifacts are retained in
`/tmp/nebula-shared-browser-input-final` and
`/tmp/nebula-shared-browser-input-reconnect`. Desktop screenshots visibly show
the host button reaching `Clicked 2`; typing is asserted against host page content.

Rollout: branch pushed; live Core reports `3b773eb`, status `ok`. Online and
stopped-service database backups passed integrity checks, including
`/home/agent/.local/share/nebula/v3/backups/pre-3b773eb-stopped.db`.
The live bundle includes the Model expansion controls. Existing browser views
may need Reconnect after the Core restart.
