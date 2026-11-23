import type { ButlerStage } from "../butlerStage";

/**
 * A Butler conversation's stage: a word plus a shape, so it reads without
 * colour. The tooltip says why the conversation is in that stage.
 */
export function ButlerStageLabel({ stage, prefix, className }: { stage: ButlerStage; prefix?: string; className?: string }) {
  return <span className={`butler-stage${className ? ` ${className}` : ""}`} data-stage={stage.key} title={stage.description}>
    <span className="butler-stage-mark" aria-hidden="true" />
    <span className="butler-stage-text">{prefix ? `${prefix} · ${stage.label}` : stage.label}</span>
  </span>;
}
