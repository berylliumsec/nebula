# Expanded chat storage comparison (2026-10-17)

This experiment extends the [initial read-only comparison](chat-history-backend-benchmark.md)
to the saved-chat API path, older pages, writes, and concurrent access. All data and
stores are disposable. No production storage code was changed.

## Workload

- The 100,000-message corpus has 200 chats of 500 messages; the scaling check
  has 1,000 chats of 500 messages. Both use the same deterministic fake content:
  mostly 512-byte messages, a 4 KiB message every tenth record, and 32 KiB of
  reasoning every hundredth record. Every 41st message has a creation time that
  does not follow sequence order; every 97th has retraction metadata.
- The SQLite candidate is the current `NebulaStore` and `ChatService` path,
  including its `(kind, chat_session_id, created_at, id)` index, SQLAlchemy
  materialization, and Pydantic validation. Core configures SQLite WAL with
  `synchronous=NORMAL`.
- The LMDB prototype stores message JSON by ID and indexes
  `(session_id, created_at, id)` in a second LMDB database. It keeps session
  metadata in a separate SQLite database. Reads validate JSON into the same
  `ChatMessage` model and check that the session exists. It is **not** a complete
  Core storage adapter; it omits other Core transaction side effects and cannot
  atomically commit across its two stores. Its metadata-only SQLite file also
  has a smaller cache footprint than a real Core database.
- Three LMDB commit settings were measured in separate fresh stores: `full`
  (`sync=True, metasync=True`), `metadata-deferred` (`sync=True,
  metasync=False`), and `buffered` (`sync=False, metasync=False`). The first two
  sync message data on commit; the third does not. These do not offer identical
  recovery behavior to SQLite WAL `synchronous=NORMAL`. The [LMDB
  documentation](https://lmdb.readthedocs.io/en/py-lmdb_2.2.1/index.html) warns
  that `sync=False` can lose transactions or corrupt a database after a system
  crash, depending on filesystem write ordering. [SQLite's WAL
  documentation](https://www.sqlite.org/pragma.html#pragma_synchronous) says
  `synchronous=NORMAL` maintains consistency but may lose recent commits after
  a power loss or operating-system crash.
- Every paired page, full-chat, point-read, and authenticated API result is
  checked for equality. The API measurement uses authenticated FastAPI
  `TestClient` requests for messages and then pending turn, with the prototype
  substituted at `ChatService.session_messages_page`. It includes Core route,
  authentication, and response serialization, but excludes a network socket,
  browser rendering, and a real operator session. Page requests return 61
  records, matching the UI's initial history window.
- Timings are warm local measurements in one Python process. Each paired
  single-operation read/write randomly alternates candidate order. The shared
  host had variable background load; compare paired results within a run, not
  small differences between independent runs. Seed time writes both stores and
  is not a throughput result. The concurrent phase runs four readers and one
  writer per backend, one backend at a time.

## Results

All entries below are median milliseconds. The raw receipts also contain sample
counts, p95, p99, maxima, environment versions, and file sizes.

### 100,000 messages, LMDB `full`

| Operation | Current SQLite | LMDB | Samples |
| --- | ---: | ---: | ---: |
| Recent 61-message page | 1.955 | 0.873 | 200 |
| Older 61-message page | 2.389 | 0.897 | 200 |
| Full 500-message chat | 11.644 | 4.938 | 50 |
| Message by ID | 0.317 | 0.020 | 200 |
| Authenticated messages request | 5.298 | 3.834 | 100 |
| Messages then pending-turn requests | 8.262 | 6.676 | 100 |
| Append message | 0.595 | 12.539 | 80 |
| Edit message | 1.211 | 11.051 | 80 |
| Delete message | 0.426 | 13.014 | 20 |
| Concurrent page read | 10.466 | 4.710 | 200 |
| Concurrent append | 0.838 | 12.277 | 80 |

The message request gained **1.464 ms** and the two-request sequence gained
**1.586 ms**. LMDB's fully synced isolated append took about **21 times** the
current SQLite time in this run. The LMDB write prototype omits some Core
behavior, so this is not an upper bound on integrated LMDB write latency.

### Commit-setting sensitivity at 100,000 messages

| LMDB commit setting | SQLite append | LMDB append | SQLite edit | LMDB edit | SQLite delete | LMDB delete | SQLite messages + pending | LMDB messages + pending |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Full sync | 0.595 | 12.539 | 1.211 | 11.051 | 0.426 | 13.014 | 8.262 | 6.676 |
| Metadata deferred | 0.948 | 3.577 | 1.663 | 3.429 | 0.705 | 4.189 | 8.910 | 7.144 |
| Buffered | 0.555 | 0.046 | 1.058 | 0.069 | 0.368 | 0.055 | 7.912 | 6.456 |

The buffered setting makes LMDB writes fast, but changes crash-recovery risk.
Deferring only metadata sync still made appends, edits, and deletes slower than
current SQLite in this run. The SQLite values differ across rows because each
setting was measured in a separate run under variable host load.

### 500,000 messages, LMDB `full`

| Operation | Current SQLite | LMDB | Samples |
| --- | ---: | ---: | ---: |
| Recent 61-message page | 1.965 | 0.876 | 100 |
| Full 500-message chat | 12.004 | 4.960 | 25 |
| Authenticated messages request | 5.109 | 3.768 | 100 |
| Messages then pending-turn requests | 7.858 | 6.465 | 100 |
| Append message | 0.579 | 5.860 | 40 |

Increasing the project from 100,000 to 500,000 messages did not materially
change either backend's fixed-size page timing. The slower initial load this
experiment tries to address is therefore unlikely to be caused by the number
of unrelated chats once the SQLite session index is present.

| Corpus | Current SQLite database + WAL + SHM | LMDB data + metadata SQLite |
| --- | ---: | ---: |
| 100,000 messages | 253.6 MiB | 262.4 MiB |
| 500,000 messages | 1,244.4 MiB | 1,310.0 MiB |

The LMDB candidate used about 3–5% more space for this payload and schema. A
production migration, temporary duplicate copies, backups, and LMDB map-size
allocation would have additional operational costs not captured here.

## Decision

**Keep indexed SQLite for now.** LMDB is faster on every measured read, but its
gain at the authenticated two-request chat load is roughly **1.4–1.6 ms**. At
the two LMDB settings that sync message data on each commit, measured writes
are slower. The one setting that wins writes has a different, weaker crash
recovery contract, and the two-store prototype has no atomic transaction with
Core's SQLite session metadata. These measurements do not justify a production
storage migration for the chat-loading symptom.

The [initial benchmark](chat-history-backend-benchmark.md) also measured a
direct-JSON read from the existing SQLite index within 0.061 ms of LMDB for a
61-message page, using smaller messages. That candidate would need its own
integrated API and browser validation before adopting it. This experiment
does not establish that database access dominates actual chat loading; the next
diagnostic should time the Core requests and browser render for a slow saved
chat on a disposable real Core.

## Reproduce

Run from the repository root with `PYTHONPATH=src`; `python-lmdb` is needed for
this optional experiment. The script deletes both stores after each run and
prints a JSON receipt. It does not run the repository's full test suite.

```sh
PYTHONPATH=src python scripts/benchmark_chat_storage_workloads.py --sessions 200 --messages-per-session 500 --reads 200 --writes 80 --lmdb-durability full --output expanded-100k-full.json
PYTHONPATH=src python scripts/benchmark_chat_storage_workloads.py --sessions 200 --messages-per-session 500 --reads 200 --writes 80 --lmdb-durability metadata-deferred --output expanded-100k-metadata-deferred.json
PYTHONPATH=src python scripts/benchmark_chat_storage_workloads.py --sessions 200 --messages-per-session 500 --reads 200 --writes 80 --lmdb-durability buffered --output expanded-100k-buffered.json
PYTHONPATH=src python scripts/benchmark_chat_storage_workloads.py --sessions 1000 --messages-per-session 500 --reads 100 --writes 40 --lmdb-durability full --output expanded-500k-full.json
```

The retained receipts are in `docs/benchmarks/chat-history-20261017/`.
