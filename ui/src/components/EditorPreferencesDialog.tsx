import { useMemo, useState, type KeyboardEvent } from "react";
import { RotateCcw, X } from "lucide-react";
import { DEFAULT_EDITOR_PREFERENCES, shortcutFromKeyboardEvent, type AutoSaveMode, type EditorAction, type EditorPreferences } from "../state/editorPreferences";
import { InlineValidationNotice } from "./InlineValidationNotice";
import { ModalSurface } from "./DialogSystem";

interface EditorPreferencesDialogProps {
  preferences: EditorPreferences;
  onApply(preferences: EditorPreferences): void;
  onClose(): void;
}

type ToggleKey = "bracketPairColors" | "indentGuides" | "minimap" | "stickyScroll" | "wordWrap";

/** Rendering choices, in the order they change what the operator sees most. */
const toggles: Array<[ToggleKey, string, string]> = [
  ["minimap", "Minimap", "A scale model of the whole file with a viewport slider and problem markers. Hidden automatically when the pane is too narrow."],
  ["stickyScroll", "Sticky scroll", "Pins the declarations the top line sits inside; click one to jump back to it. Hidden with the minimap when the pane is too narrow."],
  ["indentGuides", "Indent guides", "A vertical rule per indent level, with the block holding the cursor emphasised."],
  ["bracketPairColors", "Bracket pair colours", "Three rotating colours by nesting depth, and red for a bracket with no partner."],
  ["wordWrap", "Word wrap", "Wrap long lines inside each editor pane."],
];

const autoSaveOptions: Array<[AutoSaveMode, string]> = [
  ["off", "Off"],
  ["afterDelay", "After 1 second"],
  ["onFocusChange", "When focus leaves the editor"],
];

const actions: Array<[EditorAction, string]> = [
  ["save", "Save active file"],
  ["commandPalette", "Show command palette"],
  ["quickOpen", "Quick open"],
  ["workspaceSearch", "Search workspace"],
  ["find", "Find in active file"],
  ["gotoLine", "Go to line"],
  ["problems", "Show Problems"],
  ["format", "Format document"],
  ["rename", "Rename symbol"],
  ["tasks", "Show project tasks"],
  ["debug", "Review and start debugging"],
  ["definition", "Go to definition"],
  ["references", "Find references"],
  ["closeEditor", "Close active editor"],
  ["nextEditor", "Next editor"],
  ["splitEditor", "Split editor"],
];

export function EditorPreferencesDialog({ preferences, onApply, onClose }: EditorPreferencesDialogProps) {
  const [draft, setDraft] = useState(preferences);
  const [saveError, setSaveError] = useState<string>();
  const duplicates = useMemo(() => {
    const counts = Object.values(draft.keybindings).reduce<Record<string, number>>((current, shortcut) => ({ ...current, [shortcut]: (current[shortcut] ?? 0) + 1 }), {});
    return new Set(Object.entries(counts).filter(([, count]) => count > 1).map(([shortcut]) => shortcut));
  }, [draft.keybindings]);

  const capture = (action: EditorAction, event: KeyboardEvent<HTMLInputElement>) => {
    // Plain Tab and Shift+Tab move focus between the fields; swallowing them
    // trapped keyboard operators in the first shortcut input. Only a modified
    // chord such as Mod+Tab is a shortcut worth capturing.
    if (event.key === "Tab" && !event.metaKey && !event.ctrlKey && !event.altKey) return;
    event.preventDefault();
    event.stopPropagation();
    const shortcut = shortcutFromKeyboardEvent(event.nativeEvent);
    if (shortcut) setDraft((current) => ({ ...current, keybindings: { ...current.keybindings, [action]: shortcut } }));
  };

  return <ModalSurface className="editor-preferences-dialog" labelledBy="editor-preferences-title" onClose={onClose}>
    <header><div><small>Device-local preferences</small><h2 id="editor-preferences-title">Editor settings and keybindings</h2></div><button className="icon-button subtle" type="button" aria-label="Close editor settings" onClick={onClose}><X size={17} /></button></header>
    <div className="editor-preference-fields">
      <label>Font size<select value={draft.fontSize} onChange={(event) => setDraft({ ...draft, fontSize: Number(event.target.value) as EditorPreferences["fontSize"] })}><option value="12">12 px</option><option value="13">13 px</option><option value="14">14 px</option><option value="16">16 px</option></select></label>
      <label>Tab size<select value={draft.tabSize} onChange={(event) => setDraft({ ...draft, tabSize: Number(event.target.value) as EditorPreferences["tabSize"] })}><option value="2">2 spaces</option><option value="4">4 spaces</option></select></label>
      <label>Auto save<select value={draft.autoSave} onChange={(event) => setDraft({ ...draft, autoSave: event.target.value as AutoSaveMode })}>{autoSaveOptions.map(([mode, label]) => <option key={mode} value={mode}>{label}</option>)}</select></label>
      {toggles.map(([key, label, note]) => <label className="editor-toggle-setting" key={key}><input type="checkbox" checked={draft[key]} onChange={(event) => setDraft({ ...draft, [key]: event.target.checked })} /><span><strong>{label}</strong><small>{note}</small></span></label>)}
    </div>
    <p className="editor-preference-note">Auto save only writes files that already exist in the workspace, and never while a newer version is waiting for your decision.</p>
    <section className="editor-keybindings" aria-labelledby="editor-keybindings-title"><header><div><h3 id="editor-keybindings-title">Keyboard shortcuts</h3><p>Focus a field and press the complete shortcut.</p></div><button className="button quiet" type="button" onClick={() => setDraft(DEFAULT_EDITOR_PREFERENCES)}><RotateCcw size={13} /> Restore defaults</button></header>{actions.map(([action, label]) => <label key={action}><span>{label}</span><input aria-label={`${label} shortcut`} className={duplicates.has(draft.keybindings[action]) ? "invalid" : undefined} value={draft.keybindings[action]} readOnly onKeyDown={(event) => capture(action, event)} /></label>)}</section>
    {duplicates.size > 0 && <InlineValidationNotice message="Each editor action needs a distinct shortcut." />}
    {saveError && <InlineValidationNotice message={saveError} />}
    <footer><button className="button quiet" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="button" disabled={duplicates.size > 0} onClick={() => { try { onApply(draft); onClose(); } catch { /* diagnostic-expected: the preference writer already records the storage failure. */ setSaveError("This browser rejected device-local settings. Free browser storage or change its privacy policy, then retry."); } }}>Apply settings</button></footer>
  </ModalSurface>;
}
