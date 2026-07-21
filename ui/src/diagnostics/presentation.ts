import { diagnosticRemediationCatalog } from "./catalog.generated";
import type {
  DiagnosticAction,
  DiagnosticIncident,
  DiagnosticReasonCode,
  DiagnosticRecord,
} from "./types";

const knownReasons = new Set(Object.keys(diagnosticRemediationCatalog.reason_families));

export interface DiagnosticFailurePresentation {
  featureLabel: string;
  operationLabel: string;
  cause: string;
  impact: string;
  safeState: string;
  recovery: string;
  steps: string[];
  verification: string;
  destination?: string;
  actionLabel?: string;
}

export function humanizeDiagnosticValue(value: string): string {
  const normalized = value.replaceAll("_", " ").replaceAll("-", " ").trim();
  return normalized ? `${normalized[0].toUpperCase()}${normalized.slice(1)}` : "Unspecified";
}

function reasonForRecord(record: DiagnosticRecord): DiagnosticReasonCode {
  if (record.reason_code && knownReasons.has(record.reason_code)) {
    return record.reason_code as DiagnosticReasonCode;
  }
  const evidence = `${record.event_code} ${record.exception_type ?? ""}`.toLowerCase();
  const status = typeof record.metadata?.http_status === "number" ? record.metadata.http_status : undefined;
  if (status === 429 || evidence.includes("rate_limit")) return "rate_limited";
  if (status === 401 || /auth|credential|unauthorized/.test(evidence)) return "authentication_failed";
  if (status === 403 || /permission|denied|privacy|policy/.test(evidence)) return "permission_denied";
  if ([408, 504].includes(status ?? 0) || /timeout|timedout/.test(evidence)) return "timeout";
  if (/integrity|digest|signature|checksum/.test(evidence)) return "integrity_failed";
  if (/conflict|stale|state_error|revision/.test(evidence)) return "stale_state";
  if (/transport|disconnect|closed|endofstream/.test(evidence)) return "transport_closed";
  if (/protocol|malformed|decode|parse/.test(evidence)) return "protocol_invalid";
  if ([502, 503].includes(status ?? 0) || /unavailable|not_available/.test(evidence)) return "dependency_unavailable";
  if (/invalid|validation|unsupported|configuration/.test(evidence)) return "invalid_input";
  if (/cancel|interrupt/.test(evidence)) return "cancelled";
  return "unknown_internal_fault";
}

export function diagnosticFailurePresentation(record: DiagnosticRecord): DiagnosticFailurePresentation {
  const feature = diagnosticRemediationCatalog.features[record.feature];
  const reason = diagnosticRemediationCatalog.reason_families[reasonForRecord(record)];
  const component = typeof record.metadata?.component === "string"
    ? record.metadata.component
    : undefined;
  const operation = component ?? record.stage;
  const steps = [...reason.steps];
  return {
    featureLabel: feature.label,
    operationLabel: operation
      ? `${feature.label} · ${humanizeDiagnosticValue(operation)}`
      : feature.operation,
    cause: record.operator_detail ?? record.safe_failure_cause ?? reason.cause,
    impact: record.impact ?? reason.impact,
    safeState: reason.confirmed_safe_state,
    recovery: steps[0],
    steps,
    verification: reason.verification,
    destination: feature.destination,
    actionLabel: `Open ${feature.label}`,
  };
}

function correlationValues(record: DiagnosticRecord): Set<string> {
  return new Set([
    record.error_id,
    record.request_id,
    record.operation_id,
    record.parent_operation_id,
  ].filter((value): value is string => Boolean(value)));
}

function primaryRecord(records: DiagnosticRecord[]): DiagnosticRecord {
  const sourceRank = new Map([["core", 0], ["desktop", 1], ["browser", 2], ["interface", 3]]);
  return records.slice().sort((left, right) => {
    const source = (sourceRank.get(left.source ?? "") ?? 4) - (sourceRank.get(right.source ?? "") ?? 4);
    if (source) return source;
    const wrapper = Number(left.event_code.startsWith("interface.")) - Number(right.event_code.startsWith("interface."));
    return wrapper || (left.timestamp ?? "").localeCompare(right.timestamp ?? "");
  })[0];
}

function actionsForRecord(record: DiagnosticRecord): DiagnosticAction[] {
  const feature = diagnosticRemediationCatalog.features[record.feature];
  const metadata = record.metadata ?? {};
  const durableRetry = record.retryable === true
    && metadata.entity_type === "harness_turn"
    && typeof metadata.entity_id === "string";
  return [
    {
      id: "open_affected_view",
      label: `Open ${feature.label}`,
      kind: "navigate",
      confirmation_required: false,
      enabled: true,
      destination: feature.destination,
    },
    {
      id: "run_health_check",
      label: "Run health check",
      kind: "health_check",
      confirmation_required: true,
      enabled: !["interface", "notes", "findings"].includes(record.feature),
      disabled_reason: ["interface", "notes", "findings"].includes(record.feature)
        ? "This feature does not expose an independent health check."
        : undefined,
    },
    {
      id: "retry_operation",
      label: "Retry failed operation",
      kind: "retry",
      confirmation_required: true,
      enabled: durableRetry,
      disabled_reason: durableRetry
        ? undefined
        : "No durably retained failed operation is linked to this incident.",
    },
  ];
}

export function resolveDiagnosticIncidents(records: DiagnosticRecord[]): DiagnosticIncident[] {
  const groups: Array<{ records: DiagnosticRecord[]; correlations: Set<string> }> = [];
  for (const record of records.slice(0, 500)) {
    const values = correlationValues(record);
    const matches = groups.filter((group) => [...values].some((value) => group.correlations.has(value)));
    if (!matches.length) {
      groups.push({ records: [record], correlations: values });
      continue;
    }
    const target = matches[0];
    target.records.push(record);
    values.forEach((value) => target.correlations.add(value));
    for (const duplicate of matches.slice(1)) {
      target.records.push(...duplicate.records);
      duplicate.correlations.forEach((value) => target.correlations.add(value));
      groups.splice(groups.indexOf(duplicate), 1);
    }
  }
  const incidents: DiagnosticIncident[] = groups.map((group, index) => {
    const primary = primaryRecord(group.records);
    const reasonCode = reasonForRecord(primary);
    const presentation = diagnosticFailurePresentation(primary);
    const reason = diagnosticRemediationCatalog.reason_families[reasonCode];
    const feature = diagnosticRemediationCatalog.features[primary.feature];
    return {
      schema: "nebula.diagnostic-incident/v1" as const,
      error_id: primary.error_id ?? [...group.correlations][0] ?? `historical-${index}`,
      status: primary.reason_code ? "active" as const : "historical" as const,
      primary,
      related_records: group.records.filter((record) => record !== primary),
      guidance: {
        remediation_id: primary.remediation_id ?? `${primary.feature}.${reasonCode}`,
        title: reason.title,
        affected_operation: feature.operation,
        cause: presentation.cause,
        impact: presentation.impact,
        confirmed_safe_state: presentation.safeState,
        steps: presentation.steps,
        verification: presentation.verification,
        help_article: feature.help_article,
      },
      actions: actionsForRecord(primary),
      facts: Object.fromEntries(
        Object.entries({
          feature: primary.feature,
          stage: primary.stage,
          provider: primary.metadata?.provider,
          transport: primary.metadata?.transport,
          http_status: primary.metadata?.http_status,
          model_id: primary.metadata?.model_id,
          state: primary.metadata?.state,
          component: primary.metadata?.component,
        }).filter((entry): entry is [string, string | number | boolean] => ["string", "number", "boolean"].includes(typeof entry[1])),
      ),
      sensitive_detail_available: primary.sensitive_detail_available === true,
      sensitive_detail_expires_at: primary.sensitive_detail_expires_at,
    };
  });
  return incidents.sort((left, right) => (right.primary.timestamp ?? "").localeCompare(left.primary.timestamp ?? ""));
}

function incidentSignature(incident: DiagnosticIncident): string {
  const record = incident.primary;
  return [record.feature, record.event_code, reasonForRecord(record), record.stage ?? "", record.exception_type ?? ""].join(":" );
}

function referencesForIncident(incident: DiagnosticIncident): string[] {
  return [...new Set([incident.error_id, ...[incident.primary, ...incident.related_records].flatMap((record) => [
    record.error_id,
    record.request_id,
    record.operation_id,
    record.parent_operation_id,
  ])].filter((value): value is string => Boolean(value)))];
}

/** Collapses repeated incidents while retaining every record and deep-link reference. */
export function rollupDiagnosticIncidents(incidents: DiagnosticIncident[]): DiagnosticIncident[] {
  const groups = new Map<string, DiagnosticIncident[]>();
  for (const incident of incidents) {
    const signature = incidentSignature(incident);
    groups.set(signature, [...(groups.get(signature) ?? []), incident]);
  }
  return [...groups.values()].map((group) => {
    const ordered = group.slice().sort((left, right) => (right.primary.timestamp ?? "").localeCompare(left.primary.timestamp ?? ""));
    const latest = ordered[0];
    const records = ordered.flatMap((incident) => [incident.primary, ...incident.related_records]);
    const timestamps = records.map((record) => record.timestamp).filter((value): value is string => Boolean(value)).sort();
    return {
      ...latest,
      related_records: records.filter((record) => record !== latest.primary),
      occurrence_count: group.reduce((count, incident) => count + (incident.occurrence_count ?? 1), 0),
      first_occurred_at: timestamps[0],
      last_occurred_at: timestamps.at(-1),
      individual_references: [...new Set(ordered.flatMap(referencesForIncident))],
    };
  }).sort((left, right) => (right.last_occurred_at ?? right.primary.timestamp ?? "").localeCompare(left.last_occurred_at ?? left.primary.timestamp ?? ""));
}

export function diagnosticRecordMatchesReference(record: DiagnosticRecord, reference: string): boolean {
  return [record.error_id, record.request_id, record.operation_id, record.parent_operation_id].includes(reference);
}

export function diagnosticIncidentMatchesReference(incident: DiagnosticIncident, reference: string): boolean {
  return incident.error_id === reference
    || incident.individual_references?.includes(reference) === true
    || [incident.primary, ...incident.related_records]
      .some((record) => diagnosticRecordMatchesReference(record, reference));
}

export function diagnosticTechnicalDetails(record: DiagnosticRecord): string {
  return JSON.stringify(record, null, 2);
}
