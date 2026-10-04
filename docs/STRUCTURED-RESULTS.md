# Structured results

Nebula's result explorer presents arbitrary typed output — whatever an agent,
tool or integration produces — as summaries, tables, relationships, trees and
raw JSON, while keeping the published value as the authoritative source.

No schema is registered anywhere. The explorer derives every view from the
runtime shape of the value it was given, so a producer can add fields, change
its output or invent a new result type without a Nebula release.

Operators read published results in **Project → Results**. The chat's
**Project Snapshot** shows the project's Core-owned dashboard summary instead.
It does not read published results or require a model to publish snapshots.

## Publishing

Results can still be published through the project API by an external agent,
integration or operator. Existing published results and goal series remain
readable in Project → Results. Goal turns no longer receive `dashboard.publish`
or instructions to update a separate view.

The publish request takes:

| Argument | Required | Meaning |
| --- | --- | --- |
| `title` | yes | Short operator-readable name. |
| `result` | yes | Any JSON value: object, array, string, number, boolean or null. |
| `summary` | no | One line describing what was published. |
| `producer` | no | Label for what produced it. |
| `stream` | no | Optional key for a series of related results. |
| `labels` | no | Up to 12 short tags. |
| `hints` | no | Optional presentation hints (below). |

### Over the API

```
POST   /api/v1/projects/{project_id}/structured-results
GET    /api/v1/projects/{project_id}/structured-results?stream=&chat_session_id=&offset=&limit=
GET    /api/v1/projects/{project_id}/structured-results/{result_id}
DELETE /api/v1/projects/{project_id}/structured-results/{result_id}
```

The API accepts `stream`, `stream_label`, `origin`, and optional conversation
identifiers in addition to the fields above. `POST` answers
`{"result": …, "retention_removed": […]}`. The list
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
