import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useWorkspace } from "../state/WorkspaceContext";
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { PageHeader } from "../components/PageHeader";
import { ApplicationModelGraph } from "../components/ApplicationModelGraph";
import {
  ObjectEditor,
  RelationshipEditor,
  SchemaEditor,
  cryptoId,
} from "./ApplicationModelEditors";
import type {
  Claim,
  Evidence,
  Graph,
  Operation,
} from "./applicationModelTypes";
import "./applicationModel.css";

type Transaction = {
  expected_revision: number;
  idempotency_key: string;
  operations: Operation[];
};
type Edit = {
  revision: number;
  producer: string;
  updated_at: string;
  changes: {
    id?: string;
    op: string;
    reason?: string;
    before?: unknown;
    after?: unknown;
  }[];
};

export function ApplicationModelPage() {
  const { engagement } = useWorkspace();
  return engagement ? (
    <ProjectModel key={engagement.id} />
  ) : (
    <p>Select a project to explore its application model.</p>
  );
}

function ProjectModel() {
  const { api, engagement } = useWorkspace();
  const { requestNebulaDraft } = useWorkbenchDrafts();
  const [params, setParams] = useSearchParams();
  const [graph, setGraph] = useState<Graph>();
  const [evidence, setEvidence] = useState<Evidence[]>([]),
    [evidenceOffset, setEvidenceOffset] = useState<number | null>(null);
  const [history, setHistory] = useState<Edit[]>([]),
    [historyAfter, setHistoryAfter] = useState(0),
    [historyMore, setHistoryMore] = useState(false);
  const [editor, setEditor] = useState<
      "object" | "relationship" | "schema" | null
    >(null),
    [editing, setEditing] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [pending, setPending] = useState<Transaction>();
  const draftRevision = useRef<number | undefined>(undefined);
  const [question, setQuestion] = useState("");
  const [legacy] = useState(() =>
    ["collection", "state", "query", "modelTab"].some((k) => params.has(k)),
  );
  const revision = useRef(-1),
    alive = useRef(true),
    requestSequence = useRef(0);
  const base = `engagements/${encodeURIComponent(engagement!.id)}/application-model`;
  const objectId = params.get("object") ?? "",
    edgeId = params.get("relationship") ?? "";
  const query = params.get("q") ?? "",
    category = params.get("category") ?? "";
  const depth = Math.min(3, Math.max(1, Number(params.get("depth")) || 1));
  const object = graph?.objects.find((o) => o.id === objectId),
    edge = graph?.relationships.find((r) => r.id === edgeId);
  const select = (key: string, value: string) =>
    setParams((current) => {
      const next = new URLSearchParams(current);
      value ? next.set(key, value) : next.delete(key);
      if (key === "object") next.delete("relationship");
      if (key === "relationship") next.delete("object");
      return next;
    });
  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!api) return;
      const seq = ++requestSequence.current;
      const next = await api.request<Graph>(`${base}/graph`, { signal });
      if (signal?.aborted || !alive.current || seq !== requestSequence.current)
        return;
      if (next.revision >= revision.current) {
        revision.current = next.revision;
        setGraph(next);
      }
    },
    [api, base],
  );
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    const refresh = () =>
      void load(controller.signal).catch((e) => {
        if (!controller.signal.aborted)
          setError(`Could not refresh the model. ${String(e)}`);
      });
    refresh();
    const timer = window.setInterval(refresh, 5000);
    window.addEventListener("online", refresh);
    return () => {
      alive.current = false;
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("online", refresh);
    };
  }, [load]);
  useEffect(() => {
    if (legacy)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const k of ["collection", "state", "query", "modelTab"])
            next.delete(k);
          return next;
        },
        { replace: true },
      );
  }, [legacy, setParams]);
  const loadEvidence = async (offset = 0) => {
    try {
      const result = await api!.request<{
        evidence: Evidence[];
        next_offset: number | null;
      }>(`${base}/evidence?offset=${offset}`);
      if (alive.current) {
        setEvidence((v) =>
          offset ? [...v, ...result.evidence] : result.evidence,
        );
        setEvidenceOffset(result.next_offset);
      }
    } catch (e) {
      setError(`Could not load evidence. ${String(e)}`);
    }
  };
  const loadHistory = async (after = 0) => {
    try {
      const result = await api!.request<{
        edits: Edit[];
        next_revision: number;
        has_more: boolean;
      }>(`${base}/updates?after=${after}`);
      if (alive.current) {
        setHistory((v) => (after ? [...v, ...result.edits] : result.edits));
        setHistoryAfter(result.next_revision);
        setHistoryMore(result.has_more);
      }
    } catch (e) {
      setError(`Could not load history. ${String(e)}`);
    }
  };
  const submit = async (tx: Transaction) => {
    setBusy(true);
    setError("");
    setPending(tx);
    try {
      const result = await api!.request<{
        revision: number;
        changed_ids: string[];
      }>(`${base}/transactions`, { method: "POST", body: JSON.stringify(tx) });
      if (!alive.current) return;
      await load();
      setPending(undefined);
      setEditor(null);
      setNotice(`Saved project revision ${result.revision}.`);
      const first = tx.operations[0];
      if (first.op === "put_object") select("object", String(first.id));
      if (first.op === "put_relationship")
        select("relationship", String(first.id));
      if (first.op === "dismiss") {
        select("object", "");
        select("relationship", "");
      }
    } catch (e) {
      if (alive.current) {
        setError(`Edit not confirmed. Your draft is retained. ${String(e)}`);
        await load().catch(() => undefined);
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const save = async (operations: Operation[]) => {
    if (!graph || busy) return;
    await submit(
      pending ?? {
        expected_revision: editor
          ? (draftRevision.current ?? graph.revision)
          : graph.revision,
        idempotency_key: cryptoId(),
        operations,
      },
    );
  };
  const open = (kind: "object" | "relationship" | "schema", edit = false) => {
    draftRevision.current = graph?.revision;
    setEditor(kind);
    setEditing(edit);
    setPending(undefined);
    setError("");
    void loadEvidence();
  };
  const filtered =
    graph?.objects.filter(
      (o) =>
        o.label.toLowerCase().includes(query.toLowerCase()) &&
        (!category ||
          graph.schema.types.find((t) => t.name === o.classification.value)
            ?.category === category),
    ) ?? [];
  const showClaim = (claim: Claim, title: string) => (
    <article className={`am-claim ${claim.status}`} key={title}>
      <h3>{title}</h3>
      <p>{String(claim.value)}</p>
      <strong className="am-status">{claim.status}</strong>
      <span> · {claim.review}</span>
      {claim.reason && <p>{claim.reason}</p>}
      <details>
        <summary>Evidence ({claim.evidence.length}) and provenance</summary>
        <p>
          {claim.producer} ·{" "}
          {claim.updated_at ? new Date(claim.updated_at).toLocaleString() : ""}
        </p>
        {claim.evidence.map((r) => (
          <button
            className="button secondary"
            key={r.kind + r.id + r.role}
            onClick={() => {
              setParams((current) => {
                const next = new URLSearchParams(current);
                next.set("evidence", `${r.kind}:${r.id}`);
                next.set("evidenceRevision", String(r.revision));
                return next;
              });
              void loadEvidence();
            }}
          >
            {r.role} · {r.kind.replaceAll("_", " ")}
          </button>
        ))}
      </details>
    </article>
  );
  const ev = params.get("evidence");
  const selectedEvidence = evidence.find((e) => `${e.kind}:${e.id}` === ev);
  useEffect(() => {
    if (!ev || !api) return;
    const split = ev.indexOf(":");
    if (split < 0) return;
    let active = true;
    void api
      .request<Evidence>(
        `${base}/evidence/${encodeURIComponent(ev.slice(0, split))}/${encodeURIComponent(ev.slice(split + 1))}`,
      )
      .then((e) => {
        if (active)
          setEvidence((v) => [
            ...v.filter((x) => x.id !== e.id || x.kind !== e.kind),
            e,
          ]);
      })
      .catch((e) => {
        if (active) setError(`Evidence unavailable. ${String(e)}`);
      });
    return () => {
      active = false;
    };
  }, [ev, api, base]);
  return (
    <div className="page application-model-page">
      <PageHeader
        title="Application model"
        description="Explore what the evidence supports. Question what remains uncertain."
      />
      {legacy && (
        <p role="status">
          This older link now opens the project model. Browser evidence is
          retained.
        </p>
      )}
      {error && (
        <div role="alert" className="am-error">
          <p>{error}</p>
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => {
              setError("");
              void (pending
                ? submit(pending)
                : load().catch((e) => setError(String(e))));
            }}
          >
            Retry
          </button>
          {pending && (
            <>
              <details>
                <summary>Review retained draft</summary>
                <pre>{JSON.stringify(pending.operations, null, 2)}</pre>
              </details>
              {graph && graph.revision !== pending.expected_revision && (
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    void submit({
                      ...pending,
                      expected_revision: graph.revision,
                      idempotency_key: cryptoId(),
                    })
                  }
                >
                  Apply reviewed draft to revision {graph.revision}
                </button>
              )}
              <button
                className="button secondary"
                onClick={() => {
                  setPending(undefined);
                  setError("");
                }}
              >
                Continue editing draft
              </button>
            </>
          )}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {!graph && !error && <p role="status">Loading project model…</p>}
      {graph && (
        <>
          <div className="am-actions">
            <button className="button primary" onClick={() => open("object")}>
              Add object
            </button>
            <button
              className="button secondary"
              disabled={!graph.objects.length}
              onClick={() => open("relationship")}
            >
              Link objects
            </button>
            <button className="button secondary" onClick={() => open("schema")}>
              Define project schema
            </button>
            <button
              className="button secondary"
              onClick={() => void loadHistory()}
            >
              Revision history
            </button>
            <button
              className="button secondary"
              onClick={() => void loadEvidence()}
            >
              Browse evidence
            </button>
            <span className="am-hint">Revision {graph.revision}</span>
          </div>
          {!graph.objects.length && (
            <section className="panel am-empty">
              <h2>Build a model from evidence</h2>
              <p>
                Browse the project site and ask the assistant to compose
                meaningful objects, or create an object. The catalog adds no
                empty nodes.
              </p>
              <Link to={`/projects/${engagement!.id}/workbench?view=browser`}>
                Open Browser
              </Link>
            </section>
          )}
          <div className="am-workspace">
            <aside className="panel am-outline" aria-label="Object outline">
              <h2>
                Objects <small>{graph.objects.length}</small>
              </h2>
              <label>
                Search objects
                <input
                  value={query}
                  onChange={(e) => select("q", e.target.value)}
                />
              </label>
              <label>
                Category
                <select
                  value={category}
                  onChange={(e) => select("category", e.target.value)}
                >
                  <option value="">All categories</option>
                  {graph.schema.categories.map((c) => (
                    <option key={c.name}>{c.name}</option>
                  ))}
                </select>
              </label>
              {graph.schema.categories.map((c) => {
                const items = filtered.filter(
                  (o) =>
                    graph.schema.types.find(
                      (t) => t.name === o.classification.value,
                    )?.category === c.name,
                );
                return items.length ? (
                  <section key={c.name}>
                    <h3>{c.name}</h3>
                    {items.map((o) => (
                      <button
                        className={`button secondary am-object ${o.id === objectId ? "selected" : ""}`}
                        key={o.id}
                        onClick={() => select("object", o.id)}
                      >
                        {o.label}
                        <small>
                          {String(o.classification.value)} ·{" "}
                          {o.classification.status}
                        </small>
                      </button>
                    ))}
                  </section>
                ) : null;
              })}
              {!filtered.length && graph.objects.length > 0 && (
                <p>
                  No objects match.{" "}
                  <button
                    className="button secondary"
                    onClick={() => {
                      select("q", "");
                      select("category", "");
                    }}
                  >
                    Clear filters
                  </button>
                </p>
              )}
            </aside>
            <section className="am-map" aria-label="Project relationships">
              <h2>Relationships</h2>
              <p className="am-legend">
                ━━ Observed · ┄┄ Hypothesized · ··· Disputed
              </p>
              <label>
                Neighborhood depth
                <select
                  value={depth}
                  onChange={(e) => select("depth", e.target.value)}
                >
                  {[1, 2, 3].map((n) => (
                    <option key={n} value={n}>
                      {n} {n === 1 ? "hop" : "hops"}
                    </option>
                  ))}
                </select>
              </label>
              <div
                className={`am-desktop-map ${params.get("map") === "show" ? "am-show-map" : ""}`}
              >
                <ApplicationModelGraph
                  objects={filtered}
                  layoutObjects={graph.objects}
                  relationships={graph.relationships}
                  selected={objectId}
                  depth={depth}
                  onSelect={(id) => select("object", id)}
                  onRelationship={(id) => select("relationship", id)}
                />
              </div>
              <button
                className="button am-map-toggle"
                onClick={() =>
                  select("map", params.get("map") === "show" ? "" : "show")
                }
              >
                {params.get("map") === "show" ? "Hide map" : "Show map"}
              </button>
              <div className="am-relationship-list">
                {graph.relationships
                  .filter(
                    (r) =>
                      (!objectId ||
                        r.source === objectId ||
                        r.target === objectId) &&
                      filtered.some(
                        (o) => o.id === r.source || o.id === r.target,
                      ),
                  )
                  .map((r) => (
                    <button
                      className={`button secondary am-relation ${r.claim.status}`}
                      key={r.id}
                      onClick={() => select("relationship", r.id)}
                    >
                      {graph.objects.find((o) => o.id === r.source)?.label} →{" "}
                      {r.type} →{" "}
                      {graph.objects.find((o) => o.id === r.target)?.label}
                      <small>{r.claim.status}</small>
                    </button>
                  ))}
                {!graph.relationships.length && (
                  <p>
                    No relationships yet. Link objects when their relationship
                    is supported by evidence or an explicit hypothesis.
                  </p>
                )}
              </div>
            </section>
            <aside className="panel am-inspector" aria-label="Model inspector">
              {editor ? (
                <fieldset
                  disabled={busy || !!pending}
                  className="am-editor-boundary"
                >
                  {editor === "object" ? (
                    <ObjectEditor
                      key={editing ? objectId : "new"}
                      graph={graph}
                      item={editing ? object : undefined}
                      evidence={evidence}
                      save={save}
                      cancel={() => setEditor(null)}
                      busy={busy}
                    />
                  ) : editor === "relationship" ? (
                    <RelationshipEditor
                      key={editing ? edgeId : "new"}
                      graph={graph}
                      item={editing ? edge : undefined}
                      evidence={evidence}
                      save={save}
                      cancel={() => setEditor(null)}
                      busy={busy}
                    />
                  ) : (
                    <SchemaEditor
                      graph={graph}
                      save={save}
                      cancel={() => setEditor(null)}
                      busy={busy}
                    />
                  )}
                </fieldset>
              ) : (
                <>
                  <h2>
                    {object?.label ?? (edge ? edge.type : "Inspect the model")}
                  </h2>
                  {((objectId && !object) || (edgeId && !edge)) && (
                    <p role="status">
                      This item is unavailable or was dismissed.{" "}
                      <button
                        className="button secondary"
                        onClick={() => {
                          select("object", "");
                          select("relationship", "");
                        }}
                      >
                        Clear selection
                      </button>
                    </p>
                  )}
                  {object && (
                    <>
                      <p>Context: {object.authentication_context}</p>
                      {showClaim(object.classification, "Classification")}
                      {Object.entries(object.properties).map(([k, c]) => (
                        <div key={k}>
                          {showClaim(c, k)}
                          <button
                            className="button secondary"
                            onClick={() => {
                              const reason = window.prompt(`Why dismiss ${k}?`);
                              if (reason)
                                void save([
                                  {
                                    op: "dismiss",
                                    kind: "property",
                                    id: object.id,
                                    property: k,
                                    reason,
                                  },
                                ]);
                            }}
                          >
                            Dismiss {k}
                          </button>
                        </div>
                      ))}
                      <button
                        className="button secondary"
                        onClick={() => open("object", true)}
                      >
                        Edit object
                      </button>
                    </>
                  )}
                  {edge && (
                    <>
                      {showClaim(
                        edge.claim,
                        `${graph.objects.find((o) => o.id === edge.source)?.label} → ${edge.type} → ${graph.objects.find((o) => o.id === edge.target)?.label}`,
                      )}
                      <button
                        className="button secondary"
                        onClick={() => open("relationship", true)}
                      >
                        Edit relationship
                      </button>
                    </>
                  )}
                  {(object || edge) && (
                    <button
                      className="button secondary"
                      onClick={() => {
                        const reason = window.prompt(
                          "Why dismiss this interpretation? Original evidence and history are retained.",
                        );
                        if (reason)
                          void save([
                            {
                              op: "dismiss",
                              kind: object ? "object" : "relationship",
                              id: object?.id ?? edge!.id,
                              reason,
                            },
                          ]);
                      }}
                    >
                      Dismiss selection
                    </button>
                  )}
                  {!object && !edge && (
                    <p>
                      Select an object or labeled relationship to inspect
                      individual claims and source evidence.
                    </p>
                  )}
                </>
              )}
              {evidenceOffset !== null && (
                <button
                  className="button secondary"
                  onClick={() => void loadEvidence(evidenceOffset)}
                >
                  Load more evidence
                </button>
              )}
            </aside>
          </div>
          <section className="panel am-questions">
            <h2>Ask about this model</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                requestNebulaDraft(
                  {
                    text: `Application model question: ${question}\nProject revision: ${graph.revision}. Selected object: ${objectId || "none"}; relationship: ${edgeId || "none"}. Use model.search, model.neighborhood and model.get_evidence. Cite recorded evidence, identify contradictions, distinguish observed claims from interpretations, and propose changes before applying them.`,
                    sourceKind: "application_model",
                    sourceId: engagement!.id,
                    sourceLabel: "Application model",
                  },
                  "chat",
                );
              }}
            >
              <label>
                Model question
                <textarea
                  required
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="What supports this relationship?"
                />
              </label>
              <button className="button primary" disabled={!question.trim()}>
                Discuss with assistant
              </button>
            </form>
            <p className="am-hint">
              Opens an editable draft in the existing assistant. Graph browsing
              and editing work independently of the assistant runtime.
            </p>
          </section>
          {!!evidence.length && (
            <details className="panel am-evidence" open={!!ev}>
              <summary>Recorded evidence ({evidence.length})</summary>
              {(selectedEvidence ? [selectedEvidence] : evidence).map((e) => (
                <article key={e.kind + e.id}>
                  <h3>{e.kind.replaceAll("_", " ")}</h3>
                  <p>
                    {e.producer} · {e.occurred_at}
                  </p>
                  {params.get("evidenceRevision") &&
                    Number(params.get("evidenceRevision")) !== e.revision && (
                      <p role="status">
                        This source changed after the cited revision. Review the
                        saved source snapshot in revision history before
                        changing the claim.
                      </p>
                    )}
                  <dl>
                    {Object.entries(e.facts).map(([k, v]) => (
                      <div key={k}>
                        <dt>{k}</dt>
                        <dd>{String(v.value)}</dd>
                      </div>
                    ))}
                  </dl>
                  {e.context.browser_session_id && (
                    <Link
                      to={`/projects/${engagement!.id}/workbench?view=browser&browserSession=${encodeURIComponent(e.context.browser_session_id)}${e.kind === "browser_traffic" ? `&browserEngine=native&browserTool=traffic&browserExchange=${encodeURIComponent(e.id)}` : ""}`}
                    >
                      Open source browser
                    </Link>
                  )}
                  {e.kind === "evidence" && (
                    <Link to={`/projects/${engagement!.id}/evidence/${e.id}`}>
                      Open original evidence
                    </Link>
                  )}
                </article>
              ))}
              {selectedEvidence && (
                <button
                  className="button secondary"
                  onClick={() => select("evidence", "")}
                >
                  All evidence
                </button>
              )}
              {evidenceOffset !== null && (
                <button
                  className="button secondary"
                  onClick={() => void loadEvidence(evidenceOffset)}
                >
                  Load more evidence
                </button>
              )}
            </details>
          )}
          {!!history.length && (
            <details className="panel am-history" open>
              <summary>Revision history</summary>
              {history.map((h) => (
                <article key={h.revision}>
                  <h3>Revision {h.revision}</h3>
                  <p>
                    {h.producer} · {new Date(h.updated_at).toLocaleString()}
                  </p>
                  {h.changes.map((c, i) => (
                    <details key={i}>
                      <summary>
                        {c.op.replaceAll("_", " ")}
                        {c.reason ? ` · ${c.reason}` : ""}
                      </summary>
                      <pre>
                        {JSON.stringify(
                          { before: c.before, after: c.after },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  ))}
                </article>
              ))}
              {historyMore && (
                <button
                  className="button secondary"
                  onClick={() => void loadHistory(historyAfter)}
                >
                  More history
                </button>
              )}
            </details>
          )}
        </>
      )}
    </div>
  );
}
