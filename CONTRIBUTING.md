# Contributing to Nebula

Thanks for helping improve Nebula. Documentation fixes, clear bug reports,
accessibility improvements, tests, and focused code changes are welcome.
Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Use Nebula only on systems and networks you own or are explicitly authorized to
test. Use synthetic data and disposable local fixtures in examples and tests.

## Choose the right place

- **Questions and setup help:** read [Support](SUPPORT.md), then use
  [Discussions](https://github.com/berylliumsec/nebula/discussions).
- **Bugs, documentation gaps, and concrete feature proposals:** search existing
  [issues](https://github.com/berylliumsec/nebula/issues) and
  [pull requests](https://github.com/berylliumsec/nebula/pulls), then use the
  [issue templates](https://github.com/berylliumsec/nebula/issues/new/choose).
- **Suspected vulnerabilities in Nebula:** follow the private reporting
  instructions in [Security](SECURITY.md). Do not include vulnerability details
  in a public issue, discussion, or pull request.

Discuss substantial changes before implementation so the problem, scope, and
expected behavior are clear. Small, self-contained fixes can go directly to a
pull request.

## Prepare a change

1. Fork the repository if you do not have write access. Create a focused branch
   from `main` in your fork or checkout.
2. Read [AGENTS.md](AGENTS.md) and the relevant documentation. For interface or
   operator-workflow changes, also follow the
   [product-quality guidance](.agents/skills/nebula-product-quality/SKILL.md) and
   [interface principles](docs/design/interface-principles.md).
3. Use the current prerequisites and commands in
   [Run from source](README.md#run-from-source). The checkout may be ahead of the
   published preview; do not assume a source-only feature exists in an installer.
4. Keep the patch focused. Update the affected documentation and add regression
   coverage for changed behavior. Avoid unrelated formatting or dependency churn.

### Repository map

- `src/nebula/v3/`: Python Core
- `ui/`: web interface and native desktop shell (`ui/src-tauri/`)
- `tests/v3/`: Core tests
- `docs/`: product, architecture, quality, and release documentation
- `packaging/`: installer and release guidance

## Select checks before running them

Follow the [focused test policy](docs/TEST_SELECTION.md). Routine contributions
use the smallest set of tests that covers the changed journeys and integrations.
A general request to test or finish a PR is not approval to run complete suites.

Before running tests:

1. Identify the changed behavior, exact files or filters, runtime boundaries, and
   exclusions. Collect or list the selected tests and record the expected count.
2. Stage new files, bind the selection to the diff with
   `scripts/test_selection.py`, and update `.github/test-selection.json` as
   described in the focused test policy.
3. Run the selected checks. Review the CI selection receipts and report failures,
   omitted layers, and checks that could not run. Re-review the selection when
   the diff or base changes.

For documentation-only changes, review wording, relative links, and template
syntax. An empty product-test selection needs an explicit rationale and a current
diff digest; it does not establish that product behavior was tested.

For operator-visible changes, provide the applicable real-workflow and
browser/device evidence required by the product-quality guidance. State missing
evidence plainly rather than claiming an untested workflow is complete.

## Open a pull request

Use the pull request template and include:

- The problem, related issue, and intended outcome
- A concise summary of the changes and any compatibility or migration impact
- Exact checks and results, including the reviewed test selection and exclusions
- Sanitized screenshots for visible changes, where useful
- Known limitations and decisions still needed from maintainers

Open unfinished work as a draft. Keep review follow-ups in the same pull request.
Do not commit credentials, tokens, private keys, personal data, customer reports,
or engagement evidence. Review logs and images before sharing them; automated
redaction is not a substitute for checking the content yourself.

## License

Nebula's existing license is [BSD 2-Clause](LICENSE.md). Preserve applicable
copyright and license notices. Identify third-party material and its source in
your pull request so maintainers can review its license.
