# Host-owned knowledge bootstrap

Ordinary headless and desktop Core composition now supplies a deferred Chroma
factory. The exclusive supervisor lock and shared-service ownership checks run
before it opens `<data-dir>/knowledge-index` (or
`NEBULA_V3_KNOWLEDGE_INDEX_DIR`). The same host object supplies project knowledge
and Butler memory embeddings; execution workers continue using the private host
protocol rather than opening their own index.

| Journey | Observed invariant | Authority | Focused evidence |
| --- | --- | --- | --- |
| Start Core | One index opens after exclusive host ownership | Supervisor lock and host index | Default supervisor and production entry |
| Product help | Packaged vectors synchronize their source digest without a model download | Bundled guidance and owned Chroma collection | Same-object metadata and passage count |
| Reopen Core | The previous owned client closes; a new owner reuses the current mirror | Exact owned Chroma client | Close/reopen without global cache reset |
| Cancel startup | Physical construction is joined and its client closes before unlock | Host startup and cleanup | Blocking factory cancellation |
| Optional backend failure | Authenticated health and recovery assets remain available | Stable host listener | Import, filesystem and index failures |

A cold embedding model remains advertised as required, while packaged product
help is ready. Backend initialization failure reports degraded product guidance
and retains keyword memory and packaged help. No retry replays accepted project
work. Injected indexes remain caller-owned; only a factory-created client closes
when the host drains.

Focused test selection: 20 collected cases in
`tests/v3/test_runtime_host_knowledge_bootstrap.py` and three exact existing
host-service knowledge nodes. The production case uses isolated synthetic data,
normal bearer authentication, a non-loopback LAN listener, a verified inert
recovery bundle, and a saved memory policy holding API admission. It observes
`product_guidance`, collection metadata, and passage count on the running host's
own object, then shuts down through the authenticated deployment operation.
It never opens a second Chroma client for inspection.

Twenty distinct cases passed: twelve ownership, optional-backend, and cancellation
cases in the initial run, followed by the remaining eight production/retrieval
cases in 15.10 seconds. Fresh independent review confirmed the missing generated
fixture asset and token initialization requirements. The previously validated
SQLite startup dependency `11e86f54` supplies local pool selection; PostgreSQL
alone declares its fixture connection budget. No startup or adoption guard was
weakened.

The actual production entry served `http://172.26.91.238:41995/api/v1` (SQLite)
and `http://172.26.91.238:44467/api/v1` (PostgreSQL). Each owned collection held
409 passages with source digest
`b771aaf1782cb64be42386e305c4aa23213dab856aa8216fbc956618aec29952`, matching
both the packaged resource and this checkout's current guidance sources. The
resource was generated with the already-cached local model; startup was tested
with a cold model status and no embedding calls. Both instances were stopped
through the normal authenticated operation and removed their discovery records.

Receipts: `/tmp/nebula-host-knowledge-rerun.txt` (twelve passes before the fixture
guard), `/tmp/nebula-host-knowledge-final-rerun.txt` (eight passes and production
observations), and `/tmp/nebula-host-knowledge-mypy-final.txt` (four source files).
Ruff and focused mypy pass. The selected runtime bundle has synthetic recovery
HTML; final regenerated guidance and complete production UI/browser acceptance
remain parent integration gates. This backend slice does not claim browser or
physical-device coverage.
