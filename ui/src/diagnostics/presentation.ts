import type { DiagnosticFeature, DiagnosticRecord } from "./types";

interface FeaturePresentation {
  label: string;
  destination?: string;
  actionLabel?: string;
}

const features: Record<DiagnosticFeature, FeaturePresentation> = {
  desktop: { label: "Desktop", destination: "/settings", actionLabel: "Open settings" },
  interface: { label: "Interface", destination: "/", actionLabel: "Open Workbench" },
  api: { label: "Nebula Core", destination: "/settings", actionLabel: "Open setup" },
  setup: { label: "Setup", destination: "/settings#setup-settings", actionLabel: "Open setup" },
  storage: { label: "Local storage", destination: "/settings#diagnostics-settings", actionLabel: "Open diagnostics" },
  projects: { label: "Projects", destination: "/project", actionLabel: "Open Project" },
  terminal: { label: "Terminal", destination: "/?view=terminal", actionLabel: "Open Terminal" },
  "terminal-audit": { label: "Terminal audit", destination: "/?view=terminal", actionLabel: "Open Terminal" },
  workspace: { label: "Project files", destination: "/?view=files", actionLabel: "Open Files" },
  notes: { label: "Notes", destination: "/?view=notes", actionLabel: "Open Notes" },
  capture: { label: "Capture", destination: "/?view=terminal", actionLabel: "Open Workbench" },
  providers: { label: "Model providers", destination: "/settings#advanced-settings", actionLabel: "Open providers" },
  chat: { label: "Assistant chat", destination: "/?view=chat", actionLabel: "Open Assistant" },
  knowledge: { label: "Project sources", destination: "/project?view=sources", actionLabel: "Open Sources" },
  harnesses: { label: "Agent harnesses", destination: "/settings#advanced-settings", actionLabel: "Open harnesses" },
  missions: { label: "Missions", destination: "/?view=missions", actionLabel: "Open Missions" },
  toolbox: { label: "Toolbox", destination: "/settings#advanced-settings", actionLabel: "Open Toolbox" },
  sandbox: { label: "Execution sandbox", destination: "/settings#advanced-settings", actionLabel: "Open runner settings" },
  executions: { label: "Reviewed executions", destination: "/?view=activity", actionLabel: "Open Activity" },
  findings: { label: "Findings", destination: "/findings", actionLabel: "Open Findings" },
  evidence: { label: "Evidence", destination: "/project?view=evidence", actionLabel: "Open Evidence" },
  reports: { label: "Reports", destination: "/reports", actionLabel: "Open Reports" },
  diagnostics: { label: "Local diagnostics", destination: "/settings#diagnostics-settings", actionLabel: "Open diagnostics" },
};

export interface DiagnosticFailurePresentation {
  featureLabel: string;
  operationLabel: string;
  cause: string;
  recovery: string;
  destination?: string;
  actionLabel?: string;
}

export function humanizeDiagnosticValue(value: string): string {
  const normalized = value.replaceAll("_", " ").replaceAll("-", " ").trim();
  return normalized ? `${normalized[0].toUpperCase()}${normalized.slice(1)}` : "Unspecified";
}

export function diagnosticFailurePresentation(record: DiagnosticRecord): DiagnosticFailurePresentation {
  const feature = features[record.feature];
  const component = typeof record.metadata?.component === "string"
    ? record.metadata.component
    : undefined;
  const operation = component ?? record.stage;
  return {
    featureLabel: feature.label,
    operationLabel: operation
      ? `${feature.label} · ${humanizeDiagnosticValue(operation)}`
      : feature.label,
    cause: record.safe_failure_cause ?? "No additional safe cause was recorded.",
    recovery: record.retryable === true
      ? "Retry the original action."
      : record.retryable === false
        ? "No verified retry procedure is available."
        : "No verified recovery procedure is available.",
    destination: feature.destination,
    actionLabel: feature.actionLabel,
  };
}

export function diagnosticRecordMatchesReference(record: DiagnosticRecord, reference: string): boolean {
  return [record.error_id, record.request_id].includes(reference);
}

export function diagnosticTechnicalDetails(record: DiagnosticRecord): string {
  return JSON.stringify(record, null, 2);
}
