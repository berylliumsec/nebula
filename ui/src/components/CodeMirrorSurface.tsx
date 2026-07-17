import { completeAnyWord } from "@codemirror/autocomplete";
import { HighlightStyle, StreamLanguage, indentUnit, syntaxHighlighting, type LanguageSupport } from "@codemirror/language";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { basicSetup } from "codemirror";
import { useEffect, useRef } from "react";

interface CodeMirrorSurfaceProps {
  active: boolean;
  filePath: string;
  onChange(value: string): void;
  onCursorChange(line: number, column: number): void;
  onSave(): void;
  value: string;
}

export function languageLabelForPath(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return ({
    bash: "Shell", css: "CSS", go: "Go", htm: "HTML", html: "HTML", java: "Java",
    js: "JavaScript", cjs: "JavaScript", mjs: "JavaScript", jsx: "JavaScript",
    json: "JSON", md: "Markdown", py: "Python", rb: "Ruby", rs: "Rust", sh: "Shell",
    sql: "SQL", toml: "TOML", ts: "TypeScript", tsx: "TypeScript", yaml: "YAML",
    yml: "YAML", zsh: "Shell",
  } as Record<string, string>)[extension] ?? "Plain text";
}

async function languageForPath(path: string): Promise<Extension> {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  if (["js", "cjs", "mjs", "jsx", "ts", "tsx"].includes(extension)) {
    const { javascript } = await import("@codemirror/lang-javascript");
    return javascript({ typescript: extension === "ts" || extension === "tsx", jsx: extension === "jsx" || extension === "tsx" });
  }
  if (extension === "py") return (await import("@codemirror/lang-python")).python();
  if (extension === "html" || extension === "htm") return (await import("@codemirror/lang-html")).html();
  if (extension === "css") return (await import("@codemirror/lang-css")).css();
  if (extension === "json") return (await import("@codemirror/lang-json")).json();
  if (extension === "md") return (await import("@codemirror/lang-markdown")).markdown();
  if (extension === "sql") return (await import("@codemirror/lang-sql")).sql();

  let parser: Parameters<typeof StreamLanguage.define>[0] | undefined;
  if (["sh", "bash", "zsh"].includes(extension)) parser = (await import("@codemirror/legacy-modes/mode/shell")).shell;
  else if (extension === "yaml" || extension === "yml") parser = (await import("@codemirror/legacy-modes/mode/yaml")).yaml;
  else if (extension === "toml") parser = (await import("@codemirror/legacy-modes/mode/toml")).toml;
  else if (extension === "go") parser = (await import("@codemirror/legacy-modes/mode/go")).go;
  else if (extension === "rs") parser = (await import("@codemirror/legacy-modes/mode/rust")).rust;
  else if (extension === "java") parser = (await import("@codemirror/legacy-modes/mode/clike")).java;
  else if (extension === "rb") parser = (await import("@codemirror/legacy-modes/mode/ruby")).ruby;
  return parser ? StreamLanguage.define(parser) : [];
}

const nebulaTheme = EditorView.theme({
  "&": { height: "100%", color: "#d4d4d4", backgroundColor: "#0b0d10", fontSize: "13px" },
  ".cm-scroller": { overflow: "auto", fontFamily: "var(--mono)", lineHeight: "1.55" },
  ".cm-content": {
    minHeight: "100%",
    padding: "10px 0",
    caretColor: "#f4f7fb",
    fontFamily: "inherit",
    outline: "none",
  },
  ".cm-content:focus, .cm-content:focus-visible": { outline: "none" },
  ".cm-line": { padding: "0 12px", border: "0", outline: "none", boxShadow: "none" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text-strong)" },
  "&.cm-focused": { outline: "none" },
  ".cm-gutters": { color: "#6e7681", backgroundColor: "#090b0e", borderRight: "1px solid #20252d" },
  ".cm-activeLine": { backgroundColor: "rgb(255 255 255 / 3%)" },
  ".cm-activeLineGutter": { color: "#c9d1d9", backgroundColor: "rgb(255 255 255 / 4%)" },
  ".cm-selectionLayer .cm-selectionBackground, &.cm-focused .cm-selectionLayer .cm-selectionBackground, ::selection": {
    border: "0",
    borderRadius: "0",
    backgroundColor: "rgb(38 79 120 / 78%)",
    boxShadow: "none",
  },
  ".cm-panels": { color: "#d4d4d4", backgroundColor: "#16191f" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-searchMatch": { backgroundColor: "rgb(81 73 27 / 80%)", outline: "1px solid #d7ba7d" },
  ".cm-tooltip": { color: "#d4d4d4", border: "1px solid #343b46", backgroundColor: "#16191f", boxShadow: "0 12px 32px rgb(0 0 0 / 45%)" },
  ".cm-tooltip-autocomplete > ul > li": { minHeight: "24px", padding: "3px 8px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { color: "#fff", backgroundColor: "#094771" },
  ".cm-completionIcon": { color: "#75beff" },
  ".cm-completionLabel": { fontFamily: "var(--mono)" },
  ".cm-completionDetail": { color: "#8b949e", fontStyle: "normal" },
});

const nebulaHighlightStyle = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.moduleKeyword], color: "#c586c0" },
  { tag: [tags.name, tags.variableName], color: "#9cdcfe" },
  { tag: [tags.definition(tags.variableName), tags.labelName], color: "#4fc1ff" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: "#dcdcaa" },
  { tag: [tags.propertyName, tags.attributeName], color: "#9cdcfe" },
  { tag: [tags.typeName, tags.className, tags.namespace], color: "#4ec9b0" },
  { tag: [tags.string, tags.special(tags.string)], color: "#ce9178" },
  { tag: [tags.number, tags.bool, tags.null], color: "#b5cea8" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: "#6a9955", fontStyle: "italic" },
  { tag: [tags.operator, tags.punctuation, tags.separator], color: "#d4d4d4" },
  { tag: [tags.regexp, tags.escape], color: "#d16969" },
  { tag: [tags.meta, tags.annotation], color: "#d7ba7d" },
  { tag: tags.heading, color: "#569cd6", fontWeight: "700" },
  { tag: tags.link, color: "#4fc1ff", textDecoration: "underline" },
  { tag: tags.invalid, color: "#f44747", textDecoration: "underline wavy" },
]);

const documentWordCompletion = EditorState.languageData.of(() => [{ autocomplete: completeAnyWord }]);

export function CodeMirrorSurface({ active, filePath, onChange, onCursorChange, onSave, value }: CodeMirrorSurfaceProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | undefined>(undefined);
  const languageRef = useRef(new Compartment());
  const onChangeRef = useRef(onChange);
  const onCursorChangeRef = useRef(onCursorChange);
  const onSaveRef = useRef(onSave);
  onChangeRef.current = onChange;
  onCursorChangeRef.current = onCursorChange;
  onSaveRef.current = onSave;

  useEffect(() => {
    if (!hostRef.current) return;
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          nebulaTheme,
          syntaxHighlighting(nebulaHighlightStyle),
          documentWordCompletion,
          indentUnit.of("  "),
          languageRef.current.of([]),
          EditorView.contentAttributes.of({ "aria-label": "Code editor", spellcheck: "false" }),
          keymap.of([{ key: "Mod-s", run: () => { onSaveRef.current(); return true; } }]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current(update.state.doc.toString());
            if (update.selectionSet || update.docChanged) {
              const position = update.state.doc.lineAt(update.state.selection.main.head);
              onCursorChangeRef.current(position.number, update.state.selection.main.head - position.from + 1);
            }
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => { view.destroy(); viewRef.current = undefined; };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === value) return;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } });
  }, [value]);

  useEffect(() => {
    let cancelled = false;
    void languageForPath(filePath).then((language: Extension | LanguageSupport) => {
      if (!cancelled && viewRef.current) viewRef.current.dispatch({ effects: languageRef.current.reconfigure(language) });
    });
    return () => { cancelled = true; };
  }, [filePath]);

  useEffect(() => { if (active) viewRef.current?.requestMeasure(); }, [active]);

  return <div className="code-mirror-host" ref={hostRef} />;
}
