import { StreamLanguage, indentUnit, type LanguageSupport } from "@codemirror/language";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
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
  "&": { height: "100%", color: "var(--text)", backgroundColor: "var(--canvas)", fontSize: "12px" },
  ".cm-content": { caretColor: "var(--text-strong)", fontFamily: "var(--mono)", padding: "10px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text-strong)" },
  "&.cm-focused": { outline: "none" },
  ".cm-gutters": { color: "var(--muted)", backgroundColor: "var(--surface-muted)", borderRight: "1px solid var(--border-soft)" },
  ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "color-mix(in srgb, var(--blue-muted) 42%, transparent)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": { backgroundColor: "color-mix(in srgb, var(--blue) 35%, transparent)" },
  ".cm-panels": { color: "var(--text)", backgroundColor: "var(--surface-raised)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-searchMatch": { backgroundColor: "var(--yellow-muted)", outline: "1px solid var(--yellow)" },
});

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
