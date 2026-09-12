import { expect, test } from "vitest";
import { browserScopeStatus, proxyScopeSignal } from "./browserScopeStatus";
const projectScope = {state: "in_scope" as const, label: "All targets", detail: "Project permits every target.", revision: 4};
test("Project all-target permission is not presented as native readiness", () => {
  expect(browserScopeStatus(projectScope)).toMatchObject({state: "unknown", label: "Project: all targets"});
});
test("missing native scope overrides a permissive Project badge", () => {
  const signal = proxyScopeSignal({blocked: true, error: "no compiled Project scope is active for this browser session"});
  expect(browserScopeStatus(projectScope, signal)).toMatchObject({state: "inactive", label: "Browser scope unavailable"});
});
test("only successful unblocked traffic clears a missing-scope signal", () => {
  expect(proxyScopeSignal({blocked: true, statusCode: 200, error: "blocked by rule"})).toBeUndefined();
  expect(proxyScopeSignal({blocked: false, statusCode: 503})).toBeUndefined();
  expect(proxyScopeSignal({blocked: false, statusCode: 200})).toBeNull();
});
