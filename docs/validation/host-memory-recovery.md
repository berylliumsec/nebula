# Host-owned memory recovery

The exclusive host-services owner now reconciles compute reservations
independently of project execution processes. It resolves the persisted runner
profile and uses the existing memory recovery protocol: confirm the launch
owner's process birth and namespace, compare the original runner fingerprint and
daemon identity, inspect the immutable job group, remove the workload and helper
groups, then persist positive cleanup evidence before releasing capacity.

| Journey | Observable invariant | Authority | Selected evidence |
| --- | --- | --- | --- |
| Controller remains alive | Its group stays charged; no cleanup probe runs | Recorded process identity | SQLite/PostgreSQL |
| Controller dies | Capacity is released after group removal proof | Original daemon and immutable group IDs | Synthetic protocol and actual Docker |
| Profile/daemon changes | Unknown work stays charged | Snapshot fingerprint and daemon identity | Missing profile, changed profile and daemon cases |
| Host detaches | Accepted cleanup finishes before the host lock is released | Exclusive host-services lifetime | Cancellation/cleanup barrier |
| Admission and recovery | Both use the same memory policy authority | Shared SQL database and service | Supervisor construction |
| Runner resolution | Launch and cleanup interpret stored profiles identically | One profile conversion helper | Docker/Podman profile shapes |

Recovery launches no workload and does not enable a disabled execution profile.
An existing disabled profile can still identify an old job for cleanup. A missing
or changed profile does not authorize a fallback runner. Unknown creation,
foreign namespaces, unavailable agents and unverified groups retain their
reservation. Cleanup does not retry a job, enlarge its budget or resume a goal.

The host joins an accepted reconciliation pass through cancellation before
detaching. It closes only its own store resources; the supervisor retains its SQL
pool until all host services have drained. The supervisor's runtime-memory
admission service is passed to recovery so initialized policy and administrator
limits retain one authority. Existing settings, Butler actions and saved policy
formats are unchanged.

Validation collected 50 focused cases: 16 new SQLite/PostgreSQL lifecycle and
authority cases, three profile conversion cases, one actual OCI/controller crash
case and 30 affected private host-service cases. The first run passed 49 in
56.00 s and rejected one malformed Podman fixture before product conversion ran.
Independent validation confirmed it paired `runtime=podman` with a Docker
executable. Correcting that fixture yielded one pass in 1.32 s; all 50 distinct
cases passed without repeating unrelated passing checks. Ruff and targeted mypy
passed. Existing WebSocket dependency deprecations were the only warnings.

The actual workload allocated 4 MiB in a 32 MiB group using the explicit
disposable daemon at `unix:///var/run/docker.sock`. Its cached image was
`postgres@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139`.
Recovery kept the live controller's reservation, observed the controller's
SIGKILL, automatically removed the group and recorded `worker_lost` with zero
charge. The fixture adapter certifies that disposable daemon only; it does not
certify a production rootless installation or actual Podman/macOS enforcement.
No provider, existing installation, SSH command, model download or deployment
was used. All compute groups were removed; only the disposable PostgreSQL fixture
remained running.

Raw evidence: `/tmp/nebula-host-memory-recovery-tests.txt`,
`/tmp/nebula-host-memory-recovery-repair-tests.txt` and the final diff-bound
selection `/tmp/nebula-host-memory-recovery-final-selection.json`.

Compatibility launchers and packaged desktop migration remain part of the
larger architecture integration. Final production LAN, mobile WebKit and
packaged desktop continuity gates remain required and are not established by
this backend stage.
