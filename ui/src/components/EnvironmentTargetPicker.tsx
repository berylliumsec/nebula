import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { Check, ChevronDown, Command, Server, Sparkles, TerminalSquare } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { SshEnvironment } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./EnvironmentTargetPicker.module.css";

/** ``auto``: every enabled host; ``core``: Core only; otherwise one environment id. */
export type EnvironmentTarget = "auto" | "core" | string;

/** The chat request's ``ssh_environment_ids`` for a target (undefined means all enabled). */
export function environmentIdsForTarget(target: EnvironmentTarget): string[] | undefined {
  if (target === "auto") return undefined;
  if (target === "core") return [];
  return [target];
}

function probeSummary(environment: SshEnvironment): { text: string; tone: "healthy" | "critical" | "muted" } {
  const probe = environment.lastProbe;
  if (!probe) return { text: "Not tested", tone: "muted" };
  if (probe.status !== "reachable") return { text: probe.status === "host_key_untrusted" ? "Host key not trusted" : probe.status === "auth_failed" ? "Sign-in failed" : "Unreachable", tone: "critical" };
  const facts = [probe.osVersion || probe.system, probe.arch, probe.latencyMs !== undefined ? `${probe.latencyMs} ms` : ""].filter(Boolean);
  return { text: facts.join(" · "), tone: "healthy" };
}

interface EnvironmentTargetPickerProps {
  api: ApiClient;
  value: EnvironmentTarget;
  onChange: (target: EnvironmentTarget) => void;
  disabled?: boolean;
}

export function EnvironmentTargetPicker({ api, value, onChange, disabled = false }: EnvironmentTargetPickerProps) {
  const [environments, setEnvironments] = useState<SshEnvironment[]>();
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState<CSSProperties>();

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const discovery = await api.discoverSshEnvironments(signal, { resolve: false });
      setEnvironments(discovery.hosts.flatMap((host) => host.inConfig && host.environment?.enabled ? [host.environment] : []));
    } catch (loadError) {
      if (signal?.aborted) return;
      void logCaughtDiagnostic("interface.environment_target_picker.caught_failure_01", "A handled interface operation failed.", loadError, "environment_target_picker");
      setEnvironments([]);
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // A pinned host that was disabled or removed falls back to Auto.
  useEffect(() => {
    if (environments && value !== "auto" && value !== "core" && !environments.some((item) => item.id === value)) onChange("auto");
  }, [environments, onChange, value]);

  // The composer clips its overflow, so the menu floats above the trigger in the viewport.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const width = Math.min(320, window.innerWidth - 32);
      setPlacement({
        left: Math.max(16, Math.min(anchor.left, window.innerWidth - width - 16)),
        bottom: window.innerHeight - anchor.top + 8,
        width,
        maxHeight: Math.max(120, Math.min(420, anchor.top - 24)),
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  // Focus the current choice: the keyboard lands in the menu and the trigger's
  // focus tooltip never covers the menu's last row.
  useEffect(() => {
    if (open) menuRef.current?.querySelector<HTMLElement>("[aria-pressed='true']")?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!wrapperRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  if (!environments?.length) return null;

  const selected = environments.find((item) => item.id === value);
  const triggerLabel = value === "core" ? "Core host" : selected?.label ?? "Auto";
  const TriggerIcon = value === "core" ? Server : selected ? (selected.lastProbe?.system === "Darwin" ? Command : TerminalSquare) : Sparkles;

  function choose(target: EnvironmentTarget) {
    onChange(target);
    setOpen(false);
    triggerRef.current?.focus();
  }

  const options: Array<{ target: EnvironmentTarget; label: string; detail: string; tone?: "healthy" | "critical" | "muted"; icon: typeof Server }> = [
    { target: "auto", label: "Auto", detail: "Model picks from enabled hosts", icon: Sparkles },
    { target: "core", label: "Core host", detail: "Only the Nebula workspace", icon: Server },
    ...environments.map((environment) => {
      const summary = probeSummary(environment);
      return {
        target: environment.id,
        label: environment.label,
        detail: summary.text,
        tone: summary.tone,
        icon: environment.lastProbe?.system === "Darwin" ? Command : TerminalSquare,
      };
    }),
  ];

  return <div className={styles.wrapper} ref={wrapperRef}>
    <button
      ref={triggerRef}
      type="button"
      className={`${styles.trigger} ${value === "auto" ? "" : styles.pinned}`}
      aria-haspopup="true"
      aria-expanded={open}
      aria-controls="environment-target-menu"
      aria-label={`Where commands run: ${triggerLabel}`}
      title="Where should commands run?"
      disabled={disabled}
      onClick={() => { if (!open) void load(); setOpen((current) => !current); }}
    >
      <TriggerIcon size={13} aria-hidden="true" />
      <span>{triggerLabel}</span>
      <ChevronDown size={13} aria-hidden="true" />
    </button>
    {open && createPortal(<div ref={menuRef} className={styles.menu} style={placement} id="environment-target-menu" role="group" aria-label="Where should commands run?">
      <p className={styles.heading}>Where should commands run?</p>
      {options.map((option) => {
        const active = option.target === value;
        const Icon = option.icon;
        return <button key={option.target} type="button" className={`${styles.option} ${active ? styles.active : ""}`} aria-pressed={active} onClick={() => choose(option.target)}>
          <Icon size={14} aria-hidden="true" className={styles.optionIcon} />
          <span className={styles.optionText}><strong>{option.label}</strong><small>{option.detail}</small></span>
          {active ? <Check size={14} aria-hidden="true" className={styles.check} /> : option.tone && <span className={`${styles.dot} ${styles[option.tone]}`} aria-hidden="true" />}
        </button>;
      })}
      <Link className={styles.manage} to="/settings#ssh-environment-settings" onClick={() => setOpen(false)}>Manage environments…</Link>
    </div>, wrapperRef.current?.closest("dialog[open]") ?? document.body)}
  </div>;
}
