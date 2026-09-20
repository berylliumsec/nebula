import { useState } from "react";
import { PanelRight } from "lucide-react";
import { IconAction } from "../IconAction";
import { copyText } from "./format";
import type { PresentationPlan } from "./hints";
import { isScalar, typeLabel, type NormalizedResult, type ResultNode } from "./normalize";
import { CopyAction, ScalarValue } from "./ValueView";

const PAGE = 50;

export interface NodeSelection {
  selected?: ResultNode;
  onSelect: (node: ResultNode, opener?: HTMLElement | null) => void;
}

export function fieldLabel(plan: PresentationPlan, node: ResultNode): string {
  const key = node.key === undefined ? "Result" : String(node.key);
  return plan.labels[key] ?? key;
}

/**
 * A sibling field that names this text's language, when the producer supplied
 * one. A generic name heuristic: with no sibling the block is still readable,
 * it simply carries no language label.
 */
export function languageFor(result: NormalizedResult, node: ResultNode): string | undefined {
  const parent = node.parentPath === undefined ? undefined : result.nodeAt(node.parentPath);
  if (!parent || parent.type !== "object") return undefined;
  const record = parent.value as Record<string, unknown>;
  for (const key of ["language", "lang", "syntax", "dialect"]) {
    const value = record[key];
    if (typeof value === "string" && value.length > 0 && value.length <= 30) return value;
  }
  return undefined;
}

export function isRedacted(plan: PresentationPlan, node: ResultNode): boolean {
  const key = node.key === undefined ? "" : String(node.key);
  return plan.redacted.includes(key) || plan.redacted.includes(node.path);
}

interface PropertyGridProps extends NodeSelection {
  result: NormalizedResult;
  node: ResultNode;
  plan: PresentationPlan;
  /** Optional explicit children, for an overview that ordered them already. */
  entries?: ResultNode[];
  emptyLabel?: string;
}

/**
 * Key and value rows for one container. Scalars are shown inline; anything
 * nested states its shape and opens in the inspector, so no view ever has to
 * flatten a value to display it.
 */
export function PropertyGrid({ result, node, plan, entries, emptyLabel = "No properties", selected, onSelect }: PropertyGridProps) {
  const [shown, setShown] = useState(PAGE);
  const all = entries ?? result.childWindow(node, 0, Number.POSITIVE_INFINITY);
  const visible = all.slice(0, shown);
  if (all.length === 0) return <p className="structured-empty">{emptyLabel}</p>;
  return <>
    <dl className="structured-properties">
      {visible.map((child) => {
        const label = fieldLabel(plan, child);
        const description = plan.descriptions[String(child.key)];
        return <div className="structured-property" key={child.path} data-selected={selected?.path === child.path ? "true" : undefined}>
          <dt>
            <span className="structured-property-name">{label}</span>
            {description && <small>{description}</small>}
          </dt>
          <dd>
            {isScalar(child.type)
              ? <ScalarValue name={child.key} value={child.value} format={plan.formats[String(child.key)]} redacted={isRedacted(plan, child)} language={languageFor(result, child)} />
              : <button type="button" className="structured-nested" onClick={(event) => onSelect(child, event.currentTarget)}>
                  {typeLabel(child)}
                </button>}
            <span className="structured-property-actions">
              <CopyAction text={copyText(child.value)} label={`Copy ${label}`} />
              <IconAction icon={PanelRight} label={`Inspect ${label}`} onClick={(event) => onSelect(child, event.currentTarget)} />
            </span>
          </dd>
        </div>;
      })}
    </dl>
    {all.length > visible.length && <button type="button" className="button quiet structured-more" onClick={() => setShown(shown + PAGE)}>
      Show {Math.min(PAGE, all.length - visible.length)} more of {all.length}
    </button>}
  </>;
}
