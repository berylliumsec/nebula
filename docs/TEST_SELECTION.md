# Focused test policy

Full suites should almost never run. A general request to test, release, or merge
does not approve one. Failures, shared-file changes, absent baselines, and uncertain
coverage require diagnosis or selection review, never automatic full expansion.

## Routine changes

1. Identify affected operator journeys and integration boundaries. Select individual
   Python/frontend files, exact Rust test names, and catalogued Playwright entries.
   Keep required browser/device coverage for these journeys, not unrelated features.
2. Collect/list those tests and tell the user the expected count, runtime boundary,
   rationale, and exclusions before execution. List-only operations are not full runs.
3. Stage new source/test files, then obtain a diff digest:

   ```sh
   python scripts/test_selection.py --baseline origin/main --candidate WORKTREE --digest
   ```

   The digest binds that diff alone. It pins git's object-name abbreviation length,
   which git otherwise scales to how many objects the local clone holds, so the same
   baseline and candidate hash identically in a working clone and in CI's fresh one.

4. Write `.github/test-selection.json` with `change_digest`, `reviewed_by`, `reason`,
   `exclusions`, `expected_tests`, and the lists `python`, `frontend`, `native`,
   `playwright`. Empty lists explicitly mean no relevant tests at that layer;
   explain them in `exclusions`. Frontend paths start with `ui/src/`; Rust names
   must be exact `module::test` names. Playwright selectors come from:

   ```sh
   python scripts/playwright_impact.py catalog --manifest ui/playwright-impact.json
   ```

   `expected_tests` records counts per layer. Python defaults to 3.12; opt into
   other supported versions with `python_versions` only when needed. Set
   `python_browser: true` only when the selected Python tests require Chromium.
   `python_node: true` requests Node for cross-language guard/tool tests.

5. Validate the plan, commit it with the change, and review CI's selection receipts.
   Changing the diff or base invalidates the digest. Update and review the selection
   again; do not merely refresh its digest without considering coverage.

CI validates the plan before test jobs start. It does not run tests for omitted
layers, and never defaults empty target arrays to whole suites. Failed selection
validation blocks the PR rather than pretending that zero tests proves coverage.
The plan is human/agent judgment, not cryptographic proof of adequate coverage.
Normal code review must assess its rationale; an agent must not invent approval.
Existing branch-protection Python check names remain present: unselected versions
only report their omission without installing or executing tests. Invalid selection
makes these checks fail, so skipped test layers cannot hide a missing review.

Focused local examples:

```sh
poetry run pytest -q tests/test_playwright_impact.py tests/test_test_selection.py
npm --prefix ui test -- src/api/runtimeDefaults.test.ts
npm --prefix ui run test:e2e -- tests/interface.spec.ts --project=mobile-webkit --grep 'configured harness default'
```

Pytest and the npm entry points reject unscoped runs; Playwright's configuration also guards
direct `npx playwright test`. Real-Core no longer implicitly runs other projects.
These guards prevent accidents, not a determined actor editing/bypassing the tools.

## Releases and the exceptional full run

Release tags do not authorize full coverage. Preparation has exactly three jobs:
validate the tagged commit, build the DEB and AppImage, and perform one Ubuntu 24.04
clean install with one desktop Chromium case against the installed Core and embedded
production UI. The case is collected as exactly one test before execution and has
zero retries. Its passing receipt must match the tag, commit, and installed DEB hash
before a draft can be created or a daily release published.

The release-only Playwright impact matrix and baseline are gone. The PR Playwright
selector and its diff-bound review remain in place, with the `gate` check required
by main branch protection. Packaging unit tests, redundant
compile checks, sandbox integration, crash cleanup, negative signature testing,
and Debian/Kali installs are optional manual diagnostics in
`nebula3-release-diagnostics.yml`; the existing manual Playwright selector remains
available. The daily schedule remains disabled until separately authorized after
a candidate run. This release gate claims only artifact integrity, installation,
and the stated core journey; it does not claim broad UI coverage.

Only after a fresh explicit user approval, manually dispatch Playwright with
`scope=full`, `full_approval=RUN_FULL_SUITE`, and a nonempty `review_reason` recording
who approved it and why focused coverage is insufficient. PRs and tag pushes cannot
authorize this mode. Full selection via a union of project selectors is rejected.
No scheduled, push-triggered, or retry-triggered full-suite execution is permitted.

For an explicitly approved local full UI run, both `NEBULA_FULL_SUITE_APPROVAL`
(`RUN_FULL_SUITE`) and `NEBULA_FULL_SUITE_REASON` must be set for that one command.
Do not persist these variables in agent profiles or CI defaults. Backend full runs
likewise need explicit user approval; ordinary CI only accepts individual files.
