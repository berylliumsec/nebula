import { useEffect, useRef } from "react";
import { FileJson, X } from "lucide-react";
import { IconAction } from "../IconAction";
import { copyText } from "./format";
import type { PresentationPlan } from "./hints";
import { isScalar, typeLabel, type NormalizedResult, type ResultNode } from "./normalize";
import { fieldLabel, isRedacted, languageFor, PropertyGrid } from "./PropertyGrid";
import { structuralReferences } from "./references";
import { CopyAction, ScalarValue } from "./ValueView";

interface InspectorPanelProps {
  result: NormalizedResult;
  plan: PresentationPlan;
  node: ResultNode;
  onSelect: (node: ResultNode, opener?: HTMLElement | null) => void;
  onClose: () => void;
  onShowInRaw: (node: ResultNode) => void;
}

/**
 * One detail surface for every renderer. Whatever selected a node — a table
 * row, a graph marker, a tree property, an overview field — it is described
 * the same way here, and the route back to the unmodified result is always
 * offered.
 */
export function InspectorPanel({ result, plan, node, onSelect, onClose, onShowInRaw }: InspectorPanelProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    // Opening the inspector moves focus into it so the keyboard follows the
    // selection; the dashboard returns focus to the opener when it closes.
    heading.current?.focus({ preventScroll: true });
  }, [node.path]);

  const label = fieldLabel(plan, node);
  const references = structuralReferences(result, plan.analysis, node);
  const description = plan.descriptions[String(node.key)];

  return <aside className="structured-inspector" aria-labelledby="structured-inspector-title">
    <header>
      <div>
        <span className="eyebrow">Detail</span>
        <h3 id="structured-inspector-title" ref={heading} tabIndex={-1}>{label}</h3>
        {description && <p>{description}</p>}
      </div>
      <IconAction icon={X} label="Close detail" onClick={onClose} />
    </header>

    <dl className="structured-inspector-facts">
      <div>
        <dt>Path</dt>
        <dd><code className="structured-mono">{node.path}</code><CopyAction text={node.path} label="Copy path" /></dd>
      </div>
      <div>
        <dt>Type</dt>
        <dd>{typeLabel(node)}</dd>
      </div>
    </dl>

    <section aria-label="Value">
      <h4>Value</h4>
      {isScalar(node.type)
        ? <p className="structured-inspector-value">
            <ScalarValue name={node.key} value={node.value} format={plan.formats[String(node.key)]} redacted={isRedacted(plan, node)} language={languageFor(result, node)} />
            <CopyAction text={copyText(node.value)} label="Copy value" />
          </p>
        : node.note
          ? <p className="structured-empty">{node.note}</p>
          : <p className="structured-inspector-value">
              <span>{typeLabel(node)}</span>
              <CopyAction text={copyText(node.value)} label="Copy value" />
            </p>}
    </section>

    {!isScalar(node.type) && !node.note && <section aria-label="Nested properties">
      <h4>Nested properties</h4>
      <PropertyGrid result={result} node={node} plan={plan} onSelect={onSelect} emptyLabel="This container is empty." />
    </section>}

    {references.length > 0 && <section aria-label="References">
      <h4>References</h4>
      <p className="structured-derived">Derived presentation: entries elsewhere in this result that carry the same value.</p>
      <ul className="structured-reference-list">
        {references.map((reference) => <li key={`${reference.node.path}:${reference.reason}`}>
          <button type="button" onClick={(event) => onSelect(reference.node, event.currentTarget)}>
            <span className="structured-mono">{reference.node.path}</span>
            <small>{reference.reason}</small>
          </button>
        </li>)}
      </ul>
    </section>}

    <footer>
      <button type="button" className="button quiet" onClick={() => onShowInRaw(node)}>
        <FileJson size={15} aria-hidden="true" /> Show in raw result
      </button>
    </footer>
  </aside>;
}
