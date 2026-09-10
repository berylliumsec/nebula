import { useEffect, useState } from "react";
import {
  blankClaim,
  inputClaim,
  parseProperty,
  type Claim,
  type Evidence,
  type Graph,
  type GraphObject,
  type Operation,
  type Relationship,
} from "./applicationModelTypes";

export function ClaimFields({
  claim,
  onChange,
  evidence,
  title,
}: {
  claim: Claim;
  onChange: (c: Claim) => void;
  evidence: Evidence[];
  title: string;
}) {
  return (
    <fieldset className="am-claim-fields">
      <legend>{title}</legend>
      <label>
        Status
        <select
          value={claim.status}
          onChange={(e) =>
            onChange({ ...claim, status: e.target.value as Claim["status"] })
          }
        >
          {["hypothesized", "observed", "disputed"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <label>
        Operator review
        <select
          value={claim.review}
          onChange={(e) =>
            onChange({ ...claim, review: e.target.value as Claim["review"] })
          }
        >
          {["unreviewed", "accepted", "rejected"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <p className="am-hint">
        Acceptance does not turn a hypothesis into an observation.
      </p>
      <label>
        Reason
        <textarea
          value={claim.reason}
          maxLength={2000}
          onChange={(e) => onChange({ ...claim, reason: e.target.value })}
        />
      </label>
      <details>
        <summary>
          Supporting / conflicting evidence ({claim.evidence.length})
        </summary>
        {!evidence.length && (
          <p>
            No recorded evidence yet. Browse the project site first, or record
            evidence in the project.
          </p>
        )}
        {evidence.map((item) => {
          const selected = claim.evidence.find(
            (r) => r.id === item.id && r.kind === item.kind,
          );
          return (
            <label key={item.kind + item.id}>
              {item.kind.replaceAll("_", " ")} ·{" "}
              {String(item.facts.route?.value ?? item.occurred_at ?? item.id)}
              <select
                aria-label={`Evidence role ${item.id}`}
                value={selected?.role ?? ""}
                onChange={(e) =>
                  onChange({
                    ...claim,
                    evidence: [
                      ...claim.evidence.filter(
                        (r) => !(r.id === item.id && r.kind === item.kind),
                      ),
                      ...(e.target.value
                        ? [
                            {
                              kind: item.kind,
                              id: item.id,
                              revision: item.revision,
                              role: e.target.value as
                                "supporting" | "conflicting",
                            },
                          ]
                        : []),
                    ],
                  })
                }
              >
                <option value="">Not attached</option>
                <option value="supporting">Supporting</option>
                <option value="conflicting">Conflicting</option>
              </select>
            </label>
          );
        })}
      </details>
    </fieldset>
  );
}

export function ObjectEditor({
  graph,
  item,
  evidence,
  save,
  cancel,
  busy,
}: {
  graph: Graph;
  item?: GraphObject;
  evidence: Evidence[];
  save: (ops: Operation[]) => Promise<void>;
  cancel: () => void;
  busy: boolean;
}) {
  const [type, setType] = useState(
    String(item?.classification.value ?? graph.schema.types[0]?.name ?? "Site"),
  );
  const [query, setQuery] = useState("");
  const [label, setLabel] = useState(item?.label ?? "");
  const [context, setContext] = useState(
    item?.authentication_context ?? "unspecified",
  );
  const [classification, setClassification] = useState(
    item ? inputClaim(item.classification) : blankClaim(type),
  );
  const [properties, setProperties] = useState<Record<string, Claim>>(
    Object.fromEntries(
      Object.entries(item?.properties ?? {}).map(([k, v]) => [
        k,
        inputClaim(v),
      ]),
    ),
  );
  const [error, setError] = useState("");
  const definition = graph.schema.types.find((t) => t.name === type)!;
  return (
    <form
      className="am-editor"
      aria-label={item ? "Edit object" : "Create object"}
      onSubmit={async (e) => {
        e.preventDefault();
        setError("");
        try {
          if ((!item || properties.purpose) && !String(properties.purpose?.value ?? "").trim()) {
            throw Error("Explain the behavior, dependency or uncertainty this object adds—or leave it as evidence instead.");
          }
          const values = Object.fromEntries(
            Object.entries(properties).map(([k, c]) => [
              k,
              {
                ...c,
                value: parseProperty(
                  definition.properties.find((p) => p.name === k)!,
                  String(c.value),
                ),
              },
            ]),
          );
          await save([
            {
              op: "put_object",
              id: item?.id ?? cryptoId(),
              label,
              authentication_context: context,
              classification: { ...classification, value: type },
              properties: values,
            },
          ]);
        } catch (err) {
          setError(String(err));
        }
      }}
    >
      <h2>{item ? "Edit object" : "Create object"}</h2>
      <label>
        Search types
        <input value={query} onChange={(e) => setQuery(e.target.value)} />
        <small className="am-hint">
          Up to 20 matches per category. Search to find a specific object.
        </small>
      </label>
      <label>
        Object type
        <select
          value={type}
          disabled={!!item}
          onChange={(e) => {
            setType(e.target.value);
            setProperties((v): Record<string, Claim> => v.purpose ? { purpose: v.purpose } : {});
          }}
        >
          {graph.schema.categories.map((c) => (
            <optgroup key={c.name} label={c.name}>
              {graph.schema.types
                .filter(
                  (t) =>
                    (!t.legacy || (!!item && t.name === type)) &&
                    t.category === c.name &&
                    (t.name === type ||
                      `${t.name} ${t.description}`
                        .toLowerCase()
                        .includes(query.toLowerCase())),
                )
                .map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.label}
                    {t.legacy ? " (legacy)" : ""}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
      </label>
      <p>{definition?.description}</p>
      <label>
        Object label
        <input
          autoFocus
          required
          value={label}
          maxLength={300}
          onChange={(e) => setLabel(e.target.value)}
        />
      </label>
      <label>
        Authentication context
        <input
          required
          disabled={!!item}
          value={context}
          maxLength={200}
          onChange={(e) => setContext(e.target.value)}
        />
      </label>
      <p className="am-hint">
        Keep anonymous and signed-in observations separate. Omit secret values.
      </p>
      {(!item || definition?.properties.some(p => p.name === "purpose")) && <>
        <label>
          What this explains
          <textarea
            aria-describedby="am-purpose-help"
            required={!item || !!properties.purpose}
            maxLength={2000}
            value={String(properties.purpose?.value ?? "")}
            onChange={e => setProperties(v => {
              const next = { ...v };
              if (item && !item.properties.purpose && !e.target.value.trim()) delete next.purpose;
              else next.purpose = { ...(v.purpose ?? blankClaim("")), value: e.target.value };
              return next;
            })}
          />
        </label>
        <p id="am-purpose-help" className="am-hint">Explain a behavior, meaningful dependency or specific uncertainty—not just what the page contains. If it adds no understanding, leave it as evidence.</p>
        {properties.purpose && <details><summary>Explanation evidence and status</summary>
          <ClaimFields title="Explanation claim" claim={properties.purpose} evidence={evidence}
            onChange={c => setProperties(v => ({ ...v, purpose: c }))} />
        </details>}
      </>}
      <ClaimFields
        title="Object classification"
        claim={classification}
        onChange={setClassification}
        evidence={evidence}
      />
      <h3>Properties</h3>
      <p className="am-hint">
        Enable only properties supported by your evidence or interpretation.
      </p>
      {definition?.properties.filter(p => p.name !== "purpose").map((p) => (
        <details key={p.name}>
          <summary>
            {p.name}
            {properties[p.name]
              ? ` · ${properties[p.name].status}`
              : " · unspecified"}
          </summary>
          <label className="am-checkbox">
            <input
              type="checkbox"
              checked={!!properties[p.name]}
              disabled={!!item?.properties[p.name]}
              onChange={(e) =>
                setProperties((v) => {
                  const next = { ...v };
                  if (e.target.checked)
                    next[p.name] = blankClaim(
                      p.kind === "boolean" ? false : "",
                    );
                  else delete next[p.name];
                  return next;
                })
              }
            />
            Include {p.name}
          </label>
          {properties[p.name] && (
            <>
              <label>
                {p.description}
                {p.kind === "boolean" || p.kind === "enum" ? (
                  <select
                    value={String(properties[p.name].value)}
                    onChange={(e) =>
                      setProperties((v) => ({
                        ...v,
                        [p.name]: { ...v[p.name], value: e.target.value },
                      }))
                    }
                  >
                    {(p.kind === "boolean" ? ["false", "true"] : p.choices).map(
                      (v) => (
                        <option key={v}>{v}</option>
                      ),
                    )}
                  </select>
                ) : (
                  <input
                    value={String(properties[p.name].value)}
                    onChange={(e) =>
                      setProperties((v) => ({
                        ...v,
                        [p.name]: { ...v[p.name], value: e.target.value },
                      }))
                    }
                  />
                )}
              </label>
              <ClaimFields
                title={`${p.name} claim`}
                claim={properties[p.name]}
                onChange={(c) => setProperties((v) => ({ ...v, [p.name]: c }))}
                evidence={evidence}
              />
            </>
          )}
        </details>
      ))}
      {error && <p role="alert">{error}</p>}
      <div className="am-actions">
        <button className="button primary" disabled={busy}>
          Save object
        </button>
        <button type="button" className="button secondary" onClick={cancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function cryptoId() {
  // getRandomValues is available on insecure LAN origins; randomUUID is not.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}

export function RelationshipEditor({
  graph,
  searchObjects,
  item,
  evidence,
  save,
  cancel,
  busy,
}: {
  graph: Graph;
  searchObjects?: (query: string) => Promise<Graph["objects"]>;
  item?: Relationship;
  evidence: Evidence[];
  save: (ops: Operation[]) => Promise<void>;
  cancel: () => void;
  busy: boolean;
}) {
  const [source, setSource] = useState(
    item?.source ?? graph.objects[0]?.id ?? "",
  );
  const [target, setTarget] = useState(
    item?.target ?? graph.objects[1]?.id ?? "",
  );
  const [type, setType] = useState(item?.type ?? "contains");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState(graph.objects);
  const [searchError, setSearchError] = useState("");
  useEffect(() => {
    if (!searchObjects) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      void searchObjects(query)
        .then((result) => {
          if (cancelled) return;
          setMatches((previous) => [
            ...new Map(
              [
                ...result,
                ...previous.filter((o) => o.id === source || o.id === target),
              ].map((o) => [o.id, o]),
            ).values(),
          ]);
          setSearchError("");
        })
        .catch(() => {
          if (!cancelled)
            setSearchError(
              "Could not search project objects. Change the search to retry; your selections are retained.",
            );
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, searchObjects, source, target]);
  const [claim, setClaim] = useState(
    item ? inputClaim(item.claim) : blankClaim(true),
  );
  const sourceObject = matches.find((o) => o.id === source),
    targetObject = matches.find((o) => o.id === target);
  const sourceDef = graph.schema.types.find(
      (t) => t.name === sourceObject?.classification.value,
    ),
    targetDef = graph.schema.types.find(
      (t) => t.name === targetObject?.classification.value,
    );
  const compatible = graph.schema.relationships.filter(
    (r) =>
      (item &&
        r.name === item.type &&
        source === item.source &&
        target === item.target) ||
      (!r.legacy &&
        !sourceDef?.legacy &&
        !targetDef?.legacy &&
        sourceDef?.outgoing_relationships.includes(r.name) &&
        targetDef?.incoming_relationships.includes(r.name)),
  );
  const effective = compatible.some((r) => r.name === type)
    ? type
    : (compatible[0]?.name ?? "");
  return (
    <form
      className="am-editor"
      aria-label="Edit relationship"
      onSubmit={(e) => {
        e.preventDefault();
        void save([
          {
            op: "put_relationship",
            id: item?.id ?? cryptoId(),
            type: effective,
            source,
            target,
            claim: inputClaim(claim),
          },
        ]);
      }}
    >
      <h2>{item ? "Edit relationship" : "Link objects"}</h2>
      {searchError && <p role="alert">{searchError}</p>}
      <label>
        Search objects to link
        <input value={query} onChange={(e) => setQuery(e.target.value)} />
      </label>
      {(
        [
          ["From", source, setSource],
          ["To", target, setTarget],
        ] as const
      ).map(([name, value, set]) => (
        <label key={name}>
          {name}
          <select required value={value} onChange={(e) => set(e.target.value)}>
            <option value="">Choose an object</option>
            {graph.schema.categories.map((category) => {
              const choicesInCategory = matches.filter(
                (o) =>
                  graph.schema.types.find(
                    (t) => t.name === o.classification.value,
                  )?.category === category.name &&
                  (o.id === value ||
                    o.label.toLowerCase().includes(query.toLowerCase())),
              );
              const choices = [
                ...choicesInCategory.filter((o) => o.id === value),
                ...choicesInCategory.filter((o) => o.id !== value).slice(0, 20),
              ];
              return choices.length ? (
                <optgroup
                  key={category.name}
                  label={`${category.name} (${choicesInCategory.length})`}
                >
                  {choices.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.label} · {String(o.classification.value)}
                    </option>
                  ))}
                </optgroup>
              ) : null;
            })}
          </select>
        </label>
      ))}
      <label>
        Relationship
        <select
          required
          value={effective}
          onChange={(e) => setType(e.target.value)}
        >
          {compatible.map((r) => (
            <option value={r.name} key={r.name}>
              {r.label}
            </option>
          ))}
        </select>
      </label>
      {!compatible.length && (
        <p>
          No compatible relationship. Choose other endpoints or define a project
          relationship.
        </p>
      )}
      <ClaimFields
        title="Relationship claim"
        claim={claim}
        onChange={setClaim}
        evidence={evidence}
      />
      <div className="am-actions">
        <button className="button primary" disabled={busy || !effective}>
          Save relationship
        </button>
        <button type="button" className="button secondary" onClick={cancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function SchemaEditor({
  graph,
  save,
  cancel,
  busy,
}: {
  graph: Graph;
  save: (ops: Operation[]) => Promise<void>;
  cancel: () => void;
  busy: boolean;
}) {
  const [kind, setKind] = useState("type"),
    [name, setName] = useState(""),
    [description, setDescription] = useState("");
  const [category, setCategory] = useState("Security"),
    [parent, setParent] = useState("SecurityPolicy");
  const [source, setSource] = useState("Operation"),
    [target, setTarget] = useState("SecurityPolicy");
  const [example, setExample] = useState("");
  const [props, setProps] = useState<
    { name: string; kind: string; description: string }[]
  >([]);
  return (
    <form
      className="am-editor"
      aria-label="Define project schema"
      onSubmit={(e) => {
        e.preventDefault();
        const base = graph.schema.types.find((t) => t.name === parent)!;
        void save([
          {
            op: kind === "type" ? "define_type" : "define_relationship",
            definition: {
              name: `custom.${name}`,
              label: name,
              description,
              evidence_examples: [example],
              ...(kind === "type"
                ? {
                    category,
                    extends: parent,
                    properties: props,
                    identity_hints: base.identity_hints,
                  }
                : { source_types: [source], target_types: [target] }),
            },
          },
        ]);
      }}
    >
      <h2>Define project schema</h2>
      <label>
        Definition kind
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="type">Object type</option>
          <option value="relationship">Relationship</option>
        </select>
      </label>
      <label>
        Name
        <input
          required
          value={name}
          pattern={kind === "type" ? "[A-Z][A-Za-z0-9]*" : "[a-z][a-z0-9_]*"}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        Definition
        <textarea
          required
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      {kind === "type" ? (
        <>
          <label>
            Category
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              {graph.schema.categories.map((c) => (
                <option key={c.name}>{c.name}</option>
              ))}
            </select>
          </label>
          <label>
            Specializes
            <select value={parent} onChange={(e) => setParent(e.target.value)}>
              {graph.schema.types
                .filter((t) => !t.legacy)
                .map((t) => (
                  <option key={t.name}>{t.name}</option>
                ))}
            </select>
          </label>
          {props.map((p, i) => (
            <fieldset key={i}>
              <legend>Property {i + 1}</legend>
              <label>
                Property name
                <input
                  required
                  pattern="[a-z][a-z0-9_]*"
                  value={p.name}
                  onChange={(e) =>
                    setProps((v) =>
                      v.map((p, j) =>
                        i === j ? { ...p, name: e.target.value } : p,
                      ),
                    )
                  }
                />
              </label>
              <label>
                Value type
                <select
                  value={p.kind}
                  onChange={(e) =>
                    setProps((v) =>
                      v.map((p, j) =>
                        i === j ? { ...p, kind: e.target.value } : p,
                      ),
                    )
                  }
                >
                  {["string", "boolean", "integer", "number"].map((k) => (
                    <option key={k}>{k}</option>
                  ))}
                </select>
              </label>
              <label>
                Property definition
                <input
                  required
                  value={p.description}
                  onChange={(e) =>
                    setProps((v) =>
                      v.map((p, j) =>
                        i === j ? { ...p, description: e.target.value } : p,
                      ),
                    )
                  }
                />
              </label>
              <button
                type="button"
                className="button secondary"
                onClick={() => setProps((v) => v.filter((_, j) => i !== j))}
              >
                Remove property
              </button>
            </fieldset>
          ))}
          <button
            className="button secondary"
            type="button"
            onClick={() =>
              setProps((v) => [
                ...v,
                { name: "", kind: "string", description: "" },
              ])
            }
          >
            Add property
          </button>
        </>
      ) : (
        <>
          {(
            [
              ["Source type", source, setSource],
              ["Target type", target, setTarget],
            ] as const
          ).map(([label, value, set]) => (
            <label key={label}>
              {label}
              <select value={value} onChange={(e) => set(e.target.value)}>
                {graph.schema.types
                  .filter((t) => !t.legacy)
                  .map((t) => (
                    <option key={t.name}>{t.name}</option>
                  ))}
              </select>
            </label>
          ))}
        </>
      )}
      <label>
        Evidence example
        <textarea
          required
          value={example}
          onChange={(e) => setExample(e.target.value)}
        />
      </label>
      <p className="am-hint">
        Available only in this project. Defining a type does not create an
        object.
      </p>
      <div className="am-actions">
        <button className="button primary" disabled={busy}>
          Save definition
        </button>
        <button type="button" className="button secondary" onClick={cancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
