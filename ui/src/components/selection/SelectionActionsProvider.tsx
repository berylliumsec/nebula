import {
  createContext,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Clipboard, MessageSquarePlus, MessageSquareText, NotebookPen, Play } from "lucide-react";
import styles from "./SelectionActionsProvider.module.css";
import {
  copySelectionText,
  createSelectionDraft,
  isSelectableTextControl,
  readDomSelection,
  readTextControlSelection,
  SELECTION_TEXT_LIMIT,
  type DomSelectionOptions,
  type PresentSelectionInput,
  type SelectionActionDraft,
} from "./selectionActions";
import { dialogObstacles, placeSelectionToolbar, type ToolbarPlacement } from "./toolbarPlacement";
import { logCaughtDiagnostic } from "../../diagnostics";

interface SelectionActionsContextValue {
  presentSelection(selection: PresentSelectionInput): void;
  dismissSelection(): void;
}

const SelectionActionsContext = createContext<SelectionActionsContextValue | undefined>(undefined);

export interface SelectionActionsProviderProps extends DomSelectionOptions {
  children: ReactNode;
  /** Opens an editable assistant draft. The provider never submits a turn. */
  onAsk: (draft: SelectionActionDraft) => void;
  /** Captures selected text as a project note. Omit to hide Take note. */
  onAddContext?: (draft: SelectionActionDraft) => void;
  onAddNote?: (draft: SelectionActionDraft) => void;
  /** Opens mandatory reviewed execution for explicitly runnable selections. */
  onRun?: (draft: SelectionActionDraft) => void;
  onCopyError?: (error: Error) => void;
}

export function useSelectionActions(): SelectionActionsContextValue {
  const value = useContext(SelectionActionsContext);
  if (!value) throw new Error("useSelectionActions must be used inside SelectionActionsProvider.");
  return value;
}

/** Lets optional surfaces, including isolated component tests, bind when a provider is present. */
export function useOptionalSelectionActions(): SelectionActionsContextValue | undefined {
  return useContext(SelectionActionsContext);
}

export function SelectionActionsProvider({
  children,
  onAsk,
  onAddContext,
  onAddNote,
  onRun,
  onCopyError,
  limit = SELECTION_TEXT_LIMIT,
  isSensitive,
  resolveSource,
}: SelectionActionsProviderProps) {
  const [draft, setDraft] = useState<SelectionActionDraft>();
  const [isExiting, setIsExiting] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const exitTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const domOptions = useMemo(() => ({ limit, isSensitive, resolveSource }), [limit, isSensitive, resolveSource]);

  const presentSelection = useCallback((selection: PresentSelectionInput) => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
    setIsExiting(false);
    setDraft(createSelectionDraft(selection, limit));
  }, [limit]);
  const closeActions = useCallback(() => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
    exitTimerRef.current = undefined;
    setIsExiting(false);
    setDraft(undefined);
  }, []);
  const dismissSelection = useCallback(() => {
    document.getSelection()?.removeAllRanges();
    closeActions();
  }, [closeActions]);
  const exitFocusRef = useRef<Element | null>(null);
  const finishExit = useCallback(() => {
    const focused = document.activeElement;
    // An action such as Ask Nebula can focus its own text box before the exit
    // finishes. The document selection is then that box's caret, and WebKit
    // drops every keystroke once it is cleared, so the box keeps it.
    const tookTyping = isSelectableTextControl(focused) || (focused instanceof HTMLElement && focused.isContentEditable);
    if (focused !== exitFocusRef.current && tookTyping) closeActions();
    else dismissSelection();
  }, [closeActions, dismissSelection]);
  const dismissSelectionElegantly = useCallback(() => {
    if (isExiting) return;
    exitFocusRef.current = document.activeElement;
    setIsExiting(true);
    // animationend handles normal browsers; the timer also covers interrupted
    // animations and DOM environments that do not dispatch animation events.
    exitTimerRef.current = setTimeout(finishExit, 180);
  }, [finishExit, isExiting]);

  useEffect(() => () => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
  }, []);

  useEffect(() => {
    const read = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key === "Escape") return;
      if (popoverRef.current?.contains(event.target as Node | null)) return;
      if (
        event.target instanceof Element
        && event.target.closest("[data-selection-actions-disabled]")
      ) {
        setDraft(undefined);
        return;
      }
      const next = isSelectableTextControl(event.target)
        ? readTextControlSelection(event.target, domOptions)
        : readDomSelection(document.getSelection(), domOptions);
      if (next) {
        // A new selection supersedes an earlier action's exit animation.
        if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
        exitTimerRef.current = undefined;
        setIsExiting(false);
      }
      setDraft(next);
    };
    const dismissForPointer = (event: PointerEvent) => {
      if (!popoverRef.current?.contains(event.target as Node | null)) setDraft(undefined);
    };
    const dismissForEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismissSelection();
    };
    document.addEventListener("pointerdown", dismissForPointer, true);
    document.addEventListener("pointerup", read, true);
    document.addEventListener("keyup", read, true);
    document.addEventListener("select", read, true);
    document.addEventListener("keydown", dismissForEscape, true);
    // A resize (on a phone, often the software keyboard opening) only moves
    // the popover. While a text box has focus the document selection is its
    // caret, and WebKit drops every keystroke once that is cleared.
    const dismissForResize = () => {
      const focused = document.activeElement;
      if (isSelectableTextControl(focused) || (focused instanceof HTMLElement && focused.isContentEditable)) closeActions();
      else dismissSelection();
    };
    globalThis.addEventListener("resize", dismissForResize);
    return () => {
      document.removeEventListener("pointerdown", dismissForPointer, true);
      document.removeEventListener("pointerup", read, true);
      document.removeEventListener("keyup", read, true);
      document.removeEventListener("select", read, true);
      document.removeEventListener("keydown", dismissForEscape, true);
      globalThis.removeEventListener("resize", dismissForResize);
    };
  }, [closeActions, dismissSelection, domOptions]);

  const context = useMemo(() => ({ presentSelection, dismissSelection }), [presentSelection, dismissSelection]);
  // The toolbar renders hidden first so its real size, including wrapping on
  // a phone, decides where it fits. A placement belongs to one draft only.
  const [placed, setPlaced] = useState<{ draft: SelectionActionDraft; placement: ToolbarPlacement | null }>();
  useLayoutEffect(() => {
    const node = popoverRef.current;
    if (!draft || !node) return;
    const rect = node.getBoundingClientRect();
    setPlaced({
      draft,
      placement: placeSelectionToolbar({
        anchor: draft.anchor,
        size: { width: rect.width, height: rect.height },
        viewport: { width: globalThis.innerWidth || 1024, height: globalThis.innerHeight || 768 },
        ...dialogObstacles(draft.anchor, node),
      }),
    });
  }, [draft]);
  const placement = draft && placed?.draft === draft ? placed.placement : undefined;
  const showAbove = placement?.side === "above";
  const position = placement
    ? { left: placement.left, top: placement.top }
    : { left: 0, top: 0, visibility: "hidden" as const, pointerEvents: "none" as const };

  const preserveSelection = (event: ReactPointerEvent) => event.preventDefault();

  return <SelectionActionsContext.Provider value={context}>
    {children}
    {draft && placement !== null && <div
      ref={popoverRef}
      className={`${styles.popover}${isExiting ? ` ${styles.exiting}` : ""}`}
      style={position}
      role="toolbar"
      aria-label="Selected text actions"
      data-placement={showAbove ? "above" : "below"}
      onPointerDown={preserveSelection}
      onAnimationEnd={(event) => {
        if (isExiting && event.target === event.currentTarget) finishExit();
      }}
    >
      {draft.truncated && <span className={styles.notice} title={`${draft.originalLength.toLocaleString()} characters selected`}>
        First {draft.text.length.toLocaleString()} characters
      </span>}
      <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        void copySelectionText(draft.text).catch((reason: unknown) => {
          void logCaughtDiagnostic("interface.selection_actions_provider.caught_failure_01", "A handled interface operation failed.", reason, "selection_actions_provider");
          onCopyError?.(reason instanceof Error ? reason : new Error("The selected text could not be copied."));
        });
      }}><Clipboard size={14} /> Copy</button>
      {onAddContext && <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onAddContext(draft);
      }}><MessageSquarePlus size={14} /> Add context to chat</button>}
      {onAddNote && <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onAddNote(draft);
      }}><NotebookPen size={14} /> Take note</button>}
      {onRun && ["terminal", "terminal_command"].includes(draft.source.kind) && <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onRun(draft);
      }}><Play size={14} /> Run</button>}
      <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onAsk(draft);
      }}><MessageSquareText size={14} /> Ask Nebula</button>
    </div>}
  </SelectionActionsContext.Provider>;
}
