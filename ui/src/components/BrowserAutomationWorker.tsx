import { useEffect, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import {
  evaluateBrowserScope,
  formatBrowserContextForAssistant,
  workbenchBrowser,
  type BrowserActionEvent,
  type BrowserContextEvent,
} from "../api/workbenchBrowser";
import type {
  EngagementScopePolicy,
  SecurityBrowserAutomationStatus,
  SecurityBrowserCommand,
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

    const registerListeners = async () => {
      const contextStop = await listen<BrowserContextEvent>("nebula-browser-context", ({ payload }) => {
        const entry = pending.current.get(payload.requestId);
        if (entry) void finishContextCommand(entry, payload);
      });
      const actionStop = await listen<BrowserActionEvent>("nebula-browser-action", ({ payload }) => {
        if (!payload.actionId.startsWith("automation:")) return;
        const entry = pending.current.get(payload.actionId.slice("automation:".length));
        if (entry) finishNativeAction(entry, payload);
      });
      if (disposed) {
        contextStop();
        actionStop();
      } else {
        stops.push(contextStop, actionStop);
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
          }, session.captureMode === "bodies");
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
          const [status, workspace, scope] = await Promise.all([
            currentApi.getSecurityBrowserAutomation(engagement.id),
            currentApi.getSecurityBrowserWorkspace(engagement.id),
            currentApi.getEngagementScope(engagement.id).catch(() => {
              // diagnostic-expected: absent scope fails closed in syncNativeSession and is retried on the next poll.
              return undefined;
            }),
          ]);
          const sessions = workspace.sessions;
          for (const session of sessions) {
            await syncNativeSession(engagement.id, session, scope, status);
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
