import { useEffect, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import {
  evaluateBrowserScope,
  formatBrowserContextForAssistant,
  workbenchBrowser,
  type BrowserActionEvent,
  type BrowserContextEvent,
  type BrowserInterceptEvent,
} from "../api/workbenchBrowser";
import type {
  EngagementScopePolicy,
  SecurityBrowserAutomationStatus,
  SecurityBrowserAttack,
  SecurityBrowserCrawlJob,
  SecurityBrowserCommand,
  SecurityBrowserRepeaterTab,
  SecurityBrowserSession,
} from "../api/types";
import { desktopDeviceId, isTauriRuntime } from "../api/runtime";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";

const MAX_WAIT_MS = 5_000;

type CommandEntry = {
  command: SecurityBrowserCommand;
  claimToken: string;
};

type Receipt = {
  entry: CommandEntry;
  state: "complete" | "failed";
  result: Record<string, unknown>;
  evidenceIds?: string[];
  error?: string;
};

type ResearchEntry =
  | { kind: "repeater"; projectId: string; session: SecurityBrowserSession; tab: SecurityBrowserRepeaterTab }
  | { kind: "attack"; projectId: string; session: SecurityBrowserSession; attack: SecurityBrowserAttack; sequence: number; payloads: string[] }
  | { kind: "crawl"; projectId: string; session: SecurityBrowserSession; crawl: SecurityBrowserCrawlJob; url: string; depth: number; scope: EngagementScopePolicy };

const CURATED_PAYLOADS: Record<string, string[]> = {
  booleans: ["true", "false", "1", "0", "null"],
  boundary_numbers: ["-1", "0", "1", "2147483647", "2147483648"],
  path_boundaries: [".", "..", "../", "%2e%2e%2f"],
  header_boundaries: ["", "0", "null", "undefined"],
};

function payloadValues(payloadSet: Record<string, unknown>): string[] {
  if (payloadSet.kind === "list" && Array.isArray(payloadSet.values)) {
    return payloadSet.values.filter((value): value is string => typeof value === "string");
  }
  if (payloadSet.kind === "curated" && typeof payloadSet.name === "string") {
    return CURATED_PAYLOADS[payloadSet.name] ?? [];
  }
  return [];
}

function transformPayload(value: string, transforms: string[]): string {
  return transforms.reduce((current, transform) => {
    if (transform === "url_encode") return encodeURIComponent(current);
    if (transform === "url_decode") { try { return decodeURIComponent(current); } catch { /* diagnostic-expected: invalid optional transforms preserve the reviewed input. */ return current; } }
    if (transform === "base64_encode") return btoa(unescape(encodeURIComponent(current)));
    if (transform === "base64_decode") { try { return decodeURIComponent(escape(atob(current))); } catch { /* diagnostic-expected: invalid optional transforms preserve the reviewed input. */ return current; } }
    if (transform === "hex_encode") return Array.from(new TextEncoder().encode(current), (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (transform === "hex_decode") { try { return new TextDecoder().decode(Uint8Array.from(current.match(/.{1,2}/g) ?? [], (item) => Number.parseInt(item, 16))); } catch { /* diagnostic-expected: invalid optional transforms preserve the reviewed input. */ return current; } }
    if (transform === "html_encode") return current.replace(/[&<>"']/g, (character) => `&#${character.charCodeAt(0)};`);
    if (transform === "html_decode") { const element = document.createElement("textarea"); element.innerHTML = current; return element.value; }
    if (transform === "lowercase") return current.toLowerCase();
    if (transform === "uppercase") return current.toUpperCase();
    return current;
  }, value);
}

function attackPayloads(attack: SecurityBrowserAttack, sequence: number): Record<string, string> | undefined {
  const sets = attack.payloadSets.map(payloadValues);
  if (!sets.length || sets.some((values) => !values.length)) return undefined;
  const result: Record<string, string> = {};
  if (attack.strategy === "battering_ram") {
    const value = sets[0][sequence];
    if (value === undefined) return undefined;
    for (const position of attack.positions) result[position] = transformPayload(value, attack.transforms);
    return result;
  }
  if (attack.strategy === "sniper") {
    const values = sets[0];
    const positionIndex = Math.floor(sequence / values.length);
    const value = values[sequence % values.length];
    if (!attack.positions[positionIndex] || value === undefined) return undefined;
    for (const position of attack.positions) result[position] = "";
    result[attack.positions[positionIndex]] = transformPayload(value, attack.transforms);
    return result;
  }
  if (attack.strategy === "pitchfork") {
    if (sequence >= Math.min(...sets.map((values) => values.length))) return undefined;
    attack.positions.forEach((position, index) => { result[position] = transformPayload(sets[index][sequence], attack.transforms); });
    return result;
  }
  let remainder = sequence;
  for (let index = sets.length - 1; index >= 0; index -= 1) {
    const values = sets[index];
    const value = values[remainder % values.length];
    remainder = Math.floor(remainder / values.length);
    result[attack.positions[index]] = transformPayload(value, attack.transforms);
  }
  return remainder > 0 ? undefined : result;
}

function substitute(template: string, payloads: Record<string, string>): string {
  return Object.entries(payloads).reduce(
    (value, [position, payload]) => value.split(`§${position}§`).join(payload),
    template,
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function utf8Base64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function locatorArguments(value: Record<string, unknown>): Record<string, string> {
  const locator = value.locator;
  if (!locator || typeof locator !== "object" || Array.isArray(locator)) return {};
  return Object.fromEntries(
    Object.entries(locator).filter(([, item]) => typeof item === "string") as Array<[string, string]>,
  );
}

function signature(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    // diagnostic-expected: signatures fail closed to a stable sentinel and are retried from durable Core state.
    return "unserializable";
  }
}

/**
 * The desktop owns this worker, not the Browser page. It keeps the durable
 * Core-to-native command bridge alive while the operator changes routes or
 * closes the Browser panel. Mobile/LAN clients never mount it.
 */
export function BrowserAutomationWorker() {
  const { api, engagements, workspaceState } = useWorkspace();
  const pending = useRef(new Map<string, CommandEntry>());
  const receipts = useRef(new Map<string, Receipt>());
  const claimedIds = useRef(new Set<string>());
  const appliedRules = useRef(new Map<string, string>());
  const appliedScopes = useRef(new Map<string, string>());
  const appliedProxyConfigs = useRef(new Map<string, string>());
  const observedLeaseStates = useRef(new Map<string, string>());
  const researchPending = useRef(new Map<string, ResearchEntry>());
  const researchNextAt = useRef(new Map<string, number>());
  const resolvedIntercepts = useRef(new Set<string>());
  const apiRef = useRef(api);
  const engagementsRef = useRef(engagements);

  apiRef.current = api;
  engagementsRef.current = engagements;

  useEffect(() => {
    if (!isTauriRuntime() || !api || workspaceState === "failed") return;
    let disposed = false;
    let deviceId = "desktop";
    const stops: Array<() => void> = [];
    let handling = false;

    const queueReceipt = (receipt: Receipt) => {
      receipts.current.set(receipt.entry.command.id, receipt);
    };

    const flushReceipts = async () => {
      const currentApi = apiRef.current;
      if (!currentApi) return;
      for (const [commandId, receipt] of receipts.current) {
        try {
          await currentApi.finishSecurityBrowserCommand(receipt.entry.command, {
            deviceId,
            claimToken: receipt.entry.claimToken,
            state: receipt.state,
            result: receipt.result,
            evidenceIds: receipt.evidenceIds,
            error: receipt.error,
          });
          receipts.current.delete(commandId);
          pending.current.delete(commandId);
          claimedIds.current.delete(commandId);
        } catch (caught) {
          // Keep the claim and retry the durable receipt on the next poll. This
          // avoids re-running a click or replay merely because Core was briefly
          // unavailable after the native action completed.
          void logCaughtDiagnostic(
            "interface.security_browser.automation_receipt_retry",
            "A native browser command receipt could not be recorded yet; it will be retried.",
            caught,
            "browser-automation-worker",
          );
        }
      }
    };

    const fail = (entry: CommandEntry, error: unknown) => {
      queueReceipt({
        entry,
        state: "failed",
        result: {},
        error: errorMessage(error).slice(0, 4_000),
      });
    };

    const finishNativeAction = (entry: CommandEntry, event: BrowserActionEvent) => {
      queueReceipt({
        entry,
        state: event.state,
        result: { untrusted_page_data: true, ...(event.result ?? {}) },
        error: event.detail,
      });
    };

    const finishContextCommand = async (entry: CommandEntry, event: BrowserContextEvent) => {
      if (event.state !== "ready" || !event.context) {
        fail(entry, event.detail ?? "The native browser could not observe the page.");
        return;
      }
      const context = event.context;
      const result: Record<string, unknown> = {
        untrusted_page_data: true,
        url: context.url,
        title: context.title,
        text: context.text.slice(0, 16_000),
        selected_text: context.selectedText.slice(0, 4_000),
        forms: context.forms,
        links: context.links,
        truncated: context.truncated,
      };
      if (entry.command.kind !== "browser.capture_evidence") {
        queueReceipt({ entry, state: "complete", result });
        return;
      }

      const currentApi = apiRef.current;
      if (!currentApi) {
        fail(entry, "Core is unavailable while saving browser Evidence.");
        return;
      }
      try {
        const [scope, workspace] = await Promise.all([
          currentApi.getEngagementScope(entry.command.engagementId),
          currentApi.getSecurityBrowserWorkspace(entry.command.engagementId),
        ]);
        const decision = evaluateBrowserScope(context.url, scope);
        if (decision.state !== "in_scope") {
          fail(entry, `The observed page is outside the frozen Project scope: ${decision.detail}`);
          return;
        }
        const session = workspace.sessions.find((item) => item.id === entry.command.sessionId);
        const captured = formatBrowserContextForAssistant(context, decision);
        const hostname = new URL(context.url).hostname;
        const evidence = await currentApi.uploadEvidence({
          engagementId: entry.command.engagementId,
          filename: `autonomous-browser-${hostname.replace(/[^a-z0-9.-]/gi, "-")}-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`,
          title: `Autonomous browser capture · ${context.title || hostname}`,
          evidenceType: "browser-page-capture",
          contentBase64: utf8Base64(captured.text),
          mediaType: "text/plain; charset=utf-8",
          description: "Bounded autonomous browser capture. Page content is untrusted data; cookies, storage, and form values are excluded.",
          source: "security-browser-autonomous",
          capturedBy: "desktop-browser-worker",
          sourceVersion: "browser-autonomous-context-v1",
          sourceContext: {
            project_id: entry.command.engagementId,
            browser_session_id: entry.command.sessionId,
            browser_tab_id: entry.command.tabId,
            browser_identity_id: session?.identityId,
            url: context.url,
            scope_revision: decision.revision,
            captured_at: new Date().toISOString(),
            truncated: captured.truncated,
          },
          metadata: {
            browser_session_id: entry.command.sessionId,
            browser_identity_id: session?.identityId,
            url: context.url,
            untrusted_page_data: true,
          },
        });
        queueReceipt({ entry, state: "complete", result, evidenceIds: [evidence.id] });
      } catch (caught) {
        void logCaughtDiagnostic(
          "interface.security_browser.evidence_capture_failed",
          "An autonomous browser capture could not be saved as Evidence.",
          caught,
          "browser-automation-worker",
        );
        fail(entry, `The browser capture could not be saved as Evidence: ${errorMessage(caught)}`);
      }
    };

    const finishResearchAction = async (event: BrowserActionEvent) => {
      const entry = researchPending.current.get(event.actionId);
      const currentApi = apiRef.current;
      if (!entry || !currentApi) return;
      const result = event.result ?? {};
      const actionError = event.state === "failed"
        ? (event.detail ?? "The native browser request failed.").slice(0, 4_000)
        : undefined;
      try {
        if (entry.kind === "repeater") {
          let artifactId: string | undefined;
          const responseText = typeof result.responseText === "string" ? result.responseText : "";
          if (responseText && entry.session.captureMode === "bodies") {
            const artifact = await currentApi.uploadSecurityBrowserBodyArtifact(entry.session.id, {
              direction: "response",
              contentBase64: utf8Base64(responseText),
              mediaType: typeof result.contentType === "string" ? result.contentType : "text/plain",
              filename: `repeater-${entry.tab.id}-${entry.tab.requestCount}.txt`,
              truncated: result.responseTruncated === true,
            });
            artifactId = artifact.id;
          }
          await currentApi.recordSecurityBrowserRepeaterResult(entry.tab, {
            statusCode: typeof result.status === "number" ? result.status : undefined,
            responseHeaders: result.responseHeaders && typeof result.responseHeaders === "object"
              ? Object.entries(result.responseHeaders).filter((pair): pair is [string, string] => typeof pair[1] === "string")
              : [],
            responseBytes: typeof result.responseBytes === "number" ? result.responseBytes : undefined,
            durationMs: typeof result.durationMs === "number" ? result.durationMs : undefined,
            responseBodyArtifactId: artifactId,
            error: actionError,
          });
          return;
        }
        if (entry.kind === "attack") {
          await currentApi.recordSecurityBrowserAttackResult(entry.attack, {
            sequence: entry.sequence,
            payloads: entry.payloads,
            statusCode: typeof result.status === "number" ? result.status : undefined,
            responseBytes: typeof result.responseBytes === "number" ? result.responseBytes : undefined,
            durationMs: typeof result.durationMs === "number" ? result.durationMs : undefined,
            error: actionError,
          });
          researchNextAt.current.set(
            entry.attack.id,
            Date.now() + Math.ceil(1_000 / entry.attack.requestsPerSecond),
          );
          return;
        }
        if (actionError) {
          await currentApi.transitionSecurityBrowserCrawl(entry.crawl, "fail", "desktop-browser-worker", { error: actionError });
          return;
        }
        await currentApi.recordSecurityBrowserSiteNode(entry.projectId, {
          sessionId: entry.session.id,
          url: entry.url,
          discoverySource: "crawl",
          statusCode: typeof result.status === "number" ? result.status : undefined,
          contentType: typeof result.contentType === "string" ? result.contentType : undefined,
        });
        const visited = Array.from(new Set([...entry.crawl.visitedUrls, entry.url])).slice(-10_000);
        const nextLinks = entry.depth < entry.crawl.maxDepth && Array.isArray(result.links)
          ? result.links.filter((value): value is string => typeof value === "string")
              .filter((url) => evaluateBrowserScope(url, entry.scope).state === "in_scope")
              .filter((url) => !visited.includes(url) && !entry.crawl.frontier.some(([queued]) => queued === url))
              .slice(0, Math.max(0, entry.crawl.maxRequests - visited.length))
              .map((url): [string, number] => [url, entry.depth + 1])
          : [];
        const frontier = [...entry.crawl.frontier.slice(1), ...nextLinks].slice(0, 10_000);
        const requestsCompleted = entry.crawl.requestsCompleted + 1;
        const complete = !frontier.length || requestsCompleted >= entry.crawl.maxRequests;
        await currentApi.transitionSecurityBrowserCrawl(
          entry.crawl,
          complete ? "complete" : "progress",
          "desktop-browser-worker",
          {
            requestsCompleted,
            nodesDiscovered: visited.length,
            checkpoint: entry.crawl.checkpoint + 1,
            frontier,
            visitedUrls: visited,
          },
        );
      } catch (caught) {
        void logCaughtDiagnostic(
          "interface.security_browser.research_receipt_failed",
          "A native browser research receipt could not be saved; durable state will be reconciled.",
          caught,
          "browser-automation-worker",
        );
      } finally {
        researchPending.current.delete(event.actionId);
      }
    };

    const registerListeners = async () => {
      const contextStop = await listen<BrowserContextEvent>("nebula-browser-context", ({ payload }) => {
        const entry = pending.current.get(payload.requestId);
        if (entry) void finishContextCommand(entry, payload);
      });
      const actionStop = await listen<BrowserActionEvent>("nebula-browser-action", ({ payload }) => {
        if (payload.actionId.startsWith("automation:")) {
          const entry = pending.current.get(payload.actionId.slice("automation:".length));
          if (entry) finishNativeAction(entry, payload);
          return;
        }
        if (payload.actionId.startsWith("research-")) void finishResearchAction(payload);
      });
      const interceptStop = await listen<BrowserInterceptEvent>("nebula-browser-intercept", ({ payload }) => {
        const currentApi = apiRef.current;
        if (!currentApi) {
          void workbenchBrowser.decideProxyIntercept(payload.projectId, payload.sessionId, payload.transactionId, "drop").catch((caught) => {
            void logCaughtDiagnostic("interface.security_browser.intercept_fail_closed_failed", "A paused native request could not be dropped after Core became unavailable.", caught, "browser-automation-worker");
          });
          return;
        }
        void currentApi.createSecurityBrowserIntercept(payload.sessionId, payload).catch((caught) => {
          void logCaughtDiagnostic(
            "interface.security_browser.intercept_persist_failed",
            "A paused native request could not be persisted and was failed closed.",
            caught,
            "browser-automation-worker",
          );
          void workbenchBrowser.decideProxyIntercept(payload.projectId, payload.sessionId, payload.transactionId, "drop").catch((dropCaught) => {
            void logCaughtDiagnostic("interface.security_browser.intercept_fail_closed_failed", "A paused native request could not be dropped after its durable receipt failed.", dropCaught, "browser-automation-worker");
          });
        });
      });
      if (disposed) {
        contextStop();
        actionStop();
        interceptStop();
      } else {
        stops.push(contextStop, actionStop, interceptStop);
      }
    };
    void registerListeners().catch((caught) => {
      void logCaughtDiagnostic(
        "interface.security_browser.automation_listener_failed",
        "The desktop browser worker could not subscribe to native command receipts.",
        caught,
        "browser-automation-worker",
      );
    });

    const syncNativeSession = async (
      projectId: string,
      session: SecurityBrowserSession,
      scope: EngagementScopePolicy | undefined,
      status: SecurityBrowserAutomationStatus,
    ) => {
      if (!session.proxyEnabled) {
        appliedScopes.current.delete(session.id);
        appliedProxyConfigs.current.delete(session.id);
        await workbenchBrowser.stopProxy(projectId, session.id).catch(() => {
          // diagnostic-expected: stopping an already-absent native proxy is idempotent cleanup.
          return undefined;
        });
        for (const [ruleId, ruleSignature] of appliedRules.current) {
          if (!ruleSignature.startsWith(`${session.id}:`)) continue;
          const rule = status.rules.find((item) => item.id === ruleId);
          if (rule) {
            await workbenchBrowser.applyProxyRule(projectId, session.id, { ...rule, enabled: false }).catch(() => {
              // diagnostic-expected: a missing native proxy handle is reconciled on the next durable-state poll.
              return undefined;
            });
          }
          appliedRules.current.delete(ruleId);
        }
        return;
      }
      const proxySignature = signature({
        enabled: session.upstreamProxyEnabled,
        url: session.upstreamProxyUrl,
        credentialRef: session.upstreamProxyCredentialRef,
        captureBodies: session.captureMode === "bodies",
      });
      if (appliedProxyConfigs.current.get(session.id) !== proxySignature) {
        try {
          await workbenchBrowser.configureProxy(projectId, session.id, {
            enabled: session.upstreamProxyEnabled,
            url: session.upstreamProxyUrl,
            credentialRef: session.upstreamProxyCredentialRef,
          }, session.captureMode === "bodies", session.interceptionEnabled);
          appliedProxyConfigs.current.set(session.id, proxySignature);
        } catch {
          // diagnostic-expected: a missing native proxy handle is configured when its visible tab is created.
          // A session without an open native proxy handle is configured when
          // the visible tab is created; the next poll retries this update.
        }
      }
      if (!scope) {
        appliedScopes.current.delete(session.id);
        await workbenchBrowser.clearProxyScope(projectId, session.id).catch(() => {
          // diagnostic-expected: clearing an already-absent native proxy scope is idempotent cleanup.
          return undefined;
        });
        return;
      }
      const scopeSignature = signature(scope);
      if (appliedScopes.current.get(session.id) !== scopeSignature) {
        try {
          await workbenchBrowser.applyProxyScope(projectId, session.id, scope);
          appliedScopes.current.set(session.id, scopeSignature);
        } catch {
          // diagnostic-expected: a missing native proxy handle is reconciled on the next durable-state poll.
          // A session without an open native tab has no proxy handle yet. The
          // next poll retries after the Browser surface creates one.
        }
      }
      const sessionRules = status.rules.filter((rule) => rule.sessionId === session.id);
      for (const rule of sessionRules) {
        const ruleSignature = `${session.id}:${signature({ match: rule.match, action: rule.action, priority: rule.priority, expiresAt: rule.expiresAt, enabled: rule.enabled })}`;
        if (appliedRules.current.get(rule.id) === ruleSignature) continue;
        try {
          await workbenchBrowser.applyProxyRule(projectId, session.id, rule);
          appliedRules.current.set(rule.id, ruleSignature);
        } catch {
          // diagnostic-expected: durable rules remain authoritative and are retried after a native tab exists.
          // See the scope retry note above; durable rules remain visible in Core.
        }
      }
    };

    const process = async () => {
      if (disposed || handling) return;
      handling = true;
      try {
        await flushReceipts();
        const currentApi = apiRef.current;
        if (!currentApi) return;
        for (const engagement of engagementsRef.current) {
          if (disposed) return;
          const [status, workspace, scope, research] = await Promise.all([
            currentApi.getSecurityBrowserAutomation(engagement.id),
            currentApi.getSecurityBrowserWorkspace(engagement.id),
            currentApi.getEngagementScope(engagement.id).catch(() => {
              // diagnostic-expected: absent scope fails closed in syncNativeSession and is retried on the next poll.
              return undefined;
            }),
            currentApi.getSecurityBrowserResearch(engagement.id),
          ]);
          const sessions = workspace.sessions;
          for (const session of sessions) {
            await syncNativeSession(engagement.id, session, scope, status);
          }
          const researchSessionBusy = (sessionId: string) => Array.from(researchPending.current.values())
            .some((entry) => entry.session.id === sessionId);
          for (const intercept of research.intercepts) {
            if (intercept.state === "paused" || resolvedIntercepts.current.has(intercept.transactionId)) continue;
            const session = sessions.find((item) => item.id === intercept.sessionId);
            if (!session || session.deviceOwner !== deviceId) continue;
            const decision = intercept.state === "forwarded" ? "forward" : "drop";
            try {
              await workbenchBrowser.decideProxyIntercept(engagement.id, session.id, intercept.transactionId, decision);
              resolvedIntercepts.current.add(intercept.transactionId);
            } catch (caught) {
              if (!/no longer paused|expired/i.test(errorMessage(caught))) {
                void logCaughtDiagnostic("interface.security_browser.intercept_delivery_failed", "A durable intercept decision could not reach the native proxy and will be retried.", caught, "browser-automation-worker");
              } else {
                resolvedIntercepts.current.add(intercept.transactionId);
              }
            }
          }
          for (const durableTab of research.repeaterTabs) {
            if (researchSessionBusy(durableTab.sessionId)) continue;
            const session = sessions.find((item) => item.id === durableTab.sessionId);
            if (!session || session.deviceOwner !== deviceId) continue;
            const nativeTab = session.tabs.find((item) => item.id === session.activeTabId) ?? session.tabs[0];
            if (!nativeTab?.url) continue;
            let tab = durableTab;
            if (tab.state === "running") {
              await currentApi.transitionSecurityBrowserRepeaterTab(
                tab,
                "fail",
                "desktop-browser-worker",
                "The desktop restarted before the request receipt was saved. Retry explicitly to avoid an ambiguous duplicate request.",
              ).catch((caught) => {
                void logCaughtDiagnostic("interface.security_browser.repeater_recovery_failed", "An interrupted Repeater request could not be marked failed during desktop recovery.", caught, "browser-automation-worker");
              });
              continue;
            }
            if (tab.state !== "queued") continue;
            try {
              tab = await currentApi.transitionSecurityBrowserRepeaterTab(tab, "start", "desktop-browser-worker");
              const actionId = `research-repeater-${tab.id}-${tab.requestCount}`;
              researchPending.current.set(actionId, { kind: "repeater", projectId: engagement.id, session, tab });
              await workbenchBrowser.executeAction(nativeTab.id, engagement.id, {
                actionId,
                kind: "research_request",
                locator: {},
                arguments: {
                  method: tab.method,
                  url: tab.url,
                  headers: Object.fromEntries(tab.headers),
                  body: tab.bodyTemplate,
                },
                pageUrl: nativeTab.url,
              });
            } catch (caught) {
              researchPending.current.forEach((entry, id) => { if (entry.kind === "repeater" && entry.tab.id === tab.id) researchPending.current.delete(id); });
              await currentApi.transitionSecurityBrowserRepeaterTab(tab, "fail", "desktop-browser-worker", errorMessage(caught)).catch((transitionCaught) => {
                void logCaughtDiagnostic("interface.security_browser.repeater_failure_receipt_failed", "A failed Repeater request could not save its terminal state.", transitionCaught, "browser-automation-worker");
              });
              void logCaughtDiagnostic("interface.security_browser.repeater_execution_failed", "A reviewed Repeater request could not execute.", caught, "browser-automation-worker");
            }
          }
          for (const durableAttack of research.attacks) {
            if (researchSessionBusy(durableAttack.sessionId)) continue;
            if ((researchNextAt.current.get(durableAttack.id) ?? 0) > Date.now()) continue;
            const session = sessions.find((item) => item.id === durableAttack.sessionId);
            if (!session || session.deviceOwner !== deviceId) continue;
            const nativeTab = session.tabs.find((item) => item.id === session.activeTabId) ?? session.tabs[0];
            if (!nativeTab?.url) continue;
            let attack = durableAttack;
            try {
              if (attack.state === "queued") attack = await currentApi.transitionSecurityBrowserAttack(attack, "start", "desktop-browser-worker");
              if (attack.state !== "running" || attack.requestCount >= attack.maxRequests) continue;
              const replacements = attackPayloads(attack, attack.requestCount);
              if (!replacements) {
                await currentApi.transitionSecurityBrowserAttack(attack, "complete", "desktop-browser-worker");
                continue;
              }
              const payloads = attack.positions.map((position) => replacements[position]);
              const actionId = `research-attack-${attack.id}-${attack.requestCount}`;
              researchPending.current.set(actionId, { kind: "attack", projectId: engagement.id, session, attack, sequence: attack.requestCount, payloads });
              await workbenchBrowser.executeAction(nativeTab.id, engagement.id, {
                actionId,
                kind: "research_request",
                locator: {},
                arguments: {
                  method: attack.method,
                  url: substitute(attack.urlTemplate, replacements),
                  headers: Object.fromEntries(attack.headersTemplate.map(([name, value]) => [name, substitute(value, replacements)])),
                  body: substitute(attack.bodyTemplate, replacements),
                },
                pageUrl: nativeTab.url,
              });
            } catch (caught) {
              researchPending.current.forEach((entry, id) => { if (entry.kind === "attack" && entry.attack.id === attack.id) researchPending.current.delete(id); });
              await currentApi.transitionSecurityBrowserAttack(attack, "fail", "desktop-browser-worker", errorMessage(caught)).catch((transitionCaught) => {
                void logCaughtDiagnostic("interface.security_browser.attack_failure_receipt_failed", "A failed Intruder request could not save its terminal state.", transitionCaught, "browser-automation-worker");
              });
              void logCaughtDiagnostic("interface.security_browser.attack_execution_failed", "A bounded Intruder request could not execute.", caught, "browser-automation-worker");
            }
          }
          if (scope) for (const durableCrawl of research.crawlJobs) {
            if (researchSessionBusy(durableCrawl.sessionId)) continue;
            const session = sessions.find((item) => item.id === durableCrawl.sessionId);
            if (!session || session.deviceOwner !== deviceId) continue;
            const nativeTab = session.tabs.find((item) => item.id === session.activeTabId) ?? session.tabs[0];
            if (!nativeTab?.url) continue;
            let crawl = durableCrawl;
            try {
              if (crawl.state === "queued") crawl = await currentApi.transitionSecurityBrowserCrawl(crawl, "start", "desktop-browser-worker");
              if (crawl.state !== "running") continue;
              const [url, depth] = crawl.frontier[0] ?? [];
              if (!url || crawl.requestsCompleted >= crawl.maxRequests) {
                await currentApi.transitionSecurityBrowserCrawl(crawl, "complete", "desktop-browser-worker");
                continue;
              }
              const actionId = `research-crawl-${crawl.id}-${crawl.checkpoint}`;
              researchPending.current.set(actionId, { kind: "crawl", projectId: engagement.id, session, crawl, url, depth, scope });
              await workbenchBrowser.executeAction(nativeTab.id, engagement.id, {
                actionId,
                kind: "research_request",
                locator: {},
                arguments: { method: "GET", url, headers: {}, body: "" },
                pageUrl: nativeTab.url,
              });
            } catch (caught) {
              researchPending.current.forEach((entry, id) => { if (entry.kind === "crawl" && entry.crawl.id === crawl.id) researchPending.current.delete(id); });
              await currentApi.transitionSecurityBrowserCrawl(crawl, "fail", "desktop-browser-worker", { error: errorMessage(caught) }).catch((transitionCaught) => {
                void logCaughtDiagnostic("interface.security_browser.crawl_failure_receipt_failed", "A failed crawl request could not save its terminal state.", transitionCaught, "browser-automation-worker");
              });
              void logCaughtDiagnostic("interface.security_browser.crawl_execution_failed", "A bounded crawl request could not execute.", caught, "browser-automation-worker");
            }
          }
          for (const lease of status.leases) {
            const previous = observedLeaseStates.current.get(lease.id);
            observedLeaseStates.current.set(lease.id, lease.status);
            if (previous === "active" && lease.status !== "active") {
              await workbenchBrowser.stopProxy(engagement.id, lease.sessionId).catch(() => {
                // diagnostic-expected: lease revocation cleanup is idempotent when no native proxy handle remains.
                return undefined;
              });
              appliedScopes.current.delete(lease.sessionId);
            }
          }
          for (const command of status.commands.filter((item) => item.status === "queued")) {
            if (claimedIds.current.has(command.id)) continue;
            claimedIds.current.add(command.id);
            try {
              const claimed = await currentApi.claimSecurityBrowserCommand(command.id, deviceId);
              if (claimed.status !== "claimed" || claimed.claimedByDeviceId !== deviceId || !claimed.claimToken) {
                claimedIds.current.delete(command.id);
                continue;
              }
              const entry = { command: claimed, claimToken: claimed.claimToken } satisfies CommandEntry;
              pending.current.set(command.id, entry);
              const args = claimed.arguments ?? {};
              const session = sessions.find((item) => item.id === claimed.sessionId);
              const tab = session?.tabs.find((item) => item.id === claimed.tabId);
              const expectedPageUrl = claimed.expectedPageUrl
                ?? (typeof args.page_url === "string" ? args.page_url : tab?.url);
              if (!tab && claimed.kind !== "proxy.observe") {
                throw new Error("The leased browser tab is not present in durable desktop state; the command was not executed.");
              }
              if (claimed.kind === "proxy.observe") {
                queueReceipt({
                  entry,
                  state: "complete",
                  result: {
                    session_id: claimed.sessionId,
                    captured_exchange_count: workspace.traffic.filter((item) => item.sessionId === claimed.sessionId).length,
                    active_rule_count: status.rules.filter((item) => item.sessionId === claimed.sessionId && item.enabled).length,
                    untrusted_traffic_data: true,
                  },
                });
              } else if (claimed.kind === "proxy.configure") {
                const upstream = args.upstream_proxy;
                if (upstream !== null && upstream !== undefined && typeof upstream !== "object") {
                  throw new Error("proxy.configure upstream_proxy must be an object or null");
                }
                queueReceipt({
                  entry,
                  state: "complete",
                  result: {
                    status: "accepted",
                    note: "The durable proxy configuration is applied by the desktop session worker; secrets remain credential references.",
                  },
                });
              } else if ((claimed.kind === "browser.control" && args.action === "wait") || (claimed.kind === "browser.interact" && args.operation === "wait")) {
                const waitMs = Math.min(MAX_WAIT_MS, Math.max(50, Number(args.milliseconds ?? args.duration_ms ?? 500)) || 500);
                window.setTimeout(() => queueReceipt({ entry, state: "complete", result: { action: "wait", waited_ms: waitMs } }), waitMs);
              } else if (!expectedPageUrl && claimed.kind !== "browser.control") {
                throw new Error("The native browser command requires an exact current page URL.");
              } else {
                await workbenchBrowser.executeAutomationCommand(claimed.tabId, engagement.id, {
                  commandId: claimed.id,
                  kind: claimed.kind,
                  locator: locatorArguments(args),
                  arguments: args,
                  pageUrl: expectedPageUrl,
                });
                if (claimed.kind === "browser.control") {
                  queueReceipt({ entry, state: "complete", result: { action: args.action ?? "control" } });
                }
              }
            } catch (caught) {
              const entry = pending.current.get(command.id);
              if (entry) fail(entry, caught);
              else claimedIds.current.delete(command.id);
              void logCaughtDiagnostic(
                "interface.security_browser.automation_execution_failed",
                "A claimed autonomous browser command could not execute.",
                caught,
                "browser-automation-worker",
              );
            }
          }
        }
      } catch (caught) {
        void logCaughtDiagnostic(
          "interface.security_browser.automation_poll_failed",
          "Autonomous browser commands could not be synchronized.",
          caught,
          "browser-automation-worker",
        );
      } finally {
        handling = false;
      }
    };

    void desktopDeviceId().then((value) => {
      deviceId = value;
      return process();
    }).catch((caught) => {
      void logCaughtDiagnostic("interface.security_browser.device_identity_unavailable", "The desktop browser identity could not be loaded.", caught, "browser-automation-worker");
    });
    const timer = window.setInterval(() => void process(), 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      stops.forEach((stop) => stop());
    };
  }, [api, workspaceState]);

  return null;
}
