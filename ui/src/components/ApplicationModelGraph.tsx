import { CircleDot, Database, GitBranch, Lightbulb, Network } from "lucide-react";

type Value = { kind: string; type: string; value?: string | number | boolean; reason?: string };
type GraphState = { id: string; branch_key: string; parent_state_ids: string[]; object_version_ids: string[]; observation_ids: string[] };
type GraphObservation = { id: string; source_kind: string; source_id: string; facts: Record<string, Value> };
type GraphVersion = { id: string; object_id: string; properties: Record<string, Value> };
type GraphObject = { id: string; label: string };
type GraphAssertion = { id: string; subject: string; predicate: string; support: string; lifecycle: string };

export type ApplicationGraphData = {
  states: GraphState[];
  observations: GraphObservation[];
  objects: GraphObject[];
  object_versions: GraphVersion[];
  assertions: GraphAssertion[];
};

function factSummary(facts: Record<string, Value>) {
  const route = facts.route?.value;
  const status = facts.status_code?.value ?? facts.status?.value;
  if (route) {
    try {
      const parsed = new URL(String(route));
      return `${parsed.pathname || "/"}${status === undefined ? "" : ` · ${status}`}`;
    } catch { return String(route); }
  }
  return status === undefined ? `${Object.keys(facts).length} facts` : `status ${status}`;
}

export function ApplicationModelGraph({ data, selectedStateId, selectedObjectId, onSelectState, onSelectObject }: {
  data: ApplicationGraphData;
  selectedStateId?: string;
  selectedObjectId?: string;
  onSelectState: (id: string) => void;
  onSelectObject: (id: string) => void;
}) {
  const selected = data.states.find(item => item.id === selectedStateId) ?? data.states.at(-1);
  const observations = data.observations.filter(item => selected?.observation_ids.includes(item.id)).slice(-8);
  const versions = data.object_versions.filter(item => selected?.object_version_ids.includes(item.id)).slice(-8);
  const objectIds = new Set(versions.map(item => item.object_id));
  const assertions = data.assertions.filter(item => objectIds.has(item.subject) && (item.support === "inferred" || item.lifecycle === "proposed")).slice(-8);
  const branchStates = data.states.filter(item => item.branch_key === selected?.branch_key).slice(-8);

  if (!selected) return <div className="model-map-empty"><Network aria-hidden="true" /><strong>No model map yet</strong><span>Import recorded history or browse through this collection to create its first knowledge state.</span></div>;

  return <div className="model-map" aria-label="Application model graph">
    <header className="model-map-heading">
      <div><span className="model-eyebrow">Black-box representation</span><h2>Application topology</h2></div>
      <div className="model-map-legend" aria-label="Graph legend"><span><i className="observed" />Observed</span><span><i className="unknown" />Unknown</span><span><i className="inferred" />Inferred</span></div>
    </header>
    <p className="model-map-description">Evidence flows from recorded interactions into stable representations and immutable knowledge states. Dashed nodes are interpretations, not observed backend facts.</p>
    <div className="model-map-canvas">
      <section className="model-map-lane" aria-labelledby="model-lane-sources">
        <h3 id="model-lane-sources"><CircleDot aria-hidden="true" />Interactions <span>{observations.length}</span></h3>
        <div className="model-map-stack">{observations.map(item => <article className="model-node observed" key={item.id}>
          <span className="model-node-kind">{item.source_kind.replaceAll("_", " ")}</span>
          <strong>{factSummary(item.facts)}</strong>
          <small>{item.id.slice(0, 12)}</small>
        </article>)}</div>
      </section>
      <section className="model-map-lane" aria-labelledby="model-lane-objects">
        <h3 id="model-lane-objects"><Database aria-hidden="true" />Objects <span>{versions.length}</span></h3>
        <div className="model-map-stack">{versions.map(version => {
          const item = data.objects.find(object => object.id === version.object_id);
          const unknowns = Object.values(version.properties).filter(value => value.kind !== "concrete").length;
          return <button type="button" className={`model-node object ${selectedObjectId === version.object_id ? "selected" : ""}`} key={version.id} onClick={() => onSelectObject(version.object_id)} aria-pressed={selectedObjectId === version.object_id}>
            <span className="model-node-kind">representation</span><strong>{item?.label ?? "Recorded object"}</strong><small>{Object.keys(version.properties).length} properties · {unknowns} unknown</small>
          </button>;
        })}</div>
      </section>
      <section className="model-map-lane" aria-labelledby="model-lane-states">
        <h3 id="model-lane-states"><GitBranch aria-hidden="true" />States <span>{branchStates.length}</span></h3>
        <div className="model-map-stack">{branchStates.map((item, index) => <button type="button" className={`model-node state ${item.id === selected.id ? "selected" : ""}`} key={item.id} onClick={() => onSelectState(item.id)} aria-pressed={item.id === selected.id}>
          <span className="model-node-kind">state {data.states.indexOf(item) + 1}</span><strong>Context {item.branch_key}</strong><small>{item.object_version_ids.length} objects · {item.parent_state_ids.length ? "successor" : "root"}{index === branchStates.length - 1 ? " · latest" : ""}</small>
        </button>)}</div>
      </section>
      <section className="model-map-lane hypotheses" aria-labelledby="model-lane-hypotheses">
        <h3 id="model-lane-hypotheses"><Lightbulb aria-hidden="true" />Hypotheses <span>{assertions.length}</span></h3>
        <div className="model-map-stack">{assertions.length ? assertions.map(item => <article className={`model-node inferred ${item.support === "unknown" ? "unknown" : ""}`} key={item.id}>
          <span className="model-node-kind">{item.support} · {item.lifecycle}</span><strong>{item.predicate}</strong><small>{data.objects.find(object => object.id === item.subject)?.label ?? item.subject.slice(0, 12)}</small>
        </article>) : <div className="model-node inferred empty"><strong>No assertions selected</strong><small>Agent and operator proposals appear here with their evidence status.</small></div>}</div>
      </section>
    </div>
  </div>;
}
