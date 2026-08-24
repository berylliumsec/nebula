import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, defaultHighlightStyle, foldGutter, foldKeymap, indentOnInput, StreamLanguage, indentUnit, syntaxHighlighting, type LanguageSupport } from "@codemirror/language";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { crosshairCursor, dropCursor, EditorView, GutterMarker, gutter, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, keymap, lineNumbers, rectangularSelection } from "@codemirror/view";
import { useEffect, useRef } from "react";
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { highlightSelectionMatches, openSearchPanel, search, searchKeymap } from "@codemirror/search";
import { openLintPanel } from "@codemirror/lint";
import { createLanguageServer, type LanguageServerState } from "../api/languageServer";
import { findReferences, formatDocument, jumpToDefinition, renameSymbol } from "@codemirror/lsp-client";

interface CodeMirrorSurfaceProps {
  active: boolean;
  ariaLabel?: string;
  filePath: string;
  fontSize?: 12 | 13 | 14 | 16;
  onChange(value: string): void;
  onCursorChange(line: number, column: number): void;
  onFocus?(): void;
  onSave(): void;
  onSelectionChange?(text: string): void;
  completionSource?(context: CompletionContext): Promise<CompletionResult | null>;
  findRequest?: number;
  problemsRequest?: number;
  formatRequest?: number;
  definitionRequest?: number;
  referencesRequest?: number;
  renameRequest?: number;
  reveal?: { line: number; column: number; request: number };
  saveKey?: string;
  tabSize?: 2 | 4;
  value: string;
  wordWrap?: boolean;
  languageServer?: { apiBaseUrl: string; engagementId: string; token?: string; onState(state: LanguageServerState): void };
  breakpointLines?: number[];
  onToggleBreakpoint?(line: number): void;
}

class BreakpointMarker extends GutterMarker {
  toDOM(): HTMLElement {
    const marker = document.createElement("span");
    marker.className = "cm-debug-breakpoint";
    marker.setAttribute("aria-hidden", "true");
    return marker;
  }
}

const breakpointMarker = new BreakpointMarker();

function breakpointGutter(lines: number[], onToggle?: (line: number) => void): Extension {
  const selected = new Set(lines);
  return gutter({
    class: "cm-debug-gutter",
    lineMarker: (view, line) => selected.has(view.state.doc.lineAt(line.from).number) ? breakpointMarker : null,
    domEventHandlers: {
      mousedown: (view, line, event) => {
        if ((event as MouseEvent).button !== 0 || !onToggle) return false;
        event.preventDefault();
        onToggle(view.state.doc.lineAt(line.from).number);
        return true;
      },
    },
  });
}

export function languageLabelForPath(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return ({
    bash: "Shell", c: "C", cc: "C++", cpp: "C++", css: "CSS", cxx: "C++",
    go: "Go", h: "C header", hpp: "C++ header", htm: "HTML", html: "HTML", java: "Java",
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
  else if (["c", "h", "cc", "cpp", "cxx", "hpp"].includes(extension)) {
    const clike = await import("@codemirror/legacy-modes/mode/clike");
    parser = ["c", "h"].includes(extension) ? clike.c : clike.cpp;
  }
  return parser ? StreamLanguage.define(parser) : [];
}

const nebulaTheme = EditorView.theme({
  "&": { width: "100%", height: "100%", color: "var(--text)", backgroundColor: "var(--canvas)", fontSize: "var(--editor-font-size, 13px)" },
  ".cm-scroller": { overflow: "auto", fontFamily: "var(--font-mono)", lineHeight: "1.5" },
  ".cm-content": { minHeight: "100%", caretColor: "var(--text-strong)", fontFamily: "inherit", padding: "10px 0", outline: "none" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text-strong)" },
  "&.cm-focused": { outline: "none" },
  ".cm-gutters": { color: "var(--muted)", backgroundColor: "var(--surface-muted)", borderRight: "1px solid var(--border-soft)" },
  ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "color-mix(in srgb, var(--blue-muted) 42%, transparent)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": { borderRadius: "0", backgroundColor: "color-mix(in srgb, var(--blue) 35%, transparent)" },
  ".cm-panels": { color: "var(--text)", backgroundColor: "var(--surface-raised)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-search": { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px", padding: "7px 9px" },
  ".cm-search input": { minHeight: "32px", padding: "5px 8px", border: "1px solid var(--border)", borderRadius: "var(--radius-control)", color: "var(--text)", backgroundColor: "var(--canvas)", font: "12px var(--font-mono)" },
  ".cm-search button": { minWidth: "32px", minHeight: "32px", border: "1px solid var(--border)", borderRadius: "var(--radius-control)", color: "var(--text)", backgroundColor: "var(--surface)", cursor: "pointer" },
  ".cm-search button:hover, .cm-search button:focus-visible": { borderColor: "var(--blue)", backgroundColor: "var(--surface-hover)" },
  ".cm-searchMatch": { backgroundColor: "var(--yellow-muted)", outline: "1px solid var(--yellow)" },
  ".cm-foldGutter .cm-gutterElement": { cursor: "pointer" },
  ".cm-debug-gutter .cm-gutterElement": { cursor: "pointer", minWidth: "15px" },
  ".cm-debug-breakpoint": { display: "block", width: "9px", height: "9px", margin: "0 3px", borderRadius: "50%", background: "var(--red)", boxShadow: "0 0 0 1px color-mix(in srgb, var(--red) 70%, black)" },
});

export function CodeMirrorSurface({ active, ariaLabel = "Code editor", filePath, fontSize = 13, onChange, onCursorChange, onFocus, onSave, onSelectionChange, completionSource, definitionRequest = 0, findRequest = 0, problemsRequest = 0, formatRequest = 0, referencesRequest = 0, renameRequest = 0, reveal, saveKey = "Mod-s", tabSize = 2, value, wordWrap = false, languageServer, breakpointLines = [], onToggleBreakpoint }: CodeMirrorSurfaceProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | undefined>(undefined);
  const languageRef = useRef(new Compartment());
  const attributesRef = useRef(new Compartment());
  const keymapRef = useRef(new Compartment());
  const settingsRef = useRef(new Compartment());
  const breakpointRef = useRef(new Compartment());
  const onChangeRef = useRef(onChange);
  const onCursorChangeRef = useRef(onCursorChange);
  const onSaveRef = useRef(onSave);
  const onSelectionChangeRef = useRef(onSelectionChange);
  const completionSourceRef = useRef(completionSource);
  const languageServerRef = useRef(languageServer);
  languageServerRef.current = languageServer;
  onChangeRef.current = onChange;
  onCursorChangeRef.current = onCursorChange;
  onSaveRef.current = onSave;
  onSelectionChangeRef.current = onSelectionChange;
  completionSourceRef.current = completionSource;

  useEffect(() => {
    if (!hostRef.current) return;
    const host = hostRef.current;
    const root = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    root.replaceChildren();
    const boundaryStyle = document.createElement("style");
    boundaryStyle.textContent = `
      :host { display: flex; width: 100%; height: 100%; min-width: 0; min-height: 0; overflow: hidden; }
      .code-mirror-mount { display: flex; min-width: 0; min-height: 0; flex: 1 1 0; overflow: hidden; }
      .code-mirror-mount > .cm-editor { min-width: 0; min-height: 0; flex: 1 1 0; }
    `;
    const mount = document.createElement("div");
    mount.className = "code-mirror-mount";
    root.append(boundaryStyle, mount);
    const lsp = filePath.endsWith(".py") && languageServerRef.current
      ? createLanguageServer(languageServerRef.current.apiBaseUrl, languageServerRef.current.engagementId, languageServerRef.current.token, languageServerRef.current.onState)
      : undefined;
    const view = new EditorView({
      parent: mount,
      root,
      state: EditorState.create({
        doc: value,
        extensions: [
          // Do not use basicSetup/drawSelection here. Its synthetic cursor layer is
          // positioned on the wrong line by macOS WKWebView after Enter key updates.
          // The native contenteditable caret stays coupled to the browser selection.
          lineNumbers(),
          breakpointRef.current.of(breakpointGutter(breakpointLines, onToggleBreakpoint)),
          foldGutter(),
          highlightActiveLineGutter(),
          highlightSpecialChars(),
          history(),
          dropCursor(),
          rectangularSelection(),
          crosshairCursor(),
          indentOnInput(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          bracketMatching(),
          closeBrackets(),
          highlightSelectionMatches(),
          highlightActiveLine(),
          EditorState.allowMultipleSelections.of(true),
          nebulaTheme,
          settingsRef.current.of(editorSettings(tabSize, wordWrap)),
          languageRef.current.of([]),
          attributesRef.current.of(EditorView.contentAttributes.of({ "aria-label": ariaLabel, spellcheck: "false" })),
          search({ top: true }),
          autocompletion({
            activateOnTyping: true,
            maxRenderedOptions: 30,
            override: completionSource ? [(context) => completionSourceRef.current?.(context) ?? null] : undefined,
          }),
          lsp?.client.plugin(`file:///workspace/${filePath.split("/").map(encodeURIComponent).join("/")}`, "python") ?? [],
          keymapRef.current.of(editorKeymap(saveKey, () => onSaveRef.current())),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current(update.state.doc.toString());
            if (update.selectionSet || update.docChanged) {
              const position = update.state.doc.lineAt(update.state.selection.main.head);
              onCursorChangeRef.current(position.number, update.state.selection.main.head - position.from + 1);
              const range = update.state.selection.main;
              onSelectionChangeRef.current?.(update.state.sliceDoc(range.from, range.to));
            }
          }),
        ],
      }),
    });
    viewRef.current = view;
    let measureFrame = 0;
    const requestMeasurement = () => {
      cancelAnimationFrame(measureFrame);
      measureFrame = requestAnimationFrame(() => view.requestMeasure());
    };
    const resizeObserver = new ResizeObserver(requestMeasurement);
    resizeObserver.observe(host);
    void document.fonts.ready.then(() => { if (viewRef.current === view) requestMeasurement(); });
    requestMeasurement();
    return () => {
      cancelAnimationFrame(measureFrame);
      resizeObserver.disconnect();
      view.destroy();
      lsp?.close();
      root.replaceChildren();
      viewRef.current = undefined;
    };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === value) return;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } });
  }, [value]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: attributesRef.current.reconfigure(EditorView.contentAttributes.of({ "aria-label": ariaLabel, spellcheck: "false" })) });
  }, [ariaLabel]);

  useEffect(() => {
    let cancelled = false;
    const host = hostRef.current;
    host?.removeAttribute("data-language-ready");
    void languageForPath(filePath).then((language: Extension | LanguageSupport) => {
      if (!cancelled && viewRef.current) {
        viewRef.current.dispatch({ effects: languageRef.current.reconfigure(language) });
        host?.setAttribute("data-language-ready", filePath);
      }
    });
    return () => { cancelled = true; };
  }, [filePath]);

  useEffect(() => {
    hostRef.current?.style.setProperty("--editor-font-size", `${fontSize}px`);
    viewRef.current?.dispatch({ effects: settingsRef.current.reconfigure(editorSettings(tabSize, wordWrap)) });
  }, [fontSize, tabSize, wordWrap]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: keymapRef.current.reconfigure(editorKeymap(saveKey, () => onSaveRef.current())) });
  }, [saveKey]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: breakpointRef.current.reconfigure(breakpointGutter(breakpointLines, onToggleBreakpoint)) });
  }, [breakpointLines, onToggleBreakpoint]);

  useEffect(() => { if (active) viewRef.current?.requestMeasure(); }, [active]);

  useEffect(() => {
    if (!findRequest || !viewRef.current) return;
    openSearchPanel(viewRef.current);
  }, [findRequest]);

  useEffect(() => {
    if (!problemsRequest || !viewRef.current) return;
    openLintPanel(viewRef.current);
  }, [problemsRequest]);

  useEffect(() => {
    if (formatRequest && viewRef.current) void formatDocument(viewRef.current);
  }, [formatRequest]);

  useEffect(() => {
    if (definitionRequest && viewRef.current) jumpToDefinition(viewRef.current);
  }, [definitionRequest]);

  useEffect(() => {
    if (referencesRequest && viewRef.current) findReferences(viewRef.current);
  }, [referencesRequest]);

  useEffect(() => {
    if (renameRequest && viewRef.current) renameSymbol(viewRef.current);
  }, [renameRequest]);

  useEffect(() => {
    const view = viewRef.current;
    if (!reveal || !view) return;
    const line = view.state.doc.line(Math.min(Math.max(reveal.line, 1), view.state.doc.lines));
    const position = Math.min(line.to, line.from + Math.max(reveal.column - 1, 0));
    view.dispatch({
      selection: { anchor: position },
      effects: EditorView.scrollIntoView(position, { y: "center" }),
    });
    view.focus();
  }, [reveal]);

  return <div className="code-mirror-host" data-selection-actions-disabled onFocusCapture={onFocus} ref={hostRef} />;
}

function editorSettings(tabSize: 2 | 4, wordWrap: boolean): Extension {
  return [
    EditorState.tabSize.of(tabSize),
    indentUnit.of(" ".repeat(tabSize)),
    wordWrap ? EditorView.lineWrapping : [],
  ];
}

function editorKeymap(saveKey: string, onSave: () => void): Extension {
  return keymap.of([
    { key: saveKey, run: () => { onSave(); return true; } },
    indentWithTab,
    ...closeBracketsKeymap,
    ...completionKeymap,
    ...searchKeymap,
    ...foldKeymap,
    ...defaultKeymap,
    ...historyKeymap,
  ]);
}
