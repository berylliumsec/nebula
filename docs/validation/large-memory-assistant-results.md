# Large Butler memory observations

Butler's memory usage action now keeps a structured, bounded job summary when
the full Core response exceeds the ordinary Settings result bound. It preserves
effective/applied limits, current and peak measurements, observation time,
pending limits, application state, uncertainty and outcome. Long details and
full group identities remain available through `verify_memory_job`. Job order
is unchanged, including charged jobs first. Byte reduction sets `at_limit` and
the displayed count; it cannot turn a partial page into a complete roster.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Inspect many jobs | Structured data survives the gateway | Validated Core records | 100 queued jobs through the actual typed action |
| Inspect through a project peer | Same partial observations and unknown measurements | Private host gateway plus SQL | SQLite/PostgreSQL private listener |
| Save amid large metadata | Unrelated policy overrides survive read-back | Core revisioned policy | 90 profile overrides and long profile names/IDs |
| Exceed a response bound | Explicit partial/unavailable state; no missing-field exception | Settings outcome envelope | Actual AnalysisTool with oversized envelopes |

The independent causal probe reproduced `KeyError('result')` with 100 valid
queued jobs (81,951 serialized characters) and with 100 allowed runner names
(30,889 characters). The gateway substituted a text excerpt above 30,000
characters, while the typed action expected `result.items` and `settings.result`.
The repair projects large memory pages before rendering and guards excerpt
envelopes explicitly. Generic truncated GET responses now correctly report
that their Core read succeeded.

Memory job summaries target 24,000 serialized characters before page metadata.
Large memory policy responses retain a structured bound of 128,000 characters,
preserving the policy while previewing up to 32 profile/capability entries.
Preview and summary flags identify omitted metadata. HTTP response models and
durable records remain unchanged. If a policy still exceeds the bound, Butler
reports that limit and must review Settings before making an update.

Nine distinct focused cases passed: six in the initial 13.41 s run and the
remaining profile case plus two defensive envelope cases in a four-case run
(one normal Butler case repeated), 5.50 s. An independent validator identified
the initial profile fixture's invalid executable basename; only its synthetic
path changed to `/inert/podman`, which is never executed. Ruff and targeted
mypy passed. Raw outputs: `/tmp/nebula-memory-large-tests.txt` and
`/tmp/nebula-memory-large-rerun-tests.txt`.

Three additional actual Chroma/source-sync checks passed in 5.53 s, for 12
distinct passing cases. The rebuilt and verified bundled index contains 407
passages (777,335 bytes), digest prefix `e1cfebe0f4c0`. Host startup synchronized
that source digest and retained it on service restart.

These fixtures validate presentation, preserved policy and read-back, including
the actual Core API, typed action and private host channel. They do not launch
compute or establish kernel enforcement. Required final production LAN, mobile
WebKit and packaged desktop gates remain outstanding.
