import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution.js";
import "monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js";
import "monaco-editor/esm/vs/basic-languages/shell/shell.contribution.js";
import "monaco-editor/esm/vs/basic-languages/rust/rust.contribution.js";
import "monaco-editor/esm/vs/basic-languages/go/go.contribution.js";
import "monaco-editor/esm/vs/basic-languages/java/java.contribution.js";
import "monaco-editor/esm/vs/basic-languages/ruby/ruby.contribution.js";
import "monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js";
import "monaco-editor/esm/vs/language/css/monaco.contribution.js";
import "monaco-editor/esm/vs/language/html/monaco.contribution.js";
import "monaco-editor/esm/vs/language/json/monaco.contribution.js";
import "monaco-editor/esm/vs/language/typescript/monaco.contribution.js";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker.js?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker.js?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker.js?worker";
import TypeScriptWorker from "monaco-editor/esm/vs/language/typescript/ts.worker.js?worker";
import { useEffect, useRef } from "react";
import { languageIdForPath } from "./editorLanguages";

interface MonacoEditorSurfaceProps {
  active: boolean;
  filePath: string;
  onChange(value: string): void;
  onCursorChange(line: number, column: number): void;
  onSave(): void;
  value: string;
}

interface MonacoWorkerEnvironment {
  getWorker(_moduleId: string, label: string): Worker;
}

declare global {
  interface Window {
    MonacoEnvironment?: MonacoWorkerEnvironment;
  }
}

window.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === "json") return new JsonWorker();
    if (["css", "scss", "less"].includes(label)) return new CssWorker();
    if (["html", "handlebars", "razor"].includes(label)) return new HtmlWorker();
    if (["typescript", "javascript"].includes(label)) return new TypeScriptWorker();
    return new EditorWorker();
  },
};

monaco.editor.defineTheme("nebula-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "keyword", foreground: "C586C0" },
    { token: "string", foreground: "CE9178" },
    { token: "comment", foreground: "6A9955", fontStyle: "italic" },
    { token: "number", foreground: "B5CEA8" },
  ],
  colors: {
    "editor.background": "#0B0D10",
    "editor.foreground": "#D4D4D4",
    "editorGutter.background": "#090B0E",
    "editorLineNumber.foreground": "#6E7681",
    "editorLineNumber.activeForeground": "#C9D1D9",
    "editor.selectionBackground": "#264F78C7",
    "editor.lineHighlightBackground": "#FFFFFF08",
    "editorCursor.foreground": "#F4F7FB",
  },
});

export function MonacoEditorSurface({ active, filePath, onChange, onCursorChange, onSave, value }: MonacoEditorSurfaceProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | undefined>(undefined);
  const modelRef = useRef<monaco.editor.ITextModel | undefined>(undefined);
  const onChangeRef = useRef(onChange);
  const onCursorChangeRef = useRef(onCursorChange);
  const onSaveRef = useRef(onSave);
  onChangeRef.current = onChange;
  onCursorChangeRef.current = onCursorChange;
  onSaveRef.current = onSave;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const model = monaco.editor.createModel(value, languageIdForPath(filePath));
    const editor = monaco.editor.create(host, {
      model,
      theme: "nebula-dark",
      ariaLabel: "Code editor",
      automaticLayout: true,
      cursorBlinking: "solid",
      cursorSmoothCaretAnimation: "off",
      cursorStyle: "line",
      cursorWidth: 2,
      fontFamily: "var(--mono)",
      fontSize: 13,
      lineHeight: 21,
      minimap: { enabled: false },
      padding: { top: 10, bottom: 10 },
      renderWhitespace: "selection",
      scrollBeyondLastLine: false,
      smoothScrolling: true,
      tabSize: 2,
      insertSpaces: true,
      wordWrap: "off",
    });
    modelRef.current = model;
    editorRef.current = editor;
    const contentSubscription = model.onDidChangeContent(() => onChangeRef.current(model.getValue()));
    const cursorSubscription = editor.onDidChangeCursorPosition(({ position }) => onCursorChangeRef.current(position.lineNumber, position.column));
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSaveRef.current());
    if (active) editor.focus();
    return () => {
      contentSubscription.dispose();
      cursorSubscription.dispose();
      editor.dispose();
      model.dispose();
      editorRef.current = undefined;
      modelRef.current = undefined;
    };
  }, []);

  useEffect(() => {
    const model = modelRef.current;
    if (model && model.getValue() !== value) model.setValue(value);
  }, [value]);

  useEffect(() => {
    const model = modelRef.current;
    if (model) monaco.editor.setModelLanguage(model, languageIdForPath(filePath));
  }, [filePath]);

  useEffect(() => {
    if (!active) return;
    const frame = globalThis.requestAnimationFrame(() => {
      editorRef.current?.layout();
      editorRef.current?.focus();
    });
    return () => globalThis.cancelAnimationFrame(frame);
  }, [active]);

  return <div className="monaco-editor-host" ref={hostRef} />;
}
