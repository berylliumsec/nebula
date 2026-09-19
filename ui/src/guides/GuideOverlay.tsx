import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { AlertTriangle, Check, CircleDot, Copy, FileCode2, Minus, X } from "lucide-react";
import { ApiError } from "../api/client";
import type { GuideStarterKind } from "../api/types";
import { copySelectionText } from "../components/selection";
import { useDialogPresence } from "../components/DialogSystem";
import { logCaughtDiagnostic } from "../diagnostics";
import { projectSurface } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import { requestGuideAction } from "./guideActions";
import type { ActiveGuide } from "./GuideProvider";
import type { GuideCheckResult, GuideRunContext } from "./types";
import { GuideText } from "./GuideText";

interface GuideOverlayProps {
  active: ActiveGuide;
  onBack(): void;
  onNext(): void;
  onExit(): void;
  onMinimize(minimized: boolean): void;
  onValue(key: string, value: string): void;
}

const CARD_WIDTH = 380;
const GAP = 16;
const TARGET_WAIT_MS = 1_500;

export function starterPath(kind: GuideStarterKind, name: string): string {
  if (kind === "hook") return `.agents/hooks/${name}/hook.json`;
  if (kind === "skill") return `.agents/skills/${name}/SKILL.md`;
  return "AGENTS.md";
}

/** Merge a guide route into the current URL so the open conversation and tabs survive. */
export function guideDestination(route: string, current: { pathname: string; search: string; hash: string }): string | undefined {
  const target = new URL(route, "http://nebula.local");
  if (target.pathname !== current.pathname) return `${target.pathname}${target.search}${target.hash}`;
  const parameters = new URLSearchParams(current.search);
  target.searchParams.forEach((value, key) => parameters.set(key, value));
  const search = parameters.toString() ? `?${parameters}` : "";
  const hash = target.hash || current.hash;
  if (search === current.search && hash === current.hash) return undefined;
  return `${current.pathname}${search}${hash}`;
}

export function GuideOverlay({ active, onBack, onNext, onExit, onMinimize, onValue }: GuideOverlayProps) {
  const { api, engagement } = useWorkspace();
  const navigate = useNavigate();
  const location = useLocation();
  const { guide, stepIndex, minimized, values } = active;
  const step = guide.steps[stepIndex];
  const last = stepIndex === guide.steps.length - 1;
  const context: GuideRunContext = { api, engagementId: engagement?.id, values };
  const contextRef = useRef(context);
  contextRef.current = context;
  const cardRef = useRef<HTMLElement>(null);
  const [rect, setRect] = useState<DOMRect>();
  const [targetMissing, setTargetMissing] = useState(false);
  const [check, setCheck] = useState<GuideCheckResult>();
  const [starterName, setStarterName] = useState(values.name ?? step.starter?.defaultName ?? "");
  const [starterBusy, setStarterBusy] = useState(false);
  const [starterError, setStarterError] = useState<string>();
  const [starterPaths, setStarterPaths] = useState<string[]>();
  const [copied, setCopied] = useState(false);
  const [cardSize, setCardSize] = useState({ width: CARD_WIDTH, height: 240 });
  const needsProject = Boolean(step.requiresProject && !engagement);
  const locationRef = useRef(location);
  locationRef.current = location;
  // navigate changes identity with the location; the step effect must run once per step.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  useDialogPresence(!minimized);

  // Go where the step happens, then ask the page to open any transient panel.
  useEffect(() => {
    if (needsProject) return;
    const route = step.route?.(contextRef.current);
    const destination = route ? guideDestination(route, locationRef.current) : undefined;
    if (destination) navigateRef.current(destination);
    if (!step.action) return;
    const action = step.action;
    const first = window.setTimeout(() => requestGuideAction(action), 250);
    const retry = window.setTimeout(() => {
      if (step.target && !document.querySelector(`[data-guide="${step.target}"]`)) requestGuideAction(action);
    }, 1_200);
    return () => { window.clearTimeout(first); window.clearTimeout(retry); };
  }, [needsProject, step]);

  // Follow the target as layouts, drawers and scroll positions change.
  useEffect(() => {
    if (!step.target || minimized) { setRect(undefined); return; }
    let scrolled = false;
    const startedAt = Date.now();
    const track = () => {
      // A control can render in several places (e.g. desktop and mobile toolbars); use the visible one.
      const element = Array.from(document.querySelectorAll<HTMLElement>(`[data-guide="${step.target}"]`))
        .find(candidate => { const box = candidate.getBoundingClientRect(); return box.width > 0 && box.height > 0; });
      const bounds = element?.getBoundingClientRect();
      if (element && bounds) {
        if (!scrolled) {
          scrolled = true;
          element.scrollIntoView?.({ block: "nearest", inline: "nearest" });
        }
        setTargetMissing(false);
        setRect(current => current && sameRect(current, bounds) ? current : bounds);
      } else {
        setRect(undefined);
        setTargetMissing(Date.now() - startedAt > TARGET_WAIT_MS);
      }
    };
    track();
    const timer = window.setInterval(track, 200);
    window.addEventListener("resize", track);
    return () => { window.clearInterval(timer); window.removeEventListener("resize", track); };
  }, [minimized, step.target]);

  // Confirm the step from real state rather than trusting a click on Next.
  useEffect(() => {
    if (!step.check || needsProject) { setCheck(undefined); return; }
    const run = step.check;
    let disposed = false;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try {
        const result = await run(contextRef.current);
        if (!disposed) setCheck(result);
      } catch (error) {
        // diagnostic-expected: the failed check is shown on the step and retried on the next poll.
        if (!disposed) setCheck({ state: "problem", message: error instanceof Error ? error.message : "This step could not be checked." });
      } finally {
        running = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1_500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [needsProject, step]);

  useEffect(() => {
    cardRef.current?.querySelector<HTMLElement>(".guide-card-title")?.focus({ preventScroll: true });
  }, []);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const next = { width: card.offsetWidth, height: card.offsetHeight };
    setCardSize(current => current.width === next.width && current.height === next.height ? current : next);
  });

  const openInCode = (path: string) => {
    if (!engagement) return;
    const route = `${projectSurface(engagement.id, "workbench")}?${new URLSearchParams({ view: "code", openFile: path })}`;
    const destination = guideDestination(route, locationRef.current);
    if (destination) navigateRef.current(destination);
  };

  const createStarter = async () => {
    if (!api || !engagement || !step.starter) return;
    const name = starterName.trim();
    setStarterBusy(true);
    setStarterError(undefined);
    try {
      const created = await api.createGuideStarterFiles(engagement.id, step.starter.kind, name);
      if (step.starter.nameLabel) onValue("name", name);
      setStarterPaths(created.paths);
      openInCode(created.paths[0]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // The files are already there; offer to open them instead of failing the step.
        if (step.starter.nameLabel) onValue("name", name);
        setStarterPaths([starterPath(step.starter.kind, name)]);
      } else {
        logCaughtDiagnostic("interface.guides.starter_files_failed", "Guide starter files could not be created.", error, "guides");
      }
      setStarterError(error instanceof Error ? error.message : "The starter files could not be created.");
    } finally {
      setStarterBusy(false);
    }
  };

  const copyCommand = async () => {
    if (!step.command) return;
    try {
      await copySelectionText(step.command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      // diagnostic-expected: copy denial leaves the command visible for manual selection.
      setCopied(false);
    }
  };

  const onKeyDown = (event: ReactKeyboardEvent) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    onMinimize(true);
  };

  if (minimized) {
    return createPortal(<div className="guide-layer" data-guide-layer>
      <button className="guide-resume" type="button" onClick={() => onMinimize(false)} aria-label={`Resume guide: ${guide.title}, step ${stepIndex + 1} of ${guide.steps.length}`}>
        <CircleDot size={14} aria-hidden="true" />
        <span>{guide.title}</span>
        <small>{stepIndex + 1} / {guide.steps.length}</small>
      </button>
    </div>, document.body);
  }

  const spotlight = rect && { top: rect.top - 6, left: rect.left - 6, width: rect.width + 12, height: rect.height + 12 };
  const cardStyle = spotlight ? placeCard(spotlight, cardSize) : undefined;
  const titleId = `guide-step-${guide.id}-${stepIndex}`;

  return createPortal(<div className={`guide-layer${spotlight ? " has-target" : ""}${check?.state === "done" ? " done" : ""}`} data-guide-layer>
    {spotlight && <div className="guide-spotlight" style={spotlight as CSSProperties} aria-hidden="true" />}
    <section ref={cardRef} className={`guide-card${spotlight ? " anchored" : " docked"}${spotlight && !cardStyle && spotlight.top + spotlight.height / 2 > window.innerHeight / 2 ? " sheet-top" : ""}`} style={cardStyle} role="dialog" aria-modal="false" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <header className="guide-card-header">
        <span className="guide-card-kicker">Step {stepIndex + 1} of {guide.steps.length} · {guide.title}</span>
        <button className="icon-button subtle" type="button" aria-label="Minimize guide" title="Minimize guide" onClick={() => onMinimize(true)}><Minus size={16} aria-hidden="true" /></button>
        <button className="icon-button subtle" type="button" aria-label="Close guide" title="Close guide · progress is kept" onClick={onExit}><X size={16} aria-hidden="true" /></button>
      </header>
      <h2 className="guide-card-title" id={titleId} tabIndex={-1}>{step.title}</h2>
      {needsProject ? <p className="guide-card-note" role="status">Open or create a project first. This step works with the project’s files.</p> : <>
        {step.body.map((paragraph, index) => <p key={index}><GuideText text={paragraph} /></p>)}
        {step.command && <div className="guide-command">
          <code>{step.command}</code>
          <button className="icon-button subtle" type="button" aria-label={copied ? "Command copied" : "Copy command"} title="Copy command" onClick={() => void copyCommand()}>{copied ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}</button>
        </div>}
        {step.starter && <div className="guide-starter">
          {step.starter.nameLabel && <label>{step.starter.nameLabel}<input value={starterName} autoCapitalize="none" spellCheck={false} pattern="[a-z0-9][a-z0-9-]*" onChange={event => setStarterName(event.target.value.toLowerCase())} /></label>}
          <div className="guide-starter-actions">
            <button className="button primary" type="button" disabled={starterBusy || !api || (Boolean(step.starter.nameLabel) && !starterName.trim())} onClick={() => void createStarter()}>{starterBusy ? "Creating…" : "Create starter files"}</button>
            {starterPaths && <button className="button secondary" type="button" onClick={() => openInCode(starterPaths[0])}><FileCode2 size={15} aria-hidden="true" /> Open in Code</button>}
          </div>
          {starterError && <p className="guide-card-error" role="alert">{starterError}</p>}
        </div>}
        {step.callout && <p className="guide-callout"><AlertTriangle size={15} aria-hidden="true" /><span><GuideText text={step.callout} /></span></p>}
        {step.target && targetMissing && step.targetMissing && <p className="guide-card-note" role="status">{step.targetMissing}</p>}
        {check && <p className={`guide-check ${check.state}`} role="status" aria-live="polite">
          {check.state === "done" ? <Check size={15} aria-hidden="true" /> : check.state === "problem" ? <AlertTriangle size={15} aria-hidden="true" /> : <CircleDot size={15} aria-hidden="true" />}
          <span>{check.message}</span>
        </p>}
      </>}
      <footer className="guide-card-footer">
        <span className="guide-dots" aria-hidden="true">{guide.steps.map((_, index) => <span key={index} className={index <= stepIndex ? "on" : undefined} />)}</span>
        {stepIndex > 0 && <button className="button quiet" type="button" onClick={onBack}>Back</button>}
        <button className="button primary" type="button" onClick={onNext}>{last ? "Finish" : "Next"}</button>
      </footer>
    </section>
  </div>, document.body);
}

function sameRect(left: DOMRect, right: DOMRect): boolean {
  return left.top === right.top && left.left === right.left && left.width === right.width && left.height === right.height;
}

/** Put the card beside the target, never over it, and inside the viewport. */
export function placeCard(target: { top: number; left: number; width: number; height: number }, card: { width: number; height: number }): CSSProperties | undefined {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  if (viewportWidth <= 760) return undefined;
  const clampTop = (top: number) => Math.min(Math.max(top, GAP), Math.max(GAP, viewportHeight - card.height - GAP));
  const clampLeft = (left: number) => Math.min(Math.max(left, GAP), Math.max(GAP, viewportWidth - card.width - GAP));
  if (target.left - GAP - card.width >= GAP) return { top: clampTop(target.top), left: target.left - GAP - card.width };
  if (target.left + target.width + GAP + card.width <= viewportWidth - GAP) return { top: clampTop(target.top), left: target.left + target.width + GAP };
  if (target.top - GAP - card.height >= GAP) return { top: target.top - GAP - card.height, left: clampLeft(target.left) };
  return { top: clampTop(target.top + target.height + GAP), left: clampLeft(target.left) };
}
