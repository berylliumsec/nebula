---
name: code-troubleshooting
description: Diagnose Nebula code defects, regressions, and failing behavior before a fix. Use when troubleshooting may lead to a code or configuration change.
---

# Code Troubleshooting

Find and validate the causal fault before changing behavior. A symptom, stack
trace, correlation, or plausible patch is not a root cause.

## Establish the cause

1. Capture the expected and observed behavior, affected build and environment,
   and a minimal reproduction or durable failure trace. Identify where the first
   incorrect state or result appears.
2. Trace that state through the responsible code and its callers, inputs, and
   owners. Form a causal explanation of how the fault produces the symptom, then
   check at least one credible alternative against the evidence. Use read-only
   inspection, targeted reproduction, or temporary diagnostic probes as needed;
   do not make a behavior-changing repair during diagnosis.
3. State the proposed root cause with the specific code path and reproduction or
   trace that supports it. Mark any unresolved link as unknown and keep
   investigating rather than promoting an inference to a confirmed cause.

## Require independent subagent validation

Before attempting a fix, start a separate subagent with fresh context (for
Codex, `fork_turns: "none"`). Give it the original failure report, reproduction
steps, raw evidence, and access to the relevant code, without your proposed
patch or conclusion. Ask it to identify the causal fault, explain how the
evidence supports it, and challenge a plausible alternative. It may reproduce
the failure when safe, but must not edit product code or configuration.

Compare the subagent's explanation with your own. The gate passes only when its
independent findings support the same causal mechanism and the evidence resolves
material competing explanations. Agreement without a causal explanation or a
review of the proposed patch does not validate the root cause. If findings
conflict or evidence is insufficient, continue diagnosis and seek validation
again before fixing. If a subagent is unavailable, report the blocked fix; do
not silently waive this gate.

## Repair and verify

After validation, make the smallest repair that addresses the confirmed cause.
Verify the observed workflow and add focused regression coverage where it proves
the fix or an applicable quality gate requires it. Follow the repository's
test-selection policy and, for operator-visible changes, the
`nebula-product-quality` skill. In the handoff, report the causal evidence, the
subagent's validation, the repair, and the tests and workflow checks performed.
