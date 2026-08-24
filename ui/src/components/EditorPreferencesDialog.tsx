import { useMemo, useState, type KeyboardEvent } from "react";
import { RotateCcw, X } from "lucide-react";
import { DEFAULT_EDITOR_PREFERENCES, shortcutFromKeyboardEvent, type EditorAction, type EditorPreferences } from "../state/editorPreferences";
import { InlineValidationNotice } from "./InlineValidationNotice";
import { ModalSurface } from "./DialogSystem";

interface EditorPreferencesDialogProps {
  preferences: EditorPreferences;
  onApply(preferences: EditorPreferences): void;
  onClose(): void;
}

const actions: Array<[EditorAction, string]> = [
  ["save", "Save active file"],
  ["commandPalette", "Show command palette"],
  ["quickOpen", "Quick open"],
  ["workspaceSearch", "Search workspace"],
  ["find", "Find in active file"],
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
      <label className="editor-wrap-setting"><input type="checkbox" checked={draft.wordWrap} onChange={(event) => setDraft({ ...draft, wordWrap: event.target.checked })} /><span><strong>Word wrap</strong><small>Wrap long lines inside each editor pane.</small></span></label>
    </div>
    <section className="editor-keybindings" aria-labelledby="editor-keybindings-title"><header><div><h3 id="editor-keybindings-title">Keyboard shortcuts</h3><p>Focus a field and press the complete shortcut.</p></div><button className="button quiet" type="button" onClick={() => setDraft(DEFAULT_EDITOR_PREFERENCES)}><RotateCcw size={13} /> Restore defaults</button></header>{actions.map(([action, label]) => <label key={action}><span>{label}</span><input aria-label={`${label} shortcut`} className={duplicates.has(draft.keybindings[action]) ? "invalid" : undefined} value={draft.keybindings[action]} readOnly onKeyDown={(event) => capture(action, event)} /></label>)}</section>
    {duplicates.size > 0 && <InlineValidationNotice message="Each editor action needs a distinct shortcut." />}
    {saveError && <InlineValidationNotice message={saveError} />}
    <footer><button className="button quiet" type="button" onClick={onClose}>Cancel</button><button className="button primary" type="button" disabled={duplicates.size > 0} onClick={() => { try { onApply(draft); onClose(); } catch { /* diagnostic-expected: the preference writer already records the storage failure. */ setSaveError("This browser rejected device-local settings. Free browser storage or change its privacy policy, then retry."); } }}>Apply settings</button></footer>
  </ModalSurface>;
}
