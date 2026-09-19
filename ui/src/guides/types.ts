import type { ApiClient } from "../api/client";
import type { GuideStarterKind } from "../api/types";
import type { GuideAction } from "./guideActions";

export type GuidePage = "workbench" | "settings" | "project";
export type GuideCategory = "assistant" | "workspace" | "connections" | "keyboard";

export interface GuideRunContext {
  api?: ApiClient;
  engagementId?: string;
  /** Values chosen earlier in the guide, such as the hook folder name. */
  values: Record<string, string>;
}

export interface GuideCheckResult {
  state: "waiting" | "done" | "problem";
  message: string;
}

export interface GuideStarter {
  kind: GuideStarterKind;
  /** Label for the name field; hooks and skills need a folder name, AGENTS.md does not. */
  nameLabel?: string;
  defaultName?: string;
}

export interface GuideStep {
  title: string;
  /** Paragraphs. Text in `backticks` renders as code. */
  body: string[];
  /** A consequence the operator must know, shown as a warning callout. */
  callout?: string;
  /** A shell command to copy; it runs where Nebula Core runs, not in this browser. */
  command?: string;
  /** Where the step happens. Undefined keeps the current page. */
  route?: (context: GuideRunContext) => string | undefined;
  action?: GuideAction;
  /** `data-guide` name of the control to highlight. */
  target?: string;
  /** Shown when the target is not on screen, e.g. the feature needs another runtime. */
  targetMissing?: string;
  requiresProject?: boolean;
  starter?: GuideStarter;
  /** Polled while the step is open; confirms the operator actually did it. */
  check?: (context: GuideRunContext) => Promise<GuideCheckResult> | GuideCheckResult;
}

export interface GuideDefinition {
  id: string;
  title: string;
  summary: string;
  category: GuideCategory;
  /** Pages where the guide is offered first. */
  pages: GuidePage[];
  keywords: string;
  steps: GuideStep[];
}

export const guideCategories: Array<{ id: GuideCategory; label: string }> = [
  { id: "assistant", label: "Assistant & agents" },
  { id: "workspace", label: "Workspace files" },
  { id: "connections", label: "Connections & devices" },
  { id: "keyboard", label: "Keyboard & palette" },
];
