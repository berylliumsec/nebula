import type { ButlerStage } from "../butlerStage";

/**
 * Butler's own status for a conversation ("Butler · Needs you"), kept
 * quieter than the project Work stage, which is the conversation's stage.
 * A word plus a shape, so it reads without colour; the tooltip says why.
 */
export function ButlerStageLabel({ stage, className }: { stage: ButlerStage; className?: string }) {
  return <span className={`butler-stage${className ? ` ${className}` : ""}`} data-stage={stage.key} title={stage.description}>
    <span className="butler-stage-mark" aria-hidden="true" />
    <span className="butler-stage-text">{`Butler · ${stage.label}`}</span>
  </span>;
}
