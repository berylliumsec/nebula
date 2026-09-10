# Coordinated local release runbook

This is a gated procedure, not permission to interrupt live work. The acceptance
ledger must contain passing evidence for all selected release journeys before
installation. A source build or extracted-package launch is not an installed
desktop acceptance result. Physical devices remain explicitly unverified.

## Prepare one candidate

Use a clean checkout of the final reviewed commit. Keep live service configuration,
data, workspaces and runtime policies out of the build directory. Record an exact
40-character commit, one UTC build timestamp and the Rust target triple. Export
those as `NEBULA_BUILD_COMMIT`, `NEBULA_BUILD_TIMESTAMP` and
`NEBULA_BUILD_TARGET` for **every** build process, with
`NEBULA_DISTRIBUTION_CHANNEL=managed`.

1. Run the reviewed, collected test selections in `.github/test-selection.json`.
   Validate its digest against the final diff using `scripts/test_selection.py`.
   For candidate browser runs, retain successful traces too: `--trace=on` and
   `--reporter=list,json`, with a distinct `--output` directory and captured
   stdout receipt per bounded batch. The default failure-only reporter discards
   successful screenshot attachments. Inspect retained screenshots as well as
   geometry assertions; a bounded row may still clip its own controls.
2. Run `npm --prefix ui run build`, then `python -m scripts.build_nebula_core`.
3. Run `python scripts/verify_stabilization_build.py --web ui/dist
   --core-identity build/nebula-core-metadata/BUILD_INFO.json --commit <commit>`.
   It rejects dirty/mixed builds, missing/extra assets and changed service workers.
4. Stage the locked browser runtime with `python -m scripts.stage_playwright_runtime
   ui/src-tauri/resources/playwright-browsers --target <target>`.
5. From `ui`, build the managed DEB with `npm exec -- tauri build --ci
   --target <target> --bundles deb --config src-tauri/tauri.managed.conf.json`.
   Keep the same identity environment during Tauri's frontend rebuild; repeat the
   integrity check afterward. Retain the DEB, its SHA-256, Core build metadata,
   browser manifest and `web-build.json` in an immutable candidate directory.
6. Extract that DEB into a new temporary directory for pre-install testing. Run
   the packaged WebDriver acceptance infrastructure against this exact binary,
   using isolated XDG data/config/cache directories and a virtual display where
   necessary. Exercise approval, reconnect, fullscreen, scrolling and relaunch;
   the existing browser-only smoke does not prove this entire gate.
7. Run the package's Core on a separate LAN staging port with a disposable data
   directory and its **embedded** web assets. Do not point it at the live DB or
   override `--static-dir` with assets from another build. Repeat the ledger
   walkthrough and configured-runtime smoke checks. No silent test skips.

   Use `nebula-core ui --no-browser --host 0.0.0.0 --port <staging-port>
   --data-dir <temporary-data> --lan --allow-insecure-lan` on the trusted test
   LAN. `serve` without `--static-dir` is intentionally API-only, not an embedded
   frontend check. Do not disable authentication; keep the generated test token
   private. For the permanent model/reset projects, the bounded launcher
   `scripts/stabilization_model_candidate.py --core <candidate-core>
   --port <staging-port> --auth-file <new-private-file>` is selected with
   `NEBULA_MODEL_TEST_COMMAND`; set `NEBULA_MODEL_TEST_AUTH_FILE` to that file.
   It launches the unmodified compiled Core, removes frontend overrides and adds
   no test routes. Only `application-model-responsive.spec.ts` and
   `application-model-reset.spec.ts` use this candidate mode; tests requiring
   custom fixture routes still use the separately identified source fixture.

## Maintenance handoff and installation

Only proceed after an explicit maintenance handoff confirms active work may be
interrupted. Record the live Core unit, all drop-ins, executable, package version,
data directory and configured origin with read-only checks; do not guess paths.
Retain the exact previous DEB and server artifacts. Back up configuration and data
with restrictive permissions, excluding linked workspaces from reset operations.

Stop writers during a coordinated maintenance window before taking a consistent
database/artifact/configuration backup. A raw copy of a live SQLite file without
its WAL is not a consistent backup. Record the backup location and verification.
Install the candidate desktop package and switch the LAN service to the same
candidate Core. Preserve service options and runtime policies. Never copy just
`ui/dist` into an older installation.

## Accept or roll back

Launch the installed application and verify Diagnostics against the running Core,
the web manifest and the retained artifact identities. Verify the served asset
hashes, not merely an HTTP 200 response. Perform a final disposable approval to
completion, reconnect and relaunch check on the installed desktop and LAN UI.

If any gate fails, stop the candidate writers, restore the recorded previous
package/artifacts and service configuration, and restore the coherent data backup
if migration compatibility requires it. Never downgrade a database speculatively.
Retain failure evidence and get operator direction before discarding new data.
Report the exact installed identities, rollback locations and remaining unverified
gates. Do not describe the release as complete while any required gate is missing.
