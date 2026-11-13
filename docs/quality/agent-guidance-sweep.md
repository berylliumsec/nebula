# Agent prompts and bundled guidance sweep

Reviewed source at `775d7bc33fb2686337c8c78e9373635d799d9b00` and decoded its
427-passage product-help bundle (source digest `a979e57b2839`).

## Findings

- Provider chat, native harness, supervisor/subagent and shared browser workflow
  instructions follow the operator's objective and available capabilities. No
  built-in offensive role, attack recipe or exploitation sequence was found.
- Two passages in the indexed settings-assistant guide used “Active testing” to
  describe the operator-owned Browser. They now say “Network actions”, retaining
  passive-only delegation, credential restrictions and scope enforcement.
- The older one-browser acceptance document referred to attack progress and
  manual attack forms. Its action-review wording is now neutral. It is excluded
  from the bundled index, as are other historical quality and release records.
- Remaining terms describe explicit exclusions, historical removals, factual
  vulnerability intelligence, defensive handling or generic tool support. The
  usage scenarios describe defensive TLS/header observations, not attack recipes.
  Their assessment labels do not establish an offensive workflow.

An independent read-only review decoded the same bundle and confirmed that the
residual indexed wording came from maintained documentation, not executable
prompt recipes. Generic browser, shell, Kali, MCP and operator-owned instruction
or skill support remain unchanged. This does not constrain third-party models or
operator-supplied content, or establish that the codebase has no vulnerabilities.

## Verification contract

The changed journey is product-help retrieval from the packaged documentation.
The generated index owns the shipped content; Chroma mirrors its source digest.
No UI, runtime prompt assembly, tool registration, permission, execution or
server-owned user state changes. Browser/device, live-provider, persistence and
LAN lifecycle gates do not apply to this documentation-only change.

Three focused product-knowledge tests verify retained browser capabilities and
passive delegation language, the generated source digest, and disposable Chroma
synchronization including stale-digest replacement. CI selects only those tests.
No live Core is restarted or deployed.
