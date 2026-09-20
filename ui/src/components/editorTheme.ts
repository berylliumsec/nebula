import { HighlightStyle } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags } from "@lezer/highlight";

/**
 * Nebula's code palette.
 *
 * CodeMirror's `defaultHighlightStyle` is tuned for white backgrounds, so every
 * dark Nebula theme rendered code between 1.3:1 and 2.1:1 contrast. Colours here
 * are CSS variables instead of literals, which keeps one highlight style working
 * for the calm, dark, light, Zero dark and Zero light themes: custom properties
 * inherit through the editor's shadow root, so the surrounding theme decides the
 * actual colour. Declarations live in `tokens.css`.
 */
export const nebulaHighlightStyle = HighlightStyle.define([
  { tag: [tags.comment, tags.lineComment, tags.blockComment, tags.docComment], color: "var(--code-comment)", fontStyle: "italic" },
  { tag: [tags.keyword, tags.controlKeyword, tags.definitionKeyword, tags.moduleKeyword, tags.operatorKeyword], color: "var(--code-keyword)" },
  { tag: [tags.self, tags.atom, tags.bool, tags.null, tags.unit, tags.constant(tags.variableName)], color: "var(--code-constant)" },
  { tag: [tags.number, tags.integer, tags.float], color: "var(--code-number)" },
  { tag: [tags.string, tags.special(tags.string), tags.regexp, tags.character], color: "var(--code-string)" },
  { tag: [tags.escape, tags.special(tags.brace)], color: "var(--code-escape)" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName), tags.function(tags.definition(tags.variableName)), tags.labelName, tags.macroName], color: "var(--code-function)" },
  { tag: [tags.typeName, tags.className, tags.namespace, tags.standard(tags.typeName), tags.definition(tags.typeName)], color: "var(--code-type)" },
  { tag: [tags.variableName, tags.definition(tags.variableName), tags.local(tags.variableName)], color: "var(--code-variable)" },
  { tag: [tags.propertyName, tags.definition(tags.propertyName), tags.special(tags.propertyName)], color: "var(--code-property)" },
  { tag: [tags.tagName, tags.angleBracket], color: "var(--code-tag)" },
  { tag: [tags.attributeName], color: "var(--code-attribute)" },
  { tag: [tags.attributeValue], color: "var(--code-string)" },
  { tag: [tags.operator, tags.compareOperator, tags.arithmeticOperator, tags.logicOperator, tags.bitwiseOperator, tags.updateOperator, tags.definitionOperator, tags.derefOperator], color: "var(--code-operator)" },
  { tag: [tags.punctuation, tags.separator, tags.bracket, tags.paren, tags.brace, tags.squareBracket], color: "var(--code-punctuation)" },
  { tag: [tags.meta, tags.processingInstruction, tags.annotation, tags.modifier, tags.documentMeta], color: "var(--code-meta)" },
  { tag: [tags.heading, tags.heading1, tags.heading2, tags.heading3, tags.heading4, tags.heading5, tags.heading6], color: "var(--code-heading)", fontWeight: "600" },
  { tag: [tags.link, tags.url], color: "var(--code-link)", textDecoration: "underline" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strong, fontWeight: "600" },
  { tag: tags.strikethrough, textDecoration: "line-through" },
  { tag: [tags.inserted, tags.changed], color: "var(--code-string)" },
  { tag: tags.deleted, color: "var(--code-invalid)" },
  { tag: [tags.invalid, tags.special(tags.name)], color: "var(--code-invalid)" },
]);

/**
 * Editor chrome. Kept beside the palette because gutters, panels and overlays
 * have to stay legible against the same theme surfaces the palette assumes.
 */
export const nebulaEditorTheme = EditorView.theme({
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
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": { outline: "1px solid color-mix(in srgb, var(--code-bracket-2) 70%, transparent)", backgroundColor: "transparent" },
  ".cm-nonmatchingBracket, &.cm-focused .cm-nonmatchingBracket": { outline: "1px solid var(--code-invalid)", backgroundColor: "transparent" },
  // Nesting depth, VS Code style. Three colours rotate so a misplaced brace is
  // visible without counting characters.
  ".cm-bracket-depth-0": { color: "var(--code-bracket-1)" },
  ".cm-bracket-depth-1": { color: "var(--code-bracket-2)" },
  ".cm-bracket-depth-2": { color: "var(--code-bracket-3)" },
  ".cm-bracket-unmatched": { color: "var(--code-invalid)" },
  // Indent guides are painted per line so they cost one decoration, not one node
  // per level. `--cm-indent-step` is measured from the real character width.
  ".cm-indent-guides": {
    backgroundImage: "repeating-linear-gradient(to right, var(--code-indent-guide) 0, var(--code-indent-guide) 1px, transparent 1px, transparent var(--cm-indent-step, 14px))",
    backgroundSize: "calc(var(--cm-indent-levels, 0) * var(--cm-indent-step, 14px)) 100%",
    backgroundRepeat: "no-repeat",
    backgroundPositionX: "var(--cm-indent-origin, 6px)",
  },
  ".cm-indent-guides.cm-indent-guide-active": {
    backgroundImage: "linear-gradient(var(--code-indent-guide-active), var(--code-indent-guide-active)), repeating-linear-gradient(to right, var(--code-indent-guide) 0, var(--code-indent-guide) 1px, transparent 1px, transparent var(--cm-indent-step, 14px))",
    backgroundSize: "1px 100%, calc(var(--cm-indent-levels, 0) * var(--cm-indent-step, 14px)) 100%",
    backgroundPositionX: "calc(var(--cm-indent-origin, 6px) + var(--cm-indent-active, 0) * var(--cm-indent-step, 14px)), var(--cm-indent-origin, 6px)",
  },
  // Top and height are set from the scroller so search panels are never covered.
  ".cm-nebula-minimap": {
    position: "absolute",
    top: "0",
    right: "0",
    height: "100%",
    width: "var(--cm-minimap-width, 74px)",
    borderLeft: "1px solid var(--border-soft)",
    backgroundColor: "var(--surface-muted)",
    cursor: "pointer",
    overflow: "hidden",
    zIndex: "2",
  },
  ".cm-nebula-minimap canvas": { display: "block", width: "100%", height: "100%" },
  ".cm-nebula-minimap-slider": {
    position: "absolute",
    left: "0",
    right: "0",
    backgroundColor: "var(--code-minimap-slider)",
    borderTop: "1px solid var(--code-minimap-slider-strong)",
    borderBottom: "1px solid var(--code-minimap-slider-strong)",
    pointerEvents: "none",
  },
  ".cm-nebula-minimap:hover .cm-nebula-minimap-slider": { backgroundColor: "var(--code-minimap-slider-strong)" },
  ".cm-nebula-sticky": {
    position: "absolute",
    top: "0",
    left: "0",
    right: "var(--cm-sticky-inset, 0px)",
    zIndex: "3",
    backgroundColor: "var(--surface)",
    borderBottom: "1px solid var(--border-soft)",
    fontFamily: "var(--font-mono)",
    lineHeight: "1.5",
    pointerEvents: "auto",
  },
  ".cm-nebula-sticky-line": {
    display: "block",
    width: "100%",
    padding: "0",
    border: "0",
    color: "var(--text)",
    background: "transparent",
    font: "inherit",
    fontSize: "var(--editor-font-size, 13px)",
    textAlign: "left",
    whiteSpace: "pre",
    overflow: "hidden",
    textOverflow: "ellipsis",
    cursor: "pointer",
  },
  ".cm-nebula-sticky-line:hover, .cm-nebula-sticky-line:focus-visible": { backgroundColor: "var(--surface-hover)", outline: "none" },
  ".cm-nebula-sticky-number": { display: "inline-block", color: "var(--muted)", textAlign: "right", paddingRight: "var(--cm-sticky-number-gap, 12px)", width: "var(--cm-sticky-number-width, 30px)" },
});
