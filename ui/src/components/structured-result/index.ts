/**
 * The schema-agnostic structured-result explorer.
 *
 * Layers, in the order a value moves through them: a normalizer describes the
 * published value, an analyzer classifies its shapes, an optional hint adapter
 * refines the presentation, renderers draw one view each, and one inspector
 * describes any node whichever view selected it. No layer imports a producer's
 * types, and none of them changes the value.
 */

export { analyze, SAMPLE_LIMIT, type GraphCandidate, type ResultAnalysis, type TableCandidate } from "./analyze";
export { compactValue, copyText, formatValue, safeHref, type FormatHint, type FormattedValue } from "./format";
export { buildPlan, readHints, type PresentationHints, type PresentationPlan } from "./hints";
export {
  formatPath,
  isScalar,
  joinPath,
  NormalizedResult,
  pathSegments,
  runtimeTypeOf,
  typeLabel,
  type ResultNode,
  type RuntimeType,
} from "./normalize";
export { renderRaw, type RawDocument } from "./rawText";
export { structuralReferences, type StructuralReference } from "./references";
export { AgentViewPanel } from "./AgentViewPanel";
export { AgentViewBody, agentViewStatus, useAgentViewStream, type AgentViewStream } from "./AgentViewBody";
export { InspectorPanel } from "./InspectorPanel";
export { PropertyGrid } from "./PropertyGrid";
export { ResultTimeline, shapeLabel, whenLabel } from "./ResultTimeline";
export {
  DASHBOARD_VIEWS,
  isDashboardView,
  StructuredResultDashboard,
  type DashboardView,
} from "./StructuredResultDashboard";
export { TreeView } from "./TreeView";
export { TableView } from "./TableView";
export { RawView } from "./RawView";
export { OverviewView } from "./OverviewView";
export { ViewBoundary } from "./ViewBoundary";
export {
  groupStreams,
  useStructuredResult,
  useStructuredResults,
  useUnseenCount,
  type ResultStream,
  type StructuredResultDetail,
  type StructuredResultList,
} from "./useStructuredResults";
