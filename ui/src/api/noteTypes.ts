/** Saved editable note types; keep Core's original type and provenance intact. */
export function isProjectNoteType(type: string): boolean {
  return type === "note" || type === "ai_tool_note" || type === "ai_execution_note";
}
