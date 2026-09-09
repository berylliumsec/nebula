export type EvidenceRef = {
  kind: string;
  id: string;
  revision: number;
  role: "supporting" | "conflicting";
};
export type Evidence = Omit<EvidenceRef, "role"> & {
  producer: string;
  occurred_at?: string;
  context: { browser_session_id?: string; identity_id?: string };
  facts: Record<string, { value: unknown }>;
};
export type Claim = {
  value: string | number | boolean;
  status: "observed" | "hypothesized" | "disputed";
  evidence: EvidenceRef[];
  reason: string;
  review: "unreviewed" | "accepted" | "rejected";
  producer?: string;
  updated_at?: string;
  revision?: number;
};
export type GraphObject = {
  id: string;
  label: string;
  authentication_context: string;
  classification: Claim;
  properties: Record<string, Claim>;
  revision: number;
};
export type Relationship = {
  id: string;
  type: string;
  source: string;
  target: string;
  claim: Claim;
};
export type Property = {
  name: string;
  kind: "string" | "integer" | "number" | "boolean" | "enum";
  description: string;
  choices: string[];
};
export type TypeDefinition = {
  name: string;
  label: string;
  category: string;
  description: string;
  extends?: string;
  legacy?: boolean;
  properties: Property[];
  identity_hints: string[];
  outgoing_relationships: string[];
  incoming_relationships: string[];
};
export type Schema = {
  categories: { name: string; purpose: string }[];
  types: TypeDefinition[];
  relationships: {
    name: string;
    legacy?: boolean;
    label: string;
    description: string;
    source_types: string[];
    target_types: string[];
  }[];
};
export type Graph = {
  project_id: string;
  revision: number;
  objects: GraphObject[];
  relationships: Relationship[];
  schema: Schema;
  object_total?: number;
  relationship_total?: number;
  category_counts?: Record<string, number>;
  effective_category?: string;
  outline_objects?: GraphObject[];
  outline_total?: number;
  outline_offset?: number;
  map_objects?: GraphObject[];
  map_relationships?: Relationship[];
  map_truncated?: boolean;
  frontier_count?: number;
  listed_relationships?: Relationship[];
  related_total?: number;
  relationship_offset?: number;
};
export type Operation = Record<string, unknown>;
export const blankClaim = (value: Claim["value"]): Claim => ({
  value,
  status: "hypothesized",
  evidence: [],
  reason: "",
  review: "unreviewed",
});
export const inputClaim = (c: Claim): Claim => ({
  value: c.value,
  status: c.status,
  evidence: c.evidence.map(({ kind, id, revision, role }) => ({
    kind,
    id,
    revision,
    role,
  })),
  reason: c.reason,
  review: c.review,
});
export function parseProperty(prop: Property, text: string): Claim["value"] {
  if (prop.kind === "boolean") {
    if (!["true", "false"].includes(text))
      throw new Error("Choose true or false.");
    return text === "true";
  }
  if (prop.kind === "integer" || prop.kind === "number") {
    const n = Number(text);
    if (
      !text.trim() ||
      !Number.isFinite(n) ||
      (prop.kind === "integer" && !Number.isSafeInteger(n))
    )
      throw new Error(`Enter a valid ${prop.kind} for ${prop.name}.`);
    return n;
  }
  return text;
}
