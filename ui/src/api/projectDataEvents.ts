/** Refresh signals only; every listener rereads authoritative Core state. */
export const PROJECT_DATA_CHANGED_EVENT = "nebula:project-data-changed";
export type ProjectDataChange = {projectId: string} & (
  | {resource: "bookmarks"; sessionId: string}
  | {resource: "results"; resultId: string}
  | {resource: "executions"; executionId: string}
);
export function notifyProjectDataChanged(detail: ProjectDataChange): void {
  window.dispatchEvent(new CustomEvent(PROJECT_DATA_CHANGED_EVENT, {detail}));
}
