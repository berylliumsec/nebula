# Chat history backend comparison (2026-10-17)

This comparison asks whether moving conversation messages to LMDB would make
opening a saved chat materially faster than the indexed SQLite implementation
already on main (`b67c08c`). It measures throwaway synthetic databases only.

## Workload and method

- The corpus has either 200 chats × 500 messages (100,000 messages) or 1,000
  chats × 500 messages (500,000 messages). Each message has 512 bytes of fake
  content, normal `ChatMessage` fields, and occasional retraction metadata.
- The same JSON message payloads are inserted into temporary SQLite `entities`
  rows and LMDB `(session, sequence, id)` keys. The temporary stores are deleted
  after each run. Seed time includes writing both stores and is not a write-speed
  comparison.
- `sqlite_indexed` calls the real `ChatService.session_messages` or
  `session_messages_page` method. `sqlite_payload_only` selects JSON payloads
  through SQLAlchemy Core. `sqlite_json_direct` uses a direct SQLite cursor and
  Pydantic JSON validation. These two are experimental SQLite read candidates,
  not Core implementations. `lmdb_hybrid` reads LMDB messages while checking
  the chat's existence in SQLite. `sqlite_legacy_scan` emulates the former
  project-wide paged scan.
- Before timing, every candidate must return the same ordered message IDs,
  content, and metadata as the current SQLite path. The actual SQLite payload
  query plan uses `ix_entities_kind_chat_session_created`.
- Each reported median is from repeated warm local reads in one Python 3.11.7
  process on Linux, with randomized candidate order per round. These numbers
  exclude HTTP, the pending-turn request, response serialization, browser
  rendering, cold cache, concurrent writes, and migration or recovery costs.

The UI currently asks Core for 61 messages on initial selection and shows 60,
using the extra record to detect older history. Its relevant Core method is
`session_messages_page(session_id, limit=61)`.

## Results

All times are milliseconds; lower is faster.

| Project messages | Returned messages | Repeats | Current indexed SQLite | SQLite payload only | SQLite direct JSON | LMDB hybrid |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | 61 (UI page) | 50 | 1.763 | 1.502 | 0.820 | 0.759 |
| 100,000 | 500 (full chat) | 30 | 10.512 | 9.001 | 5.435 | 4.073 |
| 500,000 | 500 (full chat) | 30 | 10.877 | — | — | 4.134 |

At 100,000 project messages, the emulated legacy project scan took **42,063
ms median** across three full-chat reads. The indexed SQLite read in that same
run took **10.951 ms median**. The large gain comes from selecting one chat in
the index instead of materializing the whole project's messages.

For the actual 61-message UI page, LMDB saved about **1.0 ms** relative to the
current SQLite path. Direct JSON validation from the existing SQLite index
reduced the gap to **0.061 ms** in this synthetic run. For a 500-message full
read, LMDB saved about **6–7 ms** relative to the current path. The 500,000-row
result shows that the current indexed lookup stayed around 11 ms for a fixed
500-message chat as the project grew fivefold.

The 100,000-row UI-page run had one 66 ms maximum on current SQLite, despite a
1.835 ms measured p95; some full-chat runs had 75–80 ms maxima. A separate
100,000-row full-chat run with garbage collection disabled during timing had
10.436 ms current-SQLite median and 11.38 ms p95, with no such maximum.
Garbage collection is a plausible cause of the spikes, but these short runs
do not establish production tail latency.

| Corpus | SQLite database + WAL + SHM | LMDB data file |
| ---: | ---: | ---: |
| 100,000 messages | 182.5 MiB | 202.9 MiB |
| 500,000 messages | 891.0 MiB | 1,014.4 MiB |

These are separate sizes for the same synthetic messages. The benchmark holds
both copies so it can compare them; a production design could instead migrate
messages, retain them in both stores, or use a different schema. The size table
does not estimate the full deployed footprint of any such design.

## Decision supported by these measurements

Keep the indexed SQLite path for opening conversations while measuring an actual
slow operator load. Its current 61-message database read is below 2 ms median
here, and LMDB's measured gain is about 1 ms. If a real-Core trace identifies
database materialization as a remaining bottleneck, the direct SQLite JSON
candidate merits integration testing before adding a second storage engine.

LMDB may be worth revisiting if repeated full-history reads have a measured
latency requirement that a tuned SQLite path cannot meet. An integrated LMDB
prototype would need separate evidence for writes, deletion, concurrent readers
and writers, migration, crash recovery, backup, and consistency with Core's
SQLite session metadata. None of those properties was tested here.

## Reproduce

Run from the repository root with `PYTHONPATH=src`; `python-lmdb` must be
installed. The script prints JSON, and `--output` retains a small receipt while
the databases themselves remain temporary.

```sh
PYTHONPATH=src python scripts/benchmark_chat_history_backends.py --sessions 200 --messages-per-session 500 --content-bytes 512 --page-size 61 --repeats 50 --output page61.json
PYTHONPATH=src python scripts/benchmark_chat_history_backends.py --sessions 200 --messages-per-session 500 --content-bytes 512 --repeats 30 --output full100k.json
PYTHONPATH=src python scripts/benchmark_chat_history_backends.py --sessions 1000 --messages-per-session 500 --content-bytes 512 --repeats 30 --output full500k.json
```

The retained raw outputs are in `docs/benchmarks/chat-history-20261017/`. The
`100k.json` receipt includes the optional three-read legacy scan;
`100k-no-gc.json` is the garbage-collection cross-check. The later `100k-direct.json`
and `100k-page61.json` receipts include all four backend candidates. The
`500k.json` run predates the direct SQLite candidate and therefore reports only
the current SQLite and LMDB reads.
