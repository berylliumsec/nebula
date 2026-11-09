# Focused native desktop continuity CI recipe

This is a recipe for a dedicated root-owned validation job. It does not dispatch
the full-suite CI workflow or a release/publish workflow. Run exactly
`tests/v3/test_packaged_runtime_continuity.py` once, after collecting exactly one
case, with no retries. The repaired driver/artifact helper starts at
`6e7094f4f8dfb98d78a0bca2029bbaf6bd7ec459` and must be included in the final
candidate source. Source, frozen-framework and native-UI evidence remain separate.

## Inputs and dependency boundary

- Baseline source: `557ffd7d316a272283ed7064496d38baadb69ffe`, published by root on
  its baseline branch so Actions can check out the object.
- Candidate product source: `4a39c121975bcdad01f1cbddc1c94b934ef6dc15`, or
  root's subsequent CI-only commit preserving those exact product inputs.
- Require two distinct full 40-character commit SHAs and actual product-code
  differences under `src/nebula/v3`, `ui/src` or `ui/src-tauri/src`. Do not count
  a timestamp-only rebuild as a code update.
- Use one Ubuntu 24.04 runner, Python 3.12, Node 20, the pinned Rust toolchain
  action already used by this repository, and ordinary official dependency
  installation. No existing Core, operator profile, provider or deployment.
- Scope the job to building two artifacts and one GUI journey. No broad pytest,
  frontend, Rust or Playwright suite; no release tag, signing or publication.

Install the native dependencies on the ephemeral runner:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential pkg-config libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
  libayatana-appindicator3-dev libssl-dev libdbus-1-dev libxdo-dev patchelf \
  xvfb xauth x11-utils xdotool dbus-x11 at-spi2-core python3-pyatspi \
  python3-venv fonts-dejavu-core xfonts-base
```

Install `poetry==2.1.3` as in the canonical release workflow. Create ordinary
project-local Poetry environments for **builds**, using the repository's normal
`poetry install --with dev --no-interaction` boundary and `npm --prefix ui ci`.
Use each source's tracked lock files and record their checksums. Do not substitute
an unrelated checkout's lock or notices.

Keep the GUI runner separate from the Core build environments: create an owned
`/usr/bin/python3 -m venv --system-site-packages` environment so the official
PyAT-SPI/GI bindings are visible. Install only its `pytest`, `httpx` and
`pyinstaller` dependencies, constrained to versions from the final candidate's
resolved Poetry environment. Record those constraints. This avoids adding system
GUI Python modules to the frozen headless Core build boundary.

## Build each pinned source

Use separate checkout/output directories (or preserve the first immutable bundle
and package before reusing a Cargo target cache). Set `NEBULA_BUILD_COMMIT` to
that checkout's actual full SHA, target `x86_64-unknown-linux-gnu`, and a truthful
QA/managed distribution channel. Retain the exact command and complete build log.

Export `NEBULA_BUILD_COMMIT`, `NEBULA_BUILD_TARGET` and the selected distribution
channel for all build commands, including frontend and native compilation. Use
the actual pinned SHA, rather than the workflow merge SHA, for each checkout.

Run the genuine preparation helpers **before** constructing the retained Core
bundle. Text fixtures, empty directories, different browsers with relabelled
metadata, and fabricated notices cannot satisfy this step.

```sh
poetry run python -m scripts.stage_playwright_runtime \
  ui/src-tauri/resources/playwright-browsers --target x86_64-unknown-linux-gnu
poetry run python -m scripts.stage_notebook_runtime \
  ui/src-tauri/resources/notebook-runtime --target x86_64-unknown-linux-gnu
npm --prefix ui run build
NEBULA_BUILD_TARGET=x86_64-unknown-linux-gnu \
  NEBULA_DISTRIBUTION_CHANNEL=qa \
  poetry run python -m scripts.build_nebula_core
```

The Core builder generates real dependency notices, a public onefile connector
and an independently retained onedir runtime with all dependencies/resources
hashed. Capture its JSON result. The connector hash intentionally differs from
the retained executable hash; audit both using the final candidate audit helper.

Create an external Tauri config containing only:

```json
{"build":{"beforeBuildCommand":""}}
```

This prevents a second frontend build from changing the accepted UI after Core's
retained manifest was assembled. From that checkout's `ui/` directory run:

```sh
NO_STRIP=1 npm run tauri -- build --ci --target x86_64-unknown-linux-gnu \
  --bundles deb --config "$skip_ui_config" -- --locked
```

The repository has no `pretauri` script at the reviewed pins; the explicit Core
build above is required. `NO_STRIP=1` preserves hashes of retained binaries.
Do not supply release signing keys or fake signing/notices to this QA job.

Require exactly one real `.deb` in that checkout's Cargo bundle output. Extract
it with `dpkg-deb --extract` into an owned per-build directory and run the final
candidate's `scripts/package_audit.py --tree <extracted-root>`. Verify the complete
retained manifest after packaging, not just the outer native container.

Expected paths, to be confirmed against the actual extracted package:

```text
<extract>/usr/bin/nebula-ui
<extract>/usr/bin/nebula-core
<extract>/usr/lib/Nebula/nebula-runtime
<extract>/usr/lib/Nebula/{playwright-browsers,notebook-runtime,licenses,...}
```

Tauri 2.9.3's Linux resource resolver first canonicalizes
`${exe_dir}/../lib/${package_info.name}`. Thus the genuine extracted `usr/bin`
and `usr/lib/Nebula` layout can be exercised without an APPDIR override or a
production test hook. Label this accurately as execution of extracted native
packages. It does not establish that an OS package-manager update was performed.

## Produce strict native provenance and inputs

Use the final candidate's helper for both artifacts. It reads the source Git
commit/configured frontendDist and actual built bytes; it does not relabel a
different source or substitute a scripted desktop.

```sh
"$candidate_python" "$candidate_source/tests/v3/packaged_continuity_artifacts.py" record-build \
  --source "$source_root" --source-commit "$full_source_sha" \
  --desktop "$extract/usr/bin/nebula-ui" \
  --connector "$source_root/dist/nebula-core" \
  --resources "$extract/usr/lib/Nebula" \
  --build-command "$evidence/build-command.json" \
  --build-log "$evidence/build.log" --output "$evidence/native-build.json"
```

The `nebula.native-build/v1` receipt binds the desktop, connector, complete
retained manifest, actual frontend input, source commit and resource directory.
Generate inputs with the same helper's `write-inputs --baseline <receipt>
--candidate <receipt> --output <inputs.json>`, optionally supplying only an
explicit public MiniLM cache via `--public-model-cache`. A primed public model
cache avoids unrelated first-use model downloads; never copy a real HOME/profile.

## Collect and run the single journey

Use the final candidate source on PYTHONPATH and the GUI runner environment.
Write a separate reviewed one-case selection receipt for this supplemental native
journey; root's combined source-test receipt remains independent. Then:

```sh
NEBULA_TEST_PACKAGED_CONTINUITY_INPUTS="$inputs_json" \
  PYTHONPATH="$candidate_source" "$gui_python" -m pytest --collect-only -q \
  "$candidate_source/tests/v3/test_packaged_runtime_continuity.py"
```

Require exactly one collected test. Run once with an outer 900-second deadline,
an owned temporary data directory, normal pairing/authentication and no external
provider. For example:

```sh
env -u NO_AT_BRIDGE GTK_MODULES=atk-bridge \
  NEBULA_TEST_PACKAGED_CONTINUITY_INPUTS="$inputs_json" \
  PYTHONPATH="$candidate_source" \
  timeout 900s xvfb-run -a -s '-screen 0 1440x900x24 -nolisten tcp' \
  dbus-run-session -- "$gui_python" -m pytest -q \
  "$candidate_source/tests/v3/test_packaged_runtime_continuity.py" \
  --basetemp="$native_private_temp" --junitxml="$evidence/native-junit.xml"
```

The fixture uses mapped X11 window/PID/birth identity, real `WM_DELETE_WINDOW`,
AT-SPI product controls and composer/document readback. It must observe the native
desktop creating the baseline connection before CLI attachment; a CLI-only
startup, SIGTERM or `--self-test` cannot stand in for GUI-close evidence. The held
inert provider turn must remain active through close, candidate promotion/reopen,
rollback and baseline GUI reopen. The final result requires one producer, stable
Core/principal/session identity, preserved native draft, candidate-era SQL, exact
registered-worker exit and `observed_state=stopped` on explicit shutdown.

## Bound CI scratch use

Record `df -h` before each build and before the GUI journey. Build sequentially
in a job-owned scratch root and keep one shared Cargo target directory with
`CARGO_BUILD_JOBS=2` and `CARGO_INCREMENTAL=0`; do not duplicate the Rust target
cache for baseline and candidate. Finish extraction, full package audit and the
`record-build` receipt before reclaiming that build's intermediates.

Preserve both complete extracted package graphs unchanged, both source Git
checkouts and frontend inputs, all manifests/notices, lock checksums and command
logs. After their hashes are recorded, redundant copies under that build's
`dist/`, PyInstaller work/spec outputs and `build/nebula-core-stage` can be
removed from the **owned build scratch only**. The same applies to generated
payloads in `ui/src-tauri/resources/{nebula-runtime,notebook-runtime,playwright-browsers}`,
the generated `ui/src-tauri/binaries/nebula-core-<target>`, and the compressed DEB.
Preserve all tracked source files, including resource `.gitignore` files, and
`build/nebula-core-metadata` notices/build identity. The extracted
`usr/bin/nebula-core` is the receipt's connector
path; the journey does not need the original `dist/nebula-core` after recording.
Keep metadata and provenance outside those cleanup paths. Remove the baseline
build virtualenv and Node dependencies after its package/receipt is accepted;
retain the separate GUI environment and candidate helper environment.

Before the journey, the now-unused shared Cargo target and download/package
caches can also be removed from their task-owned paths. Never prune system
directories, a caller's cache, extracted packages, driver private data or failed
runtime state. Reserve space for the test's retained baseline and candidate
release copies as well as both extracted packages. If the measured remaining
space is inadequate, report the resource blocker; do not drop real resources or
silently use a single bundle for both builds.

## Evidence and cleanup

Upload explicit build/provenance/test receipts, dependency/lock checksums and
sanitized native/runtime logs. Do not upload the whole temporary tree: when
shutdown is unconfirmed the driver deliberately retains private Core/HOME data
containing ephemeral authentication material. A failing or unavailable GUI gate
stays incomplete; do not replace it with the already-passing frozen Core receipt.
Report Xvfb/container emulation and absence of physical desktop input accurately.

Local preparation evidence is separate: the owned Debian fixture verified glibc,
Rust, GTK/WebKit dependencies and 6 GiB/zero-swap/2-CPU limits. Its first build
stopped at a missing genuine notebook resource. The canonical Chromium CDN then
returned a proxy CONNECT 403. No browser payload or native GUI result was obtained
locally; the controlled CI job is required to complete this gate.

This recipe changes documentation only. No product tests or disposable LAN Core
run are required to verify its text; native execution remains a separate pending
CI acceptance gate. Commands and CLI flags were checked against the pinned
preparation/build helpers, canonical release workflow and repaired driver.
