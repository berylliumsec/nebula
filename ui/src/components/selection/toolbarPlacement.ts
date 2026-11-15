export interface PlacementRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface ToolbarPlacementInput {
  /** The selected text, or the text box that holds it. The toolbar never covers it. */
  anchor: PlacementRect;
  /** Measured toolbar size, including any wrapping at the current width. */
  size: { width: number; height: number };
  viewport: { width: number; height: number };
  /** The modal that holds the selection, when there is one. */
  dialog?: PlacementRect;
  /** Buttons, links and other commands that the toolbar must never cover. */
  actions?: readonly PlacementRect[];
  /** Other form fields that the toolbar avoids when another spot is clear. */
  fields?: readonly PlacementRect[];
  margin?: number;
  gap?: number;
}

export interface ToolbarPlacement {
  left: number;
  top: number;
  side: "above" | "below";
}

function overlaps(a: PlacementRect, b: PlacementRect): boolean {
  return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0;
}

/**
 * Chooses where the selected-text toolbar goes. Near the selection first, then
 * outside the dialog, then at a viewport edge. Inside a dialog a spot that
 * would cover one of its commands is never used; with no safe spot the result
 * is null and the toolbar stays hidden, so a tap always reaches the control
 * drawn there.
 */
export function placeSelectionToolbar(input: ToolbarPlacementInput): ToolbarPlacement | null {
  const margin = input.margin ?? 12;
  const gap = input.gap ?? 8;
  const { width, height } = input.size;
  const { width: viewportWidth, height: viewportHeight } = input.viewport;
  const center = (input.anchor.left + input.anchor.right) / 2;
  const left = width + margin * 2 > viewportWidth
    ? Math.max(0, (viewportWidth - width) / 2)
    : Math.min(Math.max(margin, center - width / 2), viewportWidth - margin - width);

  const tops = [input.anchor.bottom + gap, input.anchor.top - gap - height];
  if (input.dialog) tops.push(input.dialog.top - gap - height, input.dialog.bottom + gap);
  tops.push(margin, viewportHeight - margin - height);

  const actions = input.actions ?? [];
  const fields = input.fields ?? [];
  const placementAt = (top: number): ToolbarPlacement => ({
    left,
    top,
    side: top + height / 2 < (input.anchor.top + input.anchor.bottom) / 2 ? "above" : "below",
  });
  const candidates = tops
    .filter((top) => top >= margin && top + height <= viewportHeight - margin)
    .map((top) => ({ top, box: { left, top, right: left + width, bottom: top + height } }))
    .filter(({ box }) => !overlaps(box, input.anchor) && !actions.some((rect) => overlaps(box, rect)));
  const clear = candidates.find(({ box }) => !fields.some((rect) => overlaps(box, rect)));
  const chosen = clear ?? candidates[0];
  if (chosen) return placementAt(chosen.top);
  if (input.dialog) return null;
  // A page too short for the toolbar: keep it on screen, as close as possible.
  return placementAt(Math.max(margin, Math.min(input.anchor.bottom + gap, viewportHeight - margin - height)));
}

const DIALOG_SELECTOR = "[aria-modal='true'], dialog[open], [role='dialog'], [role='alertdialog']";
const ACTION_SELECTOR = [
  "button",
  "a[href]",
  "summary",
  "[role='button']",
  "[role='link']",
  "[role='menuitem']",
  "[role='tab']",
  "[role='checkbox']",
  "[role='radio']",
  "[role='switch']",
  "input[type='button']",
  "input[type='submit']",
  "input[type='reset']",
  "input[type='checkbox']",
  "input[type='radio']",
  "input[type='file']",
  "input[type='image']",
].join(", ");
const FIELD_SELECTOR = "input:not([type='hidden']), select, textarea, [contenteditable='true'], [contenteditable='']";

function visibleRect(element: Element): PlacementRect | undefined {
  const rect = element.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return undefined;
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
}

function contains(rect: PlacementRect, x: number, y: number): boolean {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

/** Finds the topmost visible dialog that holds the selection, and the controls in it. */
export function dialogObstacles(
  anchor: PlacementRect,
  ignore?: Element | null,
): Pick<ToolbarPlacementInput, "dialog" | "actions" | "fields"> {
  const x = (anchor.left + anchor.right) / 2;
  const y = (anchor.top + anchor.bottom) / 2;
  const dialogs = Array.from(document.querySelectorAll(DIALOG_SELECTOR))
    .filter((element) => !ignore?.contains(element))
    .map((element) => ({ element, rect: visibleRect(element) }))
    .filter((entry): entry is { element: Element; rect: PlacementRect } => Boolean(entry.rect && contains(entry.rect, x, y)));
  const holder = dialogs.at(-1);
  if (!holder) return {};
  const rectsFor = (selector: string, skip?: (element: Element) => boolean) => Array.from(holder.element.querySelectorAll(selector))
    .filter((element) => !ignore?.contains(element) && !skip?.(element))
    .map(visibleRect)
    .filter((rect): rect is PlacementRect => Boolean(rect));
  return {
    dialog: holder.rect,
    actions: rectsFor(ACTION_SELECTOR),
    fields: rectsFor(FIELD_SELECTOR, (element) => element.matches(ACTION_SELECTOR)),
  };
}
