# Browser harness follow-up sweep

Reviewed the merged browser-primitives tree (`a823124`) across Core, native
browser/proxy code, agent tool schemas, default mission instructions, shipped
skills, current documentation, preview data, and CI selectors.

## Findings and disposition

- No additional built-in exploit recipe, attack runner, injection payload set,
  credential attack, or guided exploitation workflow was found.
- Default mission planning carries the supplied objective, selected capabilities,
  and scope. It does not append a security-testing technique or attack sequence.
- Neutralized the preview mission title and discovery-role label, editor search
  keyword, demo claim of verified exploit evidence, and scope-disclosure copy.
- Removed Nmap from the default shell-tool examples. Installed-tool discovery
  still reports the operator's actual environment; tools were not removed.
- Removed the obsolete `browser-pentest-audit` CI area. Independent review
  confirmed that both of its grep patterns matched no current test titles; the
  existing `browser-harness` area covers the retained browser journeys.

## Intentionally retained

CVE/CISA KEV references and `exploit_available` are vulnerability-intelligence
metadata, not execution. The warning that untrusted tool output can contain
exploit payloads is defensive handling guidance. Historical release/design notes
and schema migrations are records of earlier versions. Generic shell, browser,
MCP and operator-provided skills remain available as requested; this sweep is
not a restriction on what an operator can supply or ask an external model to do.

## Verification scope

Focused default-tool/mission guidance tests, the scope-disclosure refusal case,
CI selector validation, and the preview component test. Product knowledge is
regenerated and checked against its current source digest. A production UI build
checks integration. No active browser control, API schema, permission, layout,
or execution path changes in this follow-up; the preview is disabled by the
production Workspace provider. Additional browser/native lifecycle runs are not
needed for these copy and metadata edits. CI selects only these focused cases.

This is a sweep for built-in offensive functionality and guidance, not a general
vulnerability assessment or a guarantee about third-party tools and model output.

## Results

- 10 focused Python cases passed (default guidance, objective preservation,
  disclosure refusal, selector handling, current product-knowledge digest).
- 11 AgentsPage component cases passed, including neutral preview copy.
- Production UI build passed; existing chunk/import warnings remain.
- Bundled help regenerated: 427 passages, source digest prefix `251a0060ea88`.
- No production services were restarted and no deployment was performed.
