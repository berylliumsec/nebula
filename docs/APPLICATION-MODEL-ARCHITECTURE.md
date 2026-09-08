# Nebula Application Model Architecture

## Status

V1 implementation contract. The approved first delivery is the recorded-state
engine, Z3 analysis, and a minimal inspector. Sections discussing hypothetical
successors, action bindings, or broad exploration strategies describe later
extensions; V1 never dispatches a generated interaction.

### Implemented V1 decisions

- The application model is an always-available project capability. Branch isolation
  is the rollout boundary; Core does not use a runtime feature flag.
- Use the existing Core entity repository and the additive
  `application_model_outbox` migration. The source transaction writes a bounded
  projection envelope; retries and historical import share the same adapter.
- Separate browser-context branches and record unknown causality explicitly.
  State parentage records knowledge accumulation, not a claim that one remote
  action caused the next.
- States reference immutable object versions. An interpretation fork changes
  selected assumptions; it does not reset remote state or restore credentials.
- Extract metadata and bounded scalar fields from already-redacted JSON
  response artifacts. An exact route plus explicit response ID can identify a
  versioned response representation within one browser context. This does not
  prove backend database identity or durable persistence.
- Solver V1 supports quantifier-free Boolean, integer, equality, finite-domain,
  and finite-membership conditions. The API accepts a typed formula tree; it
  does not accept Python or solver scripts.
- Every query records its state, formula, assumptions, assignments, and solver
  version. Base inconsistency is distinguished from an unsatisfiable condition.
  Unknown values remain unconstrained unless an explicit domain restriction exists.
  Two worker processes may run concurrently,
  with a maximum of 16 queued/running requests and a five-second solver budget.
- The inspector is at **Project → Application model**. Collection, state,
  object, query, and view selections use URL parameters.
- Existing browser chat context advertises bounded `model.*` discovery,
  inspection, proposal, and consistency-query tools when the feature is enabled.
- Harness agents receive the same project-scoped capabilities through Nebula's
  authenticated MCP gateway and frozen command-runtime snapshot. The incremental
  `model.get_updates` capability returns immutable states and observations after
  a state checkpoint, allowing a model to follow newly committed browser facts
  without initiating or replaying browser interactions.
- V1 aliases and shared symbols resolve only through declared typed references.
  Conditional values are represented but require a supported explicit formula
  before solving; they are never silently treated as concrete facts.
- Verification evidence and remaining release gates are recorded in
  [APPLICATION-MODEL-V1-HANDOFF.md](APPLICATION-MODEL-V1-HANDOFF.md).

### Operator acceptance contract

| Journey | Observable invariant | Authority | Required evidence |
|---|---|---|---|
| Discover and create | Enumerated browser sessions create selectable collections | Core + Project navigation | Components + real Core |
| Collect and inspect | Committed source records produce attributable states | Core source records + outbox | Capture integration + rollback/retry tests |
| Compare and solve | Results identify the selected state and assumptions | State/query records | Solver + API tests |
| Refresh and reconnect | Selection and saved results survive reload | URL + Core | Production browser tests |
| Pause/resume and retry | Capture continues independently of projection | Core collection status | Fault and lifecycle tests |
| Delete | Derived data is removed; source evidence remains | Core repository | API + browser tests |
| Mobile and LAN | Same readable records and queries, without native execution claims | Core + browser UI | Chromium/WebKit + LAN checks |

Automated browser acceptance uses the dedicated
`ui/playwright.application-model.config.ts` matrix. Native browser/agent process
coverage and physical-device testing must be reported separately from Core
capture-route tests and emulated browser profiles.

V1 builds an evidence-backed representation of a network, website, or API from
observed interactions. It records immutable observations, derives typed objects
and states, models transitions between those states, and uses a constraint
solver to answer questions about the resulting model.

V1 is not an autonomous bug-discovery system. It establishes the state,
provenance, execution, and solver foundations that later versions can use to
reason about bugs. The graph UI is a projection and debugger for this engine,
not the source of truth.

## Design principles

1. **Evidence precedes inference.** Every inferred fact must reference the
   observations that support it.
2. **Observations are immutable.** New evidence supersedes or contradicts an
   assertion; it never rewrites history.
3. **Unknown is a first-class value.** Missing information is not treated as
   false, safe, or absent.
4. **States are immutable and forkable.** Each interaction creates a successor
   state or records why no successor was observed.
5. **Competing explanations are retained.** The engine can represent several
   backend models until evidence distinguishes them.
6. **The solver reasons over typed facts.** It does not invent backend behavior
   or promote model output into fact.
7. **Reproduction is deterministic.** A state must identify the observations,
   assertions, constraints, and actions from which it was derived.
8. **Network, browser, and API interactions share one model.** Protocol-specific
   adapters normalize evidence into a common representation.

## System overview

```text
                          NEBULA APPLICATION MODEL V1

  +------------------- Interaction Sources --------------------+
  | Browser | HTTP/API | WebSocket | Network tools | Operator  |
  +----+----------+----------+-------------+------------+-------+
       |          |          |             |            |
       +----------+----------+------+------+------------+
                                     |
                                     v
                         +-------------------------+
                         | Interaction Normalizer  |
                         | request/action/response |
                         +------------+------------+
                                      |
                                      v
                         +-------------------------+
                         | Immutable Observation   |
                         | Store + Artifact Blobs  |
                         +------------+------------+
                                      |
                     +----------------+----------------+
                     |                                 |
                     v                                 v
          +----------------------+          +----------------------+
          | Assertion Pipeline   |          | Object Materializer  |
          | observed / inferred  |--------->| resources, sessions, |
          | unknown / contrad.   |          | identities, routes   |
          +----------+-----------+          +----------+-----------+
                     |                                 |
                     +----------------+----------------+
                                      |
                                      v
                         +-------------------------+
                         | Immutable State Store   |
                         | snapshots + deltas      |
                         +------------+------------+
                                      |
                          action      |      result
                     +----------------+----------------+
                     |                                 |
                     v                                 v
          +----------------------+          +----------------------+
          | Successor Engine     |<-------->| Constraint Engine    |
          | apply transitions    |          | Z3 + typed formulas  |
          +----------+-----------+          +----------+-----------+
                     |                                 |
                     +----------------+----------------+
                                      |
                                      v
                         +-------------------------+
                         | Query / Projection API  |
                         +------------+------------+
                                      |
                    +-----------------+------------------+
                    |                                    |
                    v                                    v
          +----------------------+             +-------------------+
          | Graph / State UI     |             | Export / Replay   |
          | engine debugger      |             | reproducible run  |
          +----------------------+             +-------------------+
```

## angr correspondence

Nebula is not executing a server binary. It is modeling a partially observable,
distributed system from its externally visible behavior. The correspondence to
angr is therefore architectural rather than literal.

| angr concept | Nebula application-model concept |
|---|---|
| Program | The observed website, API, or network service |
| Machine state | An immutable application/world state |
| Registers | Actor, identity, tenant, role, session, request, and routing context |
| Memory objects | Resources, tokens, cookies, files, jobs, caches, and inferred persisted values |
| Basic block | A normalized browser action, request, protocol message, callback, or timer event |
| Successors | Candidate or observed states resulting from an action |
| Path constraints | Preconditions and facts accumulated along a state path |
| Symbolic value | A typed value whose concrete value or relationship is not yet known |
| Concretization | Selecting concrete interaction values that satisfy a solver model |
| Z3 backend | A solver adapter for state consistency and satisfiability queries |
| Simulation manager | Exploration context containing active, completed, errored, and unresolved states |

## Core domain model

```text
Model
 +-- Target
 +-- Interaction*
 |    +-- Action
 |    +-- Observation*
 |    +-- ArtifactRef*
 +-- Assertion*
 |    +-- EvidenceRef*
 |    +-- AlternativeAssertionRef*
 +-- Object*
 |    +-- Property*
 |    +-- ObjectVersion*
 +-- State*
 |    +-- ObjectVersionRef*
 |    +-- ConstraintRef*
 |    +-- ParentStateRef
 +-- Transition*
 |    +-- Preconditions
 |    +-- ActionRef
 |    +-- SourceStateRef
 |    +-- DestinationStateRef*
 +-- SolverQuery*
      +-- Formula
      +-- Result
      +-- ModelAssignments
```

Identifiers should be globally unique and stable. Records should include
`project_id`, `target_id`, `created_at`, and a schema version. Content hashes
should be used for large artifacts and canonical state data.

## Interactions and observations

An interaction is the causal envelope around an action and everything observed
as a result.

```yaml
interaction:
  id: int_01J...
  parent_state_id: state_01J...
  source: browser
  actor_id: actor_member_a
  action:
    kind: http_request
    method: POST
    url: https://example.test/api/v2/invoices/7/submit
    body_artifact_id: artifact_01J...
  observations:
    - id: obs_01J...
      kind: http_response
      status: 200
      headers_artifact_id: artifact_01J...
      body_artifact_id: artifact_01J...
    - id: obs_01K...
      kind: cookie_change
      cookie_name: session
      value_digest: sha256:...
  started_at: 2026-09-08T14:00:00Z
  completed_at: 2026-09-08T14:00:01Z
```

Observations record what occurred, not what it means. Raw request and response
bodies belong in an artifact store; structured records retain hashes, bounded
previews, parsing metadata, and references. Secrets must be encrypted or
redacted according to project policy while preserving stable equality tokens
where correlation is permitted.

Useful observation kinds include:

- HTTP request and response
- Redirect and navigation
- DOM mutation and browser storage change
- Cookie and token issuance
- WebSocket frame
- DNS, TCP, TLS, and protocol event
- Timeout, reset, rejection, or delivery failure
- Resource read-back
- File, cache, or queue side effect
- Operator annotation

## Assertions and hypotheses

Assertions translate observations into typed claims. They may be produced by a
deterministic extractor, an AI proposal, or an operator.

```yaml
assertion:
  id: asrt_01J...
  subject: object_gateway
  predicate: routes_to
  object: object_orders_api
  epistemic_status: inferred
  confidence: 0.63
  evidence:
    - observation_id: obs_01J...
      role: supports
  alternatives:
    - assertion_id: asrt_01K...
  producer:
    kind: model
    name: nebula-inference
    version: v1
  valid_from_state_id: state_01J...
```

`epistemic_status` is one of:

- `observed`: directly supported by normalized evidence
- `inferred`: a typed explanation consistent with evidence
- `unknown`: explicitly represented but unresolved
- `contradicted`: inconsistent with later evidence, without being deleted

Confidence is ranking metadata, not logical truth. Solver formulas use explicit
assertions selected by a query policy; they must never silently translate a
confidence score into a Boolean fact.

## Memory objects

A memory object represents a security- or behavior-relevant entity whose value
persists or influences later interactions.

```yaml
object:
  id: obj_invoice_7
  kind: resource
  type_name: Invoice
  identity:
    symbolic_name: invoice_id
    concrete_aliases:
      - source: url_path
        value: /api/v2/invoices/7
  version:
    id: objv_01J...
    properties:
      owner_id:
        type: ActorRef
        value: actor_member_a
        status: observed
      tenant_id:
        type: TenantRef
        symbolic: tenant_of_invoice_7
        status: inferred
      status:
        type: Enum
        domain: [draft, submitted, approved, cancelled]
        value: submitted
        status: observed
      amount:
        type: Integer
        symbolic: amount_invoice_7
        constraints:
          - amount_invoice_7 >= 0
    evidence:
      - obs_01J...
```

Required object categories for V1:

- Actor, identity, role, and tenant
- Session, cookie, credential, and token
- Application resource and resource version
- Browser storage entry
- Route, endpoint, and protocol peer
- Infrastructure component or generic inferred component
- File or object-storage item
- Cache entry and queued job
- Timer, expiration, or pending asynchronous effect

Values support the following forms:

```text
Concrete     owner_id = actor_a
Symbolic     amount = Int("amount")
Constrained  0 <= amount <= account_limit
Conditional  token exists only when auth_path = success
Aliased      /invoice/7 and gid://Invoice/7 may identify one object
Unknown      issuer is represented but has no selected value
Alternative  issuer is gateway OR auth_service
```

Object identity and object version are separate. Two observations may refer to
the same object at different times; conversely, matching strings do not prove
object identity. Alias relationships therefore carry evidence and epistemic
status like every other assertion.

## Application state

A state is an immutable snapshot of all facts required to reproduce and reason
about a point in an interaction path.

```yaml
state:
  id: state_01J...
  target_id: target_example
  parent_state_id: state_01I...
  caused_by_interaction_id: int_01J...
  actors:
    active_actor_id: actor_member_a
    authenticated_actor_ids: [actor_member_a]
  context:
    tenant_id: tenant_x
    browser_context_id: browser_3
    network_path_id: path_edge_a
  object_versions:
    - objv_session_a_4
    - objv_invoice_7_2
  pending_effects:
    - effect_job_91
  path_constraints:
    - constraint_authenticated_a
    - constraint_invoice_submitted
  epistemic_context:
    accepted_assertion_ids: [asrt_01J...]
    unresolved_assertion_sets: [[asrt_01K..., asrt_01L...]]
  depth: 12
  canonical_hash: sha256:...
```

State creation follows copy-on-write semantics:

```text
State S0
  |
  +-- interaction A --> State S1
  |                       |
  |                       +-- interaction B --> State S2
  |                       |
  |                       +-- interaction C --> State S3
  |
  +-- competing interpretation --> State S1'
```

The implementation may persist deltas for efficiency, but the domain contract
must expose a complete immutable state. State equality uses canonical semantic
content, not database row identity.

## Transitions and successor generation

A transition connects a source state, an action, and zero or more possible
destination states.

```yaml
transition:
  id: trans_01J...
  source_state_id: state_01J...
  action_template_id: action_submit_invoice
  preconditions:
    - authenticated(active_actor)
    - invoice.status == draft
  bindings:
    invoice: obj_invoice_7
    active_actor: actor_member_a
  outcomes:
    - destination_state_id: state_01K...
      status: observed
      evidence: [obs_01M...]
    - destination_state_id: state_01L...
      status: inferred
      assertion_ids: [asrt_01N...]
```

The successor engine:

1. Loads a source state and action template.
2. Creates typed symbolic variables for unbound inputs.
3. Adds state path constraints and transition preconditions.
4. Asks the solver whether the transition is satisfiable.
5. Optionally obtains concrete bindings from the solver model.
6. Records a candidate successor before execution or an observed successor after
   an interaction.
7. Preserves alternative outcomes when evidence is insufficient.

V1 should distinguish successor status clearly:

- `candidate`: solver-consistent but not executed
- `observed`: supported by a completed interaction
- `infeasible`: unsatisfiable under the selected assertions
- `blocked`: execution could not reach the target
- `errored`: the adapter or action failed
- `unresolved`: evidence cannot select among successor explanations

## Constraint engine

The constraint engine provides a typed intermediate representation with a Z3
adapter. Domain records must not store raw solver-specific expressions as their
only representation.

```text
Typed formula IR
   |
   +-- Boolean: and, or, not, implies
   +-- Equality and inequality
   +-- Integer and bounded numeric expressions
   +-- Enumerations and finite domains
   +-- Strings and byte sequences, used selectively
   +-- Object identity and alias relationships
   +-- Set membership for actors, roles, scopes, and visibility
   +-- Time, expiry, and ordering constraints
   +-- Conditional existence
   |
   v
Z3 compiler -> solver context -> SAT / UNSAT / UNKNOWN + model
```

Example query:

```yaml
solver_query:
  id: query_01J...
  base_state_id: state_01J...
  assertion_policy: observed_and_selected_inferences
  formula:
    and:
      - eq: [invoice.status, submitted]
      - neq: [active_actor.id, invoice.owner_id]
      - contains: [active_actor.roles, member]
  result:
    status: sat
    assignments:
      active_actor.id: actor_member_b
      invoice.id: obj_invoice_7
  solver:
    name: z3
    version: recorded-at-runtime
  formula_hash: sha256:...
```

The API should support:

- `check(state, formula) -> SAT | UNSAT | UNKNOWN`
- `model(state, formula) -> typed assignments`
- `enumerate(state, formula, limit) -> assignment set`
- `explain_unsat(query) -> unsat core when available`
- `simplify(formula) -> normalized formula`
- `compare(state_a, state_b) -> changed constraints and objects`

`UNKNOWN` must remain distinct from `UNSAT`. Unsupported types, solver
timeouts, and incomplete assertion sets must be visible in the result.

## Exploration contexts

An exploration context is the equivalent of an angr simulation manager. It
organizes states without changing their contents.

```yaml
exploration:
  id: exploration_01J...
  root_state_id: state_000...
  stashes:
    active: [state_01J...]
    observed: [state_01K...]
    candidate: [state_01L...]
    unresolved: [state_01M...]
    infeasible: [state_01N...]
    errored: [state_01P...]
  strategy:
    kind: breadth_first
    max_depth: 20
    max_states: 1000
```

V1 strategies should remain bounded and operator-directed:

- Follow a recorded browser or API workflow
- Fork on competing interpretations
- Enumerate finite action bindings
- Compare protocol or endpoint variants
- Stop at a depth, state count, time, or interaction budget
- Merge states only when their semantic identity is proven equivalent

## Ingestion and inference pipeline

```text
Raw interaction
      |
      v
Protocol adapter
      |
      +--> immutable observations and artifacts
      |
      v
Deterministic extractors
      |  headers, cookies, routes, identifiers, schemas, status changes
      v
Typed assertion proposals
      |
      +--> AI proposals with evidence references
      +--> operator-authored assertions
      |
      v
Validation
      |  schema, provenance, type, confidence, contradiction checks
      v
Object and state materialization
      |
      v
Constraint compilation and successor indexing
```

AI output must pass through a strict proposal schema. It may name a likely
component, relation, object type, or transition, but it cannot directly mutate
observations or authoritative state. A proposal without evidence references is
rejected.

## Integration with existing Nebula features

The application model should be a Core subsystem, not a separate scanner or a
second browser stack. Existing Nebula features already own execution, scope,
identity, traffic capture, artifacts, evidence, and durable agent activity. The
new subsystem consumes those authoritative records and adds a semantic state and
constraint layer above them.

```text
                         EXISTING NEBULA EXECUTION

  Operator ----------------------------------------------------------+
     |                                                               |
     v                                                               v
  Workbench > Browser       Agent run + browser tools         Repeater / Crawl
     |                         |                                  |
     +------------+------------+----------------------------------+
                  |
                  v
        +----------------------------+
        | Existing browser authority |
        | session + identity + tab   |
        | scope + lease + budgets    |
        +-------------+--------------+
                      |
             native command / traffic
                      |
                      v
        +----------------------------+
        | Browser engine and proxy   |
        +-------------+--------------+
                      |
                      v
        +----------------------------+
        | Existing Core records      |
        | traffic, frames, actions,  |
        | receipts, Evidence, blobs  |
        +-------------+--------------+
                      |
                      | durable IDs and Core events
                      v
              APPLICATION MODEL V1
        +----------------------------+
        | Observation adapters       |
        +-------------+--------------+
                      |
                      v
        +----------------------------+
        | Objects + states +         |
        | transitions + assertions   |
        +-------------+--------------+
                      |
                      v
        +----------------------------+
        | Typed constraints + Z3     |
        +-------------+--------------+
                      |
            model query / action bindings
                      |
              +-------+--------+
              |                |
              v                v
       Application model   Agent context
       UI projection       bounded projection
              |                |
              +-------+--------+
                      |
                      | proposed interaction only
                      v
        Existing approval, scope, lease, command,
        receipt, evidence, and browser execution path
```

The return path is important: a solver-generated binding is only a proposal. If
an operator or agent elects to execute it, it must travel through the existing
browser tool broker and browser automation controls. The application-model
engine never opens its own socket, manipulates a native browser directly, or
bypasses the current scope revision.

### Existing entities and their model roles

| Existing Nebula entity or service | Application-model use | Ownership rule |
|---|---|---|
| Project / engagement | Top-level model and target partition | Existing Project remains authoritative |
| `ScopePolicy` and scope revision | Records the allowed target universe for an interaction | Core continues to authorize every mutable operation |
| `BrowserIdentity` | Concrete actor/identity object | Cookie and storage secrets remain in the native identity partition |
| `BrowserSession` | Execution context for related interactions | Existing browser session lifecycle remains authoritative |
| `BrowserTabState` | Browser-context and page-location input to a state | Model stores references and normalized facts, not a competing tab record |
| `BrowserTrafficExchange` | HTTP interaction and request/response observations | Existing redacted record remains canonical |
| Browser body artifact | Referenced request or response payload evidence | Artifact store owns bytes, digest, redaction, and access policy |
| `BrowserWebSocketFrame` | Ordered protocol-message observation | Existing bounded payload preview and digest are reused |
| `BrowserAction` | Action proposal, approval, execution, and result envelope | Existing revision and operator-decision workflow remains authoritative |
| Browser automation lease and command | Agent action authority, bounds, and durable receipt | Existing lease, scope, device claim, idempotency, and budgets apply |
| `BrowserSiteNode` / `BrowserSiteEdge` | Seed route and navigation objects and relations | Site map is evidence input, not the semantic state source of truth |
| Crawl job | Bounded producer of navigation and traffic interactions | Existing crawl lifecycle and request limits apply |
| Repeater tab and result | Explicit request template and resulting observation | Existing identity-bound replay path executes it |
| Intruder attack and result | Bounded parameterized interaction series | Existing strategy and request budgets remain authoritative |
| Immutable `Evidence` | Stable provenance target for observations and assertions | Existing Evidence and artifact integrity rules remain authoritative |
| Agent run and durable tool ledger | Producer identity and causal lineage for agent-originated interactions | Existing run events and tool receipts remain authoritative |

This mapping avoids copying entire browser or evidence records into the model.
Application-model records keep stable foreign references plus the minimum
normalized values required for deterministic reasoning. Raw bodies, DOM data,
tokens, cookies, and other sensitive artifacts remain under their existing
authorities.

### Browser-control ingestion

Nebula already supports both supervised browser action proposals and lease-bound
agent browser commands. Both paths should generate the same application-model
interaction envelope.

```text
Browser action proposed
        |
        +-- rejected ------------------> interaction outcome: rejected
        |
        +-- approved / lease-authorized
                    |
                    v
             native command claimed
                    |
                    v
            browser engine executes
                    |
                    +--> action receipt
                    +--> traffic exchange(s)
                    +--> WebSocket frame(s)
                    +--> semantic page Evidence
                    +--> browser tab revision
                    |
                    v
          ApplicationModelIngestor
                    |
                    +--> immutable interaction
                    +--> normalized observations
                    +--> successor state
                    +--> assertion proposals
```

The ingestor should correlate records using existing durable IDs wherever
available:

- Project or engagement ID
- Browser session, identity, and tab ID
- Agent run and tool-call ID
- Browser action or automation command ID
- Traffic exchange and replay-origin ID
- Evidence and artifact ID
- Scope revision
- Recorded timestamps and ordered event sequence

One command may yield several exchanges, redirects, frames, and artifacts. They
belong to one interaction when they share the command or action receipt. Passive
traffic without a known command becomes an observed interaction with an unknown
initiating action; the engine must not fabricate causality.

### Site map and application model

The current site map answers, “Which pages, endpoints, forms, resources, and
links have been observed?” The application model answers, “What typed objects
and states existed, under which identity and context, and how did an interaction
change them?”

The site map therefore feeds the model but does not become the model:

```text
BrowserSiteNode(page or endpoint) ----+
BrowserSiteEdge(navigation/request) --+--> route and action-template proposals
Traffic exchanges -------------------+--> observed interactions
Evidence ----------------------------+--> provenance
Identity + session ------------------+--> actor and execution context
                                         |
                                         v
                               State and transition graph
```

Multiple site nodes can map to one logical resource type, and one endpoint can
produce many state transitions depending on actor, resource version, or input.
Those relationships remain typed assertions until evidence supports them.

### Agent integration

The agent should receive a bounded, typed projection rather than a dump of the
entire graph or raw browser traffic. A model-context request specifies:

```yaml
application_model_context:
  project_id: project_01J...
  base_state_id: state_01K...
  focus:
    object_ids: [obj_invoice_7]
    route_ids: [route_invoice_approve_v2]
  include:
    observations: summaries
    assertion_statuses: [observed, inferred, unknown, contradicted]
    state_ancestors: 5
    transition_depth: 2
    competing_explanations: true
  budgets:
    max_objects: 100
    max_transitions: 200
    max_observation_summaries: 100
```

The agent can then use application-model tools such as:

```text
model.get_state
model.diff_states
model.trace_lineage
model.list_objects
model.list_transitions
model.get_assertion_evidence
model.propose_assertion
model.check_constraints
model.get_solver_model
model.propose_action_binding
```

Agent proposals are untrusted inputs. `model.propose_assertion` requires typed
subject, predicate, object, and evidence IDs. `model.propose_action_binding`
creates an inert proposal linked to its base state and solver query. Execution
is delegated to the existing `browser.*`, `proxy.*`, `target.*`, or Repeater
path, retaining the existing approval and automation-lease behavior.

### Evidence integration

No new raw-evidence system should be introduced. The application model adds
semantic provenance records that point to existing immutable Evidence and
artifact records.

```text
Assertion
   |
   +-- supported by --> Observation
   |                       |
   |                       +-- derived from --> BrowserTrafficExchange
   |                       +-- references ----> Evidence
   |                       +-- references ----> Artifact digest
   |
   +-- contradicted by -> Observation
```

When only redacted metadata is available, the assertion must preserve that
limitation. Stable secret digests may support equality or change comparisons,
but the model must not expose or reconstruct the secret value.

### Core event integration

Application-model updates should be driven from committed Core records. The
preferred boundary is a transactional outbox or an idempotent projector over the
existing durable event stream:

```text
Core transaction
  1. save authoritative browser/evidence record
  2. append event with entity ID and revision
  3. commit

Application-model projector
  4. claim event by sequence
  5. normalize observations idempotently
  6. materialize successor state transactionally
  7. save projection checkpoint
```

The idempotency key should include the source entity ID, source revision, and
adapter version. Reconnect or event replay must reconstruct the same interaction
and state hashes without duplication. A failed projector must not roll back or
invalidate the original browser interaction.

### User-interface integration

The Project should expose **Application model** alongside its existing
operator-facing workspaces. The UI reads projections from Core and does not
construct authoritative graph nodes locally.

Primary views:

- **Architecture:** objects, inferred components, relations, confidence, and
  direct evidence links
- **State machine:** actors, states, transitions, path conditions, and unresolved
  successor branches
- **Hypotheses:** competing typed assertions, their evidence, and solver queries
- **Object history:** versions, aliases, and the interactions that changed an
  object

From Browser traffic, Repeater results, Evidence, or an agent activity item, the
operator can choose **Show in application model**. The resulting deep link must
select the corresponding interaction, state, object, or assertion. Conversely,
every model item must link back to its authoritative browser record, tool
receipt, Evidence entry, or artifact metadata.

Desktop owns browser execution. Mobile and LAN clients can inspect the durable
model, run read-only solver queries, review proposals, and stop work using
existing controls, but they must not imply ownership of live browser state.

### State authority matrix

| State | Authority |
|---|---|
| Project, target scope, active scope revision | Existing Core Project and `ScopePolicy` |
| Live browser process and tab execution | Desktop browser engine |
| Identity cookies, cache, and local storage | Native identity partition |
| Browser session, tab metadata, traffic, frames, and actions | Existing Core browser entities |
| Raw or redacted evidence bytes | Existing artifact and Evidence stores |
| Agent run, tool call, lease, command, and receipt | Existing agent and browser automation subsystems |
| Normalized observation | Application-model observation store |
| Typed object and object version | Application-model object store |
| Immutable semantic state and transition | Application-model state store |
| Assertion, alternatives, and epistemic status | Application-model assertion store |
| Formula, solver result, and assignments | Application-model constraint store |
| Selected model view and expanded panels | URL and local React component state |

### Integration delivery sequence

1. Add observation adapters for existing traffic exchanges, WebSocket frames,
   browser actions/results, Evidence, and Repeater results.
2. Project recorded manual browser interactions into objects, immutable states,
   and observed transitions.
3. Add bounded application-model query tools to agent context.
4. Add typed assertion proposals with evidence validation.
5. Add formula IR, Z3 compilation, persisted queries, and typed assignments.
6. Route proposed action bindings through existing browser proposal or
   lease-bound automation paths.
7. Add the Application model UI and bidirectional deep links to Browser,
   Evidence, Repeater, and agent activity.
8. Extend adapters to crawl, Intruder, imported API descriptions, and additional
   network interaction sources.

The first vertical slice should be one real browser workflow:

```text
named identity -> browser action -> traffic and Evidence -> objects -> S0/S1
-> solver query -> typed assignment -> inert action proposal -> approved browser
execution -> receipt and Evidence -> S2 -> refresh/reconnect reconstruction
```

This proves that the model is integrated with Nebula's real execution and
durability boundaries before broadening protocol coverage.

## Persistence architecture

Use a relational database as the source of truth, with an artifact store for
large payloads. The graph is a query projection over normalized records.

```text
PostgreSQL / SQLite-compatible logical schema

projects             targets
interactions         observations          artifacts
assertions           assertion_evidence    assertion_alternatives
objects              object_versions       object_properties
states               state_parents         state_object_versions
constraints          state_constraints
transitions          transition_outcomes
solver_queries       solver_results         solver_assignments
explorations         exploration_stashes
```

Recommended rules:

- Append-only observations and solver-query records
- Versioned assertions and object versions
- Transactional creation of an interaction and its observed successor
- Canonical JSON serialization for hashes
- Explicit project and target scoping on every query
- Separate encrypted artifact blobs from indexed metadata
- Provenance edges enforced with foreign keys where possible
- Schema migrations retain the version used to create historical records

A dedicated graph database is unnecessary for V1. Recursive queries and
materialized projections are sufficient initially and keep evidence integrity
within one transactional store.

## Service boundaries

```text
application_model/
  domain/          typed records and identifiers
  observations/    immutable ingestion and artifact references
  adapters/        browser, HTTP, API, WebSocket, and network normalization
  assertions/      extraction, AI proposal validation, contradictions
  objects/         identity, aliasing, versioning, and materialization
  states/          snapshots, deltas, hashing, forks, and comparisons
  transitions/     action templates, bindings, and successor generation
  constraints/     typed formula IR and type checking
  solvers/         Z3 adapter and solver lifecycle
  exploration/     state stashes, bounds, and strategies
  repository/      persistence interfaces and transactions
  api/             commands, queries, exports, and UI projections
```

The domain and constraint layers must not depend on browser automation, HTTP
clients, UI types, AI providers, or database implementations.

## Command and query API

Initial commands:

```text
record_interaction(parent_state, action, observations)
propose_assertion(subject, predicate, object, evidence)
accept_assertion(assertion_id, scope)
contradict_assertion(assertion_id, evidence)
materialize_object(assertion_set)
fork_state(state_id, assertion_selection)
apply_observed_transition(state_id, interaction_id)
create_solver_query(state_id, formula, assertion_policy)
create_exploration(root_state_id, bounds, strategy)
```

Initial queries:

```text
get_state(state_id)
diff_states(left_state_id, right_state_id)
trace_state_lineage(state_id)
list_objects(state_id, filters)
get_object_history(object_id)
list_transitions(state_id, status)
get_assertion_evidence(assertion_id)
list_competing_assertions(subject, predicate)
run_solver_query(query_id)
project_graph(state_id, filters)
```

## End-to-end example

```text
1. Browser logs in as member A.
   Observation: response sets a session cookie.

2. Extractors create a concrete Session object and associate it with actor A.
   An inferred assertion proposes that an authentication service issued it.

3. Browser creates invoice 7.
   Observations identify the resource, owner field, tenant context, and draft
   lifecycle value.

4. The state materializer produces S1 containing actor A, the session, invoice
   version 1, and their evidence references.

5. A submit action is recorded.
   The successor engine produces observed state S2 with invoice version 2 and
   status = submitted.

6. An API description exposes both v1 and v2 submit routes.
   The engine records two action templates and leaves their backend relationship
   unresolved.

7. A solver query asks whether a member actor distinct from the owner can be
   bound while the invoice is submitted.
   Z3 returns SAT and supplies actor B and invoice 7 as typed assignments.

8. V1 displays the assignments and relevant state lineage. Execution remains an
   explicit, bounded operator action. Any resulting observation creates another
   immutable successor state.
```

The solver result means only that the query is consistent with the selected
model. It is not proof of backend behavior or proof of a bug.

## V1 delivery boundary

V1 includes:

- Common interaction and observation envelope
- Immutable evidence and artifact storage
- Typed assertions with provenance and competing explanations
- Versioned memory objects with concrete, symbolic, and unknown values
- Immutable, forkable application states
- Transition records and bounded successor generation
- Typed formula intermediate representation
- Z3 satisfiability, model, simplification, and bounded enumeration
- Exploration contexts and state stashes
- State, object, assertion, transition, and solver query APIs
- Graph and state-machine projections for inspection
- Deterministic export and replay metadata

V1 excludes:

- Autonomous bug discovery or prioritization
- Unbounded interaction generation
- Claims that inferred topology is confirmed infrastructure
- Automatic execution of destructive or state-changing experiments
- General symbolic execution of unknown server implementation code
- Treating AI confidence as solver truth
- Treating a satisfiable model as proof of observed behavior

## Acceptance criteria

V1 is complete when it can:

1. Ingest a recorded browser or API workflow without losing raw evidence.
2. Reconstruct every materialized state from immutable records.
3. Show which observations support or contradict every assertion.
4. Represent concrete, symbolic, unknown, conditional, and aliased values.
5. Fork a state for competing backend interpretations.
6. Compile the supported formula IR into Z3 deterministically.
7. Return and persist distinct `SAT`, `UNSAT`, and `UNKNOWN` results.
8. Convert a satisfying model into typed, reviewable action bindings.
9. Compare two states and explain their changed objects and constraints.
10. Render the graph as a faithful projection of stored state rather than an
    independently editable source of truth.
