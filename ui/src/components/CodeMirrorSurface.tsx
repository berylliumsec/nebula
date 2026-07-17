import { completeAnyWord } from "@codemirror/autocomplete";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { HighlightStyle, StreamLanguage, indentUnit, syntaxHighlighting, type LanguageSupport } from "@codemirror/language";
import { c, cpp, java } from "@codemirror/legacy-modes/mode/clike";
import { go } from "@codemirror/legacy-modes/mode/go";
import { ruby } from "@codemirror/legacy-modes/mode/ruby";
import { rust } from "@codemirror/legacy-modes/mode/rust";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { yaml } from "@codemirror/legacy-modes/mode/yaml";
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

function languageForPath(path: string): Extension | LanguageSupport {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  if (["js", "cjs", "mjs", "jsx", "ts", "tsx"].includes(extension)) {
    return javascript({ typescript: extension === "ts" || extension === "tsx", jsx: extension === "jsx" || extension === "tsx" });
  }
  if (extension === "py") return python();
  if (extension === "html" || extension === "htm") return html();
  if (extension === "css") return css();
  if (extension === "json") return json();
  if (extension === "md") return markdown();
  if (extension === "sql") return sql();

  let parser: Parameters<typeof StreamLanguage.define>[0] | undefined;
  if (["sh", "bash", "zsh"].includes(extension)) parser = shell;
  else if (extension === "yaml" || extension === "yml") parser = yaml;
  else if (extension === "toml") parser = toml;
  else if (extension === "go") parser = go;
  else if (extension === "rs") parser = rust;
  else if (extension === "java") parser = java;
  else if (extension === "rb") parser = ruby;
  else if (["c", "h"].includes(extension)) parser = c;
  else if (["cc", "cpp", "cxx", "hpp"].includes(extension)) parser = cpp;
  return parser ? StreamLanguage.define(parser) : [];
}

const fontStack = '"SFMono-Regular", "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace';

const nebulaTheme = EditorView.theme({
  "&": { height: "100%", color: "#d4d4d4", backgroundColor: "#0b0d10", fontSize: "13px" },
  ".cm-scroller": { overflow: "auto", fontFamily: fontStack, lineHeight: "21px" },
  ".cm-content": { minHeight: "100%", padding: "0", caretColor: "#f4f7fb", fontFamily: "inherit", outline: "none" },
  ".cm-content:focus, .cm-content:focus-visible": { outline: "none" },
  ".cm-line": { minHeight: "21px", padding: "0 12px", border: "0", outline: "none", boxShadow: "none" },
  ".cm-cursor, .cm-dropCursor": { borderLeft: "2px solid #f4f7fb" },
  "&.cm-focused": { outline: "none" },
  ".cm-gutters": { color: "#6e7681", backgroundColor: "#090b0e", borderRight: "1px solid #20252d" },
  ".cm-activeLine": { backgroundColor: "rgb(255 255 255 / 3%)" },
  ".cm-activeLineGutter": { color: "#c9d1d9", backgroundColor: "rgb(255 255 255 / 4%)" },
  ".cm-selectionLayer .cm-selectionBackground, &.cm-focused .cm-selectionLayer .cm-selectionBackground, ::selection": {
    border: "0", borderRadius: "0", backgroundColor: "rgb(38 79 120 / 78%)", boxShadow: "none",
  },
  ".cm-panels": { color: "#d4d4d4", backgroundColor: "#16191f" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-searchMatch": { backgroundColor: "rgb(81 73 27 / 80%)", outline: "1px solid #d7ba7d" },
  ".cm-tooltip": { color: "#d4d4d4", border: "1px solid #343b46", backgroundColor: "#16191f", boxShadow: "0 12px 32px rgb(0 0 0 / 45%)" },
  ".cm-tooltip-autocomplete > ul > li": { minHeight: "24px", padding: "3px 8px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { color: "#fff", backgroundColor: "#094771" },
  ".cm-completionIcon": { color: "#75beff" },
  ".cm-completionLabel": { fontFamily: fontStack },
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
          languageRef.current.of(languageForPath(filePath)),
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
    if (active) view.focus();
    return () => { view.destroy(); viewRef.current = undefined; };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === value) return;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } });
  }, [value]);

  useEffect(() => {
    const view = viewRef.current;
    if (view) view.dispatch({ effects: languageRef.current.reconfigure(languageForPath(filePath)) });
  }, [filePath]);

  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(() => { viewRef.current?.requestMeasure(); viewRef.current?.focus(); });
    return () => cancelAnimationFrame(frame);
  }, [active]);

  return <div className="code-mirror-host" ref={hostRef} />;
}
