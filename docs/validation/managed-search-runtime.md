# Managed search service boundary

Source checkpoint `b2a69046` adds `ManagedSearchRuntime` and
`SearchContainerRunner`. The stable host is the sole service owner; project tools
use a separately integrated host-query proxy. No project gets a second local
container or a fallback to the old unmanaged launcher.

One `web_search_service` MemoryJob, with `project_id=None`, reserves capacity for
the entire persistent service. The normal administrator/host/environment/profile
precedence applies, followed by a request that can only lower RAM to at most
512 MiB and swap to zero. A required unsupported pressure threshold refuses the
launch. There is no per-query reservation and no additional search-specific
RuntimeProcess/Core-reserve charge. The Core/SQL/other-services floor is unchanged.

The adapter uses the existing create/start admission barrier, non-root identity,
read-only root, dropped capabilities, private cgroup namespace, kernel readback
and immutable group IDs. It mounts only a read-only configuration file readable
inside the worker; its host parent is private. Readiness also verifies the exact
daemon authority, job label, memory/swap and loopback publication. Changed
authority holds capacity and refuses destructive cleanup. Normal cleanup removes
only the identified group and releases capacity after verified emptiness. Bounded
stdout/stderr survive configuration cleanup. Positive OOM evidence remains in
the durable job and is returned as structured failure metadata on explicit retry.

`ContainerImagePreparer._runtime_command` refuses `pull` and `build` before
starting their client. A controller-bound caller gets a durable blocked job;
without a controller the caller gets an explicit dependency error. Fully verified
cached-image reuse and fixed inspection remain available. Docker/BuildKit workers
run in the daemon, so a client-side cgroup would not enforce their memory.

The missing provisioning runner must accept a durable job identity and frozen
policy, authenticate a stable runner/daemon identity, and return immutable worker
group IDs before release. Its pre-release proof must bind the job and nonce to
kernel RAM/swap/required-pressure limits and protected descendants. Observations
must retain usage/peak and positive OOM evidence; cancellation/recovery must prove
all transfer, decompression, build and background groups empty before capacity is
released. Source/platform/recipe and resulting image digests must remain bound to
the operation. A shared unbounded daemon or a successful CLI exit supplies none
of that proof. This dependency is refused, not advertised as implemented.

Fifteen distinct focused checks passed in phases of 8 (11.10 s), 3 (8.43 s), and
5 (6.41 s); only the OOM case repeated after structured metadata changed. Five
cases exercise real cgroup memory controls on the explicitly assigned disposable
Docker fixture. They run an inert shell in a pinned fixture image, not SearxNG or
upstream engines. The checks cover non-root configuration use, one persistent
reservation, memory-limit failure/log retention, aggregate queuing/cancellation,
and publication/daemon-authority refusal. The existing authenticated unconfigured
API and cached-image paths also passed. Four guidance/index checks passed in
2.04 s, including isolated actual Chroma source-digest synchronization. The
generated ignored index has 411 passages and source prefix `f57356a8d5cc`.

The [receipt](managed-search-runtime-receipt.json) preserves durable job/evidence
and bounded fixture logs. Six exact created group IDs were independently checked
absent; every recorded reservation was zero. Synthetic fixture data was removed
after capture. Source selections and exact test entry points are recorded in
the source checkpoint and guidance follow-up.

```sh
PYTHONPATH=src NEBULA_TEST_MEMORY_RUNTIME=/usr/local/bin/docker NEBULA_TEST_MEMORY_SOCKET=unix:///var/run/docker.sock NEBULA_TEST_MEMORY_IMAGE=postgres@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139 /workspace/nebula/.venv/bin/python -m pytest -q tests/v3/test_managed_search_runtime.py
```

The fixture overrides production runner certification and image/readiness
selection; kernel enforcement itself is real. Official cached SearxNG command
compatibility, production-rootless certification, combined host/query integration,
and installed GUI/LAN/browser behavior remain outside this receipt. An image
that cannot run with the enforced unprivileged/read-only boundary must fail
readiness and cleanup, never gain an unrestricted fallback.
