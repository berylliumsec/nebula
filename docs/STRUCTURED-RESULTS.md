# Structured results

Nebula's result explorer presents arbitrary typed output — whatever an agent,
tool or integration produces — as summaries, tables, relationships, trees and
raw JSON, while keeping the published value as the authoritative source.

No schema is registered anywhere. The explorer derives every view from the
runtime shape of the value it was given, so a producer can add fields, change
its output or invent a new result type without a Nebula release.

Operators read results in **Project → Results**, and the snapshots of a
conversation beside it in the assistant's **Agent view** drawer.

## Publishing

### From a goal, as it works

Publishing belongs to **goal mode**: a goal is the conversation that runs long
enough for an operator to lose sight of it. `dashboard.publish` is offered to a
turn dispatched under a **running** goal and to nothing else.

A running goal is also asked, in its instructions, to show where the work
stands whenever its series has been quiet for more than ten minutes — and
immediately on the first turn, so there is something to watch from the start. A
goal that is already publishing is left alone. The ask names what a snapshot
should depict: what was examined, decided or changed, the values, code or
relationships behind it, and what is next.

Core owns the series. Every snapshot of one goal joins that goal's own stream
in order, labelled with its objective, whatever `stream` the model passes.

The tool takes:

| Argument | Required | Meaning |
| --- | --- | --- |
| `title` | yes | Short operator-readable name. |
| `result` | yes | Any JSON value: object, array, string, number, boolean or null. |
| `summary` | no | One line describing what was published. |
| `producer` | no | Label for what produced it. |
| `stream` | no | Ignored inside a goal: the goal owns its own series. |
| `labels` | no | Up to 12 short tags. |
| `hints` | no | Optional presentation hints (below). |

The tool returns the new result's id, its shape statistics and the path the
operator reads it at. It deliberately does not echo the payload back into the
conversation.

### Over the API

```
POST   /api/v1/projects/{project_id}/structured-results
GET    /api/v1/projects/{project_id}/structured-results?stream=&chat_session_id=&offset=&limit=
GET    /api/v1/projects/{project_id}/structured-results/{result_id}
DELETE /api/v1/projects/{project_id}/structured-results/{result_id}
```

The API is not restricted to goal mode: it is how an external agent or an
integration publishes, and it accepts its own `stream` and `stream_label`.
`POST` takes the same fields as the tool plus `origin` and the conversation
identifiers, and answers `{"result": …, "retention_removed": […]}`. The list
answers bounded summaries — title, producer, shape statistics and a short
preview — so a busy project's list stays light; the payload arrives when a
result is opened.

## Limits

A payload is refused, with the reason, above 4 MB, 250 000 values or 200 levels
of nesting, and when it is not JSON-compatible or refers back into itself.
A project retains its newest 500 results; publishing past that removes the
oldest and every caller is told which ones went.

## Presentation hints

Hints are optional advice, kept beside the result and never merged into it.
Each field is validated on its own: an invalid field is dropped with a reason
the operator can read, and the result still renders exactly as it would with no
hints at all.

```jsonc
{
  "titleField": "name",
  "summaryField": "description",
  "fieldOrder": ["status", "name"],
  "hiddenFields": ["internal_id"],       // hidden by default, never removed
  "labels": {"ttl": "Time to live"},
  "descriptions": {"status": "As the scanner reported it"},
  "tableColumns": {"$.hosts": ["ip", "port"]},  // "*" applies to every table
  "graph": {"nodesPath": "$.people", "edgesPath": "$.ties",
            "sourceKey": "parent", "targetKey": "child", "nodeIdKey": "key"},
  "formats": {"digest": "code"},          // text | code | timestamp | url | badge | number | identifier
  "redactFields": ["api_key"]             // masked with an explicit reveal
}
```

## What the explorer does and does not do

It derives, it does not interpret:

- Field names such as `summary`, `title`, `status` only affect ordering and
  formatting. An unrecognised status renders as its own text in a neutral
  badge — never as a success or a failure, and never by colour alone.
- A table appears for a homogeneous array of objects, with columns inferred
  from a bounded sample of the rows. A row carrying other fields keeps them in
  its detail; a row missing a column is marked absent rather than blank.
- Relationships appear only when the result actually carries them: a `nodes`
  collection with `edges`/`links`/`relationships`, or a collection whose
  entries carry `source`/`target`, `src`/`dst`, `from`/`to` or
  `source_id`/`target_id`. Missing relationships are never manufactured, and
  every relationship view has a table beside it.
- The tree and the raw JSON are always available, whatever the shape.
- Numeric precision and exact string contents are preserved everywhere,
  including in copied values. Long values are truncated for preview only, and
  the full value is always one control away.
- Result text is data. It is rendered as text, never as markup, and only
  `http`, `https` and `mailto` links are ever made clickable.
